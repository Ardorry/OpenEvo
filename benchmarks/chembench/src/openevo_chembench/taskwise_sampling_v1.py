"""Deterministic stream manifests for the non-standard taskwise protocol."""

from __future__ import annotations

import hashlib
import json
import os
import random
from dataclasses import dataclass
from pathlib import Path

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_CATEGORIES,
    PrivateChemBench4KTask,
)
from openevo_chembench.sampling_v2 import largest_remainder_allocation


PROTOCOL_FAMILY = "chembench4k_taskwise_online_v1"
CONTROL_PROTOCOL_ID = "repeated_session_control_v1"
ONLINE_PROTOCOL_ID = "taskwise_online_evolution_v1"
MANIFEST_SCHEMA = "chembench4k_taskwise_stream_manifest_v1"
CANARY9_SCOPE = "canary9"
PILOT500_SCOPE = "pilot500"
CANARY9_SEED = "openevo-chembench4k-taskwise-online-v1-canary9"
PILOT500_SEED = "openevo-chembench4k-taskwise-online-v1-pilot500"
PILOT500_SIZE = 500
PILOT500_STREAM_COUNT = 10
PILOT500_TASKS_PER_STREAM = 50
PILOT500_STREAMS_SEED = "openevo-chembench4k-taskwise-online-v1-pilot500-ten-independent-streams"
PILOT500_STREAM_SCOPES = tuple(
    f"pilot500_stream_{index:02d}" for index in range(PILOT500_STREAM_COUNT)
)
LEGACY_SINGLE_CHAIN_CLASSIFICATION = (
    "LEGACY_SINGLE_STREAM_PILOT_MANIFEST",
    "NOT_PRIMARY_STATISTICAL_PROTOCOL",
    "LEGACY_SINGLE_CHAIN_ONLINE_PILOT500",
    "SUPERSEDED_BY_TEN_INDEPENDENT_STREAMS",
    "NOT_FOR_PRIMARY_STREAM_AWARE_INFERENCE",
)
PILOT500_STREAM_DESIGN = "ten_independent_memory_chains_x_50"
PILOT500_INTERLEAVING = "weighted_deficit_category_interleave_v1"
PROTOCOL_CLASSIFICATION = (
    "ONLINE_TASKWISE_EVOLUTION",
    "TEST_TIME_ADAPTATION",
    "NON_STANDARD_CHEMBENCH4K_PROTOCOL",
    "NOT_A_STANDARD_LEADERBOARD_SCORE",
)


class TaskwiseManifestError(RuntimeError):
    """Raised when taskwise stream identity cannot be established."""


@dataclass(frozen=True, slots=True)
class TaskwiseManifestSet:
    """One byte-stable public/private/summary stream manifest set."""

    public_path: Path
    private_path: Path
    summary_path: Path
    public_sha256: str
    private_sha256: str
    summary_sha256: str
    ordered_uid_sha256: str
    item_count: int


@dataclass(frozen=True, slots=True)
class TaskwisePilotStreamSuite:
    """The complete deterministic ten-stream pilot layout."""

    streams: tuple[tuple[PrivateChemBench4KTask, ...], ...]
    category_population: dict[str, int]
    category_sample_count: dict[str, int]
    ordered_uid_sha256: str
    uid_set_sha256: str

    def __post_init__(self) -> None:
        if len(self.streams) != PILOT500_STREAM_COUNT:
            raise TaskwiseManifestError("pilot stream suite must contain ten streams")
        if any(len(stream) != PILOT500_TASKS_PER_STREAM for stream in self.streams):
            raise TaskwiseManifestError("each pilot stream must contain exactly 50 tasks")
        uids = [task.uid for stream in self.streams for task in stream]
        if len(uids) != PILOT500_SIZE or len(set(uids)) != PILOT500_SIZE:
            raise TaskwiseManifestError("pilot streams must contain 500 unique tasks")


