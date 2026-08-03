"""Safe-Evolve Candidate planning, context binding and private evaluation."""

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
    PublicChemBench4KTask,
    RenderedChemBench4KPrompt,
)
from openevo_chembench.models import RawAttempt
from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes, sha256_bytes
from openevo_chembench.supervised_transfer_v2.context_binding import SupervisedAgentRequestV2
from openevo_chembench.supervised_transfer_v2.executor import (
    RolloutClientV2,
    SupervisedManagedCodexExecutorV2,
    SupervisedTaskExecutionErrorV2,
    _raw_attempt_from_task_status,
    task_request_digest_v2,
)
from openevo_chembench.temperature_full_evolve_v1.retry_semantics import (
    task_request_retry_semantics_sha256_v1,
)
from openevo_chembench.temperature_safe_evolve_v2.config import PROTOCOL_ID

CANDIDATE_TIMEOUT_SECONDS = 1200
RUNTIME_POLICY = (
    "Runtime policy:\n"
    "- Solve only from the official public prompt and approved same-category Core context.\n"
    "- Do not use shell, files, network, web, browser, MCP, plugins, apps, subagents, or external tools.\n"
    "- Reply with exactly one uppercase A, B, C, or D."
)
REMOTE_SKILLS_ROOT = "/openevo/session/workspace/.openevo-approved-skills"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{7,191}\Z", re.ASCII)
_RUN_ID = re.compile(r"stv3-temperature-safe-evolve-v2-[A-Za-z0-9._:-]{8,160}\Z")


class SafeCandidateError(RuntimeError):
    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


class _NoSubmitClient(RolloutClientV2):
    def submit_task(self, payload: dict[str, object]) -> str:
        del payload
        raise AssertionError("build-only client cannot submit")

    def get_task(self, task_id: str) -> dict[str, object]:
        del task_id
        raise AssertionError("build-only client cannot poll")

    def cancel_task(self, task_id: str) -> dict[str, object]:
        del task_id
        raise AssertionError("build-only client cannot cancel")

    def close(self) -> None:
        return None


@runtime_checkable
class CandidateLedgerV2(Protocol):
    def accepted_call(self, logical_call_id: str) -> dict[str, Any] | None: ...

    def latest_claim(self, logical_call_id: str) -> dict[str, Any] | None: ...

    def failure_has_no_completion(self, call_id: str) -> bool: ...


@dataclass(frozen=True, slots=True, repr=False)
class TargetInjectionV2:
    target_id: Literal["text_memory", "skill_bundle", "agent_system"]
    core_artifact_id: str
    canonical_artifact_sha256: str
    canonical_artifact_utf8_bytes: int
    payload: str
    payload_sha256: str
    payload_utf8_bytes: int
    retrieval_input_sha256: str | None = None
    selected_entry_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        encoded = self.payload.encode("utf-8")
        if (
            not self.core_artifact_id
            or "/" in self.core_artifact_id
            or "\\" in self.core_artifact_id
            or _SHA256.fullmatch(self.canonical_artifact_sha256) is None
            or type(self.canonical_artifact_utf8_bytes) is not int
            or self.canonical_artifact_utf8_bytes < 1
            or len(encoded) != self.payload_utf8_bytes
            or hashlib.sha256(encoded).hexdigest() != self.payload_sha256
            or (
                self.retrieval_input_sha256 is not None
                and _SHA256.fullmatch(self.retrieval_input_sha256) is None
            )
            or tuple(sorted(self.selected_entry_ids)) != self.selected_entry_ids
            or len(set(self.selected_entry_ids)) != len(self.selected_entry_ids)
        ):
            raise ValueError("target injection is invalid")

    def __repr__(self) -> str:
        return f"TargetInjectionV2(target_id={self.target_id!r}, <payload-redacted>)"

    def binding(self) -> dict[str, object]:
        return {
            "target_id": self.target_id,
            "core_artifact_id": self.core_artifact_id,
            "canonical_artifact_sha256": self.canonical_artifact_sha256,
            "canonical_artifact_utf8_bytes": self.canonical_artifact_utf8_bytes,
            "injected_payload_sha256": self.payload_sha256,
            "injected_payload_utf8_bytes": self.payload_utf8_bytes,
            "retrieval_input_sha256": self.retrieval_input_sha256,
            "selected_entry_ids": list(self.selected_entry_ids),
        }


