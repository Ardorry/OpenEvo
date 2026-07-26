"""Exposure-aware deterministic split with primary and recovery Test sets."""

from __future__ import annotations

import hashlib
import json
import random
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_CATEGORIES,
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
from openevo_chembench.supervised_transfer_v1.exposure_v2 import (
    ACTUAL_EXPOSURE_LABELS_V2,
    ExposureLabelV2,
    HistoricalExposureBundleV2,
    HistoricalExposureItemV2,
)

SPLIT_SUMMARY_SCHEMA_V2 = "chembench_supervised_transfer_split_summary_v2"
SPLIT_ISOLATION_SCHEMA_V2 = "SplitIsolationReceiptV2"
MAX_RECOVERY_TESTS = 2
BASE_PARTITIONS_V2 = ("train", "probe", "test_primary")


class SupervisedSplitV2Error(RuntimeError):
    """Fail-closed V2 split error."""

    def __init__(self, finding_code: str, *, category_counts: dict[str, int] | None = None) -> None:
        self.finding_code = finding_code
        self.category_counts = dict(category_counts or {})
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True)
class FrozenSupervisedSplitV2:
    train: tuple[PrivateChemBench4KTask, ...]
    probe: tuple[PrivateChemBench4KTask, ...]
    test_primary: tuple[PrivateChemBench4KTask, ...]
    recovery_tests: tuple[tuple[PrivateChemBench4KTask, ...], ...]
    reserve: tuple[PrivateChemBench4KTask, ...]
    actual_exposed_uids: frozenset[str]
    train_supplemented_unseen: frozenset[str]

    def __post_init__(self) -> None:
        if not 0 <= len(self.recovery_tests) <= MAX_RECOVERY_TESTS:
            raise ValueError("recovery Test count is outside the closed policy")
        partitions = self.partitions()
        uid_sets = {name: {task.uid for task in tasks} for name, tasks in partitions.items()}
        if any(len(uid_sets[name]) != len(tasks) for name, tasks in partitions.items()):
            raise ValueError("split contains a duplicate UID")
        names = tuple(partitions)
        for index, left in enumerate(names):
            for right in names[index + 1 :]:
                if uid_sets[left] & uid_sets[right]:
                    raise ValueError("split partitions overlap")
        expected = {
            "train": TRAIN_PER_CATEGORY,
            "probe": PROBE_PER_CATEGORY,
            "test_primary": TEST_PER_CATEGORY,
        }
        expected.update(
            {f"test_recovery_{index:02d}": TEST_PER_CATEGORY for index in range(1, len(self.recovery_tests) + 1)}
        )
        for name, per_category in expected.items():
            counts = {
                category: sum(task.category == category for task in partitions[name])
                for category in CHEMBENCH4K_CATEGORIES
            }
            if set(counts.values()) != {per_category}:
                raise ValueError(f"{name} is not category-balanced")
        holdout_names = tuple(
            name for name in partitions if name.startswith(("probe", "test_"))
        )
        if any(uid_sets[name] & self.actual_exposed_uids for name in holdout_names):
            raise ValueError("Probe/Test contains actual historical exposure")
        if not self.train_supplemented_unseen <= uid_sets["train"]:
            raise ValueError("Train unseen supplement is not a Train subset")

    def partitions(self) -> dict[str, tuple[PrivateChemBench4KTask, ...]]:
        result = {
            "train": self.train,
            "probe": self.probe,
            "test_primary": self.test_primary,
        }
        result.update(
            {
                f"test_recovery_{index:02d}": tasks
                for index, tasks in enumerate(self.recovery_tests, start=1)
            }
        )
        result["reserve"] = self.reserve
        return result


@dataclass(frozen=True, slots=True)
class GeneratedSplitArtifactsV2:
    paths: dict[str, Path]
    sha256: dict[str, str]
    split_summary_sha256: str
    isolation_receipt_sha256: str


