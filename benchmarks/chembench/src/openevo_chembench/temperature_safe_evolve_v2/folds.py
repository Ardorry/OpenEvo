"""Target-blind deterministic four-fold construction for the reused official pool."""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass

from openevo_chembench.chembench4k_dataset import (
    ChemBench4KDatasetLoader,
    normalize_benchmark_text,
)
from openevo_chembench.chembench4k_models import PrivateChemBench4KTask
from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes, sha256_bytes
from openevo_chembench.temperature_full_evolve_v1.split import build_public_split_plan_v1
from openevo_chembench.temperature_safe_evolve_v2.config import (
    CATEGORY,
    EXPECTED_POOL_COUNT,
    FOLD_NAMESPACE,
    RUN_FOLDS,
)

FOLD_IDS = ("F0", "F1", "F2", "F3")
FOLD_SIZE = 50
RESERVE_SIZE = 2
GROUPING_ALGORITHM = "temperature_reaction_lexical_union_find_v1"


class SafeFoldError(RuntimeError):
    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True, repr=False)
class SafeFoldPlanV2:
    folds: tuple[tuple[str, ...], ...]
    reserve: tuple[str, ...]
    groups: tuple[tuple[str, ...], ...]
    fold_sha256: str
    group_manifest_sha256: str
    public_pool_sha256: str
    dataset_sha256: str

    def __post_init__(self) -> None:
        if len(self.folds) != 4 or any(len(value) != FOLD_SIZE for value in self.folds):
            raise ValueError("fold counts are invalid")
        if len(self.reserve) != RESERVE_SIZE:
            raise ValueError("reserve count is invalid")
        parts = (*self.folds, self.reserve)
        flat = tuple(uid for part in parts for uid in part)
        if len(flat) != EXPECTED_POOL_COUNT or len(set(flat)) != EXPECTED_POOL_COUNT:
            raise ValueError("fold UID closure is invalid")
        assignment = {uid: index for index, part in enumerate(parts) for uid in part}
        if any(len({assignment[uid] for uid in group}) != 1 for group in self.groups):
            raise ValueError("a near-duplicate group crosses folds")

    def __repr__(self) -> str:
        return "SafeFoldPlanV2(<UIDs-redacted>; folds=50/50/50/50; reserve=2)"

    def fold(self, fold_id: str) -> tuple[str, ...]:
        if fold_id not in FOLD_IDS:
            raise KeyError(fold_id)
        return self.folds[int(fold_id[1])]

    def public_receipt(self) -> dict[str, object]:
        sizes = Counter(map(len, self.groups))
        return {
            "schema_version": "TemperatureSafeEvolveFoldManifestV2",
            "status": "PASS",
            "classification": "reused_official_temperature_pool_independent_state_mechanism_experiment",
            "namespace": FOLD_NAMESPACE,
            "fold_counts": {fold: FOLD_SIZE for fold in FOLD_IDS},
            "reserve_count": RESERVE_SIZE,
            "grouping_algorithm": GROUPING_ALGORITHM,
            "group_count": len(self.groups),
            "singleton_group_count": sum(len(group) == 1 for group in self.groups),
            "maximum_group_size": max(map(len, self.groups)),
            "group_size_histogram": {str(k): v for k, v in sorted(sizes.items())},
            "whole_group_assignment": True,
            "public_pool_sha256": self.public_pool_sha256,
            "dataset_sha256": self.dataset_sha256,
            "group_manifest_sha256": self.group_manifest_sha256,
            "fold_sha256": self.fold_sha256,
            "public_manifest_contains_uid": False,
            "public_manifest_contains_target": False,
            "public_manifest_contains_task_content": False,
        }


