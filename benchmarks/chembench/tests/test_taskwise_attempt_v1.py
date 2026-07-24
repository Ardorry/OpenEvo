from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from openevo_chembench.taskwise_attempt_v1 import (
    TaskwiseAttemptError,
    allocate_taskwise_control_attempt_v1,
    format_taskwise_attempt_id_v1,
    require_taskwise_active_arm_attempt_v1,
    require_taskwise_completed_paired_attempt_v1,
    require_taskwise_online_attempt_v1,
    read_taskwise_attempt_suite_state_v1,
    taskwise_attempt_root_v1,
    taskwise_attempt_suite_state_path_v1,
    write_taskwise_attempt_suite_state_v1,
)


_GENERATION = "gen_" + ("a" * 64)


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _write_suite_state(
    repository_root: Path,
    *,
    attempt_id: str,
    arm: str,
    status: str,
    infrastructure: bool = False,
    security: bool = False,
    active_scope: str | None = None,
) -> None:
    path = taskwise_attempt_suite_state_path_v1(
        repository_root,
        suite_kind="pilot500",
        generation_id=_GENERATION,
        attempt_id=attempt_id,
        arm=arm,
    )
    payload = {
        "schema_version": "taskwise_pilot500_stream_suite_state_v1",
        "generation_id": _GENERATION,
        "attempt_id": attempt_id,
        "arm": arm,
        "status": status,
        "active_scope": active_scope,
        "infrastructure_failures": int(infrastructure),
        "security_violations": int(security),
        "evolution_update_failures": 0,
        "artifact_validation_failures": 0,
        "context_binding_violations": 0,
        "executor_failure_codes": ({"EXECUTOR_TIMEOUT": 1} if infrastructure else {}),
        "streams": {
            scope: {
                "action": (
                    "STARTING"
                    if active_scope == scope
                    else (
                        "COMPLETED"
                        if active_scope is not None and scope < active_scope
                        else "NOT_STARTED"
                    )
                )
            }
            for scope in (f"pilot500_stream_{index:02d}" for index in range(10))
        },
    }
    path.write_bytes(_canonical(payload))
    path.chmod(0o644)


def test_infrastructure_failure_allocates_strict_fresh_paired_attempt(
    tmp_path: Path,
) -> None:
    first = allocate_taskwise_control_attempt_v1(
        tmp_path,
        suite_kind="pilot500",
        generation_id=_GENERATION,
    )
    assert first == "attempt_000001"
    _write_suite_state(tmp_path, attempt_id=first, arm="control", status="COMPLETED")
    assert (
        require_taskwise_online_attempt_v1(
            tmp_path,
            suite_kind="pilot500",
            generation_id=_GENERATION,
        )
        == first
    )
    _write_suite_state(
        tmp_path,
        attempt_id=first,
        arm="online",
        status="FAILED",
        infrastructure=True,
    )

    second = allocate_taskwise_control_attempt_v1(
        tmp_path,
        suite_kind="pilot500",
        generation_id=_GENERATION,
    )
    assert second == "attempt_000002"
    assert taskwise_attempt_root_v1(
        tmp_path,
        suite_kind="pilot500",
        generation_id=_GENERATION,
        attempt_id=first,
    ).is_dir()
    assert taskwise_attempt_root_v1(
        tmp_path,
        suite_kind="pilot500",
        generation_id=_GENERATION,
        attempt_id=second,
    ).is_dir()


def test_successful_attempt_cannot_be_retried_for_performance(tmp_path: Path) -> None:
    attempt = allocate_taskwise_control_attempt_v1(
        tmp_path,
        suite_kind="pilot500",
        generation_id=_GENERATION,
    )
    for arm in ("control", "online"):
        _write_suite_state(tmp_path, attempt_id=attempt, arm=arm, status="COMPLETED")
    assert (
        require_taskwise_completed_paired_attempt_v1(
            tmp_path,
            suite_kind="pilot500",
            generation_id=_GENERATION,
        )
        == attempt
    )
    with pytest.raises(TaskwiseAttemptError, match="RETRY_FORBIDDEN"):
        allocate_taskwise_control_attempt_v1(
            tmp_path,
            suite_kind="pilot500",
            generation_id=_GENERATION,
        )


