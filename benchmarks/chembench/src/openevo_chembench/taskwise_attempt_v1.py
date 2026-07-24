"""Private, immutable execution-attempt namespaces for taskwise suites.

The generation id binds protocol authority.  An attempt id only isolates a
fresh all-stream execution after a closed infrastructure failure; it never
changes manifests, treatment, or statistical authority.  Attempts are paired:
control allocates the namespace, online must use that same namespace, and
comparison consumes only a namespace in which both arms completed.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Literal

ATTEMPT_SCHEMA_V1 = "taskwise_paired_execution_attempt_v1"
ATTEMPT_ID_WIDTH = 6
_ATTEMPT_ID_RE = re.compile(r"attempt_([0-9]{6})")
_GENERATION_ID_RE = re.compile(r"gen_[0-9a-f]{64}")
_SUITE_KINDS = frozenset({"pilot500", "full"})
_ARMS = frozenset({"control", "online"})
_CLOSED_INFRASTRUCTURE_CODES = frozenset(
    {
        "EXECUTOR_PREFLIGHT_FAILED",
        "EXECUTOR_AUTH_MATERIALIZATION_FAILED",
        "EXECUTOR_PROCESS_SPAWN_FAILED",
        "EXECUTOR_CODEX_STARTUP_FAILED",
        "EXECUTOR_MODEL_TRANSPORT_FAILED",
        "EXECUTOR_TIMEOUT",
        "EXECUTOR_NONZERO_EXIT",
        "EXECUTOR_EVENT_STREAM_INVALID",
        "EXECUTOR_OUTPUT_MISSING",
        "EXECUTOR_OUTPUT_INVALID",
        "EXECUTOR_PRIVATE_DIAGNOSTICS_FAILED",
        "EXECUTOR_CLEANUP_FAILED",
        "EXECUTOR_POST_ATTESTATION_FAILED",
        "EXECUTOR_INTERNAL_ERROR",
        "EXECUTOR_PUBLIC_STATE_UNAVAILABLE",
        "EXECUTOR_SUITE_ORCHESTRATION_FAILED",
        "EXECUTOR_CONFIG_POLICY_AGENTS_ENABLED_FORBIDDEN",
        "EXECUTOR_CONFIG_POLICY_COMMAND_INVALID",
        "EXECUTOR_CONFIG_POLICY_CONFIG_MISMATCH",
        "EXECUTOR_CONFIG_POLICY_FEATURES_MISMATCH",
        "EXECUTOR_CONFIG_POLICY_PROBE_REJECTED",
        "EXECUTOR_CONFIG_POLICY_PROBE_UNAVAILABLE",
        "EXECUTOR_CONFIG_POLICY_VERSION_MISMATCH",
    }
)


class TaskwiseAttemptError(RuntimeError):
    """Closed attempt-allocation or evidence failure."""


def format_taskwise_attempt_id_v1(index: int) -> str:
    """Return the canonical positive, monotonically sortable attempt id."""

    if type(index) is not int or index < 1 or index >= 10**ATTEMPT_ID_WIDTH:
        raise ValueError("taskwise attempt index is invalid")
    return f"attempt_{index:0{ATTEMPT_ID_WIDTH}d}"


def validate_taskwise_attempt_id_v1(attempt_id: str) -> str:
    """Require the exact attempt-id grammar and a positive index."""

    if type(attempt_id) is not str:
        raise ValueError("taskwise attempt id is invalid")
    match = _ATTEMPT_ID_RE.fullmatch(attempt_id)
    if match is None or int(match.group(1)) < 1:
        raise ValueError("taskwise attempt id is invalid")
    return attempt_id


def taskwise_attempt_index_v1(attempt_id: str) -> int:
    """Return the positive integer encoded by an exact attempt id."""

    validate_taskwise_attempt_id_v1(attempt_id)
    return int(attempt_id.removeprefix("attempt_"))


def _validate_generation_id(generation_id: str) -> str:
    if type(generation_id) is not str or _GENERATION_ID_RE.fullmatch(generation_id) is None:
        raise ValueError("taskwise generation id is invalid")
    return generation_id


def taskwise_attempts_root_v1(
    repository_root: Path,
    *,
    suite_kind: Literal["pilot500", "full"],
    generation_id: str,
) -> Path:
    """Return the private attempts root for one immutable authority."""

    if not isinstance(repository_root, Path):
        raise TypeError("repository_root must be pathlib.Path")
    if suite_kind not in _SUITE_KINDS:
        raise ValueError("taskwise suite kind is invalid")
    generation = _validate_generation_id(generation_id)
    generation_directory = (
        "pilot500_generations" if suite_kind == "pilot500" else "full_generations"
    )
    return (
        repository_root.resolve()
        / "results"
        / "chembench4k_taskwise_online_v1"
        / generation_directory
        / generation
        / "attempts"
    )


def taskwise_attempt_root_v1(
    repository_root: Path,
    *,
    suite_kind: Literal["pilot500", "full"],
    generation_id: str,
    attempt_id: str,
) -> Path:
    """Return one private paired-attempt root."""

    return taskwise_attempts_root_v1(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
    ) / validate_taskwise_attempt_id_v1(attempt_id)


def taskwise_attempt_suite_state_path_v1(
    repository_root: Path,
    *,
    suite_kind: Literal["pilot500", "full"],
    generation_id: str,
    attempt_id: str,
    arm: Literal["control", "online"],
) -> Path:
    """Return one arm's suite state inside a paired attempt."""

    if arm not in _ARMS:
        raise ValueError("taskwise suite arm is invalid")
    return (
        taskwise_attempt_root_v1(
            repository_root,
            suite_kind=suite_kind,
            generation_id=generation_id,
            attempt_id=attempt_id,
        )
        / f"{arm}_suite_state.json"
    )


