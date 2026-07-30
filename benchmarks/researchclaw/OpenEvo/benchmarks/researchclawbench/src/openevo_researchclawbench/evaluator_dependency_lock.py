"""Content-addressed dependency identity for the evaluator-only Python runtime."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import sys
from collections import deque
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from .training_state_store import canonical_bytes, canonical_sha256

SCHEMA_VERSION = "openevo.researchclawbench.evaluator_dependency_lock.v1"
ROOT_DISTRIBUTIONS = {
    "openai": "2.50.0",
    "packaging": "26.2",
    "structai": "0.1.23",
}


class EvaluatorDependencyLockError(RuntimeError):
    """The evaluator dependency environment is incomplete or drifted."""


def _distribution_closure() -> tuple[tuple[str, tuple[str, ...]], ...]:
    queue: deque[tuple[str, tuple[str, ...]]] = deque(
        (name, ()) for name in sorted(ROOT_DISTRIBUTIONS)
    )
    requested_extras: dict[str, set[str]] = {}
    complete: set[tuple[str, tuple[str, ...]]] = set()
    while queue:
        raw_name, extras = queue.popleft()
        name = canonicalize_name(raw_name)
        requested = requested_extras.setdefault(name, set())
        before = set(requested)
        requested.update(extras)
        state = (name, tuple(sorted(requested)))
        if state in complete and before == requested:
            continue
        complete.add(state)
        try:
            package = distribution(name)
        except PackageNotFoundError as exc:
            raise EvaluatorDependencyLockError(
                f"evaluator dependency is unavailable: {name}"
            ) from exc
        contexts = ({"extra": ""}, *({"extra": item} for item in sorted(requested)))
        for raw_requirement in package.requires or ():
            requirement = Requirement(raw_requirement)
            if requirement.marker is not None and not any(
                requirement.marker.evaluate(context) for context in contexts
            ):
                continue
            queue.append(
                (
                    canonicalize_name(requirement.name),
                    tuple(sorted(requirement.extras)),
                )
            )
    return tuple(
        (name, tuple(sorted(extras)))
        for name, extras in sorted(requested_extras.items())
    )


def _distribution_identity(name: str, extras: tuple[str, ...]) -> dict[str, Any]:
    package = distribution(name)
    files = package.files
    if files is None:
        raise EvaluatorDependencyLockError(
            f"evaluator dependency has no installed file inventory: {name}"
        )
    prefix = Path(sys.prefix).resolve(strict=True)
    record_entries = [item for item in files if str(item).endswith(".dist-info/RECORD")]
    if len(record_entries) != 1:
        raise EvaluatorDependencyLockError(
            f"evaluator dependency RECORD authority is incomplete: {name}"
        )
    record_path = Path(package.locate_file(record_entries[0]))
    try:
        resolved = record_path.resolve(strict=True)
        metadata = os.stat(record_path, follow_symlinks=False)
    except OSError as exc:
        raise EvaluatorDependencyLockError(
            f"evaluator dependency RECORD is unavailable: {name}"
        ) from exc
    if (
        record_path.is_symlink()
        or not resolved.is_relative_to(prefix)
        or not stat.S_ISREG(metadata.st_mode)
    ):
        raise EvaluatorDependencyLockError(
            f"evaluator dependency RECORD escaped its environment: {name}"
        )
    record_payload = resolved.read_bytes()
    return {
        "name": canonicalize_name(package.metadata["Name"]),
        "version": package.version,
        "extras": list(extras),
        "record_path": resolved.relative_to(prefix).as_posix(),
        "record_sha256": hashlib.sha256(record_payload).hexdigest(),
    }


def current_evaluator_dependency_identity() -> dict[str, Any]:
    """Measure the exact interpreter and installed evaluator dependency closure."""

    distributions = [
        _distribution_identity(name, extras)
        for name, extras in _distribution_closure()
    ]
    observed = {item["name"]: item["version"] for item in distributions}
    if any(observed.get(name) != version for name, version in ROOT_DISTRIBUTIONS.items()):
        raise EvaluatorDependencyLockError("evaluator root dependency version drifted")
    body = {
        "schema_version": SCHEMA_VERSION,
        "python": {
            "executable": os.path.abspath(sys.executable),
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
        "root_distributions": dict(sorted(ROOT_DISTRIBUTIONS.items())),
        "distributions": distributions,
    }
    return {**body, "content_sha256": canonical_sha256(body)}


def write_evaluator_dependency_lock(path: str | Path) -> dict[str, Any]:
    """Publish a new append-only dependency lock."""

    destination = Path(os.path.abspath(path))
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = current_evaluator_dependency_identity()
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(canonical_bytes(payload) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    directory = os.open(
        destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    )
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return payload


def validate_evaluator_dependency_lock(path: str | Path) -> dict[str, Any]:
    """Fail closed unless the current evaluator environment matches the lock."""

    source = Path(os.path.abspath(path))
    try:
        metadata = os.stat(source, follow_symlinks=False)
        expected = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluatorDependencyLockError(
            "evaluator dependency lock is unavailable"
        ) from exc
    if (
        source.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o022
        or not isinstance(expected, dict)
    ):
        raise EvaluatorDependencyLockError("evaluator dependency lock is unsafe")
    observed = current_evaluator_dependency_identity()
    if expected != observed:
        raise EvaluatorDependencyLockError("evaluator dependency environment drifted")
    return expected


__all__ = [
    "ROOT_DISTRIBUTIONS",
    "SCHEMA_VERSION",
    "EvaluatorDependencyLockError",
    "current_evaluator_dependency_identity",
    "validate_evaluator_dependency_lock",
    "write_evaluator_dependency_lock",
]
