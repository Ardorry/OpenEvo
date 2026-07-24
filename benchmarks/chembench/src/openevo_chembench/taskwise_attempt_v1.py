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
import secrets
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

    existing = _ordered_attempt_ids(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
    )
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

    existing = _ordered_attempt_ids(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
    )
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
    try:
        control = read_taskwise_attempt_suite_state_v1(
            repository_root,
            suite_kind=suite_kind,
            generation_id=generation_id,
            attempt_id=attempt_id,
            arm="control",
        )
    except TaskwiseAttemptError as exc:
        raise TaskwiseAttemptError("TASKWISE_PAIRED_ATTEMPT_CONTROL_INCOMPLETE") from exc
    _validate_suite_state(
        control,
        expected_arm="control",
        expected_suite_kind=suite_kind,
        expected_generation_id=generation_id,
        expected_attempt_id=attempt_id,
    )
    online_exists = taskwise_attempt_suite_state_exists_v1(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
        attempt_id=attempt_id,
        arm="online",
    )
    if control.get("status") != "COMPLETED":
        raise TaskwiseAttemptError("TASKWISE_PAIRED_ATTEMPT_CONTROL_INCOMPLETE")
    if online_exists:
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

    existing = _ordered_attempt_ids(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
    )
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
        state = read_taskwise_attempt_suite_state_v1(
            repository_root,
            suite_kind=suite_kind,
            generation_id=generation_id,
            attempt_id=attempt_id,
            arm=arm,
        )
        _validate_suite_state(
            state,
            expected_arm=arm,
            expected_suite_kind=suite_kind,
            expected_generation_id=generation_id,
            expected_attempt_id=attempt_id,
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
    scope: str,
) -> str:
    """Require that ``run-arm`` was entered through the paired suite owner."""

    if arm not in _ARMS:
        raise TaskwiseAttemptError("TASKWISE_SUITE_ARM_INVALID")
    if type(scope) is not str or not scope:
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_SUITE_STATE_INVALID")
    attempt = validate_taskwise_attempt_id_v1(attempt_id)
    _verify_attempt_authority(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
        attempt_id=attempt,
    )
    state = read_taskwise_attempt_suite_state_v1(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
        attempt_id=attempt,
        arm=arm,
    )
    _validate_suite_state(
        state,
        expected_arm=arm,
        expected_suite_kind=suite_kind,
        expected_generation_id=generation_id,
        expected_attempt_id=attempt,
    )
    streams = state.get("streams")
    expected_scopes = tuple(
        f"{'pilot500' if suite_kind == 'pilot500' else 'full'}_stream_{index:02d}"
        for index in range(10)
    )
    active = (
        tuple(
            stream_scope
            for stream_scope, entry in streams.items()
            if type(entry) is dict and entry.get("action") == "STARTING"
        )
        if type(streams) is dict
        else ()
    )
    if scope not in expected_scopes:
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_NOT_ACTIVE")
    scope_index = expected_scopes.index(scope)
    if (
        state.get("status") != "INCOMPLETE"
        or state.get("active_scope") != scope
        or type(streams) is not dict
        or tuple(sorted(streams)) != expected_scopes
        or active != (scope,)
        or type(streams.get(scope)) is not dict
        or streams[scope].get("action") != "STARTING"
        or any(
            type(streams[prior]) is not dict or streams[prior].get("action") != "COMPLETED"
            for prior in expected_scopes[:scope_index]
        )
        or any(
            type(streams[later]) is not dict or streams[later].get("action") != "NOT_STARTED"
            for later in expected_scopes[scope_index + 1 :]
        )
    ):
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_NOT_ACTIVE")
    if _comparison_path(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
        attempt_id=attempt,
    ).exists():
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_ALREADY_COMPARED")
    _claim_active_scope(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
        attempt_id=attempt,
        arm=arm,
        scope=scope,
    )
    return attempt


