"""Ledger-derived formal state machine for Temperature full-evolve v1.

The state machine has no model or service startup behavior.  It validates the
only legal experiment order, derives restart work from fsync ledger evidence,
bridges the actual three-job Core coordinator, and atomically publishes a
content-free status snapshot for the ten-minute monitor.
"""

from __future__ import annotations

import os
import re
import stat
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from openevo_chembench.supervised_transfer_v1.common import (
    canonical_pretty_json_bytes,
)
from openevo_chembench.temperature_full_evolve_v1.config import (
    BATCH_SIZE,
    TARGETS,
    TEST_COUNT,
    TRAIN_COUNT,
)
from openevo_chembench.temperature_full_evolve_v1.core_evolution import (
    CoreEvolutionStateV1,
    PreparedCoreBatchV1,
    TemperatureCoreEvolutionCoordinatorV1,
)
from openevo_chembench.temperature_full_evolve_v1.ledger import (
    TemperatureExperimentLedgerV1,
)

STATUS_SCHEMA = "TemperatureFullEvolveControllerStatusV1"
CONTROLLER_CHECKPOINT_SCHEMA = "TemperatureFullEvolveControllerCheckpointV1"
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,191}\Z", re.ASCII)
_BATCH_TOKEN = re.compile(r"(?:^|-)b(?P<batch>0[1-4])(?:-|$)", re.ASCII)
_SEMANTIC_KINDS = frozenset(
    {
        "RUN_CREATED",
        "SPLIT_FROZEN",
        "BATCH_PRE_CLOSED",
        "REFLECTOR_ACCEPTED",
        "EVIDENCE_VALIDATED",
        "TARGET_JOB_COMPLETED",
        "BATCH_ARTIFACT_SET_COMMITTED",
        "BATCH_POST_CLOSED",
        "FINAL_STATE_FROZEN",
        "EVOLVED_TEST_CLOSED",
        "BASELINE_TEST_CLOSED",
        "AUDIT_CLOSED",
    }
)


class TemperatureControllerError(RuntimeError):
    """Content-free invalid protocol transition or status finding."""

    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


class ControllerActionKindV1(StrEnum):
    CREATE_RUN = "create_run"
    FREEZE_SPLIT = "freeze_split"
    RECOVER_UNRESOLVED_CALL = "recover_unresolved_call"
    RUN_TRAIN_PRE = "run_train_pre"
    CLOSE_TRAIN_PRE = "close_train_pre"
    RUN_REFLECTOR = "run_reflector"
    FINALIZE_REFLECTOR = "finalize_reflector"
    VALIDATE_EVIDENCE = "validate_evidence"
    RUN_CORE_JOBS = "run_core_jobs"
    COMMIT_BATCH_ARTIFACT_SET = "commit_batch_artifact_set"
    RUN_TRAIN_POST = "run_train_post"
    CLOSE_TRAIN_POST = "close_train_post"
    FREEZE_FINAL_STATE = "freeze_final_state"
    RUN_EVOLVED_TEST = "run_evolved_test"
    CLOSE_EVOLVED_TEST = "close_evolved_test"
    RUN_BASELINE_TEST = "run_baseline_test"
    CLOSE_BASELINE_TEST = "close_baseline_test"
    CLOSE_AUDIT = "close_audit"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class ControllerActionV1:
    kind: ControllerActionKindV1
    phase: str
    batch_index: int | None
    completed_items: int
    expected_items: int
    unresolved_logical_call_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.kind) is not ControllerActionKindV1
            or not self.phase
            or (self.batch_index is not None and not 1 <= self.batch_index <= 4)
            or not 0 <= self.completed_items <= self.expected_items
            or tuple(sorted(set(self.unresolved_logical_call_ids)))
            != self.unresolved_logical_call_ids
        ):
            raise ValueError("controller action is invalid")


