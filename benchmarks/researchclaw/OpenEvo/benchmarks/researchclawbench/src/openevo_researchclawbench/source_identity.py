"""Canonical source-tree identities used by formal training gates."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


_IGNORED_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
_IGNORED_SUFFIXES = {".pyc", ".pyo"}


def source_tree_sha256(root: str | Path) -> str:
    """Hash regular source files with path and bytes in a stable closed order."""

    base = Path(os.path.abspath(root))
    if base.is_symlink() or not base.is_dir():
        raise ValueError("source identity root is unsafe")
    digest = hashlib.sha256(b"openevo-source-tree-v1\0")
    count = 0
    for path in sorted(base.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(base)
        if any(part in _IGNORED_PARTS for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError("source identity tree contains a symlink")
        if not path.is_file() or path.suffix in _IGNORED_SUFFIXES:
            continue
        payload = path.read_bytes()
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
        count += 1
    if count == 0:
        raise ValueError("source identity tree is empty")
    return digest.hexdigest()


def active_source_identities(project_root: str | Path) -> dict[str, str]:
    project = Path(os.path.abspath(project_root))
    return {
        "openevo_core_source_tree_sha256": source_tree_sha256(
            project / "OpenEvo" / "src" / "openevo"
        ),
        "adapter_tree_sha256": source_tree_sha256(
            project / "OpenEvo" / "benchmarks" / "researchclawbench"
        ),
    }


__all__ = ["active_source_identities", "source_tree_sha256"]