def _json_line(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _scope_identity(scope: str) -> tuple[int, str]:
    if scope == CANARY9_SCOPE:
        return len(CHEMBENCH4K_CATEGORIES), CANARY9_SEED
    if scope == PILOT500_SCOPE:
        return PILOT500_SIZE, PILOT500_SEED
    if scope in PILOT500_STREAM_SCOPES:
        return PILOT500_TASKS_PER_STREAM, PILOT500_STREAMS_SEED
    raise ValueError("scope must be canary9, pilot500, or a frozen pilot stream")


def pilot_stream_index(scope: str) -> int:
    """Return the closed zero-based pilot stream index."""

    if scope not in PILOT500_STREAM_SCOPES:
        raise ValueError("scope is not a frozen pilot500 stream")
    return int(scope.rsplit("_", maxsplit=1)[1])


def _weighted_category_schedule(category_counts: dict[str, int]) -> tuple[str, ...]:
    """Interleave categories with deterministic weighted-deficit scheduling."""

    if set(category_counts) != set(CHEMBENCH4K_CATEGORIES):
        raise TaskwiseManifestError("category schedule requires all nine categories")
    total = sum(category_counts.values())
    if total != PILOT500_SIZE:
        raise TaskwiseManifestError("category schedule must cover exactly 500 tasks")
    used = {category: 0 for category in CHEMBENCH4K_CATEGORIES}
    rank = {category: index for index, category in enumerate(CHEMBENCH4K_CATEGORIES)}
    schedule: list[str] = []
    for position in range(total):
        available = [
            category
            for category in CHEMBENCH4K_CATEGORIES
            if used[category] < category_counts[category]
        ]
        chosen = max(
            available,
            key=lambda category: (
                category_counts[category] * (position + 1) - used[category] * total,
                -rank[category],
            ),
        )
        schedule.append(chosen)
        used[chosen] += 1
    if any(left == right for left, right in zip(schedule, schedule[1:], strict=False)):
        raise TaskwiseManifestError("category interleaving produced adjacent equal categories")
    return tuple(schedule)


def select_taskwise_pilot_streams(
    loader: ChemBench4KDatasetLoader,
) -> TaskwisePilotStreamSuite:
    """Repartition the frozen pilot sample into ten independent 50-task streams."""

    if type(loader) is not ChemBench4KDatasetLoader:
        raise TypeError("loader must be an exact ChemBench4KDatasetLoader")
    legacy_tasks, allocation, _seed = select_taskwise_stream(
        loader,
        scope=PILOT500_SCOPE,
    )
    selected_uids = {task.uid for task in legacy_tasks}
    by_category: dict[str, list[PrivateChemBench4KTask]] = {
        category: [] for category in CHEMBENCH4K_CATEGORIES
    }
    for task in legacy_tasks:
        by_category[task.category].append(task)
    for category, tasks in by_category.items():
        tasks.sort(key=lambda task: task.uid)
        category_seed = hashlib.sha256(f"{PILOT500_STREAMS_SEED}\x1f{category}".encode()).digest()
        random.Random(category_seed).shuffle(tasks)

    schedule = _weighted_category_schedule(allocation)
    offsets = {category: 0 for category in CHEMBENCH4K_CATEGORIES}
    ordered: list[PrivateChemBench4KTask] = []
    for category in schedule:
        offset = offsets[category]
        ordered.append(by_category[category][offset])
        offsets[category] += 1
    streams = tuple(
        tuple(
            ordered[
                stream_index * PILOT500_TASKS_PER_STREAM : (stream_index + 1)
                * PILOT500_TASKS_PER_STREAM
            ]
        )
        for stream_index in range(PILOT500_STREAM_COUNT)
    )
    ordered_uids = [task.uid for task in ordered]
    if set(ordered_uids) != selected_uids:
        raise TaskwiseManifestError("stream repartition changed the frozen pilot task set")
    population = {
        category: loader.manifest.category_counts[category]["test"]
        for category in CHEMBENCH4K_CATEGORIES
    }
    return TaskwisePilotStreamSuite(
        streams=streams,
        category_population=population,
        category_sample_count=dict(allocation),
        ordered_uid_sha256=hashlib.sha256("\n".join(ordered_uids).encode("ascii")).hexdigest(),
        uid_set_sha256=hashlib.sha256("\n".join(sorted(ordered_uids)).encode("ascii")).hexdigest(),
    )


def select_taskwise_stream(
    loader: ChemBench4KDatasetLoader,
    *,
    scope: str,
) -> tuple[tuple[PrivateChemBench4KTask, ...], dict[str, int], str]:
    """Select and globally order one treatment-neutral test stream."""

    if type(loader) is not ChemBench4KDatasetLoader:
        raise TypeError("loader must be an exact ChemBench4KDatasetLoader")
    sample_size, seed = _scope_identity(scope)
    if scope in PILOT500_STREAM_SCOPES:
        suite = select_taskwise_pilot_streams(loader)
        stream = suite.streams[pilot_stream_index(scope)]
        allocation = {
            category: sum(task.category == category for task in stream)
            for category in CHEMBENCH4K_CATEGORIES
        }
        return stream, allocation, seed
    by_category = {
        category: loader.load_category(category, split="test")
        for category in CHEMBENCH4K_CATEGORIES
    }
    population = {category: len(tasks) for category, tasks in by_category.items()}
    if scope == CANARY9_SCOPE:
        allocation = {category: 1 for category in CHEMBENCH4K_CATEGORIES}
    else:
        allocation = largest_remainder_allocation(
            population,
            sample_size=sample_size,
        )

    selected: list[PrivateChemBench4KTask] = []
    for category in CHEMBENCH4K_CATEGORIES:
        tasks = list(sorted(by_category[category], key=lambda task: task.uid))
        category_seed = hashlib.sha256(f"{seed}\x1f{category}".encode()).digest()
        random.Random(category_seed).shuffle(tasks)
        selected.extend(tasks[: allocation[category]])

    # Online carry is order-sensitive.  A second, fixed global shuffle prevents
    # category-block ordering while keeping both arms byte-identical.
    order_seed = hashlib.sha256(f"{seed}\x1fstream-order".encode()).digest()
    random.Random(order_seed).shuffle(selected)
    if len(selected) != sample_size:
        raise TaskwiseManifestError("stream selection has the wrong item count")
    if len({task.uid for task in selected}) != len(selected):
        raise TaskwiseManifestError("stream task UIDs are not unique")
    return tuple(selected), allocation, seed


def _manifest_bytes(
    loader: ChemBench4KDatasetLoader,
    *,
    scope: str,
) -> tuple[bytes, bytes, bytes, str, int]:
    tasks, allocation, seed = select_taskwise_stream(loader, scope=scope)
    public_bytes = b"".join(
        _json_line({"ordinal": ordinal, **task.to_public().to_public_dict()})
        for ordinal, task in enumerate(tasks)
    )
    private_bytes = b"".join(
        _json_line(
            {
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
    ordered_uid_sha256 = hashlib.sha256(
        "\n".join(task.uid for task in tasks).encode("ascii")
    ).hexdigest()
    population = {
        category: loader.manifest.category_counts[category]["test"]
        for category in CHEMBENCH4K_CATEGORIES
    }
    summary: dict[str, object] = {
        "schema_version": MANIFEST_SCHEMA,
        "protocol_family": PROTOCOL_FAMILY,
        "arm_protocol_ids": [CONTROL_PROTOCOL_ID, ONLINE_PROTOCOL_ID],
        "classification": list(PROTOCOL_CLASSIFICATION),
        "scope": scope,
        "seed": seed,
        "dataset_repository": loader.manifest.repository,
        "dataset_revision": loader.manifest.revision,
        "dataset_hash": loader.manifest.combined_sha256,
        "exact_test_count": loader.manifest.test_count,
        "category_population": population,
        "category_sample_count": allocation,
        "item_count": len(tasks),
        "ordered_uid_sha256": ordered_uid_sha256,
        "public_manifest_sha256": hashlib.sha256(public_bytes).hexdigest(),
        "private_manifest_sha256": hashlib.sha256(private_bytes).hexdigest(),
    }
    if scope in PILOT500_STREAM_SCOPES:
        suite = select_taskwise_pilot_streams(loader)
        summary.update(
            {
                "stream_design": PILOT500_STREAM_DESIGN,
                "stream_id": scope,
                "stream_index": pilot_stream_index(scope),
                "stream_count": PILOT500_STREAM_COUNT,
                "tasks_per_stream": PILOT500_TASKS_PER_STREAM,
                "memory_chain_scope": "one_independent_chain_per_stream",
                "category_interleaving": PILOT500_INTERLEAVING,
                "global_ordered_uid_sha256": suite.ordered_uid_sha256,
                "global_uid_set_sha256": suite.uid_set_sha256,
                "legacy_single_chain": False,
            }
        )
    summary_bytes = _json_line(summary)
    return public_bytes, private_bytes, summary_bytes, ordered_uid_sha256, len(tasks)


def _write_private(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        path.chmod(0o600)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def generate_taskwise_manifests(
    loader: ChemBench4KDatasetLoader,
    *,
    scope: str,
    public_path: Path,
    private_path: Path,
    summary_path: Path,
) -> TaskwiseManifestSet:
    """Write one deterministic stream manifest set."""

    if len({public_path, private_path, summary_path}) != 3:
        raise ValueError("stream manifest paths must be distinct")
    public_bytes, private_bytes, summary_bytes, ordered_uid_sha256, count = _manifest_bytes(
        loader, scope=scope
    )
    public_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_bytes(public_bytes)
    _write_private(private_path, private_bytes)
    summary_path.write_bytes(summary_bytes)
    return TaskwiseManifestSet(
        public_path=public_path,
        private_path=private_path,
        summary_path=summary_path,
        public_sha256=hashlib.sha256(public_bytes).hexdigest(),
        private_sha256=hashlib.sha256(private_bytes).hexdigest(),
        summary_sha256=hashlib.sha256(summary_bytes).hexdigest(),
        ordered_uid_sha256=ordered_uid_sha256,
        item_count=count,
    )


def pilot500_stream_suite_summary_bytes(
    loader: ChemBench4KDatasetLoader,
    *,
    stream_manifests: tuple[TaskwiseManifestSet, ...],
) -> bytes:
    """Build the public summary binding all ten independent stream manifests."""

    if len(stream_manifests) != PILOT500_STREAM_COUNT:
        raise TaskwiseManifestError("suite summary requires exactly ten stream manifests")
    suite = select_taskwise_pilot_streams(loader)
    stream_payloads: list[dict[str, object]] = []
    for index, manifest in enumerate(stream_manifests):
        expected_scope = PILOT500_STREAM_SCOPES[index]
        if manifest.item_count != PILOT500_TASKS_PER_STREAM:
            raise TaskwiseManifestError("suite member has the wrong task count")
        stream_payloads.append(
            {
                "stream_id": expected_scope,
                "stream_index": index,
                "item_count": manifest.item_count,
                "public_manifest_sha256": manifest.public_sha256,
                "private_manifest_sha256": manifest.private_sha256,
                "summary_sha256": manifest.summary_sha256,
                "ordered_uid_sha256": manifest.ordered_uid_sha256,
            }
        )
    return _json_line(
        {
            "schema_version": "chembench4k_taskwise_pilot500_stream_suite_v1",
            "protocol_family": PROTOCOL_FAMILY,
            "arm_protocol_ids": [CONTROL_PROTOCOL_ID, ONLINE_PROTOCOL_ID],
            "classification": list(PROTOCOL_CLASSIFICATION),
            "stream_design": PILOT500_STREAM_DESIGN,
            "category_interleaving": PILOT500_INTERLEAVING,
            "memory_chain_scope": "one_independent_chain_per_stream",
            "seed": PILOT500_STREAMS_SEED,
            "sampling_seed": PILOT500_STREAMS_SEED,
            "stream_count": PILOT500_STREAM_COUNT,
            "tasks_per_stream": PILOT500_TASKS_PER_STREAM,
            "total_item_count": PILOT500_SIZE,
            "reset_memory_between_streams": True,
            "dataset_repository": loader.manifest.repository,
            "dataset_revision": loader.manifest.revision,
            "dataset_hash": loader.manifest.combined_sha256,
            "exact_test_count": loader.manifest.test_count,
            "category_population": suite.category_population,
            "category_sample_count": suite.category_sample_count,
            "global_ordered_uid_sha256": suite.ordered_uid_sha256,
            "combined_ordered_uid_sha256": suite.ordered_uid_sha256,
            "global_uid_set_sha256": suite.uid_set_sha256,
            "per_stream_ordered_uid_sha256": {
                item["stream_id"]: item["ordered_uid_sha256"] for item in stream_payloads
            },
            "per_stream_category_counts": {
                PILOT500_STREAM_SCOPES[index]: {
                    category: sum(task.category == category for task in suite.streams[index])
                    for category in CHEMBENCH4K_CATEGORIES
                }
                for index in range(PILOT500_STREAM_COUNT)
            },
            "streams": stream_payloads,
        }
    )


def legacy_single_chain_provenance_bytes(
    legacy_manifest: TaskwiseManifestSet,
) -> bytes:
    """Describe the preserved original single-chain pilot without mutating it."""

    if legacy_manifest.item_count != PILOT500_SIZE:
        raise TaskwiseManifestError("legacy provenance requires the 500-task single chain")
    return _json_line(
        {
            "schema_version": "chembench4k_taskwise_legacy_single_chain_provenance_v1",
            "protocol_family": PROTOCOL_FAMILY,
            "classification": list(LEGACY_SINGLE_CHAIN_CLASSIFICATION),
            "scope": PILOT500_SCOPE,
            "item_count": PILOT500_SIZE,
            "public_manifest_sha256": legacy_manifest.public_sha256,
            "private_manifest_sha256": legacy_manifest.private_sha256,
            "summary_sha256": legacy_manifest.summary_sha256,
            "ordered_uid_sha256": legacy_manifest.ordered_uid_sha256,
            "replacement_stream_design": PILOT500_STREAM_DESIGN,
            "replacement_stream_scopes": list(PILOT500_STREAM_SCOPES),
        }
    )


def verify_taskwise_manifests(
    loader: ChemBench4KDatasetLoader,
    *,
    scope: str,
    public_path: Path,
    private_path: Path,
    summary_path: Path,
) -> TaskwiseManifestSet:
    """Require exact manifest bytes without mutating any file."""

    expected_public, expected_private, expected_summary, ordered_uid_sha256, count = (
        _manifest_bytes(loader, scope=scope)
    )
    try:
        actual_public = public_path.read_bytes()
        actual_private = private_path.read_bytes()
        actual_summary = summary_path.read_bytes()
    except OSError as exc:
        raise TaskwiseManifestError("taskwise stream manifest set is unavailable") from exc
    if (
        actual_public != expected_public
        or actual_private != expected_private
        or actual_summary != expected_summary
    ):
        raise TaskwiseManifestError("taskwise stream manifest set is not frozen")
    return TaskwiseManifestSet(
        public_path=public_path,
        private_path=private_path,
        summary_path=summary_path,
        public_sha256=hashlib.sha256(actual_public).hexdigest(),
        private_sha256=hashlib.sha256(actual_private).hexdigest(),
        summary_sha256=hashlib.sha256(actual_summary).hexdigest(),
        ordered_uid_sha256=ordered_uid_sha256,
        item_count=count,
    )


__all__ = [
    "CANARY9_SCOPE",
    "CANARY9_SEED",
    "CONTROL_PROTOCOL_ID",
    "MANIFEST_SCHEMA",
    "ONLINE_PROTOCOL_ID",
    "PILOT500_SCOPE",
    "PILOT500_SEED",
    "PILOT500_SIZE",
    "PILOT500_INTERLEAVING",
    "PILOT500_STREAM_COUNT",
    "PILOT500_STREAM_DESIGN",
    "PILOT500_STREAM_SCOPES",
    "PILOT500_STREAMS_SEED",
    "PILOT500_TASKS_PER_STREAM",
    "PROTOCOL_CLASSIFICATION",
    "PROTOCOL_FAMILY",
    "LEGACY_SINGLE_CHAIN_CLASSIFICATION",
    "TaskwiseManifestError",
    "TaskwiseManifestSet",
    "TaskwisePilotStreamSuite",
    "generate_taskwise_manifests",
    "legacy_single_chain_provenance_bytes",
    "pilot500_stream_suite_summary_bytes",
    "pilot_stream_index",
    "select_taskwise_pilot_streams",
    "select_taskwise_stream",
    "verify_taskwise_manifests",
]
