"""Formal Candidate/baseline TaskRequest planning and terminal observation."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from openevo.rollout.models import TaskRequest, TaskStatus

from openevo_chembench.chembench4k_evaluation import (
    ChemBench4KPrivateEvaluator,
    PrivateChemBench4KEvaluation,
)
from openevo_chembench.chembench4k_models import (
    PrivateChemBench4KTask,
    RenderedChemBench4KPrompt,
)
from openevo_chembench.models import RawAttempt
from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes, sha256_bytes
from openevo_chembench.supervised_transfer_v2.artifacts import CoreResolvedSupervisedContextV2
from openevo_chembench.supervised_transfer_v2.context_binding import (
    SupervisedAgentRequestV2,
    SupervisedSessionContextBindingV2,
    issue_supervised_context_binding_receipt_v2,
)
from openevo_chembench.supervised_transfer_v2.executor import (
    RolloutClientV2,
    SupervisedManagedCodexExecutorV2,
    SupervisedTaskExecutionErrorV2,
    _materialize_core_resolved_context,
    _raw_attempt_from_task_status,
    task_request_digest_v2,
)
from openevo_chembench.temperature_full_evolve_v1.config import PROTOCOL_ID
from openevo_chembench.temperature_full_evolve_v1.retry_semantics import (
    task_request_retry_semantics_sha256_v1,
)

CANDIDATE_TASK_REQUEST_SCHEMA = "TemperatureCandidateTaskRequestV1"
CANDIDATE_ACCEPTED_SCHEMA = "TemperatureCandidateAcceptedV1"
CANDIDATE_EVALUATION_SCHEMA = "TemperatureCandidateEvaluationV1"
CANDIDATE_TIMEOUT_SECONDS = 1200
PHASES = (
    "train_pre",
    "train_post",
    "evolved_test",
    "baseline_test",
)

_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,191}\Z", re.ASCII)
_LEDGER_ID = re.compile(r"[a-z0-9][a-z0-9._-]{7,127}\Z", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_TRANSCRIPT_PREFIX = "openevo-rollout-jsonl:sha256:"


class TemperatureCandidateError(RuntimeError):
    """Content-free Candidate planning or observation failure."""


class _NoSubmitClient(RolloutClientV2):
    def submit_task(self, payload: dict[str, object]) -> str:
        del payload
        raise AssertionError("build-only Candidate client cannot submit")

    def get_task(self, task_id: str) -> dict[str, object]:
        del task_id
        raise AssertionError("build-only Candidate client cannot poll")

    def cancel_task(self, task_id: str) -> dict[str, object]:
        del task_id
        raise AssertionError("build-only Candidate client cannot cancel")

    def close(self) -> None:
        return None


@runtime_checkable
class CandidateLedgerViewV1(Protocol):
    def accepted_call(self, logical_call_id: str) -> dict[str, Any] | None: ...

    def latest_claim(self, logical_call_id: str) -> dict[str, Any] | None: ...

    def failure_has_no_completion(self, call_id: str) -> bool: ...


@dataclass(frozen=True, slots=True, repr=False)
class CandidateCallPlanV1:
    phase: Literal["train_pre", "train_post", "evolved_test", "baseline_test"]
    logical_call_id: str
    call_id: str
    attempt_number: int
    task_uid: str
    task_ordinal: int
    batch_index: int | None
    context_binding_sha256: str
    task_request: TaskRequest
    task_request_sha256: str
    claim_payload: dict[str, object]

    def __post_init__(self) -> None:
        if (
            self.phase not in PHASES
            or _LEDGER_ID.fullmatch(self.logical_call_id) is None
            or _LEDGER_ID.fullmatch(self.call_id) is None
            or self.attempt_number < 1
            or _SHA256.fullmatch(self.task_uid) is None
            or self.task_ordinal < 0
            or (self.batch_index is not None and not 1 <= self.batch_index <= 4)
            or _SHA256.fullmatch(self.context_binding_sha256) is None
            or type(self.task_request) is not TaskRequest
            or self.task_request_sha256 != task_request_digest_v2(self.task_request)
            or self.claim_payload.get("logical_call_id") != self.logical_call_id
            or self.claim_payload.get("call_id") != self.call_id
            or self.claim_payload.get("task_request_sha256") != self.task_request_sha256
            or self.claim_payload.get("retry_semantics_sha256")
            != task_request_retry_semantics_sha256_v1(self.task_request)
        ):
            raise ValueError("Candidate call plan is invalid")

    def __repr__(self) -> str:
        return "CandidateCallPlanV1(<private-task-request>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class ObservedCandidateCompletionV1:
    attempt: RawAttempt
    response_sha256: str
    transcript_sha256: str
    task_result_sha256: str
    completion_identity_sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.attempt) is not RawAttempt
            or self.response_sha256
            != hashlib.sha256(self.attempt.response.encode("utf-8")).hexdigest()
            or any(
                _SHA256.fullmatch(value) is None
                for value in (
                    self.transcript_sha256,
                    self.task_result_sha256,
                    self.completion_identity_sha256,
                )
            )
        ):
            raise ValueError("observed Candidate completion is invalid")

    def __repr__(self) -> str:
        return "ObservedCandidateCompletionV1(<private-completion>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class AcceptedCandidateV1:
    plan: CandidateCallPlanV1
    observed: ObservedCandidateCompletionV1
    context_binding_sha256: str
    call_accepted_payload: dict[str, object]

    def __repr__(self) -> str:
        return "AcceptedCandidateV1(<private-completion>)"

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class EvaluatedCandidateV1:
    accepted: AcceptedCandidateV1
    evaluation: PrivateChemBench4KEvaluation
    evaluation_json: str
    evaluation_sha256: str
    call_evaluated_payload: dict[str, object]

    def __repr__(self) -> str:
        return "EvaluatedCandidateV1(<private-evaluation>)"

    __str__ = __repr__


def materialize_candidate_context_v1(
    context: CoreResolvedSupervisedContextV2,
    *,
    workspace: Path,
) -> str:
    """Create or verify one stable Gateway-visible private context workspace."""

    if type(context) is not CoreResolvedSupervisedContextV2:
        raise TypeError("Candidate context must be Core-issued")
    if not isinstance(workspace, Path) or not workspace.is_absolute():
        raise TypeError("Candidate workspace must be absolute")
    expected = _context_workspace_digest(context)
    marker_name = ".temperature-context-sha256"
    if workspace.exists():
        _require_private_directory(workspace)
        marker = workspace / marker_name
        try:
            stored = marker.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise TemperatureCandidateError("CANDIDATE_CONTEXT_WORKSPACE_INVALID") from exc
        if stored != expected or _hash_materialized_context(workspace) != expected:
            raise TemperatureCandidateError("CANDIDATE_CONTEXT_WORKSPACE_DRIFT")
        return expected
    workspace.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace.parent.chmod(0o700)
    workspace.mkdir(mode=0o700)
    _materialize_core_resolved_context(context, workspace)
    _write_private_text(workspace / marker_name, expected + "\n")
    if _hash_materialized_context(workspace) != expected:
        raise TemperatureCandidateError("CANDIDATE_CONTEXT_WORKSPACE_DRIFT")
    return expected


def prepare_candidate_call_v1(
    *,
    task: PrivateChemBench4KTask,
    prompt: RenderedChemBench4KPrompt,
    context: CoreResolvedSupervisedContextV2 | None,
    context_workspace: Path | None,
    phase: Literal["train_pre", "train_post", "evolved_test", "baseline_test"],
    task_ordinal: int,
    batch_index: int | None,
    ledger: CandidateLedgerViewV1,
    run_id: str,
    service_identity_sha256: str,
) -> CandidateCallPlanV1:
    """Prepare a fresh call or a proven-no-completion infrastructure retry."""

    if type(task) is not PrivateChemBench4KTask or type(prompt) is not RenderedChemBench4KPrompt:
        raise TypeError("Candidate planning requires exact task and prompt DTOs")
    if task.uid != prompt.uid or task.category != "Temperature_Prediction":
        raise TemperatureCandidateError("CANDIDATE_TASK_PROMPT_IDENTITY_INVALID")
    if phase not in PHASES or type(task_ordinal) is not int or task_ordinal < 0:
        raise ValueError("Candidate phase or ordinal is invalid")
    if phase in {"train_pre", "train_post"} and batch_index not in {1, 2, 3, 4}:
        raise ValueError("Train Candidate call requires a batch")
    if phase in {"evolved_test", "baseline_test"} and batch_index is not None:
        raise ValueError("Test Candidate call must not carry a Train batch")
    if phase == "baseline_test" and context is not None:
        raise TemperatureCandidateError("BASELINE_CONTEXT_FORBIDDEN")
    if context is None and context_workspace is not None:
        raise TemperatureCandidateError("GENERATION_ZERO_WORKSPACE_FORBIDDEN")
    if context is not None:
        if context_workspace is None:
            raise TemperatureCandidateError("EVOLVED_CONTEXT_WORKSPACE_REQUIRED")
        materialize_candidate_context_v1(context, workspace=context_workspace)
    if not isinstance(ledger, CandidateLedgerViewV1):
        raise TypeError("ledger does not implement the Candidate exactly-once view")
    if type(run_id) is not str or _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("run ID is invalid")
    if _SHA256.fullmatch(service_identity_sha256) is None:
        raise ValueError("runtime service identity digest is invalid")

    logical_call_id = _candidate_logical_call_id(
        run_id=run_id,
        phase=phase,
        batch_index=batch_index,
        task_ordinal=task_ordinal,
    )
    if ledger.accepted_call(logical_call_id) is not None:
        raise TemperatureCandidateError("CANDIDATE_LOGICAL_CALL_ALREADY_ACCEPTED")
    prior = ledger.latest_claim(logical_call_id)
    if prior is None:
        attempt_number = 1
    else:
        prior_call_id = prior.get("call_id")
        prior_attempt = prior.get("attempt_number")
        if (
            type(prior_call_id) is not str
            or type(prior_attempt) is not int
            or not ledger.failure_has_no_completion(prior_call_id)
        ):
            raise TemperatureCandidateError("CANDIDATE_CALL_OWNERSHIP_UNRESOLVED")
        attempt_number = prior_attempt + 1
    if attempt_number > 3:
        raise TemperatureCandidateError("CANDIDATE_INFRASTRUCTURE_RETRY_EXHAUSTED")
    call_id = f"{logical_call_id}-a{attempt_number:02d}"
    session_id = _candidate_session_id(call_id)
    arm: Literal["control", "online"] = "control" if context is None else "online"
    agent_request = SupervisedAgentRequestV2(
        rendered_public_prompt=prompt.text,
        resolved_context=context,
        session_id=session_id,
        arm=arm,
        task_ordinal=task_ordinal,
        round_index=0,
        run_id=run_id,
        task_uid=task.uid,
    )
    builder = SupervisedManagedCodexExecutorV2(
        arm=arm,
        timeout_seconds=CANDIDATE_TIMEOUT_SECONDS,
        rollout_client=_NoSubmitClient(),
        protocol_id=PROTOCOL_ID,
    )
    try:
        task_request = builder.build_task_request(
            agent_request,
            workspace_root=context_workspace,
        )
    finally:
        builder.close()
    # The managed subscription transport needs container network access to
    # reach the provider, but this experiment is completion-only.  Bind the
    # generic Codex subscription policy that removes shell, unified exec and
    # web-search from the model-visible execution surface.
    settings = dict(task_request.agent.settings)
    settings["tool_policy"] = "disabled"
    task_request = task_request.model_copy(
        update={
            "agent": task_request.agent.model_copy(update={"settings": settings}),
        }
    )
    binding = SupervisedSessionContextBindingV2.from_context(
        session_id=session_id,
        context=context,
    )
    injected = SupervisedSessionContextBindingV2.from_injected_context(
        session_id=session_id,
        context=context,
    )
    receipt = issue_supervised_context_binding_receipt_v2(expected=binding, actual=injected)
    receipt.require_match()
    metadata = dict(task_request.metadata)
    existing = dict(metadata.get("openevo_chembench", {}))
    existing["runtime_services_identity_sha256"] = service_identity_sha256
    existing["temperature_full_evolve"] = {
        "schema_version": CANDIDATE_TASK_REQUEST_SCHEMA,
        "phase": phase,
        "batch_index": batch_index,
        "logical_call_id": logical_call_id,
        "call_id": call_id,
        "task_ordinal": task_ordinal,
        "context_binding_sha256": receipt.digest,
        "runtime_services_identity_sha256": service_identity_sha256,
        "tool_policy": "zero_tool_transcript_audit",
        "model_visible_tools_enabled": False,
        "provider_transport_network_enabled": True,
    }
    metadata["openevo_chembench"] = existing
    task_request = task_request.model_copy(update={"metadata": metadata})
    request_digest = task_request_digest_v2(task_request)
    retry_semantics_sha256 = task_request_retry_semantics_sha256_v1(task_request)
    claim_payload: dict[str, object] = {
        "logical_call_id": logical_call_id,
        "call_id": call_id,
        "task_id": task_request.task_id,
        "phase": phase.replace("_", "-"),
        "logical_arm": "candidate" if phase != "baseline_test" else "baseline",
        "attempt_number": attempt_number,
        "task_request_sha256": request_digest,
        "retry_semantics_sha256": retry_semantics_sha256,
        "service_identity_sha256": service_identity_sha256,
    }
    return CandidateCallPlanV1(
        phase=phase,
        logical_call_id=logical_call_id,
        call_id=call_id,
        attempt_number=attempt_number,
        task_uid=task.uid,
        task_ordinal=task_ordinal,
        batch_index=batch_index,
        context_binding_sha256=receipt.digest,
        task_request=task_request,
        task_request_sha256=request_digest,
        claim_payload=claim_payload,
    )


def observe_candidate_task_status_v1(
    status: TaskStatus,
    *,
    plan: CandidateCallPlanV1,
) -> ObservedCandidateCompletionV1:
    if type(status) is not TaskStatus or type(plan) is not CandidateCallPlanV1:
        raise TypeError("Candidate observation requires exact TaskStatus and plan")
    try:
        attempt = _raw_attempt_from_task_status(status, task_id=plan.task_request.task_id)
    except SupervisedTaskExecutionErrorV2 as exc:
        raise TemperatureCandidateError("CANDIDATE_TERMINAL_RESULT_INVALID") from exc
    reference = attempt.transcript_reference.reference
    if not reference.startswith(_TRANSCRIPT_PREFIX) or _SHA256.fullmatch(
        reference.removeprefix(_TRANSCRIPT_PREFIX)
    ) is None:
        raise TemperatureCandidateError("CANDIDATE_TRANSCRIPT_REFERENCE_INVALID")
    transcript_sha256 = reference.removeprefix(_TRANSCRIPT_PREFIX)
    task_result_sha256 = sha256_bytes(
        canonical_json_bytes(
            status.model_dump(
                mode="json",
                exclude_defaults=False,
                exclude_none=False,
                exclude_unset=False,
            )
        )
    )
    response_sha256 = hashlib.sha256(attempt.response.encode("utf-8")).hexdigest()
    completion_identity = sha256_bytes(
        canonical_json_bytes(
            {
                "task_request_sha256": plan.task_request_sha256,
                "task_result_sha256": task_result_sha256,
                "response_sha256": response_sha256,
                "transcript_sha256": transcript_sha256,
            }
        )
    )
    return ObservedCandidateCompletionV1(
        attempt=attempt,
        response_sha256=response_sha256,
        transcript_sha256=transcript_sha256,
        task_result_sha256=task_result_sha256,
        completion_identity_sha256=completion_identity,
    )


def accept_candidate_completion_v1(
    *,
    plan: CandidateCallPlanV1,
    observed: ObservedCandidateCompletionV1,
    ledger: CandidateLedgerViewV1,
) -> AcceptedCandidateV1:
    if (
        type(plan) is not CandidateCallPlanV1
        or type(observed) is not ObservedCandidateCompletionV1
        or not isinstance(ledger, CandidateLedgerViewV1)
    ):
        raise TypeError("Candidate acceptance requires exact DTOs and ledger")
    claim = ledger.latest_claim(plan.logical_call_id)
    if (
        claim is None
        or claim.get("call_id") != plan.call_id
        or claim.get("task_id") != plan.task_request.task_id
        or claim.get("task_request_sha256") != plan.task_request_sha256
        or ledger.failure_has_no_completion(plan.call_id)
        or ledger.accepted_call(plan.logical_call_id) is not None
    ):
        raise TemperatureCandidateError("CANDIDATE_ACCEPTANCE_WITHOUT_ACTIVE_CLAIM")
    payload: dict[str, object] = {
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
    return AcceptedCandidateV1(
        plan=plan,
        observed=observed,
        context_binding_sha256=plan.context_binding_sha256,
        call_accepted_payload=payload,
    )


def evaluate_candidate_completion_v1(
    *,
    task: PrivateChemBench4KTask,
    accepted: AcceptedCandidateV1,
) -> EvaluatedCandidateV1:
    if type(task) is not PrivateChemBench4KTask or type(accepted) is not AcceptedCandidateV1:
        raise TypeError("Candidate evaluation requires exact private task and acceptance")
    if task.uid != accepted.plan.task_uid:
        raise TemperatureCandidateError("CANDIDATE_EVALUATION_TASK_MISMATCH")
    evaluation = ChemBench4KPrivateEvaluator().evaluate(
        task=task,
        raw_completion=accepted.observed.attempt.response,
    )
    payload = {
        "schema_version": CANDIDATE_EVALUATION_SCHEMA,
        "logical_call_id": accepted.plan.logical_call_id,
        "phase": accepted.plan.phase,
        "batch_index": accepted.plan.batch_index,
        "task_ordinal": accepted.plan.task_ordinal,
        "task_uid": task.uid,
        "official_prediction": evaluation.official.prediction,
        "official_parse_status": evaluation.official.status.value,
        "strict_prediction": evaluation.strict.prediction,
        "strict_parse_status": evaluation.strict.status.value,
        "correct": evaluation.correct,
        "response_sha256": accepted.observed.response_sha256,
        "transcript_sha256": accepted.observed.transcript_sha256,
        "context_binding_sha256": accepted.context_binding_sha256,
        "runtime": accepted.observed.attempt.runtime_metadata.to_result_payload(),
    }
    evaluation_json = canonical_json_bytes(payload).decode("utf-8")
    evaluation_sha256 = hashlib.sha256(evaluation_json.encode("utf-8")).hexdigest()
    return EvaluatedCandidateV1(
        accepted=accepted,
        evaluation=evaluation,
        evaluation_json=evaluation_json,
        evaluation_sha256=evaluation_sha256,
        call_evaluated_payload={
            "logical_call_id": accepted.plan.logical_call_id,
            "evaluation_json": evaluation_json,
            "evaluation_sha256": evaluation_sha256,
        },
    )


def _candidate_logical_call_id(
    *,
    run_id: str,
    phase: str,
    batch_index: int | None,
    task_ordinal: int,
) -> str:
    run_token = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]
    batch = "b00" if batch_index is None else f"b{batch_index:02d}"
    return f"temperature-{run_token}-{phase.replace('_', '-')}-{batch}-i{task_ordinal:03d}"


def _candidate_session_id(call_id: str) -> str:
    return "temp-" + hashlib.sha256(call_id.encode("utf-8")).hexdigest()[:40]


def _context_workspace_digest(context: CoreResolvedSupervisedContextV2) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "agent_system": context.agent_system.markdown,
                "skill": context.skill.markdown,
            }
        )
    )


def _hash_materialized_context(workspace: Path) -> str:
    try:
        agent = (workspace / "AGENTS.md").read_text(encoding="utf-8")
        skill_files = tuple((workspace / ".openevo-approved-skills").glob("*/SKILL.md"))
        if len(skill_files) != 1:
            raise OSError("skill closure invalid")
        skill = skill_files[0].read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise TemperatureCandidateError("CANDIDATE_CONTEXT_WORKSPACE_INVALID") from exc
    return sha256_bytes(canonical_json_bytes({"agent_system": agent, "skill": skill}))


def _require_private_directory(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise TemperatureCandidateError("CANDIDATE_CONTEXT_WORKSPACE_INVALID")


def _write_private_text(path: Path, value: str) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


__all__ = [
    "CANDIDATE_ACCEPTED_SCHEMA",
    "CANDIDATE_EVALUATION_SCHEMA",
    "CANDIDATE_TASK_REQUEST_SCHEMA",
    "CANDIDATE_TIMEOUT_SECONDS",
    "AcceptedCandidateV1",
    "CandidateCallPlanV1",
    "CandidateLedgerViewV1",
    "EvaluatedCandidateV1",
    "ObservedCandidateCompletionV1",
    "TemperatureCandidateError",
    "accept_candidate_completion_v1",
    "evaluate_candidate_completion_v1",
    "materialize_candidate_context_v1",
    "observe_candidate_task_status_v1",
    "prepare_candidate_call_v1",
]
