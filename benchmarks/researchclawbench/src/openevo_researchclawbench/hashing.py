"""Stable hashing and safe inventory helpers."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Iterable


class UnsafePathError(ValueError):
    """Raised when an inventory encounters an unsafe filesystem object."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def canonical_json_sha256(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def ensure_within(path: str | Path, root: str | Path) -> Path:
    resolved = Path(path).resolve(strict=False)
    root_resolved = Path(root).resolve(strict=True)
    if resolved != root_resolved and not resolved.is_relative_to(root_resolved):
        raise UnsafePathError(f"path escapes root: {path}")
    return resolved


def safe_relative_path(value: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise UnsafePathError(f"unsafe relative path: {value!r}")
    if "\x00" in value or any(ord(char) < 32 for char in value):
        raise UnsafePathError("control character in path")
    return path


def iter_regular_files(root: str | Path) -> Iterable[Path]:
    root_path = Path(root).resolve(strict=True)
    for dirpath, dirnames, filenames in os.walk(root_path, followlinks=False):
        current = Path(dirpath)
        for dirname in list(dirnames):
            child = current / dirname
            mode = child.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise UnsafePathError(f"unsafe directory entry: {child}")
        for filename in filenames:
            child = current / filename
            mode = child.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise UnsafePathError(f"unsafe file entry: {child}")
            ensure_within(child, root_path)
            yield child


def tree_entries(root: str | Path) -> list[dict[str, Any]]:
    root_path = Path(root).resolve(strict=True)
    entries: list[dict[str, Any]] = []
    for path in iter_regular_files(root_path):
        rel = path.relative_to(root_path).as_posix()
        entries.append(
            {"path": rel, "size_bytes": path.stat().st_size, "sha256": sha256_file(path)}
        )
    return sorted(entries, key=lambda item: item["path"])


def tree_sha256(root: str | Path) -> str:
    return canonical_json_sha256(tree_entries(root))


def write_sha256_manifest(root: str | Path, destination: str | Path) -> None:
    root_path = Path(root).resolve(strict=True)
    destination_path = Path(destination).resolve(strict=False)
    lines = ["sha256\tsize_bytes\trelative_path"]
    for entry in tree_entries(root_path):
        if (root_path / entry["path"]).resolve(strict=False) == destination_path:
            continue
        lines.append(f"{entry['sha256']}\t{entry['size_bytes']}\t{entry['path']}")
    destination_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
