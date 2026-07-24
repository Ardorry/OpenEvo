"""Deterministic canary, pilot, and full manifests for frozen ChemBench4K v2."""

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


PILOT_SIZE = 500
PILOT_SEED = "openevo-chembench4k-frozen-generalization-v2-pilot500"
CANARY_SEED = "openevo-chembench4k-frozen-generalization-v2-canary18"
MANIFEST_SCHEMA = "chembench4k_task_manifest_v2"


class SamplingManifestError(RuntimeError):
    """Raised when a task manifest cannot be created safely."""


@dataclass(frozen=True, slots=True)
class GeneratedTaskManifests:
    public_path: Path
    private_path: Path
    summary_path: Path
    public_sha256: str
    private_sha256: str
    summary_sha256: str
    ordered_uid_hash: str
    item_count: int


def largest_remainder_allocation(
    category_population: dict[str, int],
    *,
    sample_size: int,
) -> dict[str, int]:
    """Allocate an exact stratified sample using Hamilton's method."""

    if set(category_population) != set(CHEMBENCH4K_CATEGORIES):
        raise ValueError("population must name exactly the nine frozen categories")
    if any(
        isinstance(count, bool) or not isinstance(count, int) or count < 1
        for count in category_population.values()
    ):
        raise ValueError("every category population must be a positive integer")
    if (
        isinstance(sample_size, bool)
        or not isinstance(sample_size, int)
        or sample_size < len(CHEMBENCH4K_CATEGORIES)
        or sample_size > sum(category_population.values())
    ):
        raise ValueError("sample_size cannot satisfy the stratification constraints")

    total = sum(category_population.values())
    exact = {
        category: sample_size * category_population[category] / total
        for category in CHEMBENCH4K_CATEGORIES
    }
    allocation = {
        category: max(1, int(exact[category] // 1)) for category in CHEMBENCH4K_CATEGORIES
    }
    current = sum(allocation.values())
    if current > sample_size:
        for category in sorted(
            CHEMBENCH4K_CATEGORIES,
            key=lambda name: (exact[name] - int(exact[name] // 1), name),
        ):
            while allocation[category] > 1 and current > sample_size:
                allocation[category] -= 1
                current -= 1
    elif current < sample_size:
        ranked = sorted(
            CHEMBENCH4K_CATEGORIES,
            key=lambda name: (
                -(exact[name] - int(exact[name] // 1)),
                name,
            ),
        )
        index = 0
        while current < sample_size:
            category = ranked[index % len(ranked)]
            if allocation[category] < category_population[category]:
                allocation[category] += 1
                current += 1
            index += 1
    if sum(allocation.values()) != sample_size:
        raise SamplingManifestError("largest-remainder allocation did not close")
    if any(allocation[category] < 1 for category in CHEMBENCH4K_CATEGORIES):
        raise SamplingManifestError("category minimum was not preserved")
    return allocation


def _select_tasks(
    loader: ChemBench4KDatasetLoader,
    *,
    scope: str,
) -> tuple[tuple[PrivateChemBench4KTask, ...], dict[str, int], str]:
    by_category = {
        category: loader.load_category(category, split="test")
        for category in CHEMBENCH4K_CATEGORIES
    }
    population = {category: len(tasks) for category, tasks in by_category.items()}
    if scope == "pilot500":
        allocation = largest_remainder_allocation(population, sample_size=PILOT_SIZE)
        seed = PILOT_SEED
    elif scope == "canary18":
        allocation = {category: 2 for category in CHEMBENCH4K_CATEGORIES}
        seed = CANARY_SEED
    elif scope == "full":
        allocation = dict(population)
        seed = "openevo-chembench4k-frozen-generalization-v2-full"
    else:
        raise ValueError("scope must be canary18, pilot500, or full")

    selected: list[PrivateChemBench4KTask] = []
    for category in CHEMBENCH4K_CATEGORIES:
        tasks = list(sorted(by_category[category], key=lambda task: task.uid))
        if scope != "full":
            category_seed = hashlib.sha256(f"{seed}\x1f{category}".encode("utf-8")).digest()
            random.Random(category_seed).shuffle(tasks)
        selected.extend(tasks[: allocation[category]])
    if len({task.uid for task in selected}) != len(selected):
        raise SamplingManifestError("selected task UIDs are not unique")
    return tuple(selected), allocation, seed


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


def _write_private(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp")
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


def generate_task_manifests(
    loader: ChemBench4KDatasetLoader,
    *,
    scope: str,
    public_path: Path,
    private_path: Path,
    summary_path: Path,
) -> GeneratedTaskManifests:
    """Generate byte-stable public/private task manifests and a public summary."""

    if type(loader) is not ChemBench4KDatasetLoader:
        raise TypeError("loader must be an exact ChemBench4KDatasetLoader")
    if public_path == private_path or public_path == summary_path or private_path == summary_path:
        raise ValueError("task manifest paths must be distinct")
    tasks, allocation, seed = _select_tasks(loader, scope=scope)
    public_bytes = b"".join(
        _json_line(
            {
                "ordinal": ordinal,
                **task.to_public().to_public_dict(),
            }
        )
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
    ordered_uid_hash = hashlib.sha256(
        "\n".join(task.uid for task in tasks).encode("ascii")
    ).hexdigest()
    public_sha256 = hashlib.sha256(public_bytes).hexdigest()
    private_sha256 = hashlib.sha256(private_bytes).hexdigest()
    population = {
        category: loader.manifest.category_counts[category]["test"]
        for category in CHEMBENCH4K_CATEGORIES
    }
    summary = {
        "schema_version": MANIFEST_SCHEMA,
        "protocol_id": "chembench4k_frozen_generalization_v2",
        "scope": scope,
        "seed": seed,
        "dataset_repository": loader.manifest.repository,
        "dataset_revision": loader.manifest.revision,
        "dataset_hash": loader.manifest.combined_sha256,
        "exact_test_count": loader.manifest.test_count,
        "category_population": population,
        "category_sample_count": allocation,
        "item_count": len(tasks),
        "ordered_uid_hash": ordered_uid_hash,
        "public_manifest_sha256": public_sha256,
        "private_manifest_sha256": private_sha256,
    }
    summary_bytes = _json_line(summary)

    public_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_bytes(public_bytes)
    _write_private(private_path, private_bytes)
    summary_path.write_bytes(summary_bytes)
    return GeneratedTaskManifests(
        public_path=public_path,
        private_path=private_path,
        summary_path=summary_path,
        public_sha256=public_sha256,
        private_sha256=private_sha256,
        summary_sha256=hashlib.sha256(summary_bytes).hexdigest(),
        ordered_uid_hash=ordered_uid_hash,
        item_count=len(tasks),
    )


def verify_task_manifests(
    loader: ChemBench4KDatasetLoader,
    *,
    scope: str,
    public_path: Path,
    private_path: Path,
    summary_path: Path,
) -> GeneratedTaskManifests:
    """Recompute manifests in memory and require exact bytes without mutation."""

    tasks, allocation, seed = _select_tasks(loader, scope=scope)
    expected_public = b"".join(
        _json_line({"ordinal": ordinal, **task.to_public().to_public_dict()})
        for ordinal, task in enumerate(tasks)
    )
    expected_private = b"".join(
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
    ordered_uid_hash = hashlib.sha256(
        "\n".join(task.uid for task in tasks).encode("ascii")
    ).hexdigest()
    expected_summary = _json_line(
        {
            "schema_version": MANIFEST_SCHEMA,
            "protocol_id": "chembench4k_frozen_generalization_v2",
            "scope": scope,
            "seed": seed,
            "dataset_repository": loader.manifest.repository,
            "dataset_revision": loader.manifest.revision,
            "dataset_hash": loader.manifest.combined_sha256,
            "exact_test_count": loader.manifest.test_count,
            "category_population": {
                category: loader.manifest.category_counts[category]["test"]
                for category in CHEMBENCH4K_CATEGORIES
            },
            "category_sample_count": allocation,
            "item_count": len(tasks),
            "ordered_uid_hash": ordered_uid_hash,
            "public_manifest_sha256": hashlib.sha256(expected_public).hexdigest(),
            "private_manifest_sha256": hashlib.sha256(expected_private).hexdigest(),
        }
    )
    try:
        actual_public = public_path.read_bytes()
        actual_private = private_path.read_bytes()
        actual_summary = summary_path.read_bytes()
    except OSError as exc:
        raise SamplingManifestError("task manifest set is unavailable") from exc
    if (
        actual_public != expected_public
        or actual_private != expected_private
        or actual_summary != expected_summary
    ):
        raise SamplingManifestError("task manifest set does not match frozen selection")
    return GeneratedTaskManifests(
        public_path=public_path,
        private_path=private_path,
        summary_path=summary_path,
        public_sha256=hashlib.sha256(actual_public).hexdigest(),
        private_sha256=hashlib.sha256(actual_private).hexdigest(),
        summary_sha256=hashlib.sha256(actual_summary).hexdigest(),
        ordered_uid_hash=ordered_uid_hash,
        item_count=len(tasks),
    )


__all__ = [
    "CANARY_SEED",
    "GeneratedTaskManifests",
    "MANIFEST_SCHEMA",
    "PILOT_SEED",
    "PILOT_SIZE",
    "SamplingManifestError",
    "generate_task_manifests",
    "largest_remainder_allocation",
    "verify_task_manifests",
]