def generate_balanced_split_v2(
    loader: ChemBench4KDatasetLoader,
    *,
    exposure_bundle: HistoricalExposureBundleV2,
) -> FrozenSupervisedSplitV2:
    if type(loader) is not ChemBench4KDatasetLoader:
        raise TypeError("loader must be exact ChemBench4KDatasetLoader")
    if type(exposure_bundle) is not HistoricalExposureBundleV2:
        raise TypeError("exposure_bundle must be exact HistoricalExposureBundleV2")
    all_tasks = loader.load_split("test")
    task_by_uid = {task.uid: task for task in all_tasks}
    evidence_by_uid = {item.uid: item for item in exposure_bundle.items}
    if set(evidence_by_uid) != set(task_by_uid):
        raise SupervisedSplitV2Error("EXPOSURE_DATASET_UID_SET_MISMATCH")
    if any(evidence_by_uid[uid].category != task.category for uid, task in task_by_uid.items()):
        raise SupervisedSplitV2Error("EXPOSURE_DATASET_CATEGORY_MISMATCH")

    actual = exposure_bundle.actual_exposed_uids
    train_by_category: dict[str, list[PrivateChemBench4KTask]] = {}
    remaining_holdout_by_category: dict[str, list[PrivateChemBench4KTask]] = {}
    supplemented: set[str] = set()
    recovery_capacity: dict[str, int] = {}
    for category in CHEMBENCH4K_CATEGORIES:
        category_tasks = sorted(
            (task for task in all_tasks if task.category == category),
            key=lambda task: task.uid,
        )
        actual_tasks = [task for task in category_tasks if task.uid in actual]
        eligible = [task for task in category_tasks if task.uid not in actual]
        ordered_actual = _seeded_order(
            actual_tasks,
            category=category,
            purpose="train-actual-exposure",
            evidence_by_uid=evidence_by_uid,
        )
        train = ordered_actual[:TRAIN_PER_CATEGORY]
        deficit = TRAIN_PER_CATEGORY - len(train)
        ordered_eligible = _seeded_order(
            eligible,
            category=category,
            purpose="train-never-executed-supplement",
        )
        supplement = ordered_eligible[:deficit]
        if len(supplement) != deficit:
            raise SupervisedSplitV2Error(
                "INSUFFICIENT_TRAIN_POOL",
                category_counts={category: len(eligible)},
            )
        train.extend(supplement)
        supplemented.update(task.uid for task in supplement)
        train_by_category[category] = train
        train_uids = {task.uid for task in train}
        remaining = [task for task in eligible if task.uid not in train_uids]
        required_primary = PROBE_PER_CATEGORY + TEST_PER_CATEGORY
        if len(remaining) < required_primary:
            raise SupervisedSplitV2Error(
                "INSUFFICIENT_STRICT_HOLDOUT_POOL",
                category_counts={category: len(remaining)},
            )
        remaining_holdout_by_category[category] = remaining
        recovery_capacity[category] = min(
            MAX_RECOVERY_TESTS,
            (len(remaining) - required_primary) // TEST_PER_CATEGORY,
        )

    recovery_count = min(recovery_capacity.values())
    selected: dict[str, list[PrivateChemBench4KTask]] = defaultdict(list)
    for category in CHEMBENCH4K_CATEGORIES:
        train = train_by_category[category]
        remaining = remaining_holdout_by_category[category]
        selected["train"].extend(
            _seeded_order(train, category=category, purpose="train-order")
        )
        probe = _seeded_order(remaining, category=category, purpose="probe")[:PROBE_PER_CATEGORY]
        selected["probe"].extend(probe)
        used = {task.uid for task in probe}
        primary_pool = [task for task in remaining if task.uid not in used]
        primary = _seeded_order(
            primary_pool,
            category=category,
            purpose="test-primary",
        )[:TEST_PER_CATEGORY]
        selected["test_primary"].extend(primary)
        used.update(task.uid for task in primary)
        for index in range(1, recovery_count + 1):
            recovery_pool = [task for task in remaining if task.uid not in used]
            recovery = _seeded_order(
                recovery_pool,
                category=category,
                purpose=f"test-recovery-{index:02d}",
            )[:TEST_PER_CATEGORY]
            selected[f"test_recovery_{index:02d}"].extend(recovery)
            used.update(task.uid for task in recovery)

    used_all = {
        task.uid
        for name, tasks in selected.items()
        if name != "reserve"
        for task in tasks
    }
    selected["reserve"] = [task for task in all_tasks if task.uid not in used_all]
    split = FrozenSupervisedSplitV2(
        train=tuple(selected["train"]),
        probe=tuple(selected["probe"]),
        test_primary=tuple(selected["test_primary"]),
        recovery_tests=tuple(
            tuple(selected[f"test_recovery_{index:02d}"])
            for index in range(1, recovery_count + 1)
        ),
        reserve=tuple(selected["reserve"]),
        actual_exposed_uids=actual,
        train_supplemented_unseen=frozenset(supplemented),
    )
    if sum(len(tasks) for tasks in split.partitions().values()) != loader.manifest.test_count:
        raise SupervisedSplitV2Error("SPLIT_DOES_NOT_COVER_TEST_POOL")
    return split