def test_security_failure_cannot_allocate_fresh_attempt(tmp_path: Path) -> None:
    attempt = allocate_taskwise_control_attempt_v1(
        tmp_path,
        suite_kind="pilot500",
        generation_id=_GENERATION,
    )
    _write_suite_state(
        tmp_path,
        attempt_id=attempt,
        arm="control",
        status="FAILED",
        security=True,
    )
    with pytest.raises(TaskwiseAttemptError, match="RETRY_FORBIDDEN"):
        allocate_taskwise_control_attempt_v1(
            tmp_path,
            suite_kind="pilot500",
            generation_id=_GENERATION,
        )


def test_online_requires_same_completed_control_attempt(tmp_path: Path) -> None:
    with pytest.raises(TaskwiseAttemptError, match="CONTROL_REQUIRED"):
        require_taskwise_online_attempt_v1(
            tmp_path,
            suite_kind="pilot500",
            generation_id=_GENERATION,
        )
    attempt = allocate_taskwise_control_attempt_v1(
        tmp_path,
        suite_kind="pilot500",
        generation_id=_GENERATION,
    )
    with pytest.raises(TaskwiseAttemptError, match="CONTROL_INCOMPLETE"):
        require_taskwise_online_attempt_v1(
            tmp_path,
            suite_kind="pilot500",
            generation_id=_GENERATION,
        )
    _write_suite_state(tmp_path, attempt_id=attempt, arm="control", status="COMPLETED")
    with pytest.raises(TaskwiseAttemptError, match="MISMATCH"):
        require_taskwise_online_attempt_v1(
            tmp_path,
            suite_kind="pilot500",
            generation_id=_GENERATION,
            requested_attempt_id=format_taskwise_attempt_id_v1(2),
        )


def test_attempt_authority_is_private_and_exclusive(tmp_path: Path) -> None:
    attempt = allocate_taskwise_control_attempt_v1(
        tmp_path,
        suite_kind="full",
        generation_id=_GENERATION,
    )
    root = taskwise_attempt_root_v1(
        tmp_path,
        suite_kind="full",
        generation_id=_GENERATION,
        attempt_id=attempt,
    )
    assert (root.stat().st_mode & 0o777) == 0o700
    assert ((root / "attempt_authority.json").stat().st_mode & 0o777) == 0o600
    with pytest.raises(TaskwiseAttemptError, match="INCOMPLETE"):
        allocate_taskwise_control_attempt_v1(
            tmp_path,
            suite_kind="full",
            generation_id=_GENERATION,
            requested_attempt_id=attempt,
        )
    assert os.listdir(root) == ["attempt_authority.json"]


def test_direct_arm_requires_suite_owned_active_state(tmp_path: Path) -> None:
    attempt = allocate_taskwise_control_attempt_v1(
        tmp_path,
        suite_kind="pilot500",
        generation_id=_GENERATION,
    )
    with pytest.raises(TaskwiseAttemptError, match="EVIDENCE_INVALID"):
        require_taskwise_active_arm_attempt_v1(
            tmp_path,
            suite_kind="pilot500",
            generation_id=_GENERATION,
            attempt_id=attempt,
            arm="control",
            scope="pilot500_stream_00",
        )
    _write_suite_state(
        tmp_path,
        attempt_id=attempt,
        arm="control",
        status="INCOMPLETE",
        active_scope="pilot500_stream_00",
    )
    assert (
        require_taskwise_active_arm_attempt_v1(
            tmp_path,
            suite_kind="pilot500",
            generation_id=_GENERATION,
            attempt_id=attempt,
            arm="control",
            scope="pilot500_stream_00",
        )
        == attempt
    )
    with pytest.raises(TaskwiseAttemptError, match="ATTEMPT_NOT_ACTIVE"):
        require_taskwise_active_arm_attempt_v1(
            tmp_path,
            suite_kind="pilot500",
            generation_id=_GENERATION,
            attempt_id=attempt,
            arm="control",
            scope="pilot500_stream_00",
        )
    with pytest.raises(TaskwiseAttemptError, match="ATTEMPT_NOT_ACTIVE"):
        require_taskwise_active_arm_attempt_v1(
            tmp_path,
            suite_kind="pilot500",
            generation_id=_GENERATION,
            attempt_id=attempt,
            arm="control",
            scope="pilot500_stream_01",
        )


