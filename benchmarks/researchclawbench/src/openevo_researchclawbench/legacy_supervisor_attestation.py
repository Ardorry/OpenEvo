"""Closed, content-addressed evidence for a historical Supervisor caller.

This module only inventories immutable evidence.  It does not mint Core
authority, create a recovery, update a protocol, or execute an evolution job.
The resulting mapping is suitable for a future typed Core contract once that
contract is finalized.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .candidate_reconciliation import _tree_sha256
from .training_state_store import canonical_sha256

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_EVIDENCE_NAMES = frozenset(
    {
        "core_failure_authority",
        "terminal_classification",
        "release_bundle_manifest",
        "framework_lock",
    }
)
_TREE_ALGORITHM = "openevo-reconciliation-source-tree-v1"
_HISTORICAL_BASELINE_METHOD = "unknown"
_PROVENANCE_TIER = "legacy_supervisor_attested"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _attest_experiment_file(*, experiment_root: Path, path: Path) -> dict[str, Any]:
    root = Path(os.path.abspath(experiment_root))
    value = Path(os.path.abspath(path))
    if (
        root.is_symlink()
        or not root.is_dir()
        or not value.is_relative_to(root)
        or value.is_symlink()
        or not value.is_file()
    ):
        raise ValueError("legacy evidence file is outside the experiment root")
    metadata = os.stat(value, follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError("legacy evidence must be a singly-linked regular file")
    return {
        "experiment_relative_path": value.relative_to(root).as_posix(),
        "size_bytes": metadata.st_size,
        "sha256": _file_sha256(value),
    }


def _read_immutable_supervisor_state(
    *,
    database: Path,
    namespace: str,
    allow_stable_empty_wal_sidecars: bool = False,
) -> dict[str, Any]:
    if database.is_symlink() or not database.is_file():
        raise ValueError("legacy Supervisor database is unsafe")
    value = database.resolve(strict=True)
    sidecars = (Path(str(value) + "-wal"), Path(str(value) + "-shm"))
    observed_sidecars: dict[str, dict[str, Any]] = {}
    if any(item.exists() for item in sidecars):
        if not allow_stable_empty_wal_sidecars or not all(
            item.exists() for item in sidecars
        ):
            raise ValueError("legacy Supervisor database has an active SQLite sidecar")
        for label, item in zip(("wal", "shm"), sidecars, strict=True):
            if item.is_symlink() or not item.is_file():
                raise ValueError("legacy Supervisor SQLite sidecar is unsafe")
            metadata = os.stat(item, follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError("legacy Supervisor SQLite sidecar is unsafe")
            observed_sidecars[label] = {
                "size_bytes": metadata.st_size,
                "sha256": _file_sha256(item),
            }
        if observed_sidecars["wal"]["size_bytes"] != 0:
            raise ValueError("legacy Supervisor SQLite WAL is not empty")
    before = _file_sha256(value)
    connection = sqlite3.connect(
        f"{value.as_uri()}?mode=ro&immutable=1",
        uri=True,
        timeout=0.0,
    )
    try:
        rows = connection.execute(
            "SELECT experiment_id, stage, state_sha256, revision "
            "FROM experiment_state ORDER BY experiment_id"
        ).fetchall()
    finally:
        connection.close()
    after = _file_sha256(value)
    sidecars_after = {
        label: {
            "size_bytes": item.stat().st_size,
            "sha256": _file_sha256(item),
        }
        for label, item in zip(("wal", "shm"), sidecars, strict=True)
        if item.exists()
    }
    if before != after or sidecars_after != observed_sidecars:
        raise ValueError("legacy Supervisor database changed during immutable readback")
    if len(rows) != 1 or rows[0][0] != namespace:
        raise ValueError("legacy Supervisor namespace is ambiguous")
    _experiment_id, stage, state_sha256, revision = rows[0]
    if (
        not isinstance(stage, str)
        or _SHA256.fullmatch(str(state_sha256)) is None
        or not isinstance(revision, int)
        or revision < 1
    ):
        raise ValueError("legacy Supervisor state row is malformed")
    return {
        "stage": stage,
        "state_sha256": state_sha256,
        "revision": revision,
        "database_sha256_before": before,
        "database_sha256_after": after,
        "stable_empty_wal_sidecars": observed_sidecars,
    }


def _closed_supervisor_file_inventory(
    supervisor_root: Path,
) -> dict[str, dict[str, Any]]:
    """Inventory every non-SQLite namespace file as a closed, safe set.

    The canonical tree hash deliberately excludes SQLite because its logical
    state and physical database hash are fenced independently.  Returning the
    individual file hashes makes that algorithm choice auditable without
    pretending an older, undocumented aggregate used the same algorithm.
    """

    root = Path(os.path.abspath(supervisor_root))
    if root.is_symlink() or not root.is_dir():
        raise ValueError("legacy Supervisor root is unsafe")
    files: dict[str, dict[str, Any]] = {}
    for item in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
        if item.is_symlink():
            raise ValueError("legacy Supervisor tree contains a symlink")
        if not item.is_file() or item.name == "training-supervisor.sqlite3" or (
            item.name.endswith((".sqlite3-wal", ".sqlite3-shm"))
        ):
            continue
        metadata = os.stat(item, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("legacy Supervisor tree contains an unsafe file")
        relative = item.relative_to(root).as_posix()
        files[relative] = {
            "relative_path": relative,
            "size_bytes": metadata.st_size,
            "sha256": _file_sha256(item),
        }
    if not files:
        raise ValueError("legacy Supervisor non-SQLite tree is empty")
    return files


def build_legacy_supervisor_attested_inventory(
    *,
    experiment_root: Path,
    supervisor_namespace: str,
    expected_supervisor_stage: str,
    expected_supervisor_state_sha256: str,
    expected_supervisor_database_sha256: str,
    expected_supervisor_tree_sha256: str,
    expected_supervisor_files_sha256: Mapping[str, str],
    historical_baseline_tree_sha256: str,
    evidence_files: Mapping[str, Path],
    expected_evidence_sha256: Mapping[str, str],
    expected_core_cross_check: Mapping[str, str],
    allow_stable_empty_wal_sidecars: bool = False,
) -> dict[str, Any]:
    """Build a non-secret inventory without creating a protocol or authority."""

    if _IDENTIFIER.fullmatch(supervisor_namespace) is None:
        raise ValueError("legacy Supervisor namespace is invalid")
    for value in (
        expected_supervisor_state_sha256,
        expected_supervisor_database_sha256,
        expected_supervisor_tree_sha256,
        historical_baseline_tree_sha256,
    ):
        if _SHA256.fullmatch(value) is None:
            raise ValueError("legacy Supervisor expected hash is invalid")
    if set(evidence_files) != _EVIDENCE_NAMES or set(expected_evidence_sha256) != (
        _EVIDENCE_NAMES
    ):
        raise ValueError("legacy evidence inventory is not the closed file set")
    required_core_fields = {
        "successor_transition_id",
        "dataset_id",
        "dataset_revision",
        "attachment_id",
        "attachment_sha256",
        "resolved_dataset_artifact_id",
        "resolved_view_sha256",
    }
    failure_fields = set(expected_core_cross_check) & {
        "failed_job_id",
        "failed_pre_job_operation_id",
    }
    if (
        len(failure_fields) != 1
        or set(expected_core_cross_check) != required_core_fields | failure_fields
    ):
        raise ValueError("expected Core cross-check inventory is not closed")
    for name, value in expected_core_cross_check.items():
        pattern = _SHA256 if name.endswith("_sha256") else _IDENTIFIER
        if not isinstance(value, str) or pattern.fullmatch(value) is None:
            raise ValueError(f"expected Core cross-check field is invalid: {name}")
    if not expected_supervisor_files_sha256:
        raise ValueError("legacy Supervisor expected file inventory is empty")
    for relative, value in expected_supervisor_files_sha256.items():
        if not isinstance(relative, str):
            raise TypeError("legacy Supervisor expected file path is not text")
        relative_path = Path(relative)
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or relative_path.as_posix() != relative
            or not isinstance(value, str)
            or _SHA256.fullmatch(value) is None
        ):
            raise ValueError("legacy Supervisor expected file inventory is unsafe")

    root = Path(os.path.abspath(experiment_root))
    supervisor_root = root / "supervisor" / supervisor_namespace
    if (
        supervisor_root.is_symlink()
        or not supervisor_root.is_dir()
        or not supervisor_root.is_relative_to(root / "supervisor")
    ):
        raise ValueError("legacy Supervisor root is unsafe")
    state = _read_immutable_supervisor_state(
        database=supervisor_root / "training-supervisor.sqlite3",
        namespace=supervisor_namespace,
        allow_stable_empty_wal_sidecars=allow_stable_empty_wal_sidecars,
    )
    tree_sha256 = _tree_sha256(supervisor_root)
    supervisor_files = _closed_supervisor_file_inventory(supervisor_root)
    observed_file_hashes = {
        relative: receipt["sha256"]
        for relative, receipt in supervisor_files.items()
    }
    if (
        state["stage"] != expected_supervisor_stage
        or state["state_sha256"] != expected_supervisor_state_sha256
        or state["database_sha256_before"]
        != expected_supervisor_database_sha256
        or tree_sha256 != expected_supervisor_tree_sha256
        or observed_file_hashes != dict(expected_supervisor_files_sha256)
    ):
        raise ValueError("legacy Supervisor authority differs from its attestation")

    files: dict[str, dict[str, Any]] = {}
    for name in sorted(_EVIDENCE_NAMES):
        receipt = _attest_experiment_file(
            experiment_root=root,
            path=evidence_files[name],
        )
        if receipt["sha256"] != expected_evidence_sha256[name]:
            raise ValueError(f"legacy evidence hash differs: {name}")
        files[name] = receipt

    payload: dict[str, Any] = {
        "schema_version": "openevo.researchclawbench.legacy_supervisor_attested.v1",
        "provenance": {
            "tier": _PROVENANCE_TIER,
            "core_native": False,
            "authority_minted": False,
            "requires_core_resolution": True,
        },
        "supervisor": {
            "namespace": supervisor_namespace,
            "stage": state["stage"],
            "state_sha256": state["state_sha256"],
            "revision": state["revision"],
            "database": {
                "experiment_relative_path": (
                    supervisor_root / "training-supervisor.sqlite3"
                ).relative_to(root).as_posix(),
                "size_bytes": (
                    supervisor_root / "training-supervisor.sqlite3"
                ).stat().st_size,
                "sha256_before": state["database_sha256_before"],
                "sha256_after": state["database_sha256_after"],
                **(
                    {
                        "stable_empty_wal_sidecars": state[
                            "stable_empty_wal_sidecars"
                        ]
                    }
                    if state["stable_empty_wal_sidecars"]
                    else {}
                ),
            },
            "canonical_tree": {
                "algorithm": _TREE_ALGORITHM,
                "sqlite_database_fenced_separately": True,
                "sha256": tree_sha256,
                "file_count": len(supervisor_files),
                "files": [supervisor_files[name] for name in sorted(supervisor_files)],
            },
            "historical_baseline": {
                "sha256": historical_baseline_tree_sha256,
                "method": _HISTORICAL_BASELINE_METHOD,
                "authority": "non_authoritative",
                "preserved_unchanged": True,
                "equivalent_to_canonical_tree": False,
            },
            "hash_method_reconciliation": {
                "status": "METHOD_DRIFT_RECONCILED_APPEND_ONLY",
                "historical_value_mutated": False,
                "canonical_algorithm": _TREE_ALGORITHM,
                "canonical_sha256": tree_sha256,
            },
        },
        "evidence_files": files,
        "expected_core_cross_check": {
            "provenance_tier": "caller_asserted_expected_values",
            "core_native": False,
            "values": dict(sorted(expected_core_cross_check.items())),
        },
    }
    payload["content_sha256"] = canonical_sha256(payload)
    return payload
