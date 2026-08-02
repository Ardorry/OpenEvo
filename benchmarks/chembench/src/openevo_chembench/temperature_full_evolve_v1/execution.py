"""Exactly-once Rollout execution for Temperature full-evolve v1.

The module deliberately separates admission from observation.  The controller
thread is the only ledger writer: it fsyncs ``CALL_CLAIMED`` before transport,
submits at most once for that claim, and appends accepted/evaluated records only
after a durable Rollout result has been recovered and validated.  Poll workers
perform read-only HTTP and persistence audits, so bounded parallel Candidate
execution never turns the ledger into a multi-writer resource.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import httpx
from openevo.experiments.clients import RolloutHttpClient
from openevo.rollout.models import SessionStatus, TaskRequest, TaskStatus

from openevo_chembench.chembench4k_models import (
    PrivateChemBench4KTask,
    RenderedChemBench4KPrompt,
)
from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes, sha256_bytes
from openevo_chembench.supervised_transfer_v2.artifacts import (
    CoreResolvedSupervisedContextV2,
)
from openevo_chembench.supervised_transfer_v2.executor import task_request_digest_v2
from openevo_chembench.temperature_full_evolve_v1.candidate import (
    AcceptedCandidateV1,
    CandidateCallPlanV1,
    EvaluatedCandidateV1,
    accept_candidate_completion_v1,
    evaluate_candidate_completion_v1,
    observe_candidate_task_status_v1,
    prepare_candidate_call_v1,
)
from openevo_chembench.temperature_full_evolve_v1.ledger import (
    TemperatureExperimentLedgerV1,
)
from openevo_chembench.temperature_full_evolve_v1.packet import (
    SealedBatchSupervisedPacketV1,
)
from openevo_chembench.temperature_full_evolve_v1.reflector import (
    AcceptedReflectorSynthesisV1,
    ReflectorCallPlanV1,
    TemperatureReflectorError,
    accept_reflector_synthesis_v1,
    build_reflector_rejected_completion_payload_v1,
    is_retryable_reflector_completion_rejection_v1,
    observe_reflector_task_status_v1,
    prepare_reflector_call_v1,
    recover_accepted_reflector_synthesis_v1,
    render_reflector_prompt_v1,
)
from openevo_chembench.temperature_full_evolve_v1.retry_semantics import (
    task_request_retry_semantics_sha256_v1,
)
from openevo_chembench.temperature_full_evolve_v1.runtime_services import (
    DurableNoCompletionEvidenceV1,
    PersistedRolloutResultAuditV1,
    audit_durable_no_completion_v1,
    audit_persisted_rollout_result_v1,
)

MAX_CANDIDATE_WORKERS = 3
DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_MAX_POLL_ATTEMPTS = 2400
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{7,191}\Z", re.ASCII)


class TemperatureFormalExecutionError(RuntimeError):
    """Fail-closed formal transport or recovery finding."""

    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


@runtime_checkable
class FormalExecutionLedgerV1(Protocol):
    @property
    def events(self) -> tuple[dict[str, Any], ...]: ...

    def append(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]: ...

    def accepted_call(self, logical_call_id: str) -> dict[str, Any] | None: ...

    def latest_claim(self, logical_call_id: str) -> dict[str, Any] | None: ...

    def failure_has_no_completion(self, call_id: str) -> bool: ...

    def completion_was_rejected(self, call_id: str) -> bool: ...


@runtime_checkable
class RuntimeServicesIdentityV1(Protocol):
    repository_root: Path
    service_run_id: str
    digest: str

    def require_current(self) -> dict[str, object]: ...


class RolloutClientPortV1(Protocol):
    def submit_task(self, payload: dict[str, Any]) -> str: ...

    def get_task(self, task_id: str) -> dict[str, Any]: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True, repr=False)
class FormalCallEnvelopeV1:
    """Content-redacted common identity for Candidate and Reflector calls."""

    logical_call_id: str
    call_id: str
    task_request: TaskRequest
    task_request_sha256: str
    claim_payload: dict[str, object]

    def __post_init__(self) -> None:
        if (
            _SAFE_ID.fullmatch(self.logical_call_id) is None
            or _SAFE_ID.fullmatch(self.call_id) is None
            or type(self.task_request) is not TaskRequest
            or self.task_request_sha256 != task_request_digest_v2(self.task_request)
            or self.claim_payload.get("logical_call_id") != self.logical_call_id
            or self.claim_payload.get("call_id") != self.call_id
            or self.claim_payload.get("task_id") != self.task_request.task_id
            or self.claim_payload.get("task_request_sha256") != self.task_request_sha256
            or self.claim_payload.get("retry_semantics_sha256")
            != task_request_retry_semantics_sha256_v1(self.task_request)
        ):
            raise ValueError("formal call envelope is invalid")

    def __repr__(self) -> str:
        return "FormalCallEnvelopeV1(<private-task-request>)"

    __str__ = __repr__

    def to_private_checkpoint_bytes(self) -> bytes:
        """Serialize the exact pre-claim request for crash recovery.

        The checkpoint is private because it contains the model instruction.
        Its canonical digest is checked again before an unresolved claim may be
        polled, so a restart cannot substitute a different prompt or Task ID.
        """

        body = {
            "logical_call_id": self.logical_call_id,
            "call_id": self.call_id,
            "task_request": _task_payload(self.task_request),
            "task_request_sha256": self.task_request_sha256,
            "claim_payload": self.claim_payload,
        }
        return canonical_json_bytes(
            {
                "schema_version": "TemperatureFormalCallCheckpointV1",
                "envelope": body,
                "envelope_sha256": sha256_bytes(canonical_json_bytes(body)),
            }
        )

    @classmethod
    def from_private_checkpoint_bytes(cls, payload: bytes) -> FormalCallEnvelopeV1:
        try:
            value = json.loads(payload)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise TemperatureFormalExecutionError(
                "FORMAL_CALL_CHECKPOINT_MALFORMED"
            ) from exc
        if (
            type(value) is not dict
            or set(value) != {"schema_version", "envelope", "envelope_sha256"}
            or value["schema_version"] != "TemperatureFormalCallCheckpointV1"
            or type(value["envelope"]) is not dict
            or value["envelope_sha256"]
            != sha256_bytes(canonical_json_bytes(value["envelope"]))
            or canonical_json_bytes(value) != payload
        ):
            raise TemperatureFormalExecutionError("FORMAL_CALL_CHECKPOINT_INVALID")
        body = value["envelope"]
        if set(body) != {
            "logical_call_id",
            "call_id",
            "task_request",
            "task_request_sha256",
            "claim_payload",
        }:
            raise TemperatureFormalExecutionError("FORMAL_CALL_CHECKPOINT_INVALID")
        try:
            return cls(
                logical_call_id=body["logical_call_id"],
                call_id=body["call_id"],
                task_request=TaskRequest.model_validate(body["task_request"]),
                task_request_sha256=body["task_request_sha256"],
                claim_payload=body["claim_payload"],
            )
        except (TypeError, ValueError) as exc:
            raise TemperatureFormalExecutionError(
                "FORMAL_CALL_CHECKPOINT_INVALID"
            ) from exc

    @classmethod
    def from_candidate(cls, plan: CandidateCallPlanV1) -> FormalCallEnvelopeV1:
        if type(plan) is not CandidateCallPlanV1:
            raise TypeError("Candidate plan must be exact")
        return cls(
            logical_call_id=plan.logical_call_id,
            call_id=plan.call_id,
            task_request=plan.task_request,
            task_request_sha256=plan.task_request_sha256,
            claim_payload=dict(plan.claim_payload),
        )

    @classmethod
    def from_reflector(cls, plan: ReflectorCallPlanV1) -> FormalCallEnvelopeV1:
        if type(plan) is not ReflectorCallPlanV1:
            raise TypeError("Reflector plan must be exact")
        return cls(
            logical_call_id=plan.logical_call_id,
            call_id=plan.call_id,
            task_request=plan.task_request,
            task_request_sha256=plan.task_request_sha256,
            claim_payload=dict(plan.claim_payload),
        )


@dataclass(frozen=True, slots=True, repr=False)
class DurableFormalOutcomeV1:
    """A durable completion or a proven terminal no-completion result."""

    envelope: FormalCallEnvelopeV1
    state: Literal["completion", "terminal_no_completion"]
    status: TaskStatus | None
    audit: PersistedRolloutResultAuditV1
    recovered_after_restart: bool

    def __post_init__(self) -> None:
        if (
            type(self.envelope) is not FormalCallEnvelopeV1
            or type(self.audit) is not PersistedRolloutResultAuditV1
            or (self.state == "completion")
            != (self.status is not None and self.audit.state == "PROVEN_COMPLETE")
            or (self.state == "terminal_no_completion")
            != (self.status is None and self.audit.state == "PROVEN_TERMINAL_NO_COMPLETION")
        ):
            raise ValueError("durable formal outcome is invalid")

    def __repr__(self) -> str:
        return f"DurableFormalOutcomeV1(state={self.state!r}, <private-result>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True)
class _AdmittedCallV1:
    envelope: FormalCallEnvelopeV1
    recovered_after_restart: bool


def _is_exact_successor_claim(
    *,
    predecessor: Mapping[str, object],
    successor: Mapping[str, object],
) -> bool:
    """Bind retry admission to the one intended successor claim."""

    predecessor_attempt = predecessor.get("attempt_number")
    successor_attempt = successor.get("attempt_number")
    logical_call_id = successor.get("logical_call_id")
    if (
        type(predecessor_attempt) is not int
        or type(successor_attempt) is not int
        or type(logical_call_id) is not str
        or successor_attempt != predecessor_attempt + 1
        or not 2 <= successor_attempt <= 3
        or predecessor.get("call_id") != f"{logical_call_id}-a{predecessor_attempt:02d}"
        or successor.get("call_id") != f"{logical_call_id}-a{successor_attempt:02d}"
    ):
        return False
    return all(
        predecessor.get(field) == successor.get(field)
        for field in (
            "logical_call_id",
            "phase",
            "logical_arm",
            "retry_semantics_sha256",
            "service_identity_sha256",
        )
    )


AuditFunction = Callable[..., PersistedRolloutResultAuditV1]
NoCompletionAuditFunction = Callable[..., DurableNoCompletionEvidenceV1]
ClientFactory = Callable[[], RolloutClientPortV1]


class TemperatureFormalExecutorV1:
    """Claim, submit and durably observe calls with a single ledger writer."""

    def __init__(
        self,
        *,
        runtime_services: RuntimeServicesIdentityV1,
        ledger: FormalExecutionLedgerV1,
        client_factory: ClientFactory | None = None,
        audit_function: AuditFunction = audit_persisted_rollout_result_v1,
        no_completion_audit_function: NoCompletionAuditFunction = (
            audit_durable_no_completion_v1
        ),
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        max_poll_attempts: int = DEFAULT_MAX_POLL_ATTEMPTS,
    ) -> None:
        if not isinstance(runtime_services, RuntimeServicesIdentityV1):
            raise TypeError("runtime services identity is invalid")
        if not isinstance(ledger, FormalExecutionLedgerV1):
            raise TypeError("formal execution ledger is invalid")
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, (int, float))
            or poll_interval_seconds < 0
            or isinstance(max_poll_attempts, bool)
            or not isinstance(max_poll_attempts, int)
            or max_poll_attempts < 1
        ):
            raise ValueError("formal poll policy is invalid")
        if _SHA256.fullmatch(runtime_services.digest) is None:
            raise ValueError("runtime service identity digest is invalid")
        self._runtime = runtime_services
        self._ledger = ledger
        self._client_factory = client_factory or (
            lambda: RolloutHttpClient(runtime_services.rollout_url)  # type: ignore[attr-defined]
        )
        self._audit = audit_function
        self._no_completion_audit = no_completion_audit_function
        self._poll_interval = float(poll_interval_seconds)
        self._max_polls = max_poll_attempts

    def run_many_to_durable_terminal(
        self,
        envelopes: Sequence[FormalCallEnvelopeV1],
        *,
        max_workers: int = MAX_CANDIDATE_WORKERS,
    ) -> tuple[DurableFormalOutcomeV1, ...]:
        """Execute calls in bounded chunks while preserving input-order results.

        Claims and submissions are serial.  Only read-only polling/auditing is
        concurrent.  The caller must finalize accepted/evaluated records on the
        same controller thread before admitting later protocol phases.
        """

        values = tuple(envelopes)
        if (
            not values
            or any(type(value) is not FormalCallEnvelopeV1 for value in values)
            or len({value.logical_call_id for value in values}) != len(values)
            or isinstance(max_workers, bool)
            or not isinstance(max_workers, int)
            or not 1 <= max_workers <= MAX_CANDIDATE_WORKERS
        ):
            raise ValueError("formal execution batch is invalid")
        results: list[DurableFormalOutcomeV1] = []
        for offset in range(0, len(values), max_workers):
            chunk = values[offset : offset + max_workers]
            admitted = tuple(self._admit(value) for value in chunk)
            with ThreadPoolExecutor(max_workers=len(admitted)) as pool:
                futures = [pool.submit(self._await_durable_terminal, item) for item in admitted]
                probed: list[DurableFormalOutcomeV1 | Exception] = []
                for future in futures:
                    try:
                        probed.append(future.result())
                    except Exception as exc:  # noqa: BLE001 - preserve started calls
                        probed.append(exc)
            first_error = next((value for value in probed if isinstance(value, Exception)), None)
            for value in probed:
                if isinstance(value, DurableFormalOutcomeV1):
                    self._record_no_completion_if_needed(value)
                    results.append(value)
            if first_error is not None:
                raise first_error
        return tuple(results)

    def _admit(self, envelope: FormalCallEnvelopeV1) -> _AdmittedCallV1:
        if type(envelope) is not FormalCallEnvelopeV1:
            raise TypeError("formal envelope must be exact")
        if envelope.claim_payload.get("service_identity_sha256") != self._runtime.digest:
            raise TemperatureFormalExecutionError("FORMAL_SERVICE_IDENTITY_MISMATCH")
        latest = self._ledger.latest_claim(envelope.logical_call_id)
        already_accepted = self._ledger.accepted_call(envelope.logical_call_id)
        if already_accepted is not None:
            if latest != envelope.claim_payload:
                raise TemperatureFormalExecutionError(
                    "FORMAL_ACCEPTED_CALL_CLAIM_IDENTITY_MISMATCH"
                )
            return _AdmittedCallV1(envelope=envelope, recovered_after_restart=True)
        if latest is not None:
            if latest == envelope.claim_payload:
                if self._ledger.failure_has_no_completion(
                    envelope.call_id
                ) or self._ledger.completion_was_rejected(envelope.call_id):
                    raise TemperatureFormalExecutionError("FORMAL_COMPLETED_CLAIM_CANNOT_RESUBMIT")
                return _AdmittedCallV1(envelope=envelope, recovered_after_restart=True)
            predecessor_call_id = latest.get("call_id")
            if (
                type(predecessor_call_id) is not str
                or not _is_exact_successor_claim(
                    predecessor=latest,
                    successor=envelope.claim_payload,
                )
                or not (
                    self._ledger.failure_has_no_completion(predecessor_call_id)
                    or self._ledger.completion_was_rejected(predecessor_call_id)
                )
            ):
                raise TemperatureFormalExecutionError("FORMAL_CLAIM_IDENTITY_MISMATCH")

        # Health is proven immediately before the irreversible claim/submit pair.
        # Any exception after the fsync claim remains an unresolved owned call and
        # is recovered by task identity; it is never interpreted as permission to
        # issue a replacement model call.
        self._runtime.require_current()
        self._ledger.append("CALL_CLAIMED", dict(envelope.claim_payload))
        client = self._client_factory()
        try:
            submitted = client.submit_task(_task_payload(envelope.task_request))
        except Exception as exc:  # external boundary; outcome may be ambiguous
            raise TemperatureFormalExecutionError("FORMAL_SUBMIT_OUTCOME_UNRESOLVED") from exc
        finally:
            client.close()
        if submitted != envelope.task_request.task_id:
            raise TemperatureFormalExecutionError("FORMAL_SUBMITTED_TASK_ID_MISMATCH")
        return _AdmittedCallV1(envelope=envelope, recovered_after_restart=False)

    def _await_durable_terminal(self, admitted: _AdmittedCallV1) -> DurableFormalOutcomeV1:
        envelope = admitted.envelope
        client: RolloutClientPortV1 | None = None
        live_current = False
        try:
            self._runtime.require_current()
            live_current = True
            client = self._client_factory()
        except Exception:  # noqa: BLE001 - stopped services remain auditable
            live_current = False

        try:
            for poll_index in range(self._max_polls):
                if live_current and client is not None:
                    try:
                        raw = client.get_task(envelope.task_request.task_id)
                        status = TaskStatus.model_validate(raw)
                        if status.task_id != envelope.task_request.task_id:
                            raise TemperatureFormalExecutionError(
                                "FORMAL_POLLED_TASK_ID_MISMATCH"
                            )
                        if status.status == "running":
                            if poll_index + 1 < self._max_polls and self._poll_interval:
                                time.sleep(self._poll_interval)
                            continue
                    except TemperatureFormalExecutionError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - external HTTP boundary
                        if not _is_missing_task(exc):
                            # The durable result is authoritative even if the live
                            # monitoring endpoint was interrupted.
                            live_current = False
                        else:
                            live_current = False
                audit = self._audit(
                    repository_root=self._runtime.repository_root,
                    service_run_id=self._runtime.service_run_id,
                    task_id=envelope.task_request.task_id,
                )
                if audit.state == "PROVEN_COMPLETE":
                    if audit.result is None:
                        raise TemperatureFormalExecutionError(
                            "FORMAL_DURABLE_RESULT_BODY_MISSING"
                        )
                    return DurableFormalOutcomeV1(
                        envelope=envelope,
                        state="completion",
                        status=_task_status_from_session_result(audit.result),
                        audit=audit,
                        recovered_after_restart=admitted.recovered_after_restart,
                    )
                if audit.state == "PROVEN_TERMINAL_NO_COMPLETION":
                    return DurableFormalOutcomeV1(
                        envelope=envelope,
                        state="terminal_no_completion",
                        status=None,
                        audit=audit,
                        recovered_after_restart=admitted.recovered_after_restart,
                    )
                if not live_current:
                    raise TemperatureFormalExecutionError(
                        "FORMAL_CALL_OWNERSHIP_OR_RESULT_AMBIGUOUS"
                    )
                if poll_index + 1 < self._max_polls and self._poll_interval:
                    time.sleep(self._poll_interval)
            raise TemperatureFormalExecutionError("FORMAL_POLL_LIMIT_REACHED_UNRESOLVED")
        finally:
            if client is not None:
                client.close()

    def _record_no_completion_if_needed(self, outcome: DurableFormalOutcomeV1) -> None:
        if outcome.state != "terminal_no_completion":
            return
        envelope = outcome.envelope
        if self._ledger.failure_has_no_completion(envelope.call_id):
            return
        audit = outcome.audit
        dual_proof = self._no_completion_audit(
            repository_root=self._runtime.repository_root,
            service_run_id=self._runtime.service_run_id,
            task_id=envelope.task_request.task_id,
        )
        if dual_proof.state != "PROVEN_NO_COMPLETION":
            raise TemperatureFormalExecutionError(
                "FORMAL_DURABLE_NO_COMPLETION_DUAL_PROOF_AMBIGUOUS"
            )
        payload = {
            "logical_call_id": envelope.logical_call_id,
            "call_id": envelope.call_id,
            "failure_class": "durable_rollout_terminal_no_completion",
            "terminal_task_status": str(audit.terminal_status),
            **dual_proof.ledger_fields,
        }
        self._ledger.append("CALL_NO_COMPLETION_FAILURE", payload)


class _LedgerBeforeCurrentClaimV1:
    """Read view used only to deterministically rebuild an unresolved attempt."""

    def __init__(
        self,
        *,
        ledger: FormalExecutionLedgerV1,
        envelope: FormalCallEnvelopeV1,
    ) -> None:
        self._ledger = ledger
        self._envelope = envelope

    def accepted_call(self, logical_call_id: str) -> dict[str, Any] | None:
        if logical_call_id == self._envelope.logical_call_id:
            return None
        return self._ledger.accepted_call(logical_call_id)

    def latest_claim(self, logical_call_id: str) -> dict[str, Any] | None:
        claims = [
            dict(event["payload"])
            for event in self._ledger.events
            if event.get("kind") == "CALL_CLAIMED"
            and isinstance(event.get("payload"), Mapping)
            and event["payload"].get("logical_call_id") == logical_call_id
            and event["payload"].get("call_id") != self._envelope.call_id
        ]
        return None if not claims else claims[-1]

    def failure_has_no_completion(self, call_id: str) -> bool:
        return self._ledger.failure_has_no_completion(call_id)

    def completion_was_rejected(self, call_id: str) -> bool:
        return self._ledger.completion_was_rejected(call_id)


def recover_existing_candidate_plan_v1(
    *,
    envelope: FormalCallEnvelopeV1,
    task: PrivateChemBench4KTask,
    prompt: RenderedChemBench4KPrompt,
    context: CoreResolvedSupervisedContextV2 | None,
    context_workspace: Path | None,
    phase: Literal["train_pre", "train_post", "evolved_test", "baseline_test"],
    task_ordinal: int,
    batch_index: int | None,
    ledger: FormalExecutionLedgerV1,
    run_id: str,
    service_identity_sha256: str,
) -> CandidateCallPlanV1:
    """Rebuild and revalidate the exact request behind an unresolved claim."""

    if type(envelope) is not FormalCallEnvelopeV1 or not isinstance(
        ledger, FormalExecutionLedgerV1
    ):
        raise TypeError("Candidate plan recovery inputs are invalid")
    current = ledger.latest_claim(envelope.logical_call_id)
    if (
        current != envelope.claim_payload
        or ledger.failure_has_no_completion(envelope.call_id)
        or ledger.completion_was_rejected(envelope.call_id)
    ):
        raise TemperatureFormalExecutionError("CANDIDATE_RECOVERY_CLAIM_INVALID")
    masked = _LedgerBeforeCurrentClaimV1(ledger=ledger, envelope=envelope)
    rebuilt = prepare_candidate_call_v1(
        task=task,
        prompt=prompt,
        context=context,
        context_workspace=context_workspace,
        phase=phase,
        task_ordinal=task_ordinal,
        batch_index=batch_index,
        ledger=masked,
        run_id=run_id,
        service_identity_sha256=service_identity_sha256,
    )
    if FormalCallEnvelopeV1.from_candidate(rebuilt) != envelope:
        raise TemperatureFormalExecutionError("CANDIDATE_RECOVERED_PLAN_MISMATCH")
    return rebuilt


def recover_existing_reflector_plan_v1(
    *,
    envelope: FormalCallEnvelopeV1,
    sealed: SealedBatchSupervisedPacketV1,
    ledger: FormalExecutionLedgerV1,
    run_id: str,
    service_identity_sha256: str,
) -> ReflectorCallPlanV1:
    """Rebuild and revalidate one unresolved batch Reflector request."""

    if type(envelope) is not FormalCallEnvelopeV1 or not isinstance(
        ledger, FormalExecutionLedgerV1
    ):
        raise TypeError("Reflector plan recovery inputs are invalid")
    current = ledger.latest_claim(envelope.logical_call_id)
    if (
        current != envelope.claim_payload
        or ledger.failure_has_no_completion(envelope.call_id)
        or ledger.completion_was_rejected(envelope.call_id)
    ):
        raise TemperatureFormalExecutionError("REFLECTOR_RECOVERY_CLAIM_INVALID")
    masked = _LedgerBeforeCurrentClaimV1(ledger=ledger, envelope=envelope)
    rebuilt = prepare_reflector_call_v1(
        sealed=sealed,
        ledger=masked,
        run_id=run_id,
        service_identity_sha256=service_identity_sha256,
    )
    if (
        FormalCallEnvelopeV1.from_reflector(rebuilt) != envelope
        or rebuilt.prompt != render_reflector_prompt_v1(sealed)
    ):
        raise TemperatureFormalExecutionError("REFLECTOR_RECOVERED_PLAN_MISMATCH")
    return rebuilt


def finalize_candidate_outcome_v1(
    *,
    outcome: DurableFormalOutcomeV1,
    plan: CandidateCallPlanV1,
    task: PrivateChemBench4KTask,
    ledger: TemperatureExperimentLedgerV1,
) -> EvaluatedCandidateV1:
    """Append Candidate acceptance and private evaluation idempotently."""

    if (
        type(outcome) is not DurableFormalOutcomeV1
        or type(plan) is not CandidateCallPlanV1
        or type(task) is not PrivateChemBench4KTask
        or not isinstance(ledger, TemperatureExperimentLedgerV1)
        or outcome.envelope != FormalCallEnvelopeV1.from_candidate(plan)
    ):
        raise TypeError("Candidate finalization inputs are invalid")
    if outcome.state != "completion" or outcome.status is None:
        raise TemperatureFormalExecutionError("CANDIDATE_HAS_NO_ACCEPTABLE_COMPLETION")
    observed = observe_candidate_task_status_v1(outcome.status, plan=plan)
    existing = ledger.accepted_call(plan.logical_call_id)
    if existing is None:
        accepted = accept_candidate_completion_v1(plan=plan, observed=observed, ledger=ledger)
        ledger.append("CALL_ACCEPTED", dict(accepted.call_accepted_payload))
    else:
        expected = {
            "logical_call_id": plan.logical_call_id,
            "call_id": plan.call_id,
            "response": observed.attempt.response,
            "response_sha256": observed.response_sha256,
            "task_result_sha256": observed.task_result_sha256,
            "transcript_sha256": observed.transcript_sha256,
            "completion_identity_sha256": observed.completion_identity_sha256,
            "tool_event_count": 0,
            "tool_policy_validated": True,
        }
        if existing != expected:
            raise TemperatureFormalExecutionError("CANDIDATE_ACCEPTED_RECOVERY_MISMATCH")
        accepted = AcceptedCandidateV1(
            plan=plan,
            observed=observed,
            context_binding_sha256=plan.context_binding_sha256,
            call_accepted_payload=expected,
        )
    evaluated = evaluate_candidate_completion_v1(task=task, accepted=accepted)
    prior_evaluation = _event_payload_for_logical(
        ledger.events,
        kind="CALL_EVALUATED",
        logical_call_id=plan.logical_call_id,
    )
    if prior_evaluation is None:
        ledger.append("CALL_EVALUATED", dict(evaluated.call_evaluated_payload))
    elif prior_evaluation != evaluated.call_evaluated_payload:
        raise TemperatureFormalExecutionError("CANDIDATE_EVALUATION_RECOVERY_MISMATCH")
    return evaluated


def finalize_reflector_outcome_v1(
    *,
    outcome: DurableFormalOutcomeV1,
    plan: ReflectorCallPlanV1,
    sealed: SealedBatchSupervisedPacketV1,
    ledger: TemperatureExperimentLedgerV1,
) -> AcceptedReflectorSynthesisV1:
    """Append the one batch synthesis and its special receipt idempotently."""

    if (
        type(outcome) is not DurableFormalOutcomeV1
        or type(plan) is not ReflectorCallPlanV1
        or type(sealed) is not SealedBatchSupervisedPacketV1
        or not isinstance(ledger, TemperatureExperimentLedgerV1)
        or outcome.envelope != FormalCallEnvelopeV1.from_reflector(plan)
    ):
        raise TypeError("Reflector finalization inputs are invalid")
    if outcome.state != "completion" or outcome.status is None:
        raise TemperatureFormalExecutionError("REFLECTOR_HAS_NO_ACCEPTABLE_COMPLETION")
    existing = ledger.accepted_call(plan.logical_call_id)
    if existing is None:
        observed = observe_reflector_task_status_v1(
            outcome.status,
            expected_task_id=plan.task_request.task_id,
        )
        task_result_sha256 = _task_status_sha256(outcome.status)
        completion_identity_sha256 = sha256_bytes(
            canonical_json_bytes(
                {
                    "task_request_sha256": plan.task_request_sha256,
                    "task_result_sha256": task_result_sha256,
                    "response_sha256": observed.response_sha256,
                    "transcript_sha256": observed.transcript.transcript_sha256,
                }
            )
        )
        try:
            accepted = accept_reflector_synthesis_v1(
                sealed=sealed,
                plan=plan,
                observed=observed,
                ledger=ledger,
                task_result_sha256=task_result_sha256,
                completion_identity_sha256=completion_identity_sha256,
            )
        except TemperatureReflectorError as exc:
            if not is_retryable_reflector_completion_rejection_v1(exc):
                raise
            rejected = build_reflector_rejected_completion_payload_v1(
                plan=plan,
                observed=observed,
                ledger=ledger,
                task_result_sha256=task_result_sha256,
                completion_identity_sha256=completion_identity_sha256,
                rejection=exc,
            )
            prior_rejection = _event_payload_for_call(
                ledger.events,
                kind="CALL_REJECTED_COMPLETION",
                call_id=plan.call_id,
            )
            if prior_rejection is None:
                ledger.append("CALL_REJECTED_COMPLETION", rejected)
            elif prior_rejection != rejected:
                raise TemperatureFormalExecutionError(
                    "REFLECTOR_REJECTION_RECOVERY_MISMATCH"
                ) from exc
            raise
        ledger.append("CALL_ACCEPTED", dict(accepted.call_accepted_payload))
    else:
        accepted = recover_accepted_reflector_synthesis_v1(
            sealed=sealed,
            ledger=ledger,
            run_id=sealed.packet.run_id,
        )
    prior_receipt = _event_payload_for_logical(
        ledger.events,
        kind="REFLECTOR_ACCEPTED",
        logical_call_id=plan.logical_call_id,
    )
    if prior_receipt is None:
        ledger.append("REFLECTOR_ACCEPTED", dict(accepted.reflector_accepted_payload))
    elif prior_receipt != accepted.reflector_accepted_payload:
        raise TemperatureFormalExecutionError("REFLECTOR_RECEIPT_RECOVERY_MISMATCH")
    return accepted


def _event_payload_for_logical(
    events: tuple[dict[str, Any], ...],
    *,
    kind: str,
    logical_call_id: str,
) -> dict[str, Any] | None:
    values = [
        dict(event["payload"])
        for event in events
        if event.get("kind") == kind
        and isinstance(event.get("payload"), Mapping)
        and event["payload"].get("logical_call_id") == logical_call_id
    ]
    if len(values) > 1:
        raise TemperatureFormalExecutionError("FORMAL_DUPLICATE_FINALIZATION_EVENT")
    return None if not values else values[0]


def _event_payload_for_call(
    events: tuple[dict[str, Any], ...],
    *,
    kind: str,
    call_id: str,
) -> dict[str, Any] | None:
    values = [
        dict(event["payload"])
        for event in events
        if event.get("kind") == kind
        and isinstance(event.get("payload"), Mapping)
        and event["payload"].get("call_id") == call_id
    ]
    if len(values) > 1:
        raise TemperatureFormalExecutionError("FORMAL_DUPLICATE_CALL_CLOSURE_EVENT")
    return None if not values else values[0]


def _task_payload(request: TaskRequest) -> dict[str, Any]:
    return request.model_dump(
        mode="json",
        exclude_defaults=False,
        exclude_none=False,
        exclude_unset=False,
    )


def _task_status_sha256(status: TaskStatus) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            status.model_dump(
                mode="json",
                exclude_defaults=False,
                exclude_none=False,
                exclude_unset=False,
            )
        )
    )


def _task_status_from_session_result(result: Any) -> TaskStatus:
    if result.task_id is None or result.status not in SessionStatus.terminal():
        raise TemperatureFormalExecutionError("FORMAL_PERSISTED_SESSION_RESULT_INVALID")
    return TaskStatus(
        task_id=result.task_id,
        status="completed" if result.status is SessionStatus.COMPLETED else "failed",
        total_sessions=1,
        completed_sessions=1,
        results=[result],
        result_paths=[],
    )


def _is_missing_task(exc: Exception) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404


__all__ = [
    "DEFAULT_MAX_POLL_ATTEMPTS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "MAX_CANDIDATE_WORKERS",
    "DurableFormalOutcomeV1",
    "FormalCallEnvelopeV1",
    "FormalExecutionLedgerV1",
    "RuntimeServicesIdentityV1",
    "TemperatureFormalExecutionError",
    "TemperatureFormalExecutorV1",
    "finalize_candidate_outcome_v1",
    "finalize_reflector_outcome_v1",
]