def write_taskwise_attempt_suite_state_v1(
    repository_root: Path,
    *,
    suite_kind: Literal["pilot500", "full"],
    generation_id: str,
    attempt_id: str,
    arm: Literal["control", "online"],
    state: dict[str, Any],
    create_only: bool,
) -> None:
    """Persist suite state relative to an attested attempt directory."""

    if arm not in _ARMS or type(state) is not dict or type(create_only) is not bool:
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_SUITE_STATE_INVALID")
    attempt_descriptor = _open_private_attempt_directory(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
        attempt_id=attempt_id,
    )
    filename = f"{arm}_suite_state.json"
    payload = _canonical_bytes(state)
    try:
        if create_only:
            _create_canonical_file_at(
                attempt_descriptor,
                filename,
                payload,
                mode=0o644,
                exists_code="TASKWISE_SUITE_STATE_EXISTS",
            )
        else:
            _replace_canonical_file_at(
                attempt_descriptor,
                filename,
                payload,
                mode=0o644,
            )
        os.fsync(attempt_descriptor)
    finally:
        os.close(attempt_descriptor)


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
        if not taskwise_attempt_suite_state_exists_v1(
            repository_root,
            suite_kind=suite_kind,
            generation_id=generation_id,
            attempt_id=attempt_id,
            arm=arm,
        ):
            continue
        state = read_taskwise_attempt_suite_state_v1(
            repository_root,
            suite_kind=suite_kind,
            generation_id=generation_id,
            attempt_id=attempt_id,
            arm=arm,
        )
        _validate_suite_state(
            state,
            expected_arm=arm,
            expected_suite_kind=suite_kind,
            expected_generation_id=generation_id,
            expected_attempt_id=attempt_id,
        )
        states[arm] = state
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
    validated_attempt = validate_taskwise_attempt_id_v1(attempt_id)
    attempts_descriptor = _walk_attempt_hierarchy(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
        create=True,
    )
    attempt_descriptor: int | None = None
    authority_descriptor: int | None = None
    try:
        try:
            os.mkdir(validated_attempt, mode=0o700, dir_fd=attempts_descriptor)
        except FileExistsError as exc:
            raise TaskwiseAttemptError("TASKWISE_ATTEMPT_ALREADY_EXISTS") from exc
        attempt_descriptor = os.open(
            validated_attempt,
            _open_directory_flags(),
            dir_fd=attempts_descriptor,
        )
        attempt_metadata = os.fstat(attempt_descriptor)
        if (
            not stat.S_ISDIR(attempt_metadata.st_mode)
            or stat.S_IMODE(attempt_metadata.st_mode) != 0o700
            or attempt_metadata.st_uid != os.geteuid()
        ):
            raise TaskwiseAttemptError("TASKWISE_ATTEMPT_AUTHORITY_INVALID")
        authority = {
            "schema_version": ATTEMPT_SCHEMA_V1,
            "suite_kind": suite_kind,
            "generation_id": _validate_generation_id(generation_id),
            "attempt_id": validated_attempt,
        }
        payload = _canonical_bytes(authority)
        authority_descriptor = os.open(
            "attempt_authority.json",
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=attempt_descriptor,
        )
        os.fchmod(authority_descriptor, 0o600)
        with os.fdopen(authority_descriptor, "wb") as stream:
            authority_descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(attempt_descriptor)
        os.fsync(attempts_descriptor)
    except TaskwiseAttemptError:
        raise
    except OSError as exc:
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_AUTHORITY_INVALID") from exc
    finally:
        if authority_descriptor is not None:
            os.close(authority_descriptor)
        if attempt_descriptor is not None:
            os.close(attempt_descriptor)
        os.close(attempts_descriptor)


def _private_attempt_components(
    *,
    suite_kind: Literal["pilot500", "full"],
    generation_id: str,
) -> tuple[str, ...]:
    if suite_kind not in _SUITE_KINDS:
        raise ValueError("taskwise suite kind is invalid")
    generation_directory = (
        "pilot500_generations" if suite_kind == "pilot500" else "full_generations"
    )
    return (
        "results",
        "chembench4k_taskwise_online_v1",
        generation_directory,
        _validate_generation_id(generation_id),
        "attempts",
    )