@dataclass(frozen=True, slots=True, repr=False)
class FrozenSafeFoldsV2:
    plan: SafeFoldPlanV2
    tasks_by_uid: dict[str, PrivateChemBench4KTask]

    def __repr__(self) -> str:
        return "FrozenSafeFoldsV2(<private-tasks-redacted>)"

    def tasks(self, fold_id: str) -> tuple[PrivateChemBench4KTask, ...]:
        return tuple(self.tasks_by_uid[uid] for uid in self.plan.fold(fold_id))

    def run_partitions(
        self, run_id: str
    ) -> tuple[
        tuple[PrivateChemBench4KTask, ...],
        tuple[PrivateChemBench4KTask, ...],
        tuple[PrivateChemBench4KTask, ...],
    ]:
        train_folds, validation_fold, test_fold = RUN_FOLDS[run_id]
        train = tuple(task for fold in train_folds for task in self.tasks(fold))
        return train, self.tasks(validation_fold), self.tasks(test_fold)


def build_safe_folds_v2(loader: ChemBench4KDatasetLoader) -> FrozenSafeFoldsV2:
    if type(loader) is not ChemBench4KDatasetLoader:
        raise TypeError("loader must be exact ChemBench4KDatasetLoader")
    tasks = tuple(loader.load_category(CATEGORY, split="test"))
    if (
        len(tasks) != EXPECTED_POOL_COUNT
        or len({task.uid for task in tasks}) != EXPECTED_POOL_COUNT
    ):
        raise SafeFoldError("SAFE_EVOLVE_OFFICIAL_POOL_INVALID")
    _validate_exact_closure(tasks)
    public_plan = build_public_split_plan_v1(tuple(task.to_public() for task in tasks))
    groups = tuple(public_plan.groups)
    ranked = tuple(sorted(groups, key=_group_rank))
    bins: list[list[tuple[str, ...]]] = [[] for _ in range(5)]
    capacities = [FOLD_SIZE, FOLD_SIZE, FOLD_SIZE, FOLD_SIZE, RESERVE_SIZE]
    for group in ranked:
        chosen = next(
            (index for index, capacity in enumerate(capacities) if len(group) <= capacity),
            None,
        )
        if chosen is None:
            raise SafeFoldError("BLOCKED_GROUP_AWARE_FOLD_CONSTRUCTION")
        bins[chosen].append(group)
        capacities[chosen] -= len(group)
    if capacities != [0, 0, 0, 0, 0]:
        raise SafeFoldError("BLOCKED_GROUP_AWARE_FOLD_CONSTRUCTION")
    ordered_parts = tuple(
        tuple(
            sorted(
                (uid for group in selected for uid in group),
                key=lambda uid, part=index: _uid_rank(uid, partition=part),
            )
        )
        for index, selected in enumerate(bins)
    )
    group_manifest_sha256 = sha256_bytes(
        canonical_json_bytes(
            {
                "algorithm": GROUPING_ALGORITHM,
                "groups": [list(group) for group in groups],
            }
        )
    )
    public_pool_sha256 = sha256_bytes(
        canonical_json_bytes(
            [
                task.to_public().to_public_dict()
                for task in sorted(tasks, key=lambda item: item.uid)
            ]
        )
    )
    fold_sha256 = sha256_bytes(
        canonical_json_bytes(
            {
                "namespace": FOLD_NAMESPACE,
                "group_manifest_sha256": group_manifest_sha256,
                **{fold: list(ordered_parts[index]) for index, fold in enumerate(FOLD_IDS)},
                "reserve": list(ordered_parts[4]),
            }
        )
    )
    plan = SafeFoldPlanV2(
        folds=ordered_parts[:4],
        reserve=ordered_parts[4],
        groups=groups,
        fold_sha256=fold_sha256,
        group_manifest_sha256=group_manifest_sha256,
        public_pool_sha256=public_pool_sha256,
        dataset_sha256=loader.manifest.combined_sha256,
    )
    return FrozenSafeFoldsV2(plan=plan, tasks_by_uid={task.uid: task for task in tasks})