def taskwise_attempt_comparison_path_v1(
    repository_root: Path,
    *,
    suite_kind: Literal["pilot500", "full"],
    generation_id: str,
    attempt_id: str,
) -> Path:
    """Return the immutable private comparison path for one paired attempt."""

    filename = (
        "taskwise_stream_comparison_v1.json"
        if suite_kind == "pilot500"
        else "taskwise_full_stream_comparison_v1.json"
    )
    return (
        taskwise_attempt_root_v1(
            repository_root,
            suite_kind=suite_kind,
            generation_id=generation_id,
            attempt_id=attempt_id,
        )
        / "private"
        / filename
    )


def allocate_taskwise_control_attempt_v1(
    repository_root: Path,
    *,
    suite_kind: Literal["pilot500", "full"],
    generation_id: str,
    requested_attempt_id: str | None = None,
) -> str:
    """Atomically allocate the first or next infrastructure-only retry attempt."""

    attempts_root = taskwise_attempts_root_v1(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
    )
    existing = _ordered_attempt_ids(attempts_root)
    if existing:
        latest = existing[-1]
        _require_closed_infrastructure_attempt(
            repository_root,
            suite_kind=suite_kind,
            generation_id=generation_id,
            attempt_id=latest,
        )
        expected = format_taskwise_attempt_id_v1(taskwise_attempt_index_v1(latest) + 1)
    else:
        expected = format_taskwise_attempt_id_v1(1)
    if requested_attempt_id is not None:
        try:
            requested = validate_taskwise_attempt_id_v1(requested_attempt_id)
        except ValueError as exc:
            raise TaskwiseAttemptError("TASKWISE_ATTEMPT_ID_INVALID") from exc
        if requested != expected:
            raise TaskwiseAttemptError("TASKWISE_ATTEMPT_SEQUENCE_INVALID")
    attempt_id = expected
    _create_attempt_authority(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
        attempt_id=attempt_id,
    )
    return attempt_id


def require_taskwise_online_attempt_v1(
    repository_root: Path,
    *,
    suite_kind: Literal["pilot500", "full"],
    generation_id: str,
    requested_attempt_id: str | None = None,
) -> str:
    """Require the latest attempt with completed control and no online evidence."""

    attempts_root = taskwise_attempts_root_v1(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
    )
    existing = _ordered_attempt_ids(attempts_root)
    if not existing:
        raise TaskwiseAttemptError("TASKWISE_PAIRED_ATTEMPT_CONTROL_REQUIRED")
    attempt_id = existing[-1]
    if requested_attempt_id is not None:
        try:
            requested = validate_taskwise_attempt_id_v1(requested_attempt_id)
        except ValueError as exc:
            raise TaskwiseAttemptError("TASKWISE_ATTEMPT_ID_INVALID") from exc
        if requested != attempt_id:
            raise TaskwiseAttemptError("TASKWISE_PAIRED_ATTEMPT_MISMATCH")
    _verify_attempt_authority(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
        attempt_id=attempt_id,
    )
    control_path = taskwise_attempt_suite_state_path_v1(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
        attempt_id=attempt_id,
        arm="control",
    )
    if not control_path.exists() or control_path.is_symlink():
        raise TaskwiseAttemptError("TASKWISE_PAIRED_ATTEMPT_CONTROL_INCOMPLETE")
    control = _read_suite_state(control_path, expected_arm="control")
    online_path = taskwise_attempt_suite_state_path_v1(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
        attempt_id=attempt_id,
        arm="online",
    )
    if control.get("status") != "COMPLETED":
        raise TaskwiseAttemptError("TASKWISE_PAIRED_ATTEMPT_CONTROL_INCOMPLETE")
    if online_path.exists() or online_path.is_symlink():
        raise TaskwiseAttemptError("TASKWISE_PAIRED_ATTEMPT_ONLINE_EXISTS")
    if _comparison_path(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
        attempt_id=attempt_id,
    ).exists():
        raise TaskwiseAttemptError("TASKWISE_PAIRED_ATTEMPT_COMPARISON_EXISTS")
    return attempt_id


