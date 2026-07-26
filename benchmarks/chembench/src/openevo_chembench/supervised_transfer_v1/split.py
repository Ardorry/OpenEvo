"""Deterministic balanced Train/Probe/Test/Reserve split and isolation receipt."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from openevo_chembench.chembench4k_dataset import (
    ChemBench4KDatasetLoader,
    expected_relative_files,
)
from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_CATEGORIES,
    CHOICE_LABELS,
    PrivateChemBench4KTask,
)
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_private_file,
    write_public_file,
)
from openevo_chembench.supervised_transfer_v1.config import (
    PROBE_PER_CATEGORY,
    PROTOCOL_ID,
    SPLIT_SEED,
    TEST_PER_CATEGORY,
    TRAIN_PER_CATEGORY,
)
from openevo_chembench.supervised_transfer_v1.exposure import (
    HistoricalExposureManifestV1,
)

SPLIT_SUMMARY_SCHEMA = "chembench_supervised_transfer_split_summary_v1"
SPLIT_ISOLATION_SCHEMA = "SplitIsolationReceiptV1"
DATASET_MANIFEST_SCHEMA = "chembench4k_dataset_manifest_supervised_v1"
PARTITIONS = ("train", "probe", "test", "reserve")


class SupervisedSplitError(RuntimeError):
    """Fail-closed deterministic split error."""

    def __init__(self, finding_code: str, *, category_counts: dict[str, int] | None = None) -> None:
        if type(finding_code) is not str or not finding_code:
            raise TypeError("split finding code must be non-empty text")
        self.finding_code = finding_code
        self.category_counts = dict(category_counts or {})
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True)
class FrozenSupervisedSplitV1:
    train: tuple[PrivateChemBench4KTask, ...]
    probe: tuple[PrivateChemBench4KTask, ...]
    test: tuple[PrivateChemBench4KTask, ...]
    reserve: tuple[PrivateChemBench4KTask, ...]
    historical_exposed: frozenset[str]
    train_supplemented_unseen: frozenset[str]

    def __post_init__(self) -> None:
        expected = {
            "train": TRAIN_PER_CATEGORY,
            "probe": PROBE_PER_CATEGORY,
            "test": TEST_PER_CATEGORY,
        }
        by_partition = {
            "train": self.train,
            "probe": self.probe,
            "test": self.test,
            "reserve": self.reserve,
        }
        uid_sets = {name: {task.uid for task in tasks} for name, tasks in by_partition.items()}
        if any(len(uid_sets[name]) != len(tasks) for name, tasks in by_partition.items()):
            raise ValueError("split contains duplicate UID")
        for index, left in enumerate(PARTITIONS):
            for right in PARTITIONS[index + 1 :]:
                if uid_sets[left] & uid_sets[right]:
                    raise ValueError("split partitions overlap")
        for name, count in expected.items():
            category_counts = {
                category: sum(task.category == category for task in by_partition[name])
                for category in CHEMBENCH4K_CATEGORIES
            }
            if set(category_counts.values()) != {count}:
                raise ValueError(f"{name} is not category-balanced")
        if (uid_sets["probe"] | uid_sets["test"]) & self.historical_exposed:
            raise ValueError("Probe/Test contains historical exposure")
        if not self.train_supplemented_unseen <= uid_sets["train"]:
            raise ValueError("training-supplement set is not a Train subset")

    def tasks(self, partition: str) -> tuple[PrivateChemBench4KTask, ...]:
        if partition not in PARTITIONS:
            raise ValueError("partition is invalid")
        return getattr(self, partition)


@dataclass(frozen=True, slots=True)
class GeneratedSplitArtifactsV1:
    paths: dict[str, Path]
    sha256: dict[str, str]
    split_summary_sha256: str
    isolation_receipt_sha256: str
    dataset_manifest_sha256: str


def build_supervised_dataset_manifest(loader: ChemBench4KDatasetLoader) -> dict[str, object]:
    if type(loader) is not ChemBench4KDatasetLoader:
        raise TypeError("loader must be exact ChemBench4KDatasetLoader")
    base = loader.manifest
    return {
        "schema_version": DATASET_MANIFEST_SCHEMA,
        "repository": base.repository,
        "revision": base.revision,
        "files": list(base.files),
        "file_sha256": base.file_sha256,
        "combined_sha256": base.combined_sha256,
        "dev_count": base.dev_count,
        "test_count": base.test_count,
        "category_counts": base.category_counts,
        "schema_validation": {
            "expected_files": len(expected_relative_files()),
            "required_fields": ["question", "A", "B", "C", "D", "answer"],
            "status": "PASS",
        },
        "answer_domain": list(CHOICE_LABELS),
        "uid": {
            "algorithm": "sha256_revision_category_source_index_normalized_public_content_v1",
            "unique_count": base.normalized_uid_count,
            "collision_count": 0,
        },
        "duplicate_check": {
            "normalized_public_content_duplicate_count": (
                base.normalized_content_duplicate_count
            ),
            "status": (
                "PASS" if base.normalized_content_duplicate_count == 0 else "FAIL"
            ),
        },
    }


def generate_balanced_split_v1(
    loader: ChemBench4KDatasetLoader,
    *,
    exposure_manifest: HistoricalExposureManifestV1,
) -> FrozenSupervisedSplitV1:
    """Freeze 50/10/50 per category using only the frozen test pool."""

    if type(loader) is not ChemBench4KDatasetLoader:
        raise TypeError("loader must be exact ChemBench4KDatasetLoader")
    if type(exposure_manifest) is not HistoricalExposureManifestV1:
        raise TypeError("exposure_manifest must be exact HistoricalExposureManifestV1")
    all_tasks = loader.load_split("test")
    task_by_uid = {task.uid: task for task in all_tasks}
    exposure_by_uid = {str(item["uid"]): str(item["category"]) for item in exposure_manifest.items}
    if any(uid not in task_by_uid for uid in exposure_by_uid):
        raise SupervisedSplitError("historical exposure contains a non-dataset UID")
    if any(task_by_uid[uid].category != category for uid, category in exposure_by_uid.items()):
        raise SupervisedSplitError("historical exposure category binding mismatch")
    historical = frozenset(exposure_by_uid)

    unseen_counts = {
        category: sum(
            task.category == category and task.uid not in historical for task in all_tasks
        )
        for category in CHEMBENCH4K_CATEGORIES
    }
    # At least 60 never-exposed rows per category are required even when all 50 Train
    # rows come from historical exposure. If historical Train has fewer than 50 rows,
    # the unseen requirement grows by that exact deficit.
    required_unseen = {
        category: PROBE_PER_CATEGORY
        + TEST_PER_CATEGORY
        + max(
            0,
            TRAIN_PER_CATEGORY
            - sum(
                task.category == category and task.uid in historical for task in all_tasks
            ),
        )
        for category in CHEMBENCH4K_CATEGORIES
    }
    insufficient = {
        category: unseen_counts[category]
        for category in CHEMBENCH4K_CATEGORIES
        if unseen_counts[category] < required_unseen[category]
    }
    if insufficient:
        raise SupervisedSplitError(
            "INSUFFICIENT_NEVER_EXPOSED_POOL",
            category_counts=insufficient,
        )

    selected: dict[str, list[PrivateChemBench4KTask]] = {
        partition: [] for partition in PARTITIONS
    }
    supplemented: set[str] = set()
    for category in CHEMBENCH4K_CATEGORIES:
        category_tasks = sorted(
            (task for task in all_tasks if task.category == category),
            key=lambda task: task.uid,
        )
        if len(category_tasks) < TRAIN_PER_CATEGORY + PROBE_PER_CATEGORY + TEST_PER_CATEGORY:
            raise SupervisedSplitError("category cannot satisfy the preregistered allocation")
        exposed = [task for task in category_tasks if task.uid in historical]
        unseen = [task for task in category_tasks if task.uid not in historical]

        exposed_selection = _seeded_order(exposed, category=category, purpose="train-exposed")
        train = exposed_selection[:TRAIN_PER_CATEGORY]
        missing = TRAIN_PER_CATEGORY - len(train)
        unseen_for_train = _seeded_order(unseen, category=category, purpose="train-unseen")
        supplement = unseen_for_train[:missing]
        train.extend(supplement)
        supplemented.update(task.uid for task in supplement)
        train_uids = {task.uid for task in train}

        eligible_holdout = [task for task in unseen if task.uid not in train_uids]
        probe_order = _seeded_order(eligible_holdout, category=category, purpose="probe")
        probe = probe_order[:PROBE_PER_CATEGORY]
        probe_uids = {task.uid for task in probe}
        eligible_test = [task for task in eligible_holdout if task.uid not in probe_uids]
        test_order = _seeded_order(eligible_test, category=category, purpose="test")
        test = test_order[:TEST_PER_CATEGORY]
        used = train_uids | probe_uids | {task.uid for task in test}
        reserve = [task for task in category_tasks if task.uid not in used]

        selected["train"].extend(
            _seeded_order(train, category=category, purpose="train-order")
        )
        selected["probe"].extend(probe)
        selected["test"].extend(test)
        selected["reserve"].extend(reserve)

    split = FrozenSupervisedSplitV1(
        train=tuple(selected["train"]),
        probe=tuple(selected["probe"]),
        test=tuple(selected["test"]),
        reserve=tuple(selected["reserve"]),
        historical_exposed=historical,
        train_supplemented_unseen=frozenset(supplemented),
    )
    if sum(len(split.tasks(name)) for name in PARTITIONS) != loader.manifest.test_count:
        raise SupervisedSplitError("SPLIT_DOES_NOT_COVER_TEST_POOL")
    return split


def render_split_artifacts_v1(
    split: FrozenSupervisedSplitV1,
    *,
    loader: ChemBench4KDatasetLoader,
    exposure_manifest: HistoricalExposureManifestV1,
) -> dict[str, bytes]:
    if type(split) is not FrozenSupervisedSplitV1:
        raise TypeError("split must be exact FrozenSupervisedSplitV1")
    outputs: dict[str, bytes] = {}
    file_digests: dict[str, str] = {}
    ordered_uid_digests: dict[str, str] = {}
    category_counts: dict[str, dict[str, int]] = {}
    for partition in PARTITIONS:
        tasks = split.tasks(partition)
        public = b"".join(
            canonical_json_bytes(
                {
                    "partition": partition,
                    "ordinal": ordinal,
                    "category_ordinal": _category_ordinal(tasks, ordinal),
                    **task.to_public().to_public_dict(),
                }
            )
            for ordinal, task in enumerate(tasks)
        )
        public_name = f"{partition}_public_manifest.jsonl"
        outputs[public_name] = public
        file_digests[public_name] = sha256_bytes(public)
        if partition in {"train", "probe", "test"}:
            private = b"".join(
                canonical_json_bytes(
                    {
                        "partition": partition,
                        "ordinal": ordinal,
                        "category_ordinal": _category_ordinal(tasks, ordinal),
                        "uid": task.uid,
                        "category": task.category,
                        "source_split": task.source_split,
                        "source_index": task.source_index,
                        "target": task.target,
                        "dataset_revision": task.dataset_revision,
                        "dataset_sha256": task.dataset_sha256,
                    }
                )
                for ordinal, task in enumerate(tasks)
            )
            private_name = f"{partition}_private_manifest.jsonl"
            outputs[private_name] = private
            file_digests[private_name] = sha256_bytes(private)
        ordered_uid_digests[partition] = _ordered_uid_digest(tasks)
        category_counts[partition] = {
            category: sum(task.category == category for task in tasks)
            for category in CHEMBENCH4K_CATEGORIES
        }

    code_digest = sha256_bytes(Path(__file__).read_bytes())
    train_historical_counts = {
        category: sum(
            task.uid in split.historical_exposed
            for task in split.train
            if task.category == category
        )
        for category in CHEMBENCH4K_CATEGORIES
    }
    train_unseen_counts = {
        category: sum(
            task.uid in split.train_supplemented_unseen
            for task in split.train
            if task.category == category
        )
        for category in CHEMBENCH4K_CATEGORIES
    }
    intersections = _intersection_counts(split)
    summary = {
        "schema_version": SPLIT_SUMMARY_SCHEMA,
        "protocol_id": PROTOCOL_ID,
        "seed": SPLIT_SEED,
        "dataset_repository": loader.manifest.repository,
        "dataset_revision": loader.manifest.revision,
        "dataset_combined_sha256": loader.manifest.combined_sha256,
        "historical_exposure_manifest_sha256": exposure_manifest.digest,
        "counts": {partition: len(split.tasks(partition)) for partition in PARTITIONS},
        "per_category_counts": category_counts,
        "ordered_uid_sha256": ordered_uid_digests,
        "file_sha256": file_digests,
        "historical_exposed_train": train_historical_counts,
        "train_supplemented_unseen": train_unseen_counts,
        "probe_historical_exposure": 0,
        "test_historical_exposure": 0,
        "pairwise_intersection_counts": intersections,
        "split_generation_code_sha256": code_digest,
    }
    summary_bytes = canonical_pretty_json_bytes(summary)
    outputs["split_summary.json"] = summary_bytes
    split_summary_sha256 = sha256_bytes(summary_bytes)

    isolation = {
        "schema_version": SPLIT_ISOLATION_SCHEMA,
        "protocol_id": PROTOCOL_ID,
        "status": "PASS",
        "split_summary_sha256": split_summary_sha256,
        "train_probe_test_reserve_uid_disjoint": all(value == 0 for value in intersections.values()),
        "probe_historical_exposure_count": 0,
        "test_historical_exposure_count": 0,
        "reflector_input_uid_allowlist_partition": "train",
        "reflector_input_uid_allowlist_sha256": _uid_set_digest(split.train),
        "memory_provenance_uid_allowlist_sha256": _uid_set_digest(split.train),
        "probe_evolution_jobs_allowed": False,
        "test_evolution_jobs_allowed": False,
        "test_artifact_set_policy": "frozen_before_first_test_call",
        "reserve_reflector_visibility": False,
        "probe_target_reflector_visibility": False,
        "test_target_reflector_visibility": False,
    }
    outputs["split_isolation_receipt_v1.json"] = canonical_pretty_json_bytes(isolation)
    outputs["chembench4k_dataset_manifest_supervised_v1.json"] = (
        canonical_pretty_json_bytes(build_supervised_dataset_manifest(loader))
    )
    return outputs


def write_split_artifacts_v1(
    outputs: dict[str, bytes],
    *,
    destination_root: Path,
) -> GeneratedSplitArtifactsV1:
    expected_names = {
        "train_public_manifest.jsonl",
        "train_private_manifest.jsonl",
        "probe_public_manifest.jsonl",
        "probe_private_manifest.jsonl",
        "test_public_manifest.jsonl",
        "test_private_manifest.jsonl",
        "reserve_public_manifest.jsonl",
        "split_summary.json",
        "split_isolation_receipt_v1.json",
        "chembench4k_dataset_manifest_supervised_v1.json",
    }
    if set(outputs) != expected_names:
        raise ValueError("split output set is not closed")
    paths: dict[str, Path] = {}
    digests: dict[str, str] = {}
    for name in sorted(outputs):
        path = destination_root / name
        if name.endswith("_private_manifest.jsonl"):
            write_private_file(path, outputs[name])
        else:
            write_public_file(path, outputs[name])
        paths[name] = path
        digests[name] = sha256_bytes(outputs[name])
    return GeneratedSplitArtifactsV1(
        paths=paths,
        sha256=digests,
        split_summary_sha256=digests["split_summary.json"],
        isolation_receipt_sha256=digests["split_isolation_receipt_v1.json"],
        dataset_manifest_sha256=digests[
            "chembench4k_dataset_manifest_supervised_v1.json"
        ],
    )


def verify_split_artifacts_v1(
    *,
    expected: dict[str, bytes],
    destination_root: Path,
) -> GeneratedSplitArtifactsV1:
    actual: dict[str, bytes] = {}
    for name, payload in expected.items():
        path = destination_root / name
        try:
            actual[name] = path.read_bytes()
        except OSError as exc:
            raise SupervisedSplitError("split artifact set is incomplete") from exc
        if actual[name] != payload:
            raise SupervisedSplitError("split artifact bytes do not match regeneration")
        if name.endswith("_private_manifest.jsonl") and (path.stat().st_mode & 0o777) != 0o600:
            raise SupervisedSplitError("private manifest mode is not 0600")
    digests = {name: sha256_bytes(payload) for name, payload in actual.items()}
    return GeneratedSplitArtifactsV1(
        paths={name: destination_root / name for name in actual},
        sha256=digests,
        split_summary_sha256=digests["split_summary.json"],
        isolation_receipt_sha256=digests["split_isolation_receipt_v1.json"],
        dataset_manifest_sha256=digests[
            "chembench4k_dataset_manifest_supervised_v1.json"
        ],
    )


def load_partition_from_private_manifest(
    loader: ChemBench4KDatasetLoader,
    *,
    partition: Literal["train", "probe", "test"],
    private_manifest: Path,
) -> tuple[PrivateChemBench4KTask, ...]:
    if partition not in {"train", "probe", "test"}:
        raise ValueError("private partition must be train, probe, or test")
    if (private_manifest.stat().st_mode & 0o777) != 0o600:
        raise SupervisedSplitError("private manifest mode is not 0600")
    by_uid = {task.uid: task for task in loader.load_split("test")}
    tasks: list[PrivateChemBench4KTask] = []
    try:
        with private_manifest.open("r", encoding="utf-8") as stream:
            for ordinal, line in enumerate(stream):
                row = json.loads(line)
                if not isinstance(row, dict) or row.get("partition") != partition:
                    raise SupervisedSplitError("private manifest row is invalid")
                task = by_uid.get(row.get("uid"))
                if (
                    task is None
                    or row.get("ordinal") != ordinal
                    or row.get("category") != task.category
                    or row.get("source_split") != task.source_split
                    or row.get("source_index") != task.source_index
                    or row.get("target") != task.target
                    or row.get("dataset_revision") != task.dataset_revision
                    or row.get("dataset_sha256") != task.dataset_sha256
                ):
                    raise SupervisedSplitError("private manifest binding mismatch")
                tasks.append(task)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SupervisedSplitError("private manifest is unreadable") from exc
    expected_count = {
        "train": TRAIN_PER_CATEGORY * len(CHEMBENCH4K_CATEGORIES),
        "probe": PROBE_PER_CATEGORY * len(CHEMBENCH4K_CATEGORIES),
        "test": TEST_PER_CATEGORY * len(CHEMBENCH4K_CATEGORIES),
    }[partition]
    if len(tasks) != expected_count or len({task.uid for task in tasks}) != expected_count:
        raise SupervisedSplitError("private manifest count/uniqueness mismatch")
    return tuple(tasks)


def _seeded_order(
    tasks: list[PrivateChemBench4KTask],
    *,
    category: str,
    purpose: str,
) -> list[PrivateChemBench4KTask]:
    ordered = sorted(tasks, key=lambda task: task.uid)
    seed = hashlib.sha256(
        f"{SPLIT_SEED}\x1f{category}\x1f{purpose}".encode()
    ).digest()
    random.Random(seed).shuffle(ordered)
    return ordered


def _category_ordinal(tasks: tuple[PrivateChemBench4KTask, ...], ordinal: int) -> int:
    category = tasks[ordinal].category
    return sum(task.category == category for task in tasks[:ordinal])


def _ordered_uid_digest(tasks: tuple[PrivateChemBench4KTask, ...]) -> str:
    return sha256_bytes(("\n".join(task.uid for task in tasks) + "\n").encode("ascii"))


def _uid_set_digest(tasks: tuple[PrivateChemBench4KTask, ...]) -> str:
    return sha256_bytes(("\n".join(sorted(task.uid for task in tasks)) + "\n").encode("ascii"))


def _intersection_counts(split: FrozenSupervisedSplitV1) -> dict[str, int]:
    result: dict[str, int] = {}
    for index, left in enumerate(PARTITIONS):
        left_uids = {task.uid for task in split.tasks(left)}
        for right in PARTITIONS[index + 1 :]:
            result[f"{left}_{right}"] = len(
                left_uids & {task.uid for task in split.tasks(right)}
            )
    return result


__all__ = [
    "DATASET_MANIFEST_SCHEMA",
    "PARTITIONS",
    "SPLIT_ISOLATION_SCHEMA",
    "SPLIT_SUMMARY_SCHEMA",
    "FrozenSupervisedSplitV1",
    "GeneratedSplitArtifactsV1",
    "SupervisedSplitError",
    "build_supervised_dataset_manifest",
    "generate_balanced_split_v1",
    "load_partition_from_private_manifest",
    "render_split_artifacts_v1",
    "verify_split_artifacts_v1",
    "write_split_artifacts_v1",
]