@dataclass(frozen=True, slots=True)
class _ExpectedSemanticEventV1:
    kind: str
    batch_index: int | None = None
    target_id: str | None = None


@dataclass(frozen=True, slots=True)
class ControllerAggregateCheckpointV1:
    run_id: str
    ledger_head_sha256: str
    ledger_event_count: int
    failures: int
    retries: int
    recoveries: int


class TemperatureExperimentControllerV1:
    """Strict event-prefix controller with atomic aggregate monitoring state."""

    def __init__(
        self,
        *,
        run_id: str,
        ledger: TemperatureExperimentLedgerV1,
        status_path: Path,
    ) -> None:
        if _RUN_ID.fullmatch(run_id) is None:
            raise ValueError("formal run ID is invalid")
        if not isinstance(ledger, TemperatureExperimentLedgerV1):
            raise TypeError("controller requires the formal experiment ledger")
        if not isinstance(status_path, Path) or not status_path.is_absolute():
            raise TypeError("status path must be absolute")
        self._run_id = run_id
        self._ledger = ledger
        self._status_path = status_path
        self._validate_semantic_prefix()

    @property
    def action(self) -> ControllerActionV1:
        self._validate_semantic_prefix()
        if any(event["kind"] == "RUN_FAILED" for event in self._ledger.events):
            raise TemperatureControllerError("CONTROLLER_RUN_ALREADY_FAILED")
        expected = self._next_semantic_event()
        unresolved = self._unresolved_claims()
        if unresolved:
            return ControllerActionV1(
                kind=ControllerActionKindV1.RECOVER_UNRESOLVED_CALL,
                phase=self._phase_for_claims(unresolved),
                batch_index=self._batch_for_claims(unresolved),
                completed_items=0,
                expected_items=len(unresolved),
                unresolved_logical_call_ids=unresolved,
            )
        if expected is None:
            return ControllerActionV1(
                kind=ControllerActionKindV1.COMPLETE,
                phase="complete",
                batch_index=None,
                completed_items=1,
                expected_items=1,
            )
        return self._action_for_expected(expected)

    def append_protocol_event(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Append exactly the currently admissible semantic boundary."""

        if kind not in _SEMANTIC_KINDS or type(payload) is not dict:
            raise TemperatureControllerError("CONTROLLER_EVENT_INVALID")
        expected = self._next_semantic_event()
        if expected is None or kind != expected.kind:
            raise TemperatureControllerError("CONTROLLER_EVENT_OUT_OF_ORDER")
        _require_expected_payload_identity(payload, expected)
        action = self.action
        allowed_actions = {
            "RUN_CREATED": {ControllerActionKindV1.CREATE_RUN},
            "SPLIT_FROZEN": {ControllerActionKindV1.FREEZE_SPLIT},
            "BATCH_PRE_CLOSED": {ControllerActionKindV1.CLOSE_TRAIN_PRE},
            "REFLECTOR_ACCEPTED": {ControllerActionKindV1.FINALIZE_REFLECTOR},
            "EVIDENCE_VALIDATED": {ControllerActionKindV1.VALIDATE_EVIDENCE},
            "TARGET_JOB_COMPLETED": {ControllerActionKindV1.RUN_CORE_JOBS},
            "BATCH_ARTIFACT_SET_COMMITTED": {
                ControllerActionKindV1.COMMIT_BATCH_ARTIFACT_SET
            },
            "BATCH_POST_CLOSED": {ControllerActionKindV1.CLOSE_TRAIN_POST},
            "FINAL_STATE_FROZEN": {ControllerActionKindV1.FREEZE_FINAL_STATE},
            "EVOLVED_TEST_CLOSED": {ControllerActionKindV1.CLOSE_EVOLVED_TEST},
            "BASELINE_TEST_CLOSED": {ControllerActionKindV1.CLOSE_BASELINE_TEST},
            "AUDIT_CLOSED": {ControllerActionKindV1.CLOSE_AUDIT},
        }
        if action.kind not in allowed_actions[kind]:
            raise TemperatureControllerError("CONTROLLER_BOUNDARY_NOT_READY")
        event = self._ledger.append(kind, payload)
        self.write_status()
        return event

    def write_status(self) -> dict[str, object]:
        """Atomically publish the exact closed aggregate monitor schema."""

        action = self.action
        events = self._ledger.events
        claims = [event for event in events if event["kind"] == "CALL_CLAIMED"]
        reflector_claims = [
            event
            for event in claims
            if event["payload"].get("phase") == "train_reflector"
        ]
        candidate_claims = [event for event in claims if event not in reflector_claims]
        no_completion = [
            event for event in events if event["kind"] == "CALL_NO_COMPLETION_FAILURE"
        ]
        unresolved = self._unresolved_claims()
        now = _utc_now()
        last_progress = (
            _utc_seconds(str(events[-1]["recorded_at_utc"])) if events else now
        )
        payload: dict[str, object] = {
            "schema_version": STATUS_SCHEMA,
            "run_id": self._run_id,
            "revision": len(events),
            "phase": action.phase.upper(),
            "batch_index": action.batch_index,
            "item_ordinal": None if action.completed_items == 0 else action.completed_items,
            "accepted_completions": len(
                {event["payload"]["logical_call_id"] for event in events if event["kind"] == "CALL_ACCEPTED"}
            ),
            "candidate_calls": len(
                {event["payload"]["logical_call_id"] for event in candidate_claims}
            ),
            "reflector_calls": len(
                {event["payload"]["logical_call_id"] for event in reflector_claims}
            ),
            "core_jobs": sum(event["kind"] == "TARGET_JOB_COMPLETED" for event in events),
            "active_lease": bool(unresolved),
            "staged_side_effects": len(unresolved),
            "failed_side_effects": len(no_completion),
            "last_progress_at_utc": last_progress,
            "progress_counter": len(events),
            "runner_pid": os.getpid(),
            "updated_at_utc": now,
        }
        _atomic_private_json(self._status_path, payload)
        self._write_aggregate_checkpoint()
        return payload

    def _write_aggregate_checkpoint(self) -> None:
        events = self._ledger.events
        claims = [event for event in events if event["kind"] == "CALL_CLAIMED"]
        logical = {str(event["payload"].get("logical_call_id")) for event in claims}
        payload = {
            "schema_version": CONTROLLER_CHECKPOINT_SCHEMA,
            "run_id": self._run_id,
            "ledger_head_sha256": self._ledger.head_digest,
            "ledger_event_count": len(events),
            "failures": sum(
                event["kind"] in {"CALL_NO_COMPLETION_FAILURE", "INCIDENT_OPENED"}
                for event in events
            ),
            "retries": len(claims) - len(logical),
            "recoveries": sum(event["kind"] == "RECOVERY_COMPLETED" for event in events),
        }
        _atomic_private_json(self._status_path.with_name("controller_checkpoint.json"), payload)

    def _action_for_expected(self, expected: _ExpectedSemanticEventV1) -> ControllerActionV1:
        batch = expected.batch_index
        if expected.kind == "RUN_CREATED":
            return _action(ControllerActionKindV1.CREATE_RUN, "initialization")
        if expected.kind == "SPLIT_FROZEN":
            return _action(ControllerActionKindV1.FREEZE_SPLIT, "preflight")
        if expected.kind == "BATCH_PRE_CLOSED":
            count = self._candidate_evaluation_count("train-pre", batch)
            kind = (
                ControllerActionKindV1.CLOSE_TRAIN_PRE
                if count == BATCH_SIZE
                else ControllerActionKindV1.RUN_TRAIN_PRE
            )
            return _action(kind, "train_pre", batch, count, BATCH_SIZE)
        if expected.kind == "REFLECTOR_ACCEPTED":
            count = self._reflector_acceptance_count(batch)
            kind = (
                ControllerActionKindV1.FINALIZE_REFLECTOR
                if count == 1
                else ControllerActionKindV1.RUN_REFLECTOR
            )
            return _action(kind, "train_reflector", batch, count, 1)
        if expected.kind == "EVIDENCE_VALIDATED":
            return _action(ControllerActionKindV1.VALIDATE_EVIDENCE, "evidence", batch)
        if expected.kind == "TARGET_JOB_COMPLETED":
            completed = self._completed_core_targets(batch)
            return _action(
                ControllerActionKindV1.RUN_CORE_JOBS,
                "core_evolution",
                batch,
                len(completed),
                3,
            )
        if expected.kind == "BATCH_ARTIFACT_SET_COMMITTED":
            return _action(
                ControllerActionKindV1.COMMIT_BATCH_ARTIFACT_SET,
                "core_commit",
                batch,
                3,
                3,
            )
        if expected.kind == "BATCH_POST_CLOSED":
            count = self._candidate_evaluation_count("train-post", batch)
            kind = (
                ControllerActionKindV1.CLOSE_TRAIN_POST
                if count == BATCH_SIZE
                else ControllerActionKindV1.RUN_TRAIN_POST
            )
            return _action(kind, "train_post", batch, count, BATCH_SIZE)
        if expected.kind == "FINAL_STATE_FROZEN":
            return _action(ControllerActionKindV1.FREEZE_FINAL_STATE, "freeze", 4)
        if expected.kind == "EVOLVED_TEST_CLOSED":
            count = self._candidate_evaluation_count("evolved-test", None)
            kind = (
                ControllerActionKindV1.CLOSE_EVOLVED_TEST
                if count == TEST_COUNT
                else ControllerActionKindV1.RUN_EVOLVED_TEST
            )
            return _action(kind, "evolved_test", None, count, TEST_COUNT)
        if expected.kind == "BASELINE_TEST_CLOSED":
            count = self._candidate_evaluation_count("baseline-test", None)
            kind = (
                ControllerActionKindV1.CLOSE_BASELINE_TEST
                if count == TEST_COUNT
                else ControllerActionKindV1.RUN_BASELINE_TEST
            )
            return _action(kind, "baseline_test", None, count, TEST_COUNT)
        if expected.kind == "AUDIT_CLOSED":
            return _action(ControllerActionKindV1.CLOSE_AUDIT, "audit")
        raise AssertionError("unknown semantic event")

    def _candidate_evaluation_count(self, phase: str, batch: int | None) -> int:
        claims = _claims_by_logical(self._ledger.events)
        evaluated = {
            str(event["payload"].get("logical_call_id"))
            for event in self._ledger.events
            if event["kind"] == "CALL_EVALUATED"
        }
        values = {
            logical
            for logical in evaluated
            if logical in claims
            and claims[logical]["phase"] == phase
            and _batch_from_logical(logical) == batch
        }
        maximum = BATCH_SIZE if phase.startswith("train-") else TEST_COUNT
        if len(values) > maximum:
            raise TemperatureControllerError("CONTROLLER_CANDIDATE_COUNT_EXCEEDED")
        return len(values)

    def _reflector_acceptance_count(self, batch: int | None) -> int:
        claims = _claims_by_logical(self._ledger.events)
        accepted = {
            str(event["payload"].get("logical_call_id"))
            for event in self._ledger.events
            if event["kind"] == "CALL_ACCEPTED"
        }
        count = sum(
            logical in claims
            and claims[logical]["phase"] == "train_reflector"
            and _batch_from_logical(logical) == batch
            for logical in accepted
        )
        if count > 1:
            raise TemperatureControllerError("CONTROLLER_REFLECTOR_COUNT_EXCEEDED")
        return count

    def _completed_core_targets(self, batch: int | None) -> tuple[str, ...]:
        return tuple(
            str(event["payload"].get("target_id"))
            for event in self._ledger.events
            if event["kind"] == "TARGET_JOB_COMPLETED"
            and event["payload"].get("batch_index") == batch
        )

    def _unresolved_claims(self) -> tuple[str, ...]:
        accepted = {
            str(event["payload"].get("logical_call_id"))
            for event in self._ledger.events
            if event["kind"] == "CALL_ACCEPTED"
        }
        terminal_calls = {
            str(event["payload"].get("call_id"))
            for event in self._ledger.events
            if event["kind"] == "CALL_NO_COMPLETION_FAILURE"
        }
        latest = _claims_by_logical(self._ledger.events)
        return tuple(
            sorted(
                logical
                for logical, claim in latest.items()
                if logical not in accepted and claim["call_id"] not in terminal_calls
            )
        )

    def _phase_for_claims(self, logical: tuple[str, ...]) -> str:
        claims = _claims_by_logical(self._ledger.events)
        phases = {str(claims[value]["phase"]).replace("-", "_") for value in logical}
        return phases.pop() if len(phases) == 1 else "recovery"

    def _batch_for_claims(self, logical: tuple[str, ...]) -> int | None:
        batches = {_batch_from_logical(value) for value in logical}
        return batches.pop() if len(batches) == 1 else None

    def _next_semantic_event(self) -> _ExpectedSemanticEventV1 | None:
        actual = [event for event in self._ledger.events if event["kind"] in _SEMANTIC_KINDS]
        expected = _expected_semantic_events()
        return None if len(actual) == len(expected) else expected[len(actual)]

    def _validate_semantic_prefix(self) -> None:
        actual = [event for event in self._ledger.events if event["kind"] in _SEMANTIC_KINDS]
        expected = _expected_semantic_events()
        if len(actual) > len(expected):
            raise TemperatureControllerError("CONTROLLER_SEMANTIC_EVENT_OVERFLOW")
        for event, contract in zip(actual, expected, strict=False):
            if event["kind"] != contract.kind:
                raise TemperatureControllerError("CONTROLLER_SEMANTIC_EVENT_OUT_OF_ORDER")
            _require_expected_payload_identity(event["payload"], contract)


def execute_and_commit_core_batch_v1(
    *,
    controller: TemperatureExperimentControllerV1,
    coordinator: TemperatureCoreEvolutionCoordinatorV1,
    prepared: PreparedCoreBatchV1,
    forbidden_questions: tuple[str, ...],
) -> CoreEvolutionStateV1:
    """Use the actual Core run-once path, then ledger and commit its three jobs."""

    if (
        type(controller) is not TemperatureExperimentControllerV1
        or type(coordinator) is not TemperatureCoreEvolutionCoordinatorV1
        or type(prepared) is not PreparedCoreBatchV1
    ):
        raise TypeError("Core controller bridge inputs are invalid")
    receipt = coordinator.execute_prepared_jobs(prepared=prepared)
    already = set(controller._completed_core_targets(prepared.batch_index))
    for job in prepared.jobs:
        if job.target_id in already:
            continue
        controller.append_protocol_event(
            "TARGET_JOB_COMPLETED",
            {
                "batch_index": prepared.batch_index,
                "target_id": job.target_id,
                "method_id": job.method_id,
                "job_id": job.job_id,
                "prepared_batch_sha256": prepared.digest,
                "execution_receipt_sha256": receipt.digest,
            },
        )
    successor = coordinator.commit_completed_batch(
        prepared=prepared,
        forbidden_questions=forbidden_questions,
    )
    if controller.action.kind is ControllerActionKindV1.COMMIT_BATCH_ARTIFACT_SET:
        controller.append_protocol_event(
            "BATCH_ARTIFACT_SET_COMMITTED",
            {
                "batch_index": prepared.batch_index,
                "predecessor_state_sha256": prepared.predecessor_state_sha256,
                "state_sha256": successor.digest,
                "artifact_total_utf8_bytes": successor.total_artifact_utf8_bytes,
                "artifact_count": 3,
                "core_job_count": 3,
            },
        )
    return successor


def _expected_semantic_events() -> tuple[_ExpectedSemanticEventV1, ...]:
    values = [
        _ExpectedSemanticEventV1("RUN_CREATED"),
        _ExpectedSemanticEventV1("SPLIT_FROZEN"),
    ]
    for batch in range(1, TRAIN_COUNT // BATCH_SIZE + 1):
        values.extend(
            (
                _ExpectedSemanticEventV1("BATCH_PRE_CLOSED", batch),
                _ExpectedSemanticEventV1("REFLECTOR_ACCEPTED", batch),
                _ExpectedSemanticEventV1("EVIDENCE_VALIDATED", batch),
                *(
                    _ExpectedSemanticEventV1("TARGET_JOB_COMPLETED", batch, target)
                    for target in TARGETS
                ),
                _ExpectedSemanticEventV1("BATCH_ARTIFACT_SET_COMMITTED", batch),
                _ExpectedSemanticEventV1("BATCH_POST_CLOSED", batch),
            )
        )
    values.extend(
        (
            _ExpectedSemanticEventV1("FINAL_STATE_FROZEN", 4),
            _ExpectedSemanticEventV1("EVOLVED_TEST_CLOSED"),
            _ExpectedSemanticEventV1("BASELINE_TEST_CLOSED"),
            _ExpectedSemanticEventV1("AUDIT_CLOSED"),
        )
    )
    return tuple(values)


def _require_expected_payload_identity(
    payload: Mapping[str, Any], expected: _ExpectedSemanticEventV1
) -> None:
    if not isinstance(payload, Mapping):
        raise TemperatureControllerError("CONTROLLER_EVENT_PAYLOAD_INVALID")
    if expected.batch_index is not None and payload.get("batch_index") != expected.batch_index:
        raise TemperatureControllerError("CONTROLLER_EVENT_BATCH_MISMATCH")
    if expected.target_id is not None and payload.get("target_id") != expected.target_id:
        raise TemperatureControllerError("CONTROLLER_EVENT_TARGET_MISMATCH")


def _claims_by_logical(events: tuple[dict[str, Any], ...]) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for event in events:
        if event["kind"] == "CALL_CLAIMED":
            values[str(event["payload"]["logical_call_id"])] = dict(event["payload"])
    return values


def _batch_from_logical(logical_call_id: str) -> int | None:
    match = _BATCH_TOKEN.search(logical_call_id)
    return None if match is None else int(match.group("batch"))


def _action(
    kind: ControllerActionKindV1,
    phase: str,
    batch: int | None = None,
    completed: int = 0,
    expected: int = 1,
) -> ControllerActionV1:
    return ControllerActionV1(
        kind=kind,
        phase=phase,
        batch_index=batch,
        completed_items=completed,
        expected_items=expected,
    )


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_seconds(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise TemperatureControllerError("CONTROLLER_EVENT_TIME_INVALID") from exc
    if parsed.tzinfo is None:
        raise TemperatureControllerError("CONTROLLER_EVENT_TIME_INVALID")
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_private_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    parent = path.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or stat.S_IMODE(parent.st_mode) != 0o700
        or parent.st_uid != os.geteuid()
    ):
        raise TemperatureControllerError("CONTROLLER_STATUS_DIRECTORY_UNSAFE")
    encoded = canonical_pretty_json_bytes(payload)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
    ):
        raise TemperatureControllerError("CONTROLLER_STATUS_FILE_UNSAFE")


__all__ = [
    "CONTROLLER_CHECKPOINT_SCHEMA",
    "STATUS_SCHEMA",
    "ControllerActionKindV1",
    "ControllerActionV1",
    "TemperatureControllerError",
    "TemperatureExperimentControllerV1",
    "execute_and_commit_core_batch_v1",
]