def render_split_artifacts_v2(
    split: FrozenSupervisedSplitV2,
    *,
    loader: ChemBench4KDatasetLoader,
    exposure_bundle: HistoricalExposureBundleV2,
) -> dict[str, bytes]:
    if type(split) is not FrozenSupervisedSplitV2:
        raise TypeError("split must be exact FrozenSupervisedSplitV2")
    partitions = split.partitions()
    outputs: dict[str, bytes] = {}
    data_file_sha256: dict[str, str] = {}
    ordered_uid_sha256: dict[str, str] = {}
    per_category_counts: dict[str, dict[str, int]] = {}
    for partition, tasks in partitions.items():
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
        data_file_sha256[public_name] = sha256_bytes(public)
        if partition != "reserve":
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
            data_file_sha256[private_name] = sha256_bytes(private)
        ordered_uid_sha256[partition] = _ordered_uid_digest(tasks)
        per_category_counts[partition] = {
            category: sum(task.category == category for task in tasks)
            for category in CHEMBENCH4K_CATEGORIES
        }

    intersections = _intersection_counts(partitions)
    actual_counts = {
        partition: sum(task.uid in split.actual_exposed_uids for task in tasks)
        for partition, tasks in partitions.items()
    }
    train_actual_by_category = {
        category: sum(
            task.category == category and task.uid in split.actual_exposed_uids
            for task in split.train
        )
        for category in CHEMBENCH4K_CATEGORIES
    }
    train_unseen_by_category = {
        category: sum(
            task.category == category and task.uid in split.train_supplemented_unseen
            for task in split.train
        )
        for category in CHEMBENCH4K_CATEGORIES
    }
    summary = {
        "schema_version": SPLIT_SUMMARY_SCHEMA_V2,
        "protocol_id": PROTOCOL_ID,
        "seed": SPLIT_SEED,
        "dataset_repository": loader.manifest.repository,
        "dataset_revision": loader.manifest.revision,
        "dataset_combined_sha256": loader.manifest.combined_sha256,
        "historical_exposure_manifest_v2_sha256": exposure_bundle.manifest_sha256,
        "counts": {name: len(tasks) for name, tasks in partitions.items()},
        "per_category_counts": per_category_counts,
        "ordered_uid_sha256": ordered_uid_sha256,
        "data_file_sha256": data_file_sha256,
        "train_actual_exposure": train_actual_by_category,
        "train_supplemented_never_executed": train_unseen_by_category,
        "actual_exposure_counts": actual_counts,
        "pairwise_intersection_counts": intersections,
        "recovery_test_count": len(split.recovery_tests),
        "recovery_policy": {
            "maximum": MAX_RECOVERY_TESTS,
            "primary_test_never_reduced": True,
            "performance_based_switch_forbidden": True,
            "source_bug_invalidation_required": True,
        },
        "split_generation_code_sha256": sha256_bytes(Path(__file__).read_bytes()),
    }
    outputs["split_summary_v2.json"] = canonical_pretty_json_bytes(summary)
    summary_sha256 = sha256_bytes(outputs["split_summary_v2.json"])
    holdout_names = ["probe", "test_primary"] + [
        f"test_recovery_{index:02d}" for index in range(1, len(split.recovery_tests) + 1)
    ]
    isolation = {
        "schema_version": SPLIT_ISOLATION_SCHEMA_V2,
        "protocol_id": PROTOCOL_ID,
        "status": "PASS",
        "split_summary_sha256": summary_sha256,
        "historical_exposure_manifest_v2_sha256": exposure_bundle.manifest_sha256,
        "all_partition_uids_pairwise_disjoint": all(value == 0 for value in intersections.values()),
        "probe_actual_exposure_count": actual_counts["probe"],
        "test_actual_exposure_counts": {
            name: actual_counts[name] for name in holdout_names if name.startswith("test_")
        },
        "reflector_input_uid_allowlist_partition": "train",
        "reflector_input_uid_allowlist_sha256": _uid_set_digest(split.train),
        "memory_provenance_uid_allowlist_sha256": _uid_set_digest(split.train),
        "probe_evolution_jobs_allowed": False,
        "test_evolution_jobs_allowed": False,
        "test_artifact_set_policy": "frozen_before_first_primary_test_call",
        "primary_test_ordered_uid_sha256": ordered_uid_sha256["test_primary"],
        "recovery_test_ordered_uid_sha256": {
            name: ordered_uid_sha256[name]
            for name in holdout_names
            if name.startswith("test_recovery_")
        },
        "manifest_regeneration_policy": "byte-for-byte",
    }
    outputs["split_isolation_receipt_v2.json"] = canonical_pretty_json_bytes(isolation)
    return outputs


