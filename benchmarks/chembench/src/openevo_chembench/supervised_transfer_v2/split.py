"""Deterministic exposure-aware Train/Test split for supervised transfer v2."""

from __future__ import annotations

import hashlib
import json
import random
import stat
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
from openevo_chembench.supervised_transfer_v1.exposure_v2 import (
    ACTUAL_EXPOSURE_LABELS_V2,
    ExposureLabelV2,
    HistoricalExposureBundleV2,
    HistoricalExposureItemV2,
)
from openevo_chembench.supervised_transfer_v2.config import (
    PROTOCOL_ID,
    SPLIT_SEED,
    TEST_PER_CATEGORY,
    TRAIN_PER_CATEGORY,
)

SPLIT_SUMMARY_SCHEMA = "ChemBenchSupervisedTransferSplitSummaryV2"
SPLIT_ISOLATION_SCHEMA = "SplitIsolationReceiptV2"
PARTITIONS = ("train", "test", "reserve")


class SupervisedTransferSplitV2Error(RuntimeError):
    """Fail-closed split error without task content."""

    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True)
class FrozenTrainTestSplitV2:
    train: tuple[PrivateChemBench4KTask, ...]
    test: tuple[PrivateChemBench4KTask, ...]
    reserve: tuple[PrivateChemBench4KTask, ...]
    actual_exposed_uids: frozenset[str]
    supplemented_train_uids: frozenset[str]

    def __post_init__(self) -> None:
        partitions = self.partitions
        uid_sets = {name: {task.uid for task in tasks} for name, tasks in partitions.items()}
        if any(len(uid_sets[name]) != len(tasks) for name, tasks in partitions.items()):
            raise ValueError("split contains duplicate UIDs")
        if (
            uid_sets["train"] & uid_sets["test"]
            or uid_sets["train"] & uid_sets["reserve"]
            or uid_sets["test"] & uid_sets["reserve"]
        ):
            raise ValueError("split partitions overlap")
        for name, expected in (("train", TRAIN_PER_CATEGORY), ("test", TEST_PER_CATEGORY)):
            counts = {
                category: sum(task.category == category for task in partitions[name])
                for category in CHEMBENCH4K_CATEGORIES
            }
            if set(counts.values()) != {expected}:
                raise ValueError(f"{name} is not category-balanced")
        if uid_sets["test"] & self.actual_exposed_uids:
            raise ValueError("Test contains actual historical exposure")
        if not self.supplemented_train_uids <= uid_sets["train"]:
            raise ValueError("Train supplement is not a Train subset")

    @property
    def partitions(self) -> dict[str, tuple[PrivateChemBench4KTask, ...]]:
        return {"train": self.train, "test": self.test, "reserve": self.reserve}


def generate_train_test_split_v2(
    loader: ChemBench4KDatasetLoader,
    *,
    exposure: HistoricalExposureBundleV2,
) -> FrozenTrainTestSplitV2:
    if type(loader) is not ChemBench4KDatasetLoader:
        raise TypeError("loader must be exact ChemBench4KDatasetLoader")
    if type(exposure) is not HistoricalExposureBundleV2:
        raise TypeError("exposure must be exact HistoricalExposureBundleV2")
    tasks = loader.load_split("test")
    task_by_uid = {task.uid: task for task in tasks}
    evidence_by_uid = {item.uid: item for item in exposure.items}
    if set(task_by_uid) != set(evidence_by_uid):
        raise SupervisedTransferSplitV2Error("EXPOSURE_DATASET_UID_SET_MISMATCH")
    actual = exposure.actual_exposed_uids
    train: list[PrivateChemBench4KTask] = []
    test: list[PrivateChemBench4KTask] = []
    supplemented: set[str] = set()
    for category in CHEMBENCH4K_CATEGORIES:
        category_tasks = sorted(
            (task for task in tasks if task.category == category),
            key=lambda task: task.uid,
        )
        exposed = [task for task in category_tasks if task.uid in actual]
        never = [task for task in category_tasks if task.uid not in actual]
        exposed = _ordered_exposed(exposed, category=category, evidence=evidence_by_uid)
        selected_train = exposed[:TRAIN_PER_CATEGORY]
        deficit = TRAIN_PER_CATEGORY - len(selected_train)
        never_for_train = _shuffle(never, category=category, purpose="train-supplement")
        supplement = never_for_train[:deficit]
        if len(supplement) != deficit:
            raise SupervisedTransferSplitV2Error("INSUFFICIENT_TRAIN_POOL")
        selected_train.extend(supplement)
        supplemented.update(task.uid for task in supplement)
        train.extend(_shuffle(selected_train, category=category, purpose="train-order"))
        train_uids = {task.uid for task in selected_train}
        holdout_pool = [task for task in never if task.uid not in train_uids]
        selected_test = _shuffle(holdout_pool, category=category, purpose="test")[:TEST_PER_CATEGORY]
        if len(selected_test) != TEST_PER_CATEGORY:
            raise SupervisedTransferSplitV2Error("INSUFFICIENT_NEVER_EXPOSED_TEST_POOL")
        test.extend(selected_test)
    used = {task.uid for task in (*train, *test)}
    reserve = tuple(task for task in tasks if task.uid not in used)
    split = FrozenTrainTestSplitV2(
        train=tuple(train),
        test=tuple(test),
        reserve=reserve,
        actual_exposed_uids=actual,
        supplemented_train_uids=frozenset(supplemented),
    )
    if sum(len(value) for value in split.partitions.values()) != loader.manifest.test_count:
        raise SupervisedTransferSplitV2Error("SPLIT_DOES_NOT_COVER_DATASET")
    return split


