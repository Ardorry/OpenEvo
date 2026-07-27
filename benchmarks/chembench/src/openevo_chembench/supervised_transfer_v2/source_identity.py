"""Deterministic source closure for the v2 benchmark protocol."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

from openevo_chembench.supervised_transfer_v1.common import (
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_public_file,
)
from openevo_chembench.supervised_transfer_v2.config import PROTOCOL_ID

SOURCE_MANIFEST_RELATIVE = (
    "benchmarks/chembench/manifests/supervised_transfer_v2/source_manifest_v2.json"
)
_PRIVATE_NAMES = frozenset(
    {
        "train_private_manifest.jsonl",
        "test_private_manifest.jsonl",
    }
)
_SHARED_FILES = (
    "benchmarks/chembench/src/openevo_chembench/chembench4k_dataset.py",
    "benchmarks/chembench/src/openevo_chembench/chembench4k_evaluation.py",
    "benchmarks/chembench/src/openevo_chembench/chembench4k_models.py",
    "benchmarks/chembench/src/openevo_chembench/chembench4k_prompt.py",
    "benchmarks/chembench/src/openevo_chembench/core_evolution_v2.py",
    "benchmarks/chembench/src/openevo_chembench/frozen_runtime_v2.py",
    "benchmarks/chembench/src/openevo_chembench/models.py",
    "benchmarks/chembench/src/openevo_chembench/taskwise_config_v1.py",
    "benchmarks/chembench/src/openevo_chembench/taskwise_feedback_v1.py",
    "notes/chembench_supervised_transfer_v2_design.md",
)
_SCAN_ROOTS = (
    "benchmarks/chembench/configs/supervised_transfer_v2",
    "benchmarks/chembench/scripts/supervised_transfer_v2",
    "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2",
    "benchmarks/chembench/tests/supervised_transfer_v2",
    "benchmarks/chembench/manifests/supervised_transfer_v2",
)


def build_source_manifest_v2(repository_root: Path) -> dict[str, object]:
    repository = _repository(repository_root)
    relative_paths = set(_SHARED_FILES)
    for root_relative in _SCAN_ROOTS:
        root = repository / root_relative
        if not root.is_dir():
            raise RuntimeError("V2_SOURCE_ROOT_MISSING")
        for path in root.rglob("*"):
            if (
                not path.is_file()
                or "__pycache__" in path.parts
                or path.suffix in {".pyc", ".pyo"}
                or path.name in _PRIVATE_NAMES
            ):
                continue
            relative = path.relative_to(repository).as_posix()
            if relative != SOURCE_MANIFEST_RELATIVE:
                relative_paths.add(relative)
    entries = [_source_entry(repository, relative) for relative in sorted(relative_paths)]
    combined = sha256_bytes(
        (json.dumps(entries, sort_keys=True, separators=(",", ":")) + "\n").encode()
    )
    return {
        "schema_version": "ChemBenchSupervisedTransferSourceManifestV2",
        "protocol_id": PROTOCOL_ID,
        "source_file_count": len(entries),
        "source_files": entries,
        "combined_sha256": combined,
        "self_excluded": SOURCE_MANIFEST_RELATIVE,
        "private_manifests_excluded": sorted(_PRIVATE_NAMES),
        "src_openevo_included_by_framework_lock": True,
    }


def write_source_manifest_v2(repository_root: Path) -> dict[str, object]:
    repository = _repository(repository_root)
    payload = build_source_manifest_v2(repository)
    path = repository / SOURCE_MANIFEST_RELATIVE
    write_public_file(path, canonical_pretty_json_bytes(payload))
    return {
        "status": "PASS",
        "path": SOURCE_MANIFEST_RELATIVE,
        "source_file_count": payload["source_file_count"],
        "combined_sha256": payload["combined_sha256"],
        "manifest_sha256": sha256_bytes(path.read_bytes()),
    }


def verify_source_manifest_v2(repository_root: Path) -> dict[str, object]:
    repository = _repository(repository_root)
    path = repository / SOURCE_MANIFEST_RELATIVE
    try:
        encoded = path.read_bytes()
        loaded = json.loads(encoded)
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise RuntimeError("V2_SOURCE_MANIFEST_UNAVAILABLE") from exc
    expected = build_source_manifest_v2(repository)
    if loaded != expected or encoded != canonical_pretty_json_bytes(expected):
        raise RuntimeError("V2_SOURCE_MANIFEST_DRIFT")
    return {
        "status": "PASS",
        "path": SOURCE_MANIFEST_RELATIVE,
        "source_file_count": expected["source_file_count"],
        "combined_sha256": expected["combined_sha256"],
        "manifest_sha256": sha256_bytes(encoded),
    }


def _source_entry(repository: Path, relative: str) -> dict[str, object]:
    path = repository / relative
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError("V2_SOURCE_FILE_MISSING") from exc
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError("V2_SOURCE_FILE_UNSAFE")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": relative,
        "size_bytes": metadata.st_size,
        "sha256": digest.hexdigest(),
        "executable": bool(stat.S_IMODE(metadata.st_mode) & stat.S_IXUSR),
    }


def _repository(value: Path) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise TypeError("repository root must be absolute")
    repository = value.resolve(strict=True)
    if not (repository / ".git").exists():
        raise RuntimeError("V2_REPOSITORY_IDENTITY_INVALID")
    return repository


__all__ = [
    "SOURCE_MANIFEST_RELATIVE",
    "build_source_manifest_v2",
    "verify_source_manifest_v2",
    "write_source_manifest_v2",
]