@dataclass(frozen=True, slots=True, repr=False)
class CandidateContextV2:
    state_identity_sha256: str
    injections: tuple[TargetInjectionV2, ...]
    mode: Literal["generation_zero", "frozen_c4_ablation", "safe_sparse"]

    def __post_init__(self) -> None:
        order = ("text_memory", "skill_bundle", "agent_system")
        if (
            _SHA256.fullmatch(self.state_identity_sha256) is None
            or tuple(sorted(self.injections, key=lambda value: order.index(value.target_id)))
            != self.injections
            or len({value.target_id for value in self.injections}) != len(self.injections)
            or (self.mode == "generation_zero") != (not self.injections)
            or (self.mode == "safe_sparse" and len(self.injections) > 1)
        ):
            raise ValueError("Candidate context is invalid")

    def __repr__(self) -> str:
        return "CandidateContextV2(<payloads-redacted>)"

    @property
    def memory(self) -> TargetInjectionV2 | None:
        return next((value for value in self.injections if value.target_id == "text_memory"), None)

    @property
    def skill(self) -> TargetInjectionV2 | None:
        return next(
            (value for value in self.injections if value.target_id == "skill_bundle"), None
        )

    @property
    def agent_system(self) -> TargetInjectionV2 | None:
        return next(
            (value for value in self.injections if value.target_id == "agent_system"), None
        )

    @property
    def binding_sha256(self) -> str:
        return sha256_bytes(
            canonical_json_bytes(
                {
                    "schema_version": "TemperatureSafeCandidateContextBindingV2",
                    "mode": self.mode,
                    "state_identity_sha256": self.state_identity_sha256,
                    "targets": [value.binding() for value in self.injections],
                }
            )
        )

    @classmethod
    def generation_zero(cls) -> CandidateContextV2:
        return cls(
            state_identity_sha256=hashlib.sha256(b"").hexdigest(),
            injections=(),
            mode="generation_zero",
        )


@dataclass(frozen=True, slots=True, repr=False)
class SafeCandidatePlanV2:
    logical_call_id: str
    call_id: str
    attempt_number: int
    task_uid: str
    task_ordinal: int
    phase: str
    block_id: str
    context_hash: str
    context_binding_sha256: str
    task_request: TaskRequest
    task_request_sha256: str
    claim_payload: dict[str, object]

    def __post_init__(self) -> None:
        if (
            _SAFE_ID.fullmatch(self.logical_call_id) is None
            or _SAFE_ID.fullmatch(self.call_id) is None
            or self.call_id != f"{self.logical_call_id}-a{self.attempt_number:02d}"
            or self.attempt_number not in {1, 2, 3}
            or _SHA256.fullmatch(self.task_uid) is None
            or type(self.task_ordinal) is not int
            or self.task_ordinal < 0
            or _SHA256.fullmatch(self.context_hash) is None
            or _SHA256.fullmatch(self.context_binding_sha256) is None
            or type(self.task_request) is not TaskRequest
            or self.task_request_sha256 != task_request_digest_v2(self.task_request)
            or self.claim_payload.get("logical_call_id") != self.logical_call_id
            or self.claim_payload.get("call_id") != self.call_id
            or self.claim_payload.get("task_id") != self.task_request.task_id
            or self.claim_payload.get("task_request_sha256") != self.task_request_sha256
            or self.claim_payload.get("retry_semantics_sha256")
            != task_request_retry_semantics_sha256_v1(self.task_request)
        ):
            raise ValueError("Candidate plan is invalid")

    def __repr__(self) -> str:
        return "SafeCandidatePlanV2(<private-task-request>)"


@dataclass(frozen=True, slots=True, repr=False)
class SafeObservedCompletionV2:
    attempt: RawAttempt
    response_sha256: str
    transcript_sha256: str
    task_result_sha256: str
    completion_identity_sha256: str

    def __repr__(self) -> str:
        return "SafeObservedCompletionV2(<private-completion>)"