def _open_directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _walk_attempt_hierarchy(
    repository_root: Path,
    *,
    suite_kind: Literal["pilot500", "full"],
    generation_id: str,
    create: bool,
) -> int:
    """Open ``attempts/`` without following any mutable path component."""

    components = _private_attempt_components(
        suite_kind=suite_kind,
        generation_id=generation_id,
    )
    flags = _open_directory_flags()
    current: int | None = None
    try:
        current = os.open(repository_root.resolve(), flags)
        root_metadata = os.fstat(current)
        if not stat.S_ISDIR(root_metadata.st_mode) or root_metadata.st_uid != os.geteuid():
            raise OSError("repository root attestation failed")
        for index, component in enumerate(components):
            private = index >= 3
            created = False
            try:
                child = os.open(component, flags, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=(0o700 if private else 0o755), dir_fd=current)
                child = os.open(component, flags, dir_fd=current)
                created = True
            previous = current
            current = child
            os.close(previous)
            if created and private:
                os.fchmod(current, 0o700)
            metadata = os.fstat(current)
            expected_mode = 0o700 if private else None
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or (expected_mode is not None and stat.S_IMODE(metadata.st_mode) != expected_mode)
            ):
                raise OSError("attempt hierarchy attestation failed")
        result = current
        current = None
        return result
    except OSError as exc:
        raise TaskwiseAttemptError("TASKWISE_ATTEMPTS_ROOT_INVALID") from exc
    finally:
        if current is not None:
            os.close(current)


def _open_private_attempts_directory(
    repository_root: Path,
    *,
    suite_kind: Literal["pilot500", "full"],
    generation_id: str,
) -> int:
    return _walk_attempt_hierarchy(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
        create=False,
    )


def _open_private_attempt_directory(
    repository_root: Path,
    *,
    suite_kind: Literal["pilot500", "full"],
    generation_id: str,
    attempt_id: str,
) -> int:
    try:
        parent = _open_private_attempts_directory(
            repository_root,
            suite_kind=suite_kind,
            generation_id=generation_id,
        )
    except TaskwiseAttemptError as exc:
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_AUTHORITY_INVALID") from exc
    try:
        descriptor = os.open(
            validate_taskwise_attempt_id_v1(attempt_id),
            _open_directory_flags(),
            dir_fd=parent,
        )
    except OSError as exc:
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_AUTHORITY_INVALID") from exc
    finally:
        os.close(parent)
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        os.close(descriptor)
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_AUTHORITY_INVALID")
    return descriptor


def _verify_attempt_authority(
    repository_root: Path,
    *,
    suite_kind: Literal["pilot500", "full"],
    generation_id: str,
    attempt_id: str,
) -> None:
    attempt_descriptor = _open_private_attempt_directory(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
        attempt_id=attempt_id,
    )
    try:
        payload = _read_canonical_json_at(
            attempt_descriptor,
            "attempt_authority.json",
            mode=0o600,
        )
    finally:
        os.close(attempt_descriptor)
    if payload != {
        "schema_version": ATTEMPT_SCHEMA_V1,
        "suite_kind": suite_kind,
        "generation_id": _validate_generation_id(generation_id),
        "attempt_id": validate_taskwise_attempt_id_v1(attempt_id),
    }:
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_AUTHORITY_INVALID")


def _ordered_attempt_ids(
    repository_root: Path,
    *,
    suite_kind: Literal["pilot500", "full"],
    generation_id: str,
) -> tuple[str, ...]:
    path = taskwise_attempts_root_v1(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
    )
    if not path.exists():
        return ()
    descriptor = _open_private_attempts_directory(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
    )
    names = []
    try:
        for name in os.listdir(descriptor):
            if name.startswith("."):
                raise TaskwiseAttemptError("TASKWISE_ATTEMPT_NAMESPACE_INVALID")
            try:
                validated = validate_taskwise_attempt_id_v1(name)
                metadata = os.stat(
                    name,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except (OSError, ValueError) as exc:
                raise TaskwiseAttemptError("TASKWISE_ATTEMPT_NAMESPACE_INVALID") from exc
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or metadata.st_uid != os.geteuid()
            ):
                raise TaskwiseAttemptError("TASKWISE_ATTEMPT_NAMESPACE_INVALID")
            names.append(validated)
    finally:
        os.close(descriptor)
    ordered = tuple(sorted(names, key=taskwise_attempt_index_v1))
    expected = tuple(format_taskwise_attempt_id_v1(index) for index in range(1, len(ordered) + 1))
    if ordered != expected:
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_SEQUENCE_INVALID")
    return ordered


def read_taskwise_attempt_suite_state_v1(
    repository_root: Path,
    *,
    suite_kind: Literal["pilot500", "full"],
    generation_id: str,
    attempt_id: str,
    arm: Literal["control", "online"],
) -> dict[str, Any]:
    """Read suite state relative to an attested attempt directory."""

    if arm not in _ARMS:
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_SUITE_STATE_INVALID")
    attempt_descriptor = _open_private_attempt_directory(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
        attempt_id=attempt_id,
    )
    try:
        return _read_canonical_json_at(
            attempt_descriptor,
            f"{arm}_suite_state.json",
            mode=0o644,
        )
    finally:
        os.close(attempt_descriptor)


