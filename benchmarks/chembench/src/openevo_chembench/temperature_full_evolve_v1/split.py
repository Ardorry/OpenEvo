"""Target-blind, deterministic Temperature 100/100 split construction.

The split policy intentionally does not consult historical ledgers, prior run
artifacts, labels, completions, or correctness.  The fixed protocol uses the
official Temperature pool while every *current-run* state and
Train/Test boundary starts fresh.  Public grouping consumes only
``PublicChemBench4KTask`` values.  Targets are rebound after the complete group
assignment has already been frozen.

Objects containing UID values or private manifest bytes have redacted reprs.
The public receipt contains only aggregate counts, policy strings, and digests.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

from openevo_chembench.chembench4k_dataset import (
    ChemBench4KDatasetLoader,
    normalize_benchmark_text,
)
from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_REPOSITORY,
    CHEMBENCH4K_REVISION,
    PrivateChemBench4KTask,
    PublicChemBench4KTask,
)
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_private_file,
    write_public_file,
)

CATEGORY = "Temperature_Prediction"
PROTOCOL_ID = "chembench_temperature_full_evolve_v1"
SPLIT_SCHEMA_VERSION = "TemperatureFullEvolveSplitV1"
SPLIT_RECEIPT_SCHEMA_VERSION = "TemperatureFullEvolveSplitReceiptV1"
PRIVATE_MANIFEST_SCHEMA_VERSION = "TemperatureFullEvolvePrivateSplitManifestV1"
SPLIT_NAMESPACE = "chembench-temperature-full-evolve-v1-split-v1-20260802"
HISTORICAL_EXPOSURE_POLICY = "fixed_official_temperature_pool_fresh_c0"

SOURCE_TEST_COUNT = 202
DEV_DEMONSTRATION_COUNT = 5
TRAIN_COUNT = 100
TEST_COUNT = 100
RESERVE_COUNT = 2

GROUPING_ALGORITHM_ID = "temperature_reaction_lexical_union_find_v1"
LEXICAL_BALANCED_THRESHOLD = 0.80
LEXICAL_STRONG_THRESHOLD = 0.90
LEXICAL_SECONDARY_THRESHOLD = 0.60

_UID_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


class TemperatureSplitV1Error(RuntimeError):
    """Closed split failure that never embeds task content or UID values."""

    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True, repr=False)
class PublicTemperatureSplitPlanV1:
    """A public-field-derived plan whose identity values have a redacted repr."""

    train_uids: tuple[str, ...]
    test_uids: tuple[str, ...]
    reserve_uids: tuple[str, ...]
    groups: tuple[tuple[str, ...], ...]
    public_pool_content_sha256: str
    group_manifest_sha256: str
    group_assignment_sha256: str
    split_sha256: str
    edge_counts: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        if (len(self.train_uids), len(self.test_uids), len(self.reserve_uids)) != (
            TRAIN_COUNT,
            TEST_COUNT,
            RESERVE_COUNT,
        ):
            raise ValueError("Temperature split plan counts are invalid")
        partitions = self.partitions
        sets = {name: set(values) for name, values in partitions.items()}
        if any(len(sets[name]) != len(values) for name, values in partitions.items()):
            raise ValueError("Temperature split plan contains duplicate identities")
        if sets["train"] & sets["test"] or sets["train"] & sets["reserve"] or sets[
            "test"
        ] & sets["reserve"]:
            raise ValueError("Temperature split plan partitions overlap")
        pool = set().union(*sets.values())
        if len(pool) != SOURCE_TEST_COUNT or any(_UID_RE.fullmatch(uid) is None for uid in pool):
            raise ValueError("Temperature split plan pool identity is invalid")
        grouped = [uid for group in self.groups for uid in group]
        if len(grouped) != SOURCE_TEST_COUNT or set(grouped) != pool:
            raise ValueError("Temperature grouping does not close over the source pool")
        assignment = {uid: name for name, values in partitions.items() for uid in values}
        if any(len({assignment[uid] for uid in group}) != 1 for group in self.groups):
            raise ValueError("Temperature near-duplicate group crosses split partitions")
        for value in (
            self.public_pool_content_sha256,
            self.group_manifest_sha256,
            self.group_assignment_sha256,
            self.split_sha256,
        ):
            if _UID_RE.fullmatch(value) is None:
                raise ValueError("Temperature split digest is invalid")

    def __repr__(self) -> str:
        return (
            "PublicTemperatureSplitPlanV1(<redacted>; "
            f"counts={TRAIN_COUNT}/{TEST_COUNT}/{RESERVE_COUNT}; groups={self.group_count})"
        )

    @property
    def partitions(self) -> dict[str, tuple[str, ...]]:
        return {
            "train": self.train_uids,
            "test": self.test_uids,
            "reserve": self.reserve_uids,
        }

    @property
    def group_count(self) -> int:
        return len(self.groups)

    @property
    def singleton_group_count(self) -> int:
        return sum(len(group) == 1 for group in self.groups)

    @property
    def maximum_group_size(self) -> int:
        return max(map(len, self.groups))


@dataclass(frozen=True, slots=True, repr=False)
class FrozenTemperatureSplitV1:
    """Private task binding for one already-frozen public split plan."""

    plan: PublicTemperatureSplitPlanV1
    train: tuple[PrivateChemBench4KTask, ...]
    test: tuple[PrivateChemBench4KTask, ...]
    reserve: tuple[PrivateChemBench4KTask, ...]
    dataset_combined_sha256: str
    dev_demonstration_count: int
    dev_demonstration_set_sha256: str
    dev_demonstration_content_set_sha256: str
    dev_partition_overlap_counts: tuple[tuple[str, int, int, int], ...]

    def __post_init__(self) -> None:
        if type(self.plan) is not PublicTemperatureSplitPlanV1:
            raise TypeError("plan must be exact PublicTemperatureSplitPlanV1")
        if self.dev_demonstration_count != DEV_DEMONSTRATION_COUNT:
            raise ValueError("Temperature dev demonstration count is invalid")
        if any(
            _UID_RE.fullmatch(value) is None
            for value in (
                self.dataset_combined_sha256,
                self.dev_demonstration_set_sha256,
                self.dev_demonstration_content_set_sha256,
            )
        ):
            raise ValueError("Temperature frozen split identity is invalid")
        if self.dev_partition_overlap_counts != (
            ("reserve", 0, 0, 0),
            ("test", 0, 0, 0),
            ("train", 0, 0, 0),
        ):
            raise ValueError("Temperature dev demonstrations overlap a split partition")
        actual = {
            "train": tuple(task.uid for task in self.train),
            "test": tuple(task.uid for task in self.test),
            "reserve": tuple(task.uid for task in self.reserve),
        }
        if actual != self.plan.partitions:
            raise ValueError("Private split binding differs from public plan")
        tasks = (*self.train, *self.test, *self.reserve)
        if any(
            type(task) is not PrivateChemBench4KTask
            or task.category != CATEGORY
            or task.source_split != "test"
            or task.dataset_revision != CHEMBENCH4K_REVISION
            or task.dataset_sha256 != self.dataset_combined_sha256
            for task in tasks
        ):
            raise ValueError("Private split task identity is invalid")

    def __repr__(self) -> str:
        return (
            "FrozenTemperatureSplitV1(<redacted>; "
            f"counts={len(self.train)}/{len(self.test)}/{len(self.reserve)})"
        )

    @property
    def partitions(self) -> dict[str, tuple[PrivateChemBench4KTask, ...]]:
        return {"train": self.train, "test": self.test, "reserve": self.reserve}


@dataclass(frozen=True, slots=True, repr=False)
class RenderedTemperatureSplitArtifactsV1:
    """Rendered bytes with a repr that cannot expose private manifest values."""

    public_receipt: bytes
    train_private_manifest: bytes
    test_private_manifest: bytes
    reserve_private_manifest: bytes

    def __post_init__(self) -> None:
        if any(
            type(value) is not bytes
            for value in (
                self.public_receipt,
                self.train_private_manifest,
                self.test_private_manifest,
                self.reserve_private_manifest,
            )
        ):
            raise TypeError("rendered split artifacts must contain bytes")

    def __repr__(self) -> str:
        return "RenderedTemperatureSplitArtifactsV1(<private-bytes-redacted>)"

    @property
    def private_manifests(self) -> dict[str, bytes]:
        return {
            "train": self.train_private_manifest,
            "test": self.test_private_manifest,
            "reserve": self.reserve_private_manifest,
        }


def build_public_split_plan_v1(
    tasks: tuple[PublicChemBench4KTask, ...],
) -> PublicTemperatureSplitPlanV1:
    """Group and allocate exactly 202 public tasks without consulting targets."""

    ordered = _validate_public_pool(tasks)
    groups, edge_counts = _build_groups(ordered)
    train_groups, test_groups, reserve_groups = _allocate_whole_groups(groups)
    group_to_partition = {
        group: partition
        for partition, selected in (
            ("train", train_groups),
            ("test", test_groups),
            ("reserve", reserve_groups),
        )
        for group in selected
    }
    partitions = {
        partition: _ordered_partition_uids(selected, partition=partition)
        for partition, selected in (
            ("train", train_groups),
            ("test", test_groups),
            ("reserve", reserve_groups),
        )
    }
    public_pool_content_sha256 = sha256_bytes(
        canonical_json_bytes([task.to_public_dict() for task in ordered])
    )
    group_manifest = {
        "algorithm": GROUPING_ALGORITHM_ID,
        "balanced_threshold": LEXICAL_BALANCED_THRESHOLD,
        "strong_threshold": LEXICAL_STRONG_THRESHOLD,
        "secondary_threshold": LEXICAL_SECONDARY_THRESHOLD,
        "groups": [list(group) for group in groups],
    }
    group_manifest_sha256 = sha256_bytes(canonical_json_bytes(group_manifest))
    assignment = [
        {
            "group_sha256": _group_sha256(group),
            "partition": group_to_partition[group],
        }
        for group in sorted(groups, key=_group_rank)
    ]
    group_assignment_sha256 = sha256_bytes(canonical_json_bytes(assignment))
    split_sha256 = sha256_bytes(
        canonical_json_bytes(
            {
                "namespace": SPLIT_NAMESPACE,
                "group_manifest_sha256": group_manifest_sha256,
                **{name: list(values) for name, values in partitions.items()},
            }
        )
    )
    return PublicTemperatureSplitPlanV1(
        train_uids=partitions["train"],
        test_uids=partitions["test"],
        reserve_uids=partitions["reserve"],
        groups=groups,
        public_pool_content_sha256=public_pool_content_sha256,
        group_manifest_sha256=group_manifest_sha256,
        group_assignment_sha256=group_assignment_sha256,
        split_sha256=split_sha256,
        edge_counts=tuple(sorted(edge_counts.items())),
    )


def build_temperature_split_v1(loader: ChemBench4KDatasetLoader) -> FrozenTemperatureSplitV1:
    """Load the frozen test pool, exclude dev, and bind private tasks after splitting."""

    if type(loader) is not ChemBench4KDatasetLoader:
        raise TypeError("loader must be exact ChemBench4KDatasetLoader")
    test_tasks = loader.load_category(CATEGORY, split="test")
    dev_tasks = loader.load_category(CATEGORY, split="dev")
    if len(test_tasks) != SOURCE_TEST_COUNT:
        raise TemperatureSplitV1Error("TEMPERATURE_TEST_POOL_COUNT_INVALID")
    if len(dev_tasks) != DEV_DEMONSTRATION_COUNT:
        raise TemperatureSplitV1Error("TEMPERATURE_DEV_DEMONSTRATION_COUNT_INVALID")
    test_uids = {task.uid for task in test_tasks}
    dev_uids = {task.uid for task in dev_tasks}
    if len(test_uids) != SOURCE_TEST_COUNT or len(dev_uids) != DEV_DEMONSTRATION_COUNT:
        raise TemperatureSplitV1Error("TEMPERATURE_SOURCE_UID_DUPLICATE")
    if test_uids & dev_uids:
        raise TemperatureSplitV1Error("TEMPERATURE_DEV_TEST_INTERSECTION")

    plan = build_public_split_plan_v1(tuple(task.to_public() for task in test_tasks))
    by_uid = {task.uid: task for task in test_tasks}
    partitions = {
        name: tuple(by_uid[uid] for uid in values) for name, values in plan.partitions.items()
    }
    rebound_content_sha256 = sha256_bytes(
        canonical_json_bytes(
            [by_uid[uid].to_public().to_public_dict() for uid in sorted(by_uid)]
        )
    )
    if rebound_content_sha256 != plan.public_pool_content_sha256:
        raise TemperatureSplitV1Error("PRIVATE_PUBLIC_POOL_BINDING_MISMATCH")
    dev_ordered = tuple(sorted(dev_tasks, key=lambda task: task.source_index))
    if tuple(task.source_index for task in dev_ordered) != tuple(range(DEV_DEMONSTRATION_COUNT)):
        raise TemperatureSplitV1Error("TEMPERATURE_DEV_DEMONSTRATION_ORDER_INVALID")
    dev_uid_set = {task.uid for task in dev_ordered}
    dev_ordered_signatures = {_prompt_signature(task, ordered=True) for task in dev_ordered}
    dev_unordered_signatures = {_prompt_signature(task, ordered=False) for task in dev_ordered}
    overlaps = tuple(
        (
            name,
            len(dev_uid_set & {task.uid for task in tasks}),
            len(
                dev_ordered_signatures
                & {_prompt_signature(task, ordered=True) for task in tasks}
            ),
            len(
                dev_unordered_signatures
                & {_prompt_signature(task, ordered=False) for task in tasks}
            ),
        )
        for name, tasks in sorted(partitions.items())
    )
    return FrozenTemperatureSplitV1(
        plan=plan,
        train=partitions["train"],
        test=partitions["test"],
        reserve=partitions["reserve"],
        dataset_combined_sha256=loader.manifest.combined_sha256,
        dev_demonstration_count=len(dev_ordered),
        dev_demonstration_set_sha256=sha256_bytes(
            canonical_json_bytes(tuple(task.uid for task in dev_ordered))
        ),
        dev_demonstration_content_set_sha256=sha256_bytes(
            canonical_json_bytes(tuple(sorted(dev_ordered_signatures)))
        ),
        dev_partition_overlap_counts=overlaps,
    )


def render_temperature_split_artifacts_v1(
    split: FrozenTemperatureSplitV1,
) -> RenderedTemperatureSplitArtifactsV1:
    """Render aggregate public evidence plus owner-private partition manifests."""

    if type(split) is not FrozenTemperatureSplitV1:
        raise TypeError("split must be exact FrozenTemperatureSplitV1")
    private = {
        name: b"".join(
            canonical_json_bytes(
                {
                    "schema_version": PRIVATE_MANIFEST_SCHEMA_VERSION,
                    "partition": name,
                    "ordinal": ordinal,
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
        for name, tasks in split.partitions.items()
    }
    group_sizes = Counter(map(len, split.plan.groups))
    receipt = {
        "schema_version": SPLIT_RECEIPT_SCHEMA_VERSION,
        "protocol_id": PROTOCOL_ID,
        "status": "PASS",
        "split_namespace": SPLIT_NAMESPACE,
        "historical_exposure_policy": HISTORICAL_EXPOSURE_POLICY,
        "dataset": {
            "repository": CHEMBENCH4K_REPOSITORY,
            "revision": CHEMBENCH4K_REVISION,
            "combined_sha256": split.dataset_combined_sha256,
        },
        "source_pool": {
            "test_pool_count": SOURCE_TEST_COUNT,
            "dev_demonstration_count": split.dev_demonstration_count,
            "dev_policy": "excluded_from_split_fixed_official_five_shot",
            "dev_partition_uid_overlap_count": 0,
            "dev_partition_ordered_prompt_overlap_count": 0,
            "dev_partition_unordered_prompt_overlap_count": 0,
        },
        "counts": {name: len(tasks) for name, tasks in split.partitions.items()},
        "grouping": {
            "algorithm": GROUPING_ALGORITHM_ID,
            "public_fields_only": True,
            "upstream_group_fields_available": [],
            "group_count": split.plan.group_count,
            "singleton_group_count": split.plan.singleton_group_count,
            "maximum_group_size": split.plan.maximum_group_size,
            "group_size_histogram": {
                str(size): count for size, count in sorted(group_sizes.items())
            },
            "edge_counts": dict(split.plan.edge_counts),
            "thresholds": {
                "balanced": LEXICAL_BALANCED_THRESHOLD,
                "strong": LEXICAL_STRONG_THRESHOLD,
                "secondary": LEXICAL_SECONDARY_THRESHOLD,
            },
            "whole_group_assignment": True,
        },
        "integrity": {
            "public_pool_content_sha256": split.plan.public_pool_content_sha256,
            "dev_demonstration_set_sha256": split.dev_demonstration_set_sha256,
            "dev_demonstration_content_set_sha256": (
                split.dev_demonstration_content_set_sha256
            ),
            "group_manifest_sha256": split.plan.group_manifest_sha256,
            "group_assignment_sha256": split.plan.group_assignment_sha256,
            "train_set_sha256": _uid_set_sha256(split.plan.train_uids),
            "test_set_sha256": _uid_set_sha256(split.plan.test_uids),
            "reserve_set_sha256": _uid_set_sha256(split.plan.reserve_uids),
            "train_order_sha256": _uid_order_sha256(split.plan.train_uids),
            "test_order_sha256": _uid_order_sha256(split.plan.test_uids),
            "reserve_order_sha256": _uid_order_sha256(split.plan.reserve_uids),
            "split_sha256": split.plan.split_sha256,
            "private_manifest_sha256": {
                name: sha256_bytes(value) for name, value in private.items()
            },
        },
        "privacy": {
            "public_receipt_contains_item_identities": False,
            "public_receipt_contains_task_content": False,
            "public_receipt_contains_targets": False,
            "private_manifests_owner_only_required": True,
        },
    }
    return RenderedTemperatureSplitArtifactsV1(
        public_receipt=canonical_pretty_json_bytes(receipt),
        train_private_manifest=private["train"],
        test_private_manifest=private["test"],
        reserve_private_manifest=private["reserve"],
    )


def write_temperature_split_artifacts_v1(
    artifacts: RenderedTemperatureSplitArtifactsV1,
    *,
    destination: Path,
) -> None:
    """Write one new split tree without replacing an existing destination."""

    if type(artifacts) is not RenderedTemperatureSplitArtifactsV1:
        raise TypeError("artifacts must be exact RenderedTemperatureSplitArtifactsV1")
    if not isinstance(destination, Path) or not destination.is_absolute():
        raise TypeError("destination must be an absolute pathlib.Path")
    if destination.exists():
        raise TemperatureSplitV1Error("SPLIT_DESTINATION_ALREADY_EXISTS")
    destination.mkdir(parents=True, mode=0o755)
    write_public_file(destination / "public/split_receipt.json", artifacts.public_receipt)
    for name, payload in artifacts.private_manifests.items():
        write_private_file(
            destination / f"private/{name}_manifest.jsonl",
            payload,
            replace=False,
        )


def _validate_public_pool(
    tasks: tuple[PublicChemBench4KTask, ...],
) -> tuple[PublicChemBench4KTask, ...]:
    if not isinstance(tasks, tuple) or len(tasks) != SOURCE_TEST_COUNT:
        raise TemperatureSplitV1Error("TEMPERATURE_PUBLIC_POOL_COUNT_INVALID")
    if any(type(task) is not PublicChemBench4KTask for task in tasks):
        raise TypeError("public split grouping requires exact PublicChemBench4KTask values")
    if any(
        task.category != CATEGORY
        or task.dataset_revision != CHEMBENCH4K_REVISION
        or _UID_RE.fullmatch(task.uid) is None
        for task in tasks
    ):
        raise TemperatureSplitV1Error("TEMPERATURE_PUBLIC_POOL_IDENTITY_INVALID")
    if len({task.uid for task in tasks}) != SOURCE_TEST_COUNT:
        raise TemperatureSplitV1Error("TEMPERATURE_PUBLIC_POOL_UID_DUPLICATE")
    ordered = tuple(sorted(tasks, key=lambda task: task.uid))
    for task in ordered:
        _reaction_signature(task.question)
    return ordered


def _build_groups(
    tasks: tuple[PublicChemBench4KTask, ...],
) -> tuple[tuple[tuple[str, ...], ...], dict[str, int]]:
    parent = list(range(len(tasks)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            if left_root > right_root:
                left_root, right_root = right_root, left_root
            parent[right_root] = left_root

    questions = [normalize_benchmark_text(task.question) for task in tasks]
    option_permutation_keys = [
        "\x1f".join(
            [
                questions[index],
                *sorted(normalize_benchmark_text(getattr(task, label)) for label in "ABCD"),
            ]
        )
        for index, task in enumerate(tasks)
    ]
    reactions = [_reaction_signature(task.question) for task in tasks]
    exact_key_sets = {
        "option_permutation": option_permutation_keys,
        "exact_question": questions,
        "component_order_reaction": [">".join(reaction) for reaction in reactions],
    }
    edge_counts: dict[str, int] = {}
    for name, keys in exact_key_sets.items():
        buckets: dict[str, list[int]] = defaultdict(list)
        for index, key in enumerate(keys):
            buckets[key].append(index)
        edge_counts[name] = sum(len(bucket) * (len(bucket) - 1) // 2 for bucket in buckets.values())
        for bucket in buckets.values():
            for index in bucket[1:]:
                union(bucket[0], index)

    reaction_grams = [
        (
            _character_ngrams(_stereo_insensitive(reaction[0])),
            _character_ngrams(_stereo_insensitive(reaction[2])),
        )
        for reaction in reactions
    ]
    lexical_edges = 0
    for left, right in combinations(range(len(tasks)), 2):
        reactant_similarity = _jaccard(reaction_grams[left][0], reaction_grams[right][0])
        product_similarity = _jaccard(reaction_grams[left][1], reaction_grams[right][1])
        minimum = min(reactant_similarity, product_similarity)
        maximum = max(reactant_similarity, product_similarity)
        if (
            minimum >= LEXICAL_BALANCED_THRESHOLD
            or (
                maximum >= LEXICAL_STRONG_THRESHOLD
                and minimum >= LEXICAL_SECONDARY_THRESHOLD
            )
        ):
            lexical_edges += 1
            union(left, right)
    edge_counts["high_threshold_lexical_reaction"] = lexical_edges

    grouped: dict[int, list[str]] = defaultdict(list)
    for index, task in enumerate(tasks):
        grouped[find(index)].append(task.uid)
    groups = tuple(sorted(tuple(sorted(values)) for values in grouped.values()))
    return groups, edge_counts


def _reaction_signature(question: str) -> tuple[str, str, str]:
    candidates = [token for token in question.split() if token.count(">") == 2]
    if len(candidates) != 1:
        raise TemperatureSplitV1Error("REACTION_TOKEN_INVALID")
    # Source questions append sentence punctuation directly to the reaction
    # token.  A valid reaction side cannot end in an empty dot component, so
    # stripping the complete terminal punctuation run is unambiguous.
    token = unicodedata.normalize("NFKC", candidates[0]).strip(".?")
    sides = token.split(">")
    if len(sides) != 3 or not sides[0] or not sides[2]:
        raise TemperatureSplitV1Error("REACTION_TOKEN_INVALID")
    canonical: list[str] = []
    for side_index, side in enumerate(sides):
        if not side:
            if side_index != 1:
                raise TemperatureSplitV1Error("REACTION_TOKEN_INVALID")
            canonical.append("")
            continue
        components = side.split(".")
        if any(not component for component in components):
            raise TemperatureSplitV1Error("REACTION_TOKEN_INVALID")
        canonical.append(".".join(sorted(components)))
    return canonical[0], canonical[1], canonical[2]


def _stereo_insensitive(value: str) -> str:
    return value.replace("@", "").replace("/", "").replace("\\", "")


def _character_ngrams(value: str, *, width: int = 3) -> frozenset[str]:
    if len(value) < width:
        return frozenset({value})
    return frozenset(value[index : index + width] for index in range(len(value) - width + 1))


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def _allocate_whole_groups(
    groups: tuple[tuple[str, ...], ...],
) -> tuple[
    tuple[tuple[str, ...], ...],
    tuple[tuple[str, ...], ...],
    tuple[tuple[str, ...], ...],
]:
    ranked = tuple(sorted(groups, key=_group_rank))
    reserve_candidates: list[tuple[int, ...]] = [
        (index,) for index, group in enumerate(ranked) if len(group) == RESERVE_COUNT
    ]
    singleton_indices = [index for index, group in enumerate(ranked) if len(group) == 1]
    reserve_candidates.extend(combinations(singleton_indices, RESERVE_COUNT))
    reserve_candidates.sort()

    for selected_indices in reserve_candidates:
        selected_set = set(selected_indices)
        reserve = tuple(ranked[index] for index in selected_indices)
        remaining = tuple(
            group for index, group in enumerate(ranked) if index not in selected_set
        )
        train = _first_ranked_subset(remaining, target=TRAIN_COUNT)
        if train is None:
            continue
        train_set = set(train)
        test = tuple(group for group in remaining if group not in train_set)
        if sum(map(len, test)) != TEST_COUNT:
            continue
        return train, test, reserve
    raise TemperatureSplitV1Error("GROUP_AWARE_SPLIT_CAPACITY_UNAVAILABLE")


def _first_ranked_subset(
    groups: tuple[tuple[str, ...], ...],
    *,
    target: int,
) -> tuple[tuple[str, ...], ...] | None:
    suffix: list[list[bool]] = [[False] * (target + 1) for _ in range(len(groups) + 1)]
    suffix[-1][0] = True
    for index in range(len(groups) - 1, -1, -1):
        size = len(groups[index])
        for total in range(target + 1):
            suffix[index][total] = suffix[index + 1][total] or (
                total >= size and suffix[index + 1][total - size]
            )
    if not suffix[0][target]:
        return None
    selected: list[tuple[str, ...]] = []
    remaining = target
    for index, group in enumerate(groups):
        size = len(group)
        if remaining >= size and suffix[index + 1][remaining - size]:
            selected.append(group)
            remaining -= size
    if remaining:
        raise AssertionError("subset reconstruction disagrees with feasibility table")
    return tuple(selected)


def _group_sha256(group: tuple[str, ...]) -> str:
    return sha256_bytes(("\n".join(group) + "\n").encode())


def _group_rank(group: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    rank = hashlib.sha256(
        f"{SPLIT_NAMESPACE}\x1fgroup-rank\x1f{_group_sha256(group)}".encode()
    ).hexdigest()
    return rank, group


def _ordered_partition_uids(
    groups: tuple[tuple[str, ...], ...],
    *,
    partition: str,
) -> tuple[str, ...]:
    values = [uid for group in groups for uid in group]
    return tuple(
        sorted(
            values,
            key=lambda uid: (
                hashlib.sha256(
                    f"{SPLIT_NAMESPACE}\x1f{partition}-order\x1f{uid}".encode()
                ).hexdigest(),
                uid,
            ),
        )
    )


def _uid_set_sha256(values: tuple[str, ...]) -> str:
    return sha256_bytes(("\n".join(sorted(values)) + "\n").encode())


def _uid_order_sha256(values: tuple[str, ...]) -> str:
    return sha256_bytes(("\n".join(values) + "\n").encode())


def _prompt_signature(task: PrivateChemBench4KTask, *, ordered: bool) -> str:
    options = [normalize_benchmark_text(getattr(task, label)) for label in "ABCD"]
    if not ordered:
        options.sort()
    return sha256_bytes(
        canonical_json_bytes([normalize_benchmark_text(task.question), *options])
    )


__all__ = [
    "CATEGORY",
    "DEV_DEMONSTRATION_COUNT",
    "GROUPING_ALGORITHM_ID",
    "HISTORICAL_EXPOSURE_POLICY",
    "PROTOCOL_ID",
    "RESERVE_COUNT",
    "SOURCE_TEST_COUNT",
    "SPLIT_NAMESPACE",
    "TEST_COUNT",
    "TRAIN_COUNT",
    "FrozenTemperatureSplitV1",
    "PublicTemperatureSplitPlanV1",
    "RenderedTemperatureSplitArtifactsV1",
    "TemperatureSplitV1Error",
    "build_public_split_plan_v1",
    "build_temperature_split_v1",
    "render_temperature_split_artifacts_v1",
    "write_temperature_split_artifacts_v1",
]
