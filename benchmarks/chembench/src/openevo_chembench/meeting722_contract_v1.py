"""Fail-closed audit receipt for the July 22 taskwise evolution mechanism.

The receipt contains only public identifiers and digests.  Private evaluations
are read solely to verify that each Core update consumed the complete ordered
trajectory prefix and a closed safe-feedback signal; targets, completions, and
feedback payloads are never copied into the receipt.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from openevo_chembench.taskwise_context_binding_v1 import (
    taskwise_context_binding_receipt_from_public_dict,
)
from openevo_chembench.taskwise_feedback_v1 import (
    taskwise_safe_signal_from_payload,
)


CONTRACT_ID = "Meeting722TaskwiseContractV1"
CONTRACT_VIOLATION = "MEETING_722_TASKWISE_CONTRACT_VIOLATION"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_RUNTIME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{2,255}\Z", re.ASCII)


class Meeting722TaskwiseContractViolationV1(RuntimeError):
    """Closed contract failure that carries no private benchmark content."""

    finding_code = CONTRACT_VIOLATION

    def __init__(self) -> None:
        super().__init__(CONTRACT_VIOLATION)


@dataclass(frozen=True, slots=True)
class Meeting722TaskwiseContractV1:
    """Content-free proof for one fully verified taskwise stream prefix."""

    protocol_id: str
    arm: Literal["control", "online"]
    run_id: str
    stream_id: str
    verified_task_count: int
    completion_count: int
    core_job_count: int
    core_artifact_count: int
    task_bindings: tuple[dict[str, object], ...]
    schema_version: Literal["meeting_722_taskwise_contract_v1"] = (
        "meeting_722_taskwise_contract_v1"
    )

    @property
    def receipt_sha256(self) -> str:
        return hashlib.sha256(_canonical_bytes(self._body())).hexdigest()

    def _body(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "contract_id": CONTRACT_ID,
            "protocol_id": self.protocol_id,
            "arm": self.arm,
            "run_id": self.run_id,
            "stream_id": self.stream_id,
            "verified_task_count": self.verified_task_count,
            "completion_count": self.completion_count,
            "core_job_count": self.core_job_count,
            "core_artifact_count": self.core_artifact_count,
            "passed": True,
            "finding_codes": [],
            "task_bindings": list(self.task_bindings),
        }

    def to_public_dict(self) -> dict[str, object]:
        payload = self._body()
        payload["receipt_sha256"] = self.receipt_sha256
        return payload


def issue_meeting722_taskwise_contract_v1(
    *,
    protocol_id: str,
    arm: Literal["control", "online"],
    run_id: str,
    stream_id: str,
    expected_task_uids: tuple[str, ...],
    verified_task_count: int,
    public_events: tuple[dict[str, Any], ...],
    private_evaluations: tuple[dict[str, Any], ...],
    run_state: Mapping[str, Any],
) -> Meeting722TaskwiseContractV1:
    """Recompute the complete meeting mechanism for a finalized stream prefix."""

    try:
        return _issue(
            protocol_id=protocol_id,
            arm=arm,
            run_id=run_id,
            stream_id=stream_id,
            expected_task_uids=expected_task_uids,
            verified_task_count=verified_task_count,
            public_events=public_events,
            private_evaluations=private_evaluations,
            run_state=run_state,
        )
    except Meeting722TaskwiseContractViolationV1:
        raise
    except Exception as exc:
        raise Meeting722TaskwiseContractViolationV1 from exc


def verify_persisted_meeting722_taskwise_contract_v1(
    payload: object,
    *,
    expected: Meeting722TaskwiseContractV1,
) -> Meeting722TaskwiseContractV1:
    """Reject caller assertions and compare a receipt with recomputed evidence."""

    if type(expected) is not Meeting722TaskwiseContractV1:
        raise TypeError("expected must be an exact meeting contract receipt")
    if type(payload) is not dict or payload != expected.to_public_dict():
        raise Meeting722TaskwiseContractViolationV1
    return expected


def _issue(
    *,
    protocol_id: str,
    arm: str,
    run_id: str,
    stream_id: str,
    expected_task_uids: tuple[str, ...],
    verified_task_count: int,
    public_events: tuple[dict[str, Any], ...],
    private_evaluations: tuple[dict[str, Any], ...],
    run_state: Mapping[str, Any],
) -> Meeting722TaskwiseContractV1:
    expected_protocol = (
        "taskwise_online_evolution_v1" if arm == "online" else "repeated_session_control_v1"
    )
    if (
        arm not in {"control", "online"}
        or protocol_id != expected_protocol
        or _RUNTIME_ID.fullmatch(run_id) is None
        or _RUNTIME_ID.fullmatch(stream_id) is None
        or not isinstance(expected_task_uids, tuple)
        or type(verified_task_count) is not int
        or not 1 <= verified_task_count <= len(expected_task_uids)
        or any(_SHA256.fullmatch(uid) is None for uid in expected_task_uids)
        or len(expected_task_uids) != len(set(expected_task_uids))
        or not isinstance(public_events, tuple)
        or not isinstance(private_evaluations, tuple)
    ):
        raise Meeting722TaskwiseContractViolationV1

    expected_events_per_task = 5 if arm == "online" else 3
    expected_updates = verified_task_count * 2 if arm == "online" else 0
    expected_completions = verified_task_count * 3
    if (
        len(public_events) != verified_task_count * expected_events_per_task
        or len(private_evaluations) != expected_completions
        or not _exact_int(run_state.get("completion_count"), expected_completions)
        or not _exact_int(run_state.get("session_attempt_count"), expected_completions)
        or not _exact_int(run_state.get("update_count"), expected_updates)
        or not _exact_int(run_state.get("core_job_count"), expected_updates)
        or not _exact_int(run_state.get("core_artifact_count"), expected_updates)
        or not _exact_int(run_state.get("context_binding_violation_count"), 0)
        or run_state.get("pending_invocation") is not None
        or run_state.get("failure") is not None
    ):
        raise Meeting722TaskwiseContractViolationV1

    issued_sessions = run_state.get("issued_session_ids")
    registered_jobs = run_state.get("registered_core_job_ids")
    registered_artifacts = run_state.get("registered_core_artifact_ids")
    if (
        type(issued_sessions) is not list
        or len(issued_sessions) != expected_completions
        or len(set(issued_sessions)) != expected_completions
        or type(registered_jobs) is not list
        or type(registered_artifacts) is not list
        or len(registered_jobs) != expected_updates
        or len(set(registered_jobs)) != expected_updates
        or len(registered_artifacts) != expected_updates
        or len(set(registered_artifacts)) != expected_updates
    ):
        raise Meeting722TaskwiseContractViolationV1

    private_by_position: dict[tuple[int, int], dict[str, Any]] = {}
    for row in private_evaluations:
        if type(row) is not dict:
            raise Meeting722TaskwiseContractViolationV1
        position = (row.get("task_ordinal"), row.get("round_index"))
        if (
            type(position[0]) is not int
            or type(position[1]) is not int
            or position in private_by_position
        ):
            raise Meeting722TaskwiseContractViolationV1
        private_by_position[position] = row

    event_offset = 0
    carry_memory: dict[str, str] | None = None
    task_bindings: list[dict[str, object]] = []
    observed_jobs: list[str] = []
    observed_artifacts: list[str] = []
    observed_sessions: list[str] = []
    for task_index in range(verified_task_count):
        task_uid = expected_task_uids[task_index]
        event_count = expected_events_per_task
        task_events = public_events[event_offset : event_offset + event_count]
        event_offset += event_count
        task_binding, carry_memory = _verify_task(
            arm=arm,
            stream_id=stream_id,
            task_uid=task_uid,
            task_index=task_index,
            events=task_events,
            private_by_position=private_by_position,
            incoming_memory=carry_memory,
        )
        task_bindings.append(task_binding)
        observed_sessions.extend(
            str(item["session_id"]) for item in task_binding["round_bindings"]
        )
        observed_jobs.extend(
            str(item["evolution_job_id"])
            for item in task_binding["round_bindings"]
            if item["evolution_job_id"] is not None
        )
        observed_artifacts.extend(
            str(item["artifact_id"])
            for item in task_binding["round_bindings"]
            if item["artifact_id"] is not None
        )

    if (
        observed_sessions != issued_sessions
        or observed_jobs != registered_jobs
        or observed_artifacts != registered_artifacts
    ):
        raise Meeting722TaskwiseContractViolationV1
    if arm == "control":
        if (
            carry_memory is not None
            or run_state.get("carry_memory") is not None
            or run_state.get("active_memory") is not None
            or run_state.get("predecessor_artifact_ref") is not None
        ):
            raise Meeting722TaskwiseContractViolationV1
    elif (
        carry_memory is None
        or run_state.get("carry_memory") != carry_memory
        or run_state.get("active_memory") is not None
        or run_state.get("predecessor_artifact_ref") != carry_memory
    ):
        raise Meeting722TaskwiseContractViolationV1

    return Meeting722TaskwiseContractV1(
        protocol_id=protocol_id,
        arm=arm,
        run_id=run_id,
        stream_id=stream_id,
        verified_task_count=verified_task_count,
        completion_count=expected_completions,
        core_job_count=expected_updates,
        core_artifact_count=expected_updates,
        task_bindings=tuple(task_bindings),
    )


def _verify_task(
    *,
    arm: str,
    stream_id: str,
    task_uid: str,
    task_index: int,
    events: tuple[dict[str, Any], ...],
    private_by_position: Mapping[tuple[int, int], dict[str, Any]],
    incoming_memory: dict[str, str] | None,
) -> tuple[dict[str, object], dict[str, str] | None]:
    expected_kinds = (
        ("completion", "core_update", "completion", "core_update", "completion")
        if arm == "online"
        else ("completion", "completion", "completion")
    )
    if tuple(event.get("kind") for event in events) != expected_kinds:
        raise Meeting722TaskwiseContractViolationV1
    completions = tuple(event for event in events if event.get("kind") == "completion")
    updates = tuple(event for event in events if event.get("kind") == "core_update")
    if len(completions) != 3 or len(updates) != (2 if arm == "online" else 0):
        raise Meeting722TaskwiseContractViolationV1

    sessions: list[str] = []
    round_bindings: list[dict[str, object]] = []
    current_memory = incoming_memory
    for round_index, completion in enumerate(completions):
        private = private_by_position.get((task_index, round_index))
        if (
            type(private) is not dict
            or not _exact_int(completion.get("task_ordinal"), task_index)
            or not _exact_int(completion.get("round_index"), round_index)
            or completion.get("schema_version") != "taskwise_online_public_event_v1"
            or completion.get("arm") != arm
            or completion.get("task_uid") != task_uid
            or completion.get("stream_id") != stream_id
            or private.get("task_uid") != task_uid
            or not _exact_int(private.get("task_ordinal"), task_index)
            or not _exact_int(private.get("round_index"), round_index)
            or private.get("arm") != arm
            or private.get("context_binding") != completion.get("context_binding")
            or (arm == "control" and private.get("trajectory") is not None)
            or (arm == "online" and round_index == 2 and private.get("trajectory") is not None)
        ):
            raise Meeting722TaskwiseContractViolationV1
        session_id = completion.get("session_id")
        if type(session_id) is not str or _RUNTIME_ID.fullmatch(session_id) is None:
            raise Meeting722TaskwiseContractViolationV1
        sessions.append(session_id)
        memory = _memory_reference(completion.get("memory"))
        if memory != current_memory or private.get("memory") != completion.get("memory"):
            raise Meeting722TaskwiseContractViolationV1
        context = taskwise_context_binding_receipt_from_public_dict(
            completion.get("context_binding")
        )
        if (
            not context.passed
            or context.session_id != session_id
            or context.expected_memory_artifact_id
            != (None if current_memory is None else current_memory["core_artifact_id"])
            or context.actual_resolved_artifact_id
            != (None if current_memory is None else current_memory["core_artifact_id"])
            or context.expected_memory_sha256
            != (None if current_memory is None else current_memory["resolved_memory_sha256"])
            or context.actual_injected_memory_sha256
            != (None if current_memory is None else current_memory["resolved_memory_sha256"])
            or context.context_resolution_digest
            != (None if current_memory is None else current_memory["context_resolution_digest"])
        ):
            raise Meeting722TaskwiseContractViolationV1

        update = None if round_index == 0 or arm == "control" else updates[round_index - 1]
        round_bindings.append(
            {
                "task_uid": task_uid,
                "task_index": task_index,
                "stream_id": stream_id,
                "round_index": round_index,
                "session_id": session_id,
                "predecessor_artifact_id": (
                    (None if current_memory is None else current_memory["core_artifact_id"])
                    if update is None
                    else update.get("predecessor_artifact_id")
                ),
                "resolved_artifact_id": (
                    None if current_memory is None else current_memory["core_artifact_id"]
                ),
                "expected_memory_sha256": (
                    None if current_memory is None else current_memory["resolved_memory_sha256"]
                ),
                "actual_injected_memory_sha256": context.actual_injected_memory_sha256,
                "evolution_job_id": (None if update is None else update.get("evolution_job_id")),
                "artifact_id": None if update is None else update.get("artifact_id"),
                "context_resolution_digest": context.context_resolution_digest,
                "global_update_ordinal": (
                    None if update is None else update.get("global_update_ordinal")
                ),
            }
        )
        if round_index < 2 and arm == "online":
            update = updates[round_index]
            current_memory = _verify_update(
                update=update,
                stream_id=stream_id,
                task_uid=task_uid,
                task_index=task_index,
                update_index=round_index + 1,
                prior_memory=current_memory,
                private_by_position=private_by_position,
            )

    if len(sessions) != len(set(sessions)):
        raise Meeting722TaskwiseContractViolationV1
    return (
        {
            "task_uid": task_uid,
            "task_index": task_index,
            "stream_id": stream_id,
            "round_bindings": round_bindings,
        },
        current_memory,
    )


def _verify_update(
    *,
    update: dict[str, Any],
    stream_id: str,
    task_uid: str,
    task_index: int,
    update_index: int,
    prior_memory: dict[str, str] | None,
    private_by_position: Mapping[tuple[int, int], dict[str, Any]],
) -> dict[str, str]:
    trajectories: list[dict[str, Any]] = []
    for round_index in range(update_index):
        private = private_by_position[(task_index, round_index)]
        trajectory = private.get("trajectory")
        if (
            type(trajectory) is not dict
            or trajectory.get("task_uid") != task_uid
            or not _exact_int(trajectory.get("task_index"), task_index)
            or not _exact_int(trajectory.get("round_index"), round_index)
            or trajectory.get("session_id") != private.get("context_binding", {}).get("session_id")
            or type(private.get("correct")) is not bool
        ):
            raise Meeting722TaskwiseContractViolationV1
        signal = taskwise_safe_signal_from_payload(trajectory.get("safe_feedback"))
        signal_codes = {item.value for item in signal.codes}
        if (
            trajectory.get("safe_feedback_digest") != signal.digest
            or (("correct" if private.get("correct") is True else "incorrect") not in signal_codes)
            or (
                ("parse_failure" in signal_codes)
                != (private.get("official_parse_status") == "no_uppercase")
            )
            or (
                ("format_violation" in signal_codes)
                != (private.get("strict_parse_status") == "strict_format_mismatch")
            )
        ):
            raise Meeting722TaskwiseContractViolationV1
        trajectories.append(trajectory)

    trajectory_ids = tuple(item.get("trajectory_id") for item in trajectories)
    trajectory_digest = hashlib.sha256(_canonical_bytes(trajectories)).hexdigest()
    feedback_payloads = [item["safe_feedback"] for item in trajectories]
    safe_feedback_digest = hashlib.sha256(_canonical_bytes(feedback_payloads)).hexdigest()
    output = _memory_reference(update.get("output_memory"))
    if (
        output is None
        or update.get("task_uid") != task_uid
        or update.get("stream_id") != stream_id
        or update.get("schema_version") != "taskwise_online_public_event_v1"
        or update.get("arm") != "online"
        or not _exact_int(update.get("task_ordinal"), task_index)
        or not _exact_int(update.get("update_index"), update_index)
        or not _exact_int(update.get("source_round_index"), update_index - 1)
        or not _exact_int(
            update.get("global_update_ordinal"),
            task_index * 2 + update_index,
        )
        or update.get("predecessor_artifact_id")
        != (None if prior_memory is None else prior_memory["core_artifact_id"])
        or update.get("predecessor_memory_sha256")
        != (None if prior_memory is None else prior_memory["resolved_memory_sha256"])
        or update.get("input_memory_sha256")
        != (None if prior_memory is None else prior_memory["resolved_memory_sha256"])
        or tuple(update.get("trajectory_ids", ())) != trajectory_ids
        or update.get("trajectory_digest") != trajectory_digest
        or update.get("safe_feedback_digest") != safe_feedback_digest
        or update.get("evolution_job_id") != update.get("core_job_id")
        or update.get("artifact_id") != output["core_artifact_id"]
        or update.get("context_resolution_digest") != output["context_resolution_digest"]
        or update.get("core_job_state") != "COMPLETED"
    ):
        raise Meeting722TaskwiseContractViolationV1
    return output


def _memory_reference(value: object) -> dict[str, str] | None:
    if value is None:
        return None
    expected_keys = {
        "core_artifact_id",
        "artifact_payload_sha256",
        "context_resolution_digest",
        "resolved_memory_sha256",
    }
    if type(value) is not dict or set(value) != expected_keys:
        raise Meeting722TaskwiseContractViolationV1
    if (
        type(value["core_artifact_id"]) is not str
        or not value["core_artifact_id"]
        or "/" in value["core_artifact_id"]
        or "\\" in value["core_artifact_id"]
        or any(
            _SHA256.fullmatch(value[key]) is None for key in expected_keys - {"core_artifact_id"}
        )
    ):
        raise Meeting722TaskwiseContractViolationV1
    return dict(value)


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


def _exact_int(value: object, expected: int) -> bool:
    return type(value) is int and value == expected


__all__ = [
    "CONTRACT_ID",
    "CONTRACT_VIOLATION",
    "Meeting722TaskwiseContractV1",
    "Meeting722TaskwiseContractViolationV1",
    "issue_meeting722_taskwise_contract_v1",
    "verify_persisted_meeting722_taskwise_contract_v1",
]