def taskwise_attempt_suite_state_exists_v1(
    repository_root: Path,
    *,
    suite_kind: Literal["pilot500", "full"],
    generation_id: str,
    attempt_id: str,
    arm: Literal["control", "online"],
) -> bool:
    """Check state existence without following the attempt or state entry."""

    if arm not in _ARMS:
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_SUITE_STATE_INVALID")
    attempt_descriptor = _open_private_attempt_directory(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
        attempt_id=attempt_id,
    )
    try:
        try:
            metadata = os.stat(
                f"{arm}_suite_state.json",
                dir_fd=attempt_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o644
            or metadata.st_uid != os.geteuid()
        ):
            raise TaskwiseAttemptError("TASKWISE_ATTEMPT_EVIDENCE_INVALID")
        return True
    except OSError as exc:
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_EVIDENCE_INVALID") from exc
    finally:
        os.close(attempt_descriptor)


def _validate_suite_state(
    payload: dict[str, Any],
    *,
    expected_arm: str,
    expected_suite_kind: Literal["pilot500", "full"],
    expected_generation_id: str,
    expected_attempt_id: str,
) -> None:
    expected_schema = (
        "taskwise_pilot500_stream_suite_state_v1"
        if expected_suite_kind == "pilot500"
        else "taskwise_full_stream_suite_state_v1"
    )
    if (
        payload.get("schema_version") != expected_schema
        or payload.get("generation_id") != _validate_generation_id(expected_generation_id)
        or payload.get("arm") != expected_arm
        or payload.get("status") not in {"COMPLETED", "INCOMPLETE", "FAILED"}
        or payload.get("attempt_id") != validate_taskwise_attempt_id_v1(expected_attempt_id)
        or (
            payload.get("active_scope") is not None
            and type(payload.get("active_scope")) is not str
        )
    ):
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_SUITE_STATE_INVALID")


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


def _read_canonical_json_at(
    parent_descriptor: int,
    filename: str,
    *,
    mode: int,
) -> dict[str, Any]:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            filename,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        metadata = os.fstat(descriptor)
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read()
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_EVIDENCE_INVALID") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != mode
        or metadata.st_uid != os.geteuid()
        or type(payload) is not dict
        or raw != _canonical_bytes(payload)
    ):
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_EVIDENCE_INVALID")
    return payload


def _create_canonical_file_at(
    parent_descriptor: int,
    filename: str,
    payload: bytes,
    *,
    mode: int,
    exists_code: str,
) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            filename,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=parent_descriptor,
        )
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise TaskwiseAttemptError(exists_code) from exc
    except OSError as exc:
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_EVIDENCE_INVALID") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _replace_canonical_file_at(
    parent_descriptor: int,
    filename: str,
    payload: bytes,
    *,
    mode: int,
) -> None:
    temporary = f".{filename}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=parent_descriptor,
        )
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(
            temporary,
            filename,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
    except OSError as exc:
        raise TaskwiseAttemptError("TASKWISE_ATTEMPT_EVIDENCE_INVALID") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise TaskwiseAttemptError("TASKWISE_ATTEMPT_EVIDENCE_INVALID") from exc


def _claim_active_scope(
    repository_root: Path,
    *,
    suite_kind: Literal["pilot500", "full"],
    generation_id: str,
    attempt_id: str,
    arm: Literal["control", "online"],
    scope: str,
) -> None:
    attempt_descriptor = _open_private_attempt_directory(
        repository_root,
        suite_kind=suite_kind,
        generation_id=generation_id,
        attempt_id=attempt_id,
    )
    claim = {
        "schema_version": "taskwise_active_scope_claim_v1",
        "suite_kind": suite_kind,
        "generation_id": _validate_generation_id(generation_id),
        "attempt_id": validate_taskwise_attempt_id_v1(attempt_id),
        "arm": arm,
        "scope": scope,
    }
    try:
        _create_canonical_file_at(
            attempt_descriptor,
            f".{arm}.{scope}.claim.json",
            _canonical_bytes(claim),
            mode=0o600,
            exists_code="TASKWISE_ATTEMPT_NOT_ACTIVE",
        )
        os.fsync(attempt_descriptor)
    finally:
        os.close(attempt_descriptor)


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
