"""Canonical encoding, hashing, and private-file helpers for the protocol."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
GIT_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def canonical_pretty_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: object) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def require_sha256(value: object, field_name: str) -> str:
    if type(value) is not str or SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256")
    return value


def require_git_commit(value: object, field_name: str) -> str:
    if type(value) is not str or GIT_COMMIT_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase Git commit")
    return value


def write_public_file(path: Path, payload: bytes) -> None:
    """Atomically replace one non-secret generated file."""

    if not isinstance(path, Path) or not isinstance(payload, bytes):
        raise TypeError("public output requires pathlib.Path and bytes")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o644,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o644)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_private_file(path: Path, payload: bytes, *, replace: bool = True) -> None:
    """Write one owner-only generated file without broadening permissions."""

    if not isinstance(path, Path) or not isinstance(payload, bytes):
        raise TypeError("private output requires pathlib.Path and bytes")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if not replace and path.exists():
            raise FileExistsError(path)
        os.replace(temporary, path)
        path.chmod(0o600)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def closed_mapping(
    value: object,
    *,
    keys: frozenset[str],
    field_name: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"{field_name} must contain exactly {sorted(keys)}")
    if any(type(key) is not str for key in value):
        raise TypeError(f"{field_name} keys must be strings")
    return dict(value)


__all__ = [
    "GIT_COMMIT_RE",
    "SHA256_RE",
    "canonical_json_bytes",
    "canonical_pretty_json_bytes",
    "closed_mapping",
    "require_git_commit",
    "require_sha256",
    "sha256_bytes",
    "sha256_json",
    "write_private_file",
    "write_public_file",
]