def phase_a_halves_v2(test_uids: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    if len(test_uids) != 100 or len(set(test_uids)) != 100:
        raise SafeFoldError("SAFE_EVOLVE_PHASE_A_TEST_IDENTITY_INVALID")
    ranked = tuple(
        sorted(
            test_uids,
            key=lambda uid: (
                hashlib.sha256(
                    f"chembench-temperature-safe-evolve-v2-ablation-halves-v1\0{uid}".encode()
                ).hexdigest(),
                uid,
            ),
        )
    )
    digest = sha256_bytes(
        canonical_json_bytes(
            {
                "namespace": "chembench-temperature-safe-evolve-v2-ablation-halves-v1",
                "H0": list(ranked[:50]),
                "H1": list(ranked[50:]),
            }
        )
    )
    return ranked[:50], ranked[50:], digest


def private_fold_manifest_bytes_v2(folds: FrozenSafeFoldsV2) -> bytes:
    group_by_uid = {
        uid: sha256_bytes(canonical_json_bytes(sorted(group)))
        for group in folds.plan.groups
        for uid in group
    }
    records = []
    for partition, values in (
        *((fold, folds.plan.fold(fold)) for fold in FOLD_IDS),
        ("reserve", folds.plan.reserve),
    ):
        for ordinal, uid in enumerate(values):
            task = folds.tasks_by_uid[uid]
            records.append(
                {
                    "schema_version": "TemperatureSafeEvolvePrivateFoldRecordV2",
                    "partition": partition,
                    "ordinal": ordinal,
                    "uid": uid,
                    "source_index": task.source_index,
                    "target": task.target,
                    "normalized_question": normalize_benchmark_text(task.question),
                    "normalized_options_ordered": [
                        normalize_benchmark_text(getattr(task, label))
                        for label in ("A", "B", "C", "D")
                    ],
                    "near_duplicate_group_sha256": group_by_uid[uid],
                    "dataset_revision": task.dataset_revision,
                    "dataset_sha256": task.dataset_sha256,
                }
            )
    return b"".join(canonical_json_bytes(value) for value in records)


def _validate_exact_closure(tasks: tuple[PrivateChemBench4KTask, ...]) -> None:
    ordered_signatures: set[str] = set()
    option_set_signatures: set[str] = set()
    question_signatures: set[str] = set()
    for task in tasks:
        question = normalize_benchmark_text(task.question)
        ordered = tuple(
            normalize_benchmark_text(getattr(task, label)) for label in ("A", "B", "C", "D")
        )
        unordered = tuple(sorted(ordered))
        question_signatures.add(question)
        ordered_signatures.add(sha256_bytes(canonical_json_bytes((question, ordered))))
        option_set_signatures.add(sha256_bytes(canonical_json_bytes((question, unordered))))
    if (
        len(question_signatures) != len(tasks)
        or len(ordered_signatures) != len(tasks)
        or len(option_set_signatures) != len(tasks)
    ):
        raise SafeFoldError("SAFE_EVOLVE_EXACT_DUPLICATE_PRESENT")


def _group_rank(group: tuple[str, ...]) -> tuple[str, str]:
    token = sha256_bytes(canonical_json_bytes(sorted(group)))
    return hashlib.sha256(f"{FOLD_NAMESPACE}\0group\0{token}".encode()).hexdigest(), token


def _uid_rank(uid: str, *, partition: int) -> tuple[str, str]:
    return hashlib.sha256(f"{FOLD_NAMESPACE}\0order\0{partition}\0{uid}".encode()).hexdigest(), uid


__all__ = [
    "FOLD_IDS",
    "FOLD_SIZE",
    "GROUPING_ALGORITHM",
    "RESERVE_SIZE",
    "FrozenSafeFoldsV2",
    "SafeFoldError",
    "SafeFoldPlanV2",
    "build_safe_folds_v2",
    "phase_a_halves_v2",
    "private_fold_manifest_bytes_v2",
]