def test_active_scope_rejects_out_of_order_stream(tmp_path: Path) -> None:
    attempt = allocate_taskwise_control_attempt_v1(
        tmp_path,
        suite_kind="pilot500",
        generation_id=_GENERATION,
    )
    _write_suite_state(
        tmp_path,
        attempt_id=attempt,
        arm="control",
        status="INCOMPLETE",
        active_scope="pilot500_stream_01",
    )
    path = taskwise_attempt_suite_state_path_v1(
        tmp_path,
        suite_kind="pilot500",
        generation_id=_GENERATION,
        attempt_id=attempt,
        arm="control",
    )
    state = json.loads(path.read_bytes())
    state["streams"]["pilot500_stream_00"]["action"] = "NOT_STARTED"
    path.write_bytes(_canonical(state))

    with pytest.raises(TaskwiseAttemptError, match="ATTEMPT_NOT_ACTIVE"):
        require_taskwise_active_arm_attempt_v1(
            tmp_path,
            suite_kind="pilot500",
            generation_id=_GENERATION,
            attempt_id=attempt,
            arm="control",
            scope="pilot500_stream_01",
        )


def test_attempt_hierarchy_is_private_and_rejects_symlink_parent(tmp_path: Path) -> None:
    attempt = allocate_taskwise_control_attempt_v1(
        tmp_path,
        suite_kind="pilot500",
        generation_id=_GENERATION,
    )
    root = taskwise_attempt_root_v1(
        tmp_path,
        suite_kind="pilot500",
        generation_id=_GENERATION,
        attempt_id=attempt,
    )
    assert (root.parent.parent.stat().st_mode & 0o777) == 0o700
    assert (root.parent.stat().st_mode & 0o777) == 0o700
    assert (root.stat().st_mode & 0o777) == 0o700

    other = tmp_path / "elsewhere"
    other.mkdir()
    (root.parent / "attempt_000002").symlink_to(other, target_is_directory=True)
    with pytest.raises(TaskwiseAttemptError, match="ATTEMPT_NAMESPACE_INVALID"):
        allocate_taskwise_control_attempt_v1(
            tmp_path,
            suite_kind="pilot500",
            generation_id=_GENERATION,
        )

    unsafe_root = tmp_path / "unsafe"
    (unsafe_root / "results" / "chembench4k_taskwise_online_v1").mkdir(parents=True)
    (
        unsafe_root / "results" / "chembench4k_taskwise_online_v1" / "pilot500_generations"
    ).symlink_to(
        other,
        target_is_directory=True,
    )
    with pytest.raises(TaskwiseAttemptError, match="ATTEMPTS_ROOT_INVALID"):
        allocate_taskwise_control_attempt_v1(
            unsafe_root,
            suite_kind="pilot500",
            generation_id=_GENERATION,
        )


def test_suite_state_uses_attempt_relative_create_and_replace(tmp_path: Path) -> None:
    attempt = allocate_taskwise_control_attempt_v1(
        tmp_path,
        suite_kind="pilot500",
        generation_id=_GENERATION,
    )
    state = {
        "schema_version": "taskwise_pilot500_stream_suite_state_v1",
        "generation_id": _GENERATION,
        "attempt_id": attempt,
        "arm": "control",
        "status": "INCOMPLETE",
        "active_scope": None,
    }
    write_taskwise_attempt_suite_state_v1(
        tmp_path,
        suite_kind="pilot500",
        generation_id=_GENERATION,
        attempt_id=attempt,
        arm="control",
        state=state,
        create_only=True,
    )
    assert (
        read_taskwise_attempt_suite_state_v1(
            tmp_path,
            suite_kind="pilot500",
            generation_id=_GENERATION,
            attempt_id=attempt,
            arm="control",
        )
        == state
    )
    with pytest.raises(TaskwiseAttemptError, match="SUITE_STATE_EXISTS"):
        write_taskwise_attempt_suite_state_v1(
            tmp_path,
            suite_kind="pilot500",
            generation_id=_GENERATION,
            attempt_id=attempt,
            arm="control",
            state=state,
            create_only=True,
        )

    replacement = {**state, "status": "FAILED"}
    write_taskwise_attempt_suite_state_v1(
        tmp_path,
        suite_kind="pilot500",
        generation_id=_GENERATION,
        attempt_id=attempt,
        arm="control",
        state=replacement,
        create_only=False,
    )
    assert (
        read_taskwise_attempt_suite_state_v1(
            tmp_path,
            suite_kind="pilot500",
            generation_id=_GENERATION,
            attempt_id=attempt,
            arm="control",
        )
        == replacement
    )
