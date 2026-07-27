"""Deterministic provenance for benchmark-local reuse of the read-only source repo."""

from __future__ import annotations

import subprocess
from pathlib import Path

from openevo_chembench.supervised_transfer_v1.common import (
    canonical_pretty_json_bytes,
    sha256_bytes,
)

SOURCE_IMPORT_MANIFEST = "source_import_manifest_supervised_v1.json"
_IMPORTED_FILES = (
    (
        "benchmarks/chembench/src/openevo_chembench/artifact_validator_v2.py",
        "unchanged reuse of the token-aligned leakage validator",
    ),
    (
        "benchmarks/chembench/src/openevo_chembench/core_evolution_v2.py",
        "unchanged reuse of the offline framework bundle builder",
    ),
    (
        "benchmarks/chembench/src/openevo_chembench/frozen_runtime_v2.py",
        "unchanged reuse of the resolved text-memory runtime contract",
    ),
    (
        "benchmarks/chembench/src/openevo_chembench/local_codex_executor.py",
        "extended with exact three-target supervised context injection plus digest-bound real Codex success receipts",
    ),
    (
        "benchmarks/chembench/src/openevo_chembench/reflector_execution_boundary_v2.py",
        "extended for bounded Train-only supervised memory, skill, and agent-system output",
    ),
    (
        "benchmarks/chembench/src/openevo_chembench/taskwise_core_evolution_v1.py",
        "extended with one supervised reflector and three verified Core target lifecycles",
    ),
    (
        "benchmarks/chembench/src/openevo_chembench/taskwise_cli_v1.py",
        "adapted repository-root binding for the independent compare2 clone",
    ),
    (
        "benchmarks/chembench/src/openevo_chembench/taskwise_online_runner_v1.py",
        "unchanged reuse of immutable task-session request binding",
    ),
    (
        "benchmarks/chembench/src/openevo_chembench/taskwise_trajectory_v1.py",
        "unchanged reuse of transcript trajectory construction",
    ),
    (
        "benchmarks/chembench/src/openevo_chembench/taskwise_round0_smoke_v1.py",
        "adapted repository-root binding for the independent compare2 clone",
    ),
    (
        "benchmarks/chembench/src/openevo_chembench/taskwise_update1_smoke_v1.py",
        "adapted repository-root binding for the independent compare2 clone",
    ),
    (
        "benchmarks/chembench/src/openevo_chembench/v2_cli.py",
        "adapted repository-root binding for the independent compare2 clone",
    ),
)


def build_source_import_manifest_v1(
    *,
    repository_root: Path,
    old_repository: Path,
) -> dict[str, object]:
    repository = repository_root.resolve(strict=True)
    old = old_repository.resolve(strict=True)
    old_commit = _git(old, "rev-parse", "HEAD")
    records = []
    for relative, summary in _IMPORTED_FILES:
        destination = repository / relative
        if not destination.is_file():
            raise RuntimeError("source import path is unavailable")
        source_bytes = _git_bytes(old, "show", f"{old_commit}:{relative}")
        records.append(
            {
                "source_repository": str(old),
                "source_commit": old_commit,
                "source_file": relative,
                "destination_file": relative,
                "source_sha256": sha256_bytes(source_bytes),
                "destination_sha256": sha256_bytes(destination.read_bytes()),
                "modification_summary": summary,
            }
        )
    return {
        "schema_version": "chembench_supervised_transfer_source_import_manifest_v1",
        "source_repository": str(old),
        "source_commit": old_commit,
        "records": records,
    }


def render_source_import_manifest_v1(
    *,
    repository_root: Path,
    old_repository: Path,
) -> bytes:
    return canonical_pretty_json_bytes(
        build_source_import_manifest_v1(
            repository_root=repository_root,
            old_repository=old_repository,
        )
    )


def _git(repository: Path, *arguments: str) -> str:
    return _git_bytes(repository, *arguments).decode("utf-8").strip()


def _git_bytes(repository: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("source repository identity is unavailable")
    return completed.stdout


__all__ = [
    "SOURCE_IMPORT_MANIFEST",
    "build_source_import_manifest_v1",
    "render_source_import_manifest_v1",
]