def render_split_artifacts_v2(
    split: FrozenTrainTestSplitV2,
    *,
    loader: ChemBench4KDatasetLoader,
    exposure: HistoricalExposureBundleV2,
) -> dict[str, bytes]:
    if type(split) is not FrozenTrainTestSplitV2:
        raise TypeError("split must be exact FrozenTrainTestSplitV2")
    outputs: dict[str, bytes] = {}
    file_sha256: dict[str, str] = {}
    order_sha256: dict[str, str] = {}
    category_counts: dict[str, dict[str, int]] = {}
    actual_counts: dict[str, int] = {}
    for partition, tasks in split.partitions.items():
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
        file_sha256[public_name] = sha256_bytes(public)
        if partition in {"train", "test"}:
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
            file_sha256[private_name] = sha256_bytes(private)
        order_sha256[partition] = _ordered_uid_sha256(tasks)
        category_counts[partition] = {
            category: sum(task.category == category for task in tasks)
            for category in CHEMBENCH4K_CATEGORIES
        }
        actual_counts[partition] = sum(task.uid in split.actual_exposed_uids for task in tasks)
    intersection = len({task.uid for task in split.train} & {task.uid for task in split.test})
    summary = {
        "schema_version": SPLIT_SUMMARY_SCHEMA,
        "protocol_id": PROTOCOL_ID,
        "seed": SPLIT_SEED,
        "dataset_repository": loader.manifest.repository,
        "dataset_revision": loader.manifest.revision,
        "dataset_combined_sha256": loader.manifest.combined_sha256,
        "historical_exposure_manifest_v2_sha256": exposure.manifest_sha256,
        "exposure_definition": {
            "excluded_from_test": sorted(label.value for label in ACTUAL_EXPOSURE_LABELS_V2),
            "manifest_listed_only_excluded": False,
            "reason": "manifest listing alone is not a model, evaluator, reflector, or item-review exposure",
        },
        "counts": {name: len(tasks) for name, tasks in split.partitions.items()},
        "per_category_counts": category_counts,
        "ordered_uid_sha256": order_sha256,
        "file_sha256": file_sha256,
        "actual_exposure_counts": actual_counts,
        "train_supplemented_never_executed_count": len(split.supplemented_train_uids),
        "train_test_intersection_count": intersection,
        "split_generation_code_sha256": sha256_bytes(Path(__file__).read_bytes()),
    }
    outputs["split_summary.json"] = canonical_pretty_json_bytes(summary)
    summary_sha256 = sha256_bytes(outputs["split_summary.json"])
    isolation = {
        "schema_version": SPLIT_ISOLATION_SCHEMA,
        "protocol_id": PROTOCOL_ID,
        "status": "PASS",
        "split_summary_sha256": summary_sha256,
        "historical_exposure_manifest_v2_sha256": exposure.manifest_sha256,
        "train_test_uid_disjoint": intersection == 0,
        "test_actual_exposure_count": actual_counts["test"],
        "test_ordered_uid_sha256": order_sha256["test"],
        "reflector_uid_allowlist_partition": "train",
        "reflector_uid_allowlist_sha256": _uid_set_sha256(split.train),
        "artifact_provenance_uid_allowlist_sha256": _uid_set_sha256(split.train),
        "test_evolution_jobs_allowed": False,
        "test_artifact_set_policy": "frozen_before_first_test_call",
        "test_consumption_policy": "one-valid-completion-per-uid-per-arm",
        "manifest_regeneration_policy": "byte-for-byte",
    }
    outputs["split_isolation_receipt_v2.json"] = canonical_pretty_json_bytes(isolation)
    return outputs


def write_split_artifacts_v2(outputs: dict[str, bytes], *, destination: Path) -> None:
    _validate_names(outputs)
    destination.mkdir(parents=True, exist_ok=True)
    destination.chmod(0o755)
    for name in sorted(outputs):
        path = destination / name
        if name.endswith("_private_manifest.jsonl"):
            write_private_file(path, outputs[name])
        else:
            write_public_file(path, outputs[name])