def require_taskwise_completed_paired_attempt_v1(
    repository_root: Path,
    *,
    suite_kind: Literal["pilot500", "full"],
    generation_id: str,
    requested_attempt_id: str | None = None,
) -> str:
    """Select the sole latest attempt whose control and online arms completed."""

    attempts_root = taskwise_attempts_root_v1(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
    )
    existing = _ordered_attempt_ids(attempts_root)
    if not existing:
        raise TaskwiseAttemptError("TASKWISE_PAIRED_ATTEMPT_MISSING")
    attempt_id = existing[-1]
    if requested_attempt_id is not None:
        try:
            requested = validate_taskwise_attempt_id_v1(requested_attempt_id)
        except ValueError as exc:
            raise TaskwiseAttemptError("TASKWISE_ATTEMPT_ID_INVALID") from exc
        if requested != attempt_id:
            raise TaskwiseAttemptError("TASKWISE_PAIRED_ATTEMPT_MISMATCH")
    for prior in existing[:-1]:
        _require_closed_infrastructure_attempt(
            repository_root,
            suite_kind=suite_kind,
            generation_id=generation_id,
            attempt_id=prior,
        )
    _verify_attempt_authority(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
        attempt_id=attempt_id,
    )
    for arm in ("control", "online"):
        state = _read_suite_state(
            taskwise_attempt_suite_state_path_v1(
                repository_root,
                suite_kind=suite_kind,
                generation_id=generation_id,
                attempt_id=attempt_id,
                arm=arm,
            ),
            expected_arm=arm,
        )
        if state.get("status") != "COMPLETED":
            raise TaskwiseAttemptError("TASKWISE_PAIRED_ATTEMPT_INCOMPLETE")
    return attempt_id


def require_taskwise_active_arm_attempt_v1(
    repository_root: Path,
    *,
    suite_kind: Literal["pilot500", "full"],
    generation_id: str,
    attempt_id: str,
    arm: Literal["control", "online"],
) -> str:
    """Require that ``run-arm`` was entered through the paired suite owner."""

    if arm not in _ARMS:
        raise TaskwiseAttemptError("TASKWISE_SUITE_ARM_INVALID")
    attempt = validate_taskwise_attempt_id_v1(attempt_id)
    _verify_attempt_authority(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
        attempt_id=attempt,
    )
    state = _read_suite_state(
        taskwise_attempt_suite_state_path_v1(
            repository_root,
            suite_kind=suite_kind,
            generation_id=generation_id,
            attempt_id=attempt,
            arm=arm,
        ),
        expected_arm=arm,
    )
    if state.get("status") != "INCOMPLETE":
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_NOT_ACTIVE")
    if _comparison_path(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
        attempt_id=attempt,
    ).exists():
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_ALREADY_COMPARED")
    return attempt


def _require_closed_infrastructure_attempt(
    repository_root: Path,
    *,
    suite_kind: Literal["pilot500", "full"],
    generation_id: str,
    attempt_id: str,
) -> None:
    """Reject retries unless the preceding paired attempt closed for infra only."""

    _verify_attempt_authority(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
        attempt_id=attempt_id,
    )
    comparison = _comparison_path(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
        attempt_id=attempt_id,
    )
    if comparison.exists() or comparison.is_symlink():
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_ALREADY_COMPARED")
    states: dict[str, dict[str, Any]] = {}
    for arm in ("control", "online"):
        path = taskwise_attempt_suite_state_path_v1(
            repository_root,
            suite_kind=suite_kind,
            generation_id=generation_id,
            attempt_id=attempt_id,
            arm=arm,
        )
        if path.exists() or path.is_symlink():
            states[arm] = _read_suite_state(path, expected_arm=arm)
    if not states:
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_INCOMPLETE")
    if any(state.get("status") not in {"COMPLETED", "FAILED"} for state in states.values()):
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_INCOMPLETE")
    failed = tuple(state for state in states.values() if state.get("status") == "FAILED")
    if len(failed) != 1:
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_RETRY_FORBIDDEN")
    failure = failed[0]
    if (
        _strict_nonnegative_int(failure, "infrastructure_failures") < 1
        or _strict_nonnegative_int(failure, "security_violations") != 0
        or _strict_nonnegative_int(failure, "evolution_update_failures") != 0
        or _strict_nonnegative_int(failure, "artifact_validation_failures") != 0
        or _strict_nonnegative_int(failure, "context_binding_violations") != 0
    ):
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_RETRY_FORBIDDEN")
    codes = failure.get("executor_failure_codes")
    if (
        type(codes) is not dict
        or not codes
        or any(
            type(code) is not str
            or code not in _CLOSED_INFRASTRUCTURE_CODES
            or type(count) is not int
            or isinstance(count, bool)
            or count < 1
            for code, count in codes.items()
        )
    ):
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_RETRY_FORBIDDEN")