@dataclass(frozen=True, slots=True, repr=False)
class SafeAcceptedCandidateV2:
    plan: SafeCandidatePlanV2
    observed: SafeObservedCompletionV2
    call_accepted_payload: dict[str, object]

    def __repr__(self) -> str:
        return "SafeAcceptedCandidateV2(<private-completion>)"


@dataclass(frozen=True, slots=True, repr=False)
class SafeEvaluatedCandidateV2:
    accepted: SafeAcceptedCandidateV2
    evaluation: PrivateChemBench4KEvaluation
    evaluation_json: str
    evaluation_sha256: str
    call_evaluated_payload: dict[str, object]

    def __repr__(self) -> str:
        return "SafeEvaluatedCandidateV2(<private-evaluation>)"


def materialize_context_workspace_v2(
    context: CandidateContextV2,
    *,
    workspace: Path,
) -> str:
    if (
        type(context) is not CandidateContextV2
        or not isinstance(workspace, Path)
        or not workspace.is_absolute()
    ):
        raise TypeError("workspace materialization input is invalid")
    if context.skill is None and context.agent_system is None:
        raise SafeCandidateError("SAFE_CONTEXT_WORKSPACE_NOT_NEEDED")
    expected_files: dict[str, bytes] = {}
    if context.agent_system is not None:
        expected_files["AGENTS.md"] = context.agent_system.payload.encode("utf-8")
    if context.skill is not None:
        skill_dir = f".openevo-approved-skills/artifact-{context.skill.payload_sha256[:16]}"
        expected_files[f"{skill_dir}/SKILL.md"] = context.skill.payload.encode("utf-8")
    digest = sha256_bytes(
        canonical_json_bytes(
            [
                {"path": path, "sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
                for path, value in sorted(expected_files.items())
            ]
        )
    )
    marker_name = ".temperature-safe-context-sha256"
    if workspace.exists():
        _require_private_directory(workspace)
        try:
            stored = (workspace / marker_name).read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            raise SafeCandidateError("SAFE_CONTEXT_WORKSPACE_INVALID") from exc
        if stored != digest or _workspace_inventory(workspace, marker_name=marker_name) != digest:
            raise SafeCandidateError("SAFE_CONTEXT_WORKSPACE_DRIFT")
        return digest
    workspace.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace.parent.chmod(0o700)
    workspace.mkdir(mode=0o700)
    for relative, payload in expected_files.items():
        destination = workspace / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination.parent.chmod(0o700)
        _write_private(destination, payload)
    _write_private(workspace / marker_name, (digest + "\n").encode("ascii"))
    if _workspace_inventory(workspace, marker_name=marker_name) != digest:
        raise SafeCandidateError("SAFE_CONTEXT_WORKSPACE_DRIFT")
    return digest


def prepare_candidate_call_v2(
    *,
    task: PublicChemBench4KTask,
    prompt: RenderedChemBench4KPrompt,
    context: CandidateContextV2,
    context_workspace: Path | None,
    phase: str,
    block_id: str,
    task_ordinal: int,
    ledger: CandidateLedgerV2,
    run_id: str,
    service_identity_sha256: str,
) -> SafeCandidatePlanV2:
    """Build one exact physical context call without accessing task GT."""

    if type(task) is not PublicChemBench4KTask or type(prompt) is not RenderedChemBench4KPrompt:
        raise TypeError("Candidate planning requires public task and rendered prompt")
    if task.uid != prompt.uid or task.category != "Temperature_Prediction":
        raise SafeCandidateError("SAFE_CANDIDATE_TASK_PROMPT_MISMATCH")
    if type(context) is not CandidateContextV2 or not isinstance(ledger, CandidateLedgerV2):
        raise TypeError("Candidate context or ledger is invalid")
    if _RUN_ID.fullmatch(run_id) is None or _SHA256.fullmatch(service_identity_sha256) is None:
        raise ValueError("run or service identity is invalid")
    if (
        re.fullmatch(r"[a-z0-9][a-z0-9-]{1,31}", phase) is None
        or re.fullmatch(r"[a-z0-9][a-z0-9-]{1,31}", block_id) is None
    ):
        raise ValueError("phase or block ID is invalid")
    if type(task_ordinal) is not int or task_ordinal < 0:
        raise ValueError("task ordinal is invalid")
    needs_workspace = context.skill is not None or context.agent_system is not None
    if needs_workspace != (context_workspace is not None):
        raise SafeCandidateError("SAFE_CONTEXT_WORKSPACE_BINDING_INVALID")
    workspace_digest = None
    if context_workspace is not None:
        workspace_digest = materialize_context_workspace_v2(context, workspace=context_workspace)

    instruction = _candidate_instruction(prompt.text, memory=context.memory)
    context_hash = sha256_bytes(
        canonical_json_bytes(
            {
                "schema_version": "TemperatureSafeInferenceContextHashV2",
                "protocol_id": PROTOCOL_ID,
                "public_prompt_sha256": hashlib.sha256(prompt.text.encode("utf-8")).hexdigest(),
                "final_instruction_sha256": hashlib.sha256(
                    instruction.encode("utf-8")
                ).hexdigest(),
                "context_binding_sha256": context.binding_sha256,
                "context_workspace_sha256": workspace_digest,
                "model": "gpt-5.5",
                "reasoning_effort": "medium",
                "managed_profile": "managed_science",
                "timeout_seconds": CANDIDATE_TIMEOUT_SECONDS,
                "retry_semantics": "durable_no_completion_only_max_3",
                "parser": "first_uppercase_opencompass_compatible",
                "evaluator": "ChemBench4KPrivateEvaluator",
                "tool_policy": "disabled_zero_tool_transcript_audit",
            }
        )
    )
    run_token = hashlib.sha256(run_id.encode()).hexdigest()[:14]
    logical_call_id = (
        f"safe-{run_token}-{phase}-{block_id}-i{task_ordinal:03d}-c{context_hash}"
    )
    if ledger.accepted_call(logical_call_id) is not None:
        raise SafeCandidateError("SAFE_CANDIDATE_LOGICAL_CALL_ALREADY_ACCEPTED")
    prior = ledger.latest_claim(logical_call_id)
    if prior is None:
        attempt = 1
    else:
        prior_call = prior.get("call_id")
        prior_attempt = prior.get("attempt_number")
        if (
            type(prior_call) is not str
            or type(prior_attempt) is not int
            or not ledger.failure_has_no_completion(prior_call)
        ):
            raise SafeCandidateError("SAFE_CANDIDATE_CALL_OWNERSHIP_UNRESOLVED")
        attempt = prior_attempt + 1
    if attempt > 3:
        raise SafeCandidateError("SAFE_CANDIDATE_INFRASTRUCTURE_RETRY_EXHAUSTED")
    call_id = f"{logical_call_id}-a{attempt:02d}"
    session_id = "safe-" + hashlib.sha256(call_id.encode()).hexdigest()[:40]
    arm: Literal["control", "online"] = "control" if not context.injections else "online"
    request = SupervisedAgentRequestV2(
        rendered_public_prompt=prompt.text,
        resolved_context=None,
        session_id=session_id,
        arm=arm,
        task_ordinal=task_ordinal,
        round_index=min(3, max(0, int(block_id[1:]) - 1))
        if re.fullmatch(r"b[1-4]", block_id)
        else 3,
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
        task_request = builder.build_task_request(request, workspace_root=context_workspace)
    finally:
        builder.close()
    settings = dict(task_request.agent.settings)
    settings["tool_policy"] = "disabled"
    agent = task_request.agent.model_copy(
        update={
            "settings": settings,
            "skills_path": REMOTE_SKILLS_ROOT if context.skill is not None else None,
        }
    )
    metadata = dict(task_request.metadata)
    existing = dict(metadata.get("openevo_chembench", {}))
    existing.update(
        {
            "schema_version": "TemperatureSafeCandidateTaskRequestV2",
            "protocol_id": PROTOCOL_ID,
            "phase": phase,
            "block_id": block_id,
            "physical_context_hash": context_hash,
            "context_binding_sha256": context.binding_sha256,
            "context_target_ids": [value.target_id for value in context.injections],
            "context_artifact_ids": [value.core_artifact_id for value in context.injections],
            "runtime_services_identity_sha256": service_identity_sha256,
            "model_visible_tools_enabled": False,
            "provider_transport_network_enabled": True,
        }
    )
    metadata["openevo_chembench"] = existing
    task_request = task_request.model_copy(
        update={"instruction": instruction, "agent": agent, "metadata": metadata}
    )
    request_sha = task_request_digest_v2(task_request)
    claim: dict[str, object] = {
        "logical_call_id": logical_call_id,
        "call_id": call_id,
        "task_id": task_request.task_id,
        "phase": f"inference-{phase}",
        "logical_arm": "safe_candidate",
        "attempt_number": attempt,
        "task_request_sha256": request_sha,
        "retry_semantics_sha256": task_request_retry_semantics_sha256_v1(task_request),
        "service_identity_sha256": service_identity_sha256,
    }
    return SafeCandidatePlanV2(
        logical_call_id=logical_call_id,
        call_id=call_id,
        attempt_number=attempt,
        task_uid=task.uid,
        task_ordinal=task_ordinal,
        phase=phase,
        block_id=block_id,
        context_hash=context_hash,
        context_binding_sha256=context.binding_sha256,
        task_request=task_request,
        task_request_sha256=request_sha,
        claim_payload=claim,
    )


def observe_candidate_status_v2(
    status: TaskStatus, *, plan: SafeCandidatePlanV2
) -> SafeObservedCompletionV2:
    if type(status) is not TaskStatus or type(plan) is not SafeCandidatePlanV2:
        raise TypeError("Candidate observation requires exact status and plan")
    try:
        attempt = _raw_attempt_from_task_status(status, task_id=plan.task_request.task_id)
    except SupervisedTaskExecutionErrorV2 as exc:
        raise SafeCandidateError("SAFE_CANDIDATE_TERMINAL_RESULT_INVALID") from exc
    reference = attempt.transcript_reference.reference
    prefix = "openevo-rollout-jsonl:sha256:"
    transcript_sha = reference.removeprefix(prefix)
    if not reference.startswith(prefix) or _SHA256.fullmatch(transcript_sha) is None:
        raise SafeCandidateError("SAFE_CANDIDATE_TRANSCRIPT_REFERENCE_INVALID")
    task_result_sha = sha256_bytes(
        canonical_json_bytes(
            status.model_dump(
                mode="json", exclude_defaults=False, exclude_none=False, exclude_unset=False
            )
        )
    )
    response_sha = hashlib.sha256(attempt.response.encode("utf-8")).hexdigest()
    completion_sha = sha256_bytes(
        canonical_json_bytes(
            {
                "task_request_sha256": plan.task_request_sha256,
                "task_result_sha256": task_result_sha,
                "response_sha256": response_sha,
                "transcript_sha256": transcript_sha,
            }
        )
    )
    return SafeObservedCompletionV2(
        attempt=attempt,
        response_sha256=response_sha,
        transcript_sha256=transcript_sha,
        task_result_sha256=task_result_sha,
        completion_identity_sha256=completion_sha,
    )


def accept_candidate_v2(
    *, plan: SafeCandidatePlanV2, observed: SafeObservedCompletionV2, ledger: CandidateLedgerV2
) -> SafeAcceptedCandidateV2:
    if (
        type(plan) is not SafeCandidatePlanV2
        or type(observed) is not SafeObservedCompletionV2
        or not isinstance(ledger, CandidateLedgerV2)
    ):
        raise TypeError("Candidate acceptance input is invalid")
    claim = ledger.latest_claim(plan.logical_call_id)
    if (
        claim != plan.claim_payload
        or ledger.failure_has_no_completion(plan.call_id)
        or ledger.accepted_call(plan.logical_call_id) is not None
    ):
        raise SafeCandidateError("SAFE_CANDIDATE_ACCEPTANCE_WITHOUT_ACTIVE_CLAIM")
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
    return SafeAcceptedCandidateV2(plan=plan, observed=observed, call_accepted_payload=payload)


def evaluate_candidate_v2(
    *, task: PrivateChemBench4KTask, accepted: SafeAcceptedCandidateV2
) -> SafeEvaluatedCandidateV2:
    """Evaluate only after the controller has crossed the block GT barrier."""

    if type(task) is not PrivateChemBench4KTask or type(accepted) is not SafeAcceptedCandidateV2:
        raise TypeError("Candidate evaluation input is invalid")
    if task.uid != accepted.plan.task_uid:
        raise SafeCandidateError("SAFE_CANDIDATE_EVALUATION_TASK_MISMATCH")
    evaluation = ChemBench4KPrivateEvaluator().evaluate(
        task=task,
        raw_completion=accepted.observed.attempt.response,
    )
    payload = {
        "schema_version": "TemperatureSafeCandidateEvaluationV2",
        "logical_call_id": accepted.plan.logical_call_id,
        "phase": accepted.plan.phase,
        "block_id": accepted.plan.block_id,
        "task_ordinal": accepted.plan.task_ordinal,
        "task_uid": task.uid,
        "context_hash": accepted.plan.context_hash,
        "official_prediction": evaluation.official.prediction,
        "official_parse_status": evaluation.official.status.value,
        "strict_prediction": evaluation.strict.prediction,
        "strict_parse_status": evaluation.strict.status.value,
        "correct": evaluation.correct,
        "response_sha256": accepted.observed.response_sha256,
        "transcript_sha256": accepted.observed.transcript_sha256,
        "context_binding_sha256": accepted.plan.context_binding_sha256,
        "runtime": accepted.observed.attempt.runtime_metadata.to_result_payload(),
    }
    evaluation_json = canonical_json_bytes(payload).decode("utf-8")
    evaluation_sha = hashlib.sha256(evaluation_json.encode()).hexdigest()
    return SafeEvaluatedCandidateV2(
        accepted=accepted,
        evaluation=evaluation,
        evaluation_json=evaluation_json,
        evaluation_sha256=evaluation_sha,
        call_evaluated_payload={
            "logical_call_id": accepted.plan.logical_call_id,
            "evaluation_json": evaluation_json,
            "evaluation_sha256": evaluation_sha,
        },
    )


def _candidate_instruction(public_prompt: str, *, memory: TargetInjectionV2 | None) -> str:
    sections = [RUNTIME_POLICY]
    if memory is not None:
        sections.append("Approved Core-resolved category memory:\n" + memory.payload)
    sections.append(public_prompt)
    result = "\n\n".join(sections)
    if not result.endswith(public_prompt):
        raise AssertionError("public prompt must be final")
    return result


def _workspace_inventory(workspace: Path, *, marker_name: str) -> str:
    values: list[dict[str, object]] = []
    for path in sorted(workspace.rglob("*")):
        relative = path.relative_to(workspace).as_posix()
        if relative == marker_name or path.is_dir():
            continue
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise SafeCandidateError("SAFE_CONTEXT_WORKSPACE_INVALID")
        payload = path.read_bytes()
        values.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            }
        )
    return sha256_bytes(canonical_json_bytes(values))


def _require_private_directory(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise SafeCandidateError("SAFE_CONTEXT_WORKSPACE_INVALID")


def _write_private(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


__all__ = [
    "CANDIDATE_TIMEOUT_SECONDS",
    "RUNTIME_POLICY",
    "CandidateContextV2",
    "CandidateLedgerV2",
    "SafeAcceptedCandidateV2",
    "SafeCandidateError",
    "SafeCandidatePlanV2",
    "SafeEvaluatedCandidateV2",
    "SafeObservedCompletionV2",
    "TargetInjectionV2",
    "accept_candidate_v2",
    "evaluate_candidate_v2",
    "materialize_context_workspace_v2",
    "observe_candidate_status_v2",
    "prepare_candidate_call_v2",
]