def write_split_artifacts_v2(
    outputs: dict[str, bytes],
    *,
    destination_root: Path,
) -> GeneratedSplitArtifactsV2:
    _validate_output_names(outputs)
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
    return GeneratedSplitArtifactsV2(
        paths=paths,
        sha256=digests,
        split_summary_sha256=digests["split_summary_v2.json"],
        isolation_receipt_sha256=digests["split_isolation_receipt_v2.json"],
    )


def verify_split_artifacts_v2(
    *,
    expected: dict[str, bytes],
    destination_root: Path,
) -> GeneratedSplitArtifactsV2:
    _validate_output_names(expected)
    for name, payload in expected.items():
        path = destination_root / name
        if path.read_bytes() != payload:
            raise SupervisedSplitV2Error("SPLIT_ARTIFACT_REGENERATION_MISMATCH")
        expected_mode = 0o600 if name.endswith("_private_manifest.jsonl") else 0o644
        if (path.stat().st_mode & 0o777) != expected_mode:
            raise SupervisedSplitV2Error("SPLIT_ARTIFACT_MODE_MISMATCH")
    digests = {name: sha256_bytes(payload) for name, payload in expected.items()}
    return GeneratedSplitArtifactsV2(
        paths={name: destination_root / name for name in expected},
        sha256=digests,
        split_summary_sha256=digests["split_summary_v2.json"],
        isolation_receipt_sha256=digests["split_isolation_receipt_v2.json"],
    )


def load_private_partition_v2(
    loader: ChemBench4KDatasetLoader,
    *,
    partition: str,
    private_manifest: Path,
) -> tuple[PrivateChemBench4KTask, ...]:
    if partition not in {"train", "probe", "test_primary", "test_recovery_01", "test_recovery_02"}:
        raise ValueError("partition is outside the private V2 manifest contract")
    if (private_manifest.stat().st_mode & 0o777) != 0o600:
        raise SupervisedSplitV2Error("PRIVATE_MANIFEST_MODE_MISMATCH")
    by_uid = {task.uid: task for task in loader.load_split("test")}
    tasks: list[PrivateChemBench4KTask] = []
    try:
        for ordinal, line in enumerate(private_manifest.read_text(encoding="utf-8").splitlines()):
            row = json.loads(line)
            task = by_uid.get(row.get("uid")) if isinstance(row, dict) else None
            if (
                task is None
                or row.get("partition") != partition
                or row.get("ordinal") != ordinal
                or row.get("category") != task.category
                or row.get("source_split") != task.source_split
                or row.get("source_index") != task.source_index
                or row.get("target") != task.target
                or row.get("dataset_revision") != task.dataset_revision
                or row.get("dataset_sha256") != task.dataset_sha256
            ):
                raise SupervisedSplitV2Error("PRIVATE_MANIFEST_BINDING_MISMATCH")
            tasks.append(task)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SupervisedSplitV2Error("PRIVATE_MANIFEST_UNREADABLE") from exc
    per_category = TRAIN_PER_CATEGORY if partition == "train" else (
        PROBE_PER_CATEGORY if partition == "probe" else TEST_PER_CATEGORY
    )
    expected_count = per_category * len(CHEMBENCH4K_CATEGORIES)
    if len(tasks) != expected_count or len({task.uid for task in tasks}) != expected_count:
        raise SupervisedSplitV2Error("PRIVATE_MANIFEST_COUNT_MISMATCH")
    return tuple(tasks)