def _create_attempt_authority(
    repository_root: Path,
    *,
    suite_kind: Literal["pilot500", "full"],
    generation_id: str,
    attempt_id: str,
) -> None:
    attempts_root = taskwise_attempts_root_v1(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
    )
    attempts_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    attempts_root.chmod(0o700)
    attempt_root = attempts_root / validate_taskwise_attempt_id_v1(attempt_id)
    try:
        attempt_root.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError as exc:
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_ALREADY_EXISTS") from exc
    authority = {
        "schema_version": ATTEMPT_SCHEMA_V1,
        "suite_kind": suite_kind,
        "generation_id": _validate_generation_id(generation_id),
        "attempt_id": attempt_id,
    }
    payload = _canonical_bytes(authority)
    path = attempt_root / "attempt_authority.json"
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _verify_attempt_authority(
    repository_root: Path,
    *,
    suite_kind: Literal["pilot500", "full"],
    generation_id: str,
    attempt_id: str,
) -> None:
    root = taskwise_attempt_root_v1(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
        attempt_id=attempt_id,
    )
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_AUTHORITY_INVALID") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_AUTHORITY_INVALID")
    path = root / "attempt_authority.json"
    payload = _read_canonical_json(path, mode=0o600)
    if payload != {
        "schema_version": ATTEMPT_SCHEMA_V1,
        "suite_kind": suite_kind,
        "generation_id": _validate_generation_id(generation_id),
        "attempt_id": validate_taskwise_attempt_id_v1(attempt_id),
    }:
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_AUTHORITY_INVALID")


def _ordered_attempt_ids(path: Path) -> tuple[str, ...]:
    if not path.exists():
        return ()
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise TaskwiseAttemptError("TASKWISE_ATTEMPTS_ROOT_INVALID")
    names = []
    for child in path.iterdir():
        if child.name.startswith("."):
            raise TaskwiseAttemptError("TASKWISE_ATTEMPT_NAMESPACE_INVALID")
        try:
            names.append(validate_taskwise_attempt_id_v1(child.name))
        except ValueError as exc:
            raise TaskwiseAttemptError("TASKWISE_ATTEMPT_NAMESPACE_INVALID") from exc
    ordered = tuple(sorted(names, key=taskwise_attempt_index_v1))
    expected = tuple(format_taskwise_attempt_id_v1(index) for index in range(1, len(ordered) + 1))
    if ordered != expected:
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_SEQUENCE_INVALID")
    return ordered


def _read_suite_state(path: Path, *, expected_arm: str) -> dict[str, Any]:
    payload = _read_canonical_json(path, mode=0o644)
    if (
        payload.get("arm") != expected_arm
        or payload.get("status") not in {"COMPLETED", "INCOMPLETE", "FAILED"}
        or payload.get("attempt_id") != path.parent.name
    ):
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_SUITE_STATE_INVALID")
    return payload


def _read_canonical_json(path: Path, *, mode: int) -> dict[str, Any]:
    try:
        metadata = path.lstat()
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_EVIDENCE_INVALID") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != mode
        or metadata.st_uid != os.geteuid()
        or type(payload) is not dict
        or raw != _canonical_bytes(payload)
    ):
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_EVIDENCE_INVALID")
    return payload


def _strict_nonnegative_int(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field)
    if type(value) is not int or isinstance(value, bool) or value < 0:
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_SUITE_STATE_INVALID")
    return value


def _comparison_path(
    repository_root: Path,
    *,
    suite_kind: Literal["pilot500", "full"],
    generation_id: str,
    attempt_id: str,
) -> Path:
    return taskwise_attempt_comparison_path_v1(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
        attempt_id=attempt_id,
    )


def _canonical_bytes(value: object) -> bytes:
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


__all__ = [
    "ATTEMPT_ID_WIDTH",
    "ATTEMPT_SCHEMA_V1",
    "TaskwiseAttemptError",
    "allocate_taskwise_control_attempt_v1",
    "format_taskwise_attempt_id_v1",
    "require_taskwise_completed_paired_attempt_v1",
    "require_taskwise_active_arm_attempt_v1",
    "require_taskwise_online_attempt_v1",
    "taskwise_attempt_comparison_path_v1",
    "taskwise_attempt_index_v1",
    "taskwise_attempt_root_v1",
    "taskwise_attempt_suite_state_path_v1",
    "taskwise_attempts_root_v1",
    "validate_taskwise_attempt_id_v1",
]