def verify_split_artifacts_v2(outputs: dict[str, bytes], *, destination: Path) -> dict[str, str]:
    _validate_names(outputs)
    digests: dict[str, str] = {}
    for name, expected in outputs.items():
        path = destination / name
        if path.read_bytes() != expected:
            raise SupervisedTransferSplitV2Error("SPLIT_REGENERATION_MISMATCH")
        mode = stat.S_IMODE(path.stat().st_mode)
        expected_mode = 0o600 if name.endswith("_private_manifest.jsonl") else 0o644
        if mode != expected_mode:
            raise SupervisedTransferSplitV2Error("SPLIT_FILE_MODE_INVALID")
        digests[name] = sha256_bytes(expected)
    return dict(sorted(digests.items()))


def load_private_partition_v2(
    loader: ChemBench4KDatasetLoader,
    *,
    partition: str,
    path: Path,
) -> tuple[PrivateChemBench4KTask, ...]:
    if partition not in {"train", "test"} or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise SupervisedTransferSplitV2Error("PRIVATE_MANIFEST_INVALID")
    by_uid = {task.uid: task for task in loader.load_split("test")}
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    tasks: list[PrivateChemBench4KTask] = []
    for ordinal, row in enumerate(rows):
        if (
            type(row) is not dict
            or row.get("partition") != partition
            or row.get("ordinal") != ordinal
            or row.get("uid") not in by_uid
        ):
            raise SupervisedTransferSplitV2Error("PRIVATE_MANIFEST_INVALID")
        task = by_uid[row["uid"]]
        expected = {
            "partition": partition,
            "ordinal": ordinal,
            "category_ordinal": _category_ordinal(tuple(by_uid[value["uid"]] for value in rows), ordinal),
            "uid": task.uid,
            "category": task.category,
            "source_split": task.source_split,
            "source_index": task.source_index,
            "target": task.target,
            "dataset_revision": task.dataset_revision,
            "dataset_sha256": task.dataset_sha256,
        }
        if row != expected:
            raise SupervisedTransferSplitV2Error("PRIVATE_MANIFEST_INVALID")
        tasks.append(task)
    return tuple(tasks)


def _ordered_exposed(
    tasks: list[PrivateChemBench4KTask],
    *,
    category: str,
    evidence: dict[str, HistoricalExposureItemV2],
) -> list[PrivateChemBench4KTask]:
    result: list[PrivateChemBench4KTask] = []
    priorities = sorted({_exposure_priority(evidence[task.uid]) for task in tasks})
    for priority in priorities:
        group = [task for task in tasks if _exposure_priority(evidence[task.uid]) == priority]
        result.extend(_shuffle(group, category=category, purpose=f"train-exposed-{priority}"))
    return result


def _exposure_priority(item: HistoricalExposureItemV2) -> int:
    labels = {ExposureLabelV2(value) for value in item.labels}
    for index, label in enumerate(
        (
            ExposureLabelV2.REFLECTOR_SUPERVISED,
            ExposureLabelV2.PRIVATE_EVALUATED,
            ExposureLabelV2.TASK_MODEL_EXECUTED,
            ExposureLabelV2.TASK_MODEL_ATTEMPTED,
            ExposureLabelV2.HUMAN_ITEM_REVIEWED,
        )
    ):
        if label in labels:
            return index
    raise ValueError("non-exposed item entered exposure priority")


def _shuffle(
    tasks: list[PrivateChemBench4KTask],
    *,
    category: str,
    purpose: str,
) -> list[PrivateChemBench4KTask]:
    values = sorted(tasks, key=lambda task: task.uid)
    seed = hashlib.sha256(f"{SPLIT_SEED}\x1f{category}\x1f{purpose}".encode()).digest()
    random.Random(seed).shuffle(values)
    return values


def _category_ordinal(tasks: tuple[PrivateChemBench4KTask, ...], ordinal: int) -> int:
    return sum(task.category == tasks[ordinal].category for task in tasks[:ordinal])


def _ordered_uid_sha256(tasks: tuple[PrivateChemBench4KTask, ...]) -> str:
    return sha256_bytes(("\n".join(task.uid for task in tasks) + "\n").encode())


def _uid_set_sha256(tasks: tuple[PrivateChemBench4KTask, ...]) -> str:
    return sha256_bytes(("\n".join(sorted(task.uid for task in tasks)) + "\n").encode())


def _validate_names(outputs: dict[str, bytes]) -> None:
    expected = {
        "train_public_manifest.jsonl",
        "train_private_manifest.jsonl",
        "test_public_manifest.jsonl",
        "test_private_manifest.jsonl",
        "reserve_public_manifest.jsonl",
        "split_summary.json",
        "split_isolation_receipt_v2.json",
    }
    if set(outputs) != expected or any(type(value) is not bytes for value in outputs.values()):
        raise SupervisedTransferSplitV2Error("SPLIT_OUTPUT_SET_INVALID")


__all__ = [
    "FrozenTrainTestSplitV2",
    "SupervisedTransferSplitV2Error",
    "generate_train_test_split_v2",
    "load_private_partition_v2",
    "render_split_artifacts_v2",
    "verify_split_artifacts_v2",
    "write_split_artifacts_v2",
]