def _seeded_order(
    tasks: list[PrivateChemBench4KTask],
    *,
    category: str,
    purpose: str,
    evidence_by_uid: dict[str, HistoricalExposureItemV2] | None = None,
) -> list[PrivateChemBench4KTask]:
    ordered = sorted(tasks, key=lambda task: task.uid)
    if evidence_by_uid is not None:
        ordered.sort(key=lambda task: (_exposure_priority(evidence_by_uid[task.uid]), task.uid))
        grouped: list[PrivateChemBench4KTask] = []
        for priority in sorted({_exposure_priority(evidence_by_uid[task.uid]) for task in ordered}):
            group = [task for task in ordered if _exposure_priority(evidence_by_uid[task.uid]) == priority]
            grouped.extend(_shuffle(group, category=category, purpose=f"{purpose}-priority-{priority}"))
        return grouped
    return _shuffle(ordered, category=category, purpose=purpose)


def _shuffle(
    tasks: list[PrivateChemBench4KTask],
    *,
    category: str,
    purpose: str,
) -> list[PrivateChemBench4KTask]:
    result = list(tasks)
    seed = hashlib.sha256(f"{SPLIT_SEED}\x1f{category}\x1f{purpose}".encode()).digest()
    random.Random(seed).shuffle(result)
    return result


def _exposure_priority(item: HistoricalExposureItemV2) -> int:
    labels = {ExposureLabelV2(label) for label in item.labels}
    for index, label in enumerate(
        (
            ExposureLabelV2.REFLECTOR_SUPERVISED,
            ExposureLabelV2.PRIVATE_EVALUATED,
            ExposureLabelV2.TASK_MODEL_EXECUTED,
            ExposureLabelV2.TASK_MODEL_ATTEMPTED,
        )
    ):
        if label in labels:
            return index
    if labels & ACTUAL_EXPOSURE_LABELS_V2:
        return 4
    raise ValueError("non-exposed item cannot enter actual-exposure priority")


def _validate_output_names(outputs: dict[str, bytes]) -> None:
    required = {
        "train_public_manifest.jsonl",
        "train_private_manifest.jsonl",
        "probe_public_manifest.jsonl",
        "probe_private_manifest.jsonl",
        "test_primary_public_manifest.jsonl",
        "test_primary_private_manifest.jsonl",
        "reserve_public_manifest.jsonl",
        "split_summary_v2.json",
        "split_isolation_receipt_v2.json",
    }
    optional_pairs = {
        index: {
            f"test_recovery_{index:02d}_public_manifest.jsonl",
            f"test_recovery_{index:02d}_private_manifest.jsonl",
        }
        for index in range(1, MAX_RECOVERY_TESTS + 1)
    }
    names = set(outputs)
    if not required <= names:
        raise ValueError("split V2 output set is incomplete")
    extras = names - required
    admitted: set[str] = set()
    for index in range(1, MAX_RECOVERY_TESTS + 1):
        pair = optional_pairs[index]
        if extras & pair:
            if not pair <= extras or index > 1 and not optional_pairs[index - 1] <= extras:
                raise ValueError("recovery Test output pairs are not contiguous")
            admitted.update(pair)
    if extras != admitted:
        raise ValueError("split V2 output set contains unknown files")


def _category_ordinal(tasks: tuple[PrivateChemBench4KTask, ...], ordinal: int) -> int:
    category = tasks[ordinal].category
    return sum(task.category == category for task in tasks[:ordinal])


def _ordered_uid_digest(tasks: tuple[PrivateChemBench4KTask, ...]) -> str:
    return sha256_bytes(("\n".join(task.uid for task in tasks) + "\n").encode("ascii"))


def _uid_set_digest(tasks: tuple[PrivateChemBench4KTask, ...]) -> str:
    return sha256_bytes(("\n".join(sorted(task.uid for task in tasks)) + "\n").encode("ascii"))


def _intersection_counts(
    partitions: dict[str, tuple[PrivateChemBench4KTask, ...]],
) -> dict[str, int]:
    result: dict[str, int] = {}
    names = tuple(partitions)
    for index, left in enumerate(names):
        left_uids = {task.uid for task in partitions[left]}
        for right in names[index + 1 :]:
            result[f"{left}_{right}"] = len(
                left_uids & {task.uid for task in partitions[right]}
            )
    return result


__all__ = [
    "MAX_RECOVERY_TESTS",
    "SPLIT_ISOLATION_SCHEMA_V2",
    "SPLIT_SUMMARY_SCHEMA_V2",
    "FrozenSupervisedSplitV2",
    "GeneratedSplitArtifactsV2",
    "SupervisedSplitV2Error",
    "generate_balanced_split_v2",
    "load_private_partition_v2",
    "render_split_artifacts_v2",
    "verify_split_artifacts_v2",
    "write_split_artifacts_v2",
]
