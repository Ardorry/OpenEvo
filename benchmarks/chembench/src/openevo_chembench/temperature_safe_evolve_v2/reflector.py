"""Schema-bound managed-harness Reflector for single-target Safe-Evolve updates."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from openevo.harness.models import AgentSpec
from openevo.rollout.models import TaskRequest, TaskStatus
from openevo.runtime.managed import (
    MANAGED_RUNTIME_RELEASES,
    MANAGED_SUBSCRIPTION_ENV,
    MANAGED_SUBSCRIPTION_PREPARE_COMMAND,
    MANAGED_WORKSPACE,
)
from openevo.runtime.models import PrepareAction, RuntimeSpec
from openevo.trajectory.models import StrategySpec
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes, sha256_bytes
from openevo_chembench.supervised_transfer_v2.executor import task_request_digest_v2
from openevo_chembench.temperature_full_evolve_v1.reflector import (
    ObservedReflectorCompletionV1,
    TemperatureReflectorError,
    observe_reflector_task_status_v1,
)
from openevo_chembench.temperature_full_evolve_v1.retry_semantics import (
    task_request_retry_semantics_sha256_v1,
)
from openevo_chembench.temperature_safe_evolve_v2.artifacts import (
    CanonicalEntryV2,
    SafeArtifactError,
    SelectedTarget,
    render_full_projection_v2,
    validate_reflector_entries_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.config import PROTOCOL_ID
from openevo_chembench.temperature_safe_evolve_v2.ledger import REFLECTOR_REJECTION_CODES
from openevo_chembench.temperature_safe_evolve_v2.packet import SealedReflectorPacketV2

MODEL = "gpt-5.5"
REASONING_EFFORT = "medium"
REFLECTOR_TIMEOUT_SECONDS = 1800
PROMPT_MAX_UTF8_BYTES = 768 * 1024
RESPONSE_MAX_UTF8_BYTES = 64 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{7,191}\Z", re.ASCII)
_RUN_ID = re.compile(r"stv3-temperature-safe-evolve-v2-[A-Za-z0-9._:-]{8,160}\Z")


class SafeReflectorError(RuntimeError):
    def __init__(self, finding_code: str) -> None:
        if finding_code not in REFLECTOR_REJECTION_CODES and not finding_code.startswith(
            "SAFE_REFLECTOR_"
        ):
            raise ValueError("Reflector finding code is invalid")
        self.finding_code = finding_code
        super().__init__(finding_code)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class EntryProposalV2(_FrozenModel):
    action: Literal["add", "update", "retire"]
    entry: CanonicalEntryV2

    @model_validator(mode="after")
    def _status(self) -> EntryProposalV2:
        if (self.action in {"add", "update"} and self.entry.status != "provisional") or (
            self.action == "retire" and self.entry.status != "retired"
        ):
            raise ValueError("proposal action/status mismatch")
        return self


class SafeReflectionV2(_FrozenModel):
    schema_version: Literal["TemperatureSafeReflectionV2"] = "TemperatureSafeReflectionV2"
    run_id: str
    fold_id: Literal["R0", "R1", "R2", "R3"]
    batch_index: int
    selected_target: SelectedTarget
    predecessor_artifact_sha256: str
    prior_evidence_sha256: str
    source_packet_sha256: str
    proposals: tuple[EntryProposalV2, ...]

    @field_validator(
        "predecessor_artifact_sha256", "prior_evidence_sha256", "source_packet_sha256"
    )
    @classmethod
    def _digests(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("reflection digest is invalid")
        return value

    @model_validator(mode="after")
    def _closure(self) -> SafeReflectionV2:
        if (
            _RUN_ID.fullmatch(self.run_id) is None
            or not 1 <= self.batch_index <= 4
            or len(self.proposals) > 12
            or len({proposal.entry.entry_id for proposal in self.proposals}) != len(self.proposals)
        ):
            raise ValueError("reflection closure is invalid")
        return self


@dataclass(frozen=True, slots=True, repr=False)
class SafeReflectorPromptV2:
    instruction: str
    packet_sha256: str
    test_isolation_receipt_sha256: str
    response_schema_sha256: str
    prompt_sha256: str
    prompt_utf8_bytes: int

    def __post_init__(self) -> None:
        encoded = self.instruction.encode("utf-8")
        if (
            any(
                _SHA256.fullmatch(value) is None
                for value in (
                    self.packet_sha256,
                    self.test_isolation_receipt_sha256,
                    self.response_schema_sha256,
                    self.prompt_sha256,
                )
            )
            or self.prompt_sha256 != hashlib.sha256(encoded).hexdigest()
            or self.prompt_utf8_bytes != len(encoded)
            or not 0 < self.prompt_utf8_bytes <= PROMPT_MAX_UTF8_BYTES
        ):
            raise ValueError("Reflector prompt is invalid")

    def __repr__(self) -> str:
        return "SafeReflectorPromptV2(<private-Train-GT-prompt>)"


@runtime_checkable
class ReflectorLedgerV2(Protocol):
    def accepted_call(self, logical_call_id: str) -> dict[str, Any] | None: ...

    def latest_claim(self, logical_call_id: str) -> dict[str, Any] | None: ...

    def failure_has_no_completion(self, call_id: str) -> bool: ...

    def completion_was_rejected(self, call_id: str) -> bool: ...


@dataclass(frozen=True, slots=True, repr=False)
class SafeReflectorPlanV2:
    logical_call_id: str
    call_id: str
    attempt_number: int
    prompt: SafeReflectorPromptV2
    task_request: TaskRequest
    task_request_sha256: str
    claim_payload: dict[str, object]

    def __post_init__(self) -> None:
        if (
            _SAFE_ID.fullmatch(self.logical_call_id) is None
            or self.call_id != f"{self.logical_call_id}-a{self.attempt_number:02d}"
            or self.attempt_number not in {1, 2, 3}
            or type(self.prompt) is not SafeReflectorPromptV2
            or type(self.task_request) is not TaskRequest
            or self.task_request_sha256 != task_request_digest_v2(self.task_request)
            or self.claim_payload.get("logical_call_id") != self.logical_call_id
            or self.claim_payload.get("call_id") != self.call_id
            or self.claim_payload.get("task_id") != self.task_request.task_id
            or self.claim_payload.get("task_request_sha256") != self.task_request_sha256
            or self.claim_payload.get("retry_semantics_sha256")
            != task_request_retry_semantics_sha256_v1(self.task_request)
        ):
            raise ValueError("Reflector plan is invalid")

    def __repr__(self) -> str:
        return "SafeReflectorPlanV2(<private-task-request>)"


@dataclass(frozen=True, slots=True, repr=False)
class AcceptedSafeReflectionV2:
    reflection: SafeReflectionV2
    merged_entries: tuple[CanonicalEntryV2, ...]
    projection: str
    observed: ObservedReflectorCompletionV1
    task_result_sha256: str
    completion_identity_sha256: str
    call_accepted_payload: dict[str, object]
    synthesis_receipt: dict[str, object]

    def __repr__(self) -> str:
        return "AcceptedSafeReflectionV2(<response-and-entry-content-redacted>)"


def render_reflector_prompt_v2(packet: SealedReflectorPacketV2) -> SafeReflectorPromptV2:
    if type(packet) is not SealedReflectorPacketV2:
        raise TypeError("Reflector prompt requires exact sealed packet")
    response_schema = SafeReflectionV2.model_json_schema()
    response_schema_sha = sha256_bytes(canonical_json_bytes(response_schema))
    envelope = {
        "schema_version": "TemperatureSafeReflectorPromptEnvelopeV2",
        "visible_train_packet": packet.visible_payload(),
        "packet_sha256": packet.packet_sha256,
        "test_isolation_receipt_sha256": packet.test_isolation_receipt_sha256,
        "required_response_bindings": packet.required_response_bindings,
        "required_response_json_schema": response_schema,
        "response_schema_sha256": response_schema_sha,
        "response_max_utf8_bytes": RESPONSE_MAX_UTF8_BYTES,
    }
    instruction = "\n\n".join(
        (
            (
                "You are the single supervised Train-batch Reflector for "
                "Temperature_Prediction Safe-Evolve V2. Produce one sparse, single-target "
                "provisional candidate from the supplied Train evidence only."
            ),
            (
                "Runtime and isolation policy:\n"
                "- Do not use shell, files, network tools, web, browser, MCP, apps, plugins, "
                "subagents, or any external source.\n"
                "- Do not seek, infer, mention, or encode Validation or Test information.\n"
                "- Do not memorize a question, answer letter, exact option mapping, UID-to-answer "
                "mapping, or single-item entity conclusion.\n"
                "- The controller validates the same bindings both before and after merge."
            ),
            (
                "Optimization and evidence policy:\n"
                "- Repair residual errors while assigning twice the penalty to every negative flip.\n"
                "- Preserve judgments that generation zero already gets right; avoid broad overrides.\n"
                "- Each new ordinary entry needs at least two distinct current Train UID supports.\n"
                "- State explicit applicability and exclusion conditions and retain contradictions.\n"
                "- Propose retirement for harmful entries; do not leave them active at low confidence.\n"
                "- Keep at most six non-retired entries and keep the selected target role pure.\n"
                "- text_memory contains conditional category facts only; skill_bundle contains short "
                "executable reasoning steps only."
            ),
            (
                "Output contract:\n"
                "- Output exactly one JSON object and nothing else.\n"
                "- Copy every required_response_bindings value exactly into the same top-level field.\n"
                "- New and updated entries must be provisional; retire proposals must be retired and "
                "include retirement_reason.\n"
                "- entry_id is exactly entry- followed by 24 lowercase hexadecimal characters.\n"
                "- Every proposed entry source_packet_sha256 must equal the top-level "
                "source_packet_sha256 and last_evaluated_block must equal this batch_index.\n"
                "- New support or contradiction UID references must come from this current batch; "
                "preserve all prior references.\n"
                "- For an existing entry, copy positive_flip_refs and negative_flip_refs exactly from "
                "the union of its active entry and cumulative_structured_evidence; a new entry uses "
                "empty flip arrays.\n"
                "- Sort every UID reference array and do not duplicate values.\n"
                f"- UTF-8 output must not exceed {RESPONSE_MAX_UTF8_BYTES} bytes.\n"
                "- Unknown fields, duplicate keys, non-finite numbers, invalid bindings, leakage, "
                "future/Test UID references, and role violations are rejected."
            ),
            "Closed Train-only input envelope:\n"
            + canonical_json_bytes(envelope).decode("utf-8").rstrip("\n"),
        )
    )
    encoded = instruction.encode("utf-8")
    if len(encoded) > PROMPT_MAX_UTF8_BYTES:
        raise SafeReflectorError("SAFE_REFLECTOR_PROMPT_BUDGET_EXCEEDED")
    return SafeReflectorPromptV2(
        instruction=instruction,
        packet_sha256=packet.packet_sha256,
        test_isolation_receipt_sha256=packet.test_isolation_receipt_sha256,
        response_schema_sha256=response_schema_sha,
        prompt_sha256=hashlib.sha256(encoded).hexdigest(),
        prompt_utf8_bytes=len(encoded),
    )


def prepare_reflector_call_v2(
    *,
    packet: SealedReflectorPacketV2,
    ledger: ReflectorLedgerV2,
    service_identity_sha256: str,
) -> SafeReflectorPlanV2:
    if type(packet) is not SealedReflectorPacketV2 or not isinstance(ledger, ReflectorLedgerV2):
        raise TypeError("Reflector preparation inputs are invalid")
    if _SHA256.fullmatch(service_identity_sha256) is None:
        raise ValueError("service identity is invalid")
    token = hashlib.sha256(packet.run_id.encode()).hexdigest()[:20]
    logical = f"safe-reflector-{token}-b{packet.batch_index:02d}"
    if ledger.accepted_call(logical) is not None:
        raise SafeReflectorError("SAFE_REFLECTOR_LOGICAL_CALL_ALREADY_ACCEPTED")
    prior = ledger.latest_claim(logical)
    if prior is None:
        attempt = 1
    else:
        prior_call = prior.get("call_id")
        prior_attempt = prior.get("attempt_number")
        if (
            type(prior_call) is not str
            or type(prior_attempt) is not int
            or not (
                ledger.failure_has_no_completion(prior_call)
                or ledger.completion_was_rejected(prior_call)
            )
        ):
            raise SafeReflectorError("SAFE_REFLECTOR_CALL_OWNERSHIP_UNRESOLVED")
        attempt = prior_attempt + 1
    if attempt > 3:
        raise SafeReflectorError("SAFE_REFLECTOR_ATTEMPT_RETRY_EXHAUSTED")
    call_id = f"{logical}-a{attempt:02d}"
    task_id = "chembench-" + call_id
    prompt = render_reflector_prompt_v2(packet)
    runtime = RuntimeSpec(
        backend="docker",
        profile="managed_science",
        container_user="host",
        image=MANAGED_RUNTIME_RELEASES["managed_science"].loaded_image_id,
        prepare=[PrepareAction(type="exec", command=MANAGED_SUBSCRIPTION_PREPARE_COMMAND)],
        env=dict(MANAGED_SUBSCRIPTION_ENV),
        network=None,
        workdir=MANAGED_WORKSPACE,
        allow_internet=True,
    )
    agent = AgentSpec(
        harness="codex",
        model_name=MODEL,
        settings={
            "auth_mode": "subscription",
            "capture_mode": "transcript",
            "reasoning_effort": REASONING_EFFORT,
            "tool_policy": "disabled",
        },
        env={},
        mcp_servers=[],
        skills_path=None,
    )
    request = TaskRequest(
        task_id=task_id,
        instruction=prompt.instruction,
        num_samples=1,
        timeout_seconds=float(REFLECTOR_TIMEOUT_SECONDS),
        runtime=runtime,
        agent=agent,
        builder=StrategySpec(strategy="agent_transcript"),
        evaluator=None,
        callback_url=None,
        metadata={
            "openevo_chembench": {
                "schema_version": "TemperatureSafeReflectorTaskRequestV2",
                "protocol_id": PROTOCOL_ID,
                "role": "single_target_batch_supervised_reflector",
                "run_id": packet.run_id,
                "fold_id": packet.fold_id,
                "batch_index": packet.batch_index,
                "selected_target": packet.selected_target,
                "packet_sha256": packet.packet_sha256,
                "test_isolation_receipt_sha256": packet.test_isolation_receipt_sha256,
                "prompt_sha256": prompt.prompt_sha256,
                "response_schema_sha256": prompt.response_schema_sha256,
                "logical_syntheses_per_batch": 1,
                "runtime_services_identity_sha256": service_identity_sha256,
                "model_visible_tools_enabled": False,
                "provider_transport_network_enabled": True,
            }
        },
    )
    request_sha = task_request_digest_v2(request)
    claim: dict[str, object] = {
        "logical_call_id": logical,
        "call_id": call_id,
        "task_id": task_id,
        "phase": "train-reflector",
        "logical_arm": "safe_reflector",
        "attempt_number": attempt,
        "task_request_sha256": request_sha,
        "retry_semantics_sha256": task_request_retry_semantics_sha256_v1(request),
        "service_identity_sha256": service_identity_sha256,
    }
    return SafeReflectorPlanV2(
        logical_call_id=logical,
        call_id=call_id,
        attempt_number=attempt,
        prompt=prompt,
        task_request=request,
        task_request_sha256=request_sha,
        claim_payload=claim,
    )


def observe_reflector_status_v2(
    status: TaskStatus, *, plan: SafeReflectorPlanV2
) -> tuple[ObservedReflectorCompletionV1, str, str]:
    try:
        observed = observe_reflector_task_status_v1(
            status, expected_task_id=plan.task_request.task_id
        )
    except TemperatureReflectorError as exc:
        raise SafeReflectorError("SAFE_REFLECTOR_TERMINAL_RESULT_INVALID") from exc
    task_result_sha = sha256_bytes(
        canonical_json_bytes(
            status.model_dump(
                mode="json", exclude_defaults=False, exclude_none=False, exclude_unset=False
            )
        )
    )
    completion_sha = sha256_bytes(
        canonical_json_bytes(
            {
                "task_request_sha256": plan.task_request_sha256,
                "task_result_sha256": task_result_sha,
                "response_sha256": observed.response_sha256,
                "transcript_sha256": observed.transcript.transcript_sha256,
            }
        )
    )
    return observed, task_result_sha, completion_sha


def accept_reflection_v2(
    *,
    packet: SealedReflectorPacketV2,
    plan: SafeReflectorPlanV2,
    observed: ObservedReflectorCompletionV1,
    task_result_sha256: str,
    completion_identity_sha256: str,
    ledger: ReflectorLedgerV2,
) -> AcceptedSafeReflectionV2:
    if ledger.latest_claim(plan.logical_call_id) != plan.claim_payload:
        raise SafeReflectorError("SAFE_REFLECTOR_ACCEPTANCE_WITHOUT_ACTIVE_CLAIM")
    if (
        ledger.accepted_call(plan.logical_call_id) is not None
        or ledger.failure_has_no_completion(plan.call_id)
        or ledger.completion_was_rejected(plan.call_id)
    ):
        raise SafeReflectorError("SAFE_REFLECTOR_ACCEPTANCE_WITHOUT_ACTIVE_CLAIM")
    reflection = _parse_reflection(observed.response)
    _require_bindings(reflection, packet)
    merged = _merge_reflection(reflection, packet)
    # Independent merge-layer binding check; no controller-owned value is ever
    # inserted into the model response after completion.
    _require_bindings(reflection, packet)
    projection = render_full_projection_v2(
        selected_target=packet.selected_target,
        entries=tuple(entry for entry in merged if entry.status != "retired"),
    )
    call_payload: dict[str, object] = {
        "logical_call_id": plan.logical_call_id,
        "call_id": plan.call_id,
        "response": observed.response,
        "response_sha256": observed.response_sha256,
        "task_result_sha256": task_result_sha256,
        "transcript_sha256": observed.transcript.transcript_sha256,
        "completion_identity_sha256": completion_identity_sha256,
        "tool_event_count": 0,
        "tool_policy_validated": True,
    }
    receipt = {
        "schema_version": "TemperatureSafeReflectorAcceptedV2",
        "logical_call_id": plan.logical_call_id,
        "call_id": plan.call_id,
        "attempt_number": plan.attempt_number,
        "run_id": packet.run_id,
        "fold_id": packet.fold_id,
        "batch_index": packet.batch_index,
        "selected_target": packet.selected_target,
        "packet_sha256": packet.packet_sha256,
        "test_isolation_receipt_sha256": packet.test_isolation_receipt_sha256,
        "prompt_sha256": plan.prompt.prompt_sha256,
        "response_schema_sha256": plan.prompt.response_schema_sha256,
        "response_sha256": observed.response_sha256,
        "response_utf8_bytes": len(observed.response.encode("utf-8")),
        "task_result_sha256": task_result_sha256,
        "completion_identity_sha256": completion_identity_sha256,
        "transcript_sha256": observed.transcript.transcript_sha256,
        "proposal_count": len(reflection.proposals),
        "merged_entry_count": len(merged),
        "runtime_entry_count": sum(entry.status != "retired" for entry in merged),
        "projection_sha256": hashlib.sha256(projection.encode()).hexdigest(),
        "projection_utf8_bytes": len(projection.encode("utf-8")),
        "accepted_logical_synthesis_count_for_batch": 1,
        "tool_event_count": 0,
    }
    return AcceptedSafeReflectionV2(
        reflection=reflection,
        merged_entries=merged,
        projection=projection,
        observed=observed,
        task_result_sha256=task_result_sha256,
        completion_identity_sha256=completion_identity_sha256,
        call_accepted_payload=call_payload,
        synthesis_receipt=receipt,
    )


def rebuild_accepted_reflection_v2(
    *,
    packet: SealedReflectorPacketV2,
    plan: SafeReflectorPlanV2,
    accepted_payload: dict[str, Any],
) -> tuple[tuple[CanonicalEntryV2, ...], str, dict[str, object]]:
    """Rebuild synthesis after durable acceptance without another model call.

    ``CALL_ACCEPTED`` is the exactly-once authority.  A process may terminate
    between appending that event and publishing the derived synthesis
    checkpoint.  This function deterministically replays only parsing and merge
    validation from the already accepted response.
    """

    expected = {
        "logical_call_id",
        "call_id",
        "response",
        "response_sha256",
        "task_result_sha256",
        "transcript_sha256",
        "completion_identity_sha256",
        "tool_event_count",
        "tool_policy_validated",
    }
    response = accepted_payload.get("response")
    if (
        set(accepted_payload) != expected
        or accepted_payload.get("logical_call_id") != plan.logical_call_id
        or accepted_payload.get("call_id") != plan.call_id
        or type(response) is not str
        or hashlib.sha256(response.encode("utf-8")).hexdigest()
        != accepted_payload.get("response_sha256")
        or accepted_payload.get("tool_event_count") != 0
        or accepted_payload.get("tool_policy_validated") is not True
        or any(
            _SHA256.fullmatch(str(accepted_payload.get(field))) is None
            for field in (
                "response_sha256",
                "task_result_sha256",
                "transcript_sha256",
                "completion_identity_sha256",
            )
        )
    ):
        raise SafeReflectorError("SAFE_REFLECTOR_ACCEPTED_RESPONSE_INVALID")
    reflection = _parse_reflection(response)
    _require_bindings(reflection, packet)
    merged = _merge_reflection(reflection, packet)
    _require_bindings(reflection, packet)
    projection = render_full_projection_v2(
        selected_target=packet.selected_target,
        entries=tuple(entry for entry in merged if entry.status != "retired"),
    )
    receipt = {
        "schema_version": "TemperatureSafeReflectorAcceptedV2",
        "logical_call_id": plan.logical_call_id,
        "call_id": plan.call_id,
        "attempt_number": plan.attempt_number,
        "run_id": packet.run_id,
        "fold_id": packet.fold_id,
        "batch_index": packet.batch_index,
        "selected_target": packet.selected_target,
        "packet_sha256": packet.packet_sha256,
        "test_isolation_receipt_sha256": packet.test_isolation_receipt_sha256,
        "prompt_sha256": plan.prompt.prompt_sha256,
        "response_schema_sha256": plan.prompt.response_schema_sha256,
        "response_sha256": accepted_payload["response_sha256"],
        "response_utf8_bytes": len(response.encode("utf-8")),
        "task_result_sha256": accepted_payload["task_result_sha256"],
        "completion_identity_sha256": accepted_payload["completion_identity_sha256"],
        "transcript_sha256": accepted_payload["transcript_sha256"],
        "proposal_count": len(reflection.proposals),
        "merged_entry_count": len(merged),
        "runtime_entry_count": sum(entry.status != "retired" for entry in merged),
        "projection_sha256": hashlib.sha256(projection.encode()).hexdigest(),
        "projection_utf8_bytes": len(projection.encode("utf-8")),
        "accepted_logical_synthesis_count_for_batch": 1,
        "tool_event_count": 0,
    }
    return merged, projection, receipt


def rejected_completion_payload_v2(
    *,
    plan: SafeReflectorPlanV2,
    observed: ObservedReflectorCompletionV1,
    task_result_sha256: str,
    completion_identity_sha256: str,
    rejection: SafeReflectorError,
    ledger: ReflectorLedgerV2,
) -> dict[str, object]:
    if rejection.finding_code not in REFLECTOR_REJECTION_CODES:
        raise SafeReflectorError("SAFE_REFLECTOR_REJECTION_NOT_RETRYABLE")
    if (
        ledger.latest_claim(plan.logical_call_id) != plan.claim_payload
        or ledger.accepted_call(plan.logical_call_id) is not None
        or ledger.failure_has_no_completion(plan.call_id)
        or ledger.completion_was_rejected(plan.call_id)
    ):
        raise SafeReflectorError("SAFE_REFLECTOR_REJECTION_WITHOUT_ACTIVE_CLAIM")
    return {
        "logical_call_id": plan.logical_call_id,
        "call_id": plan.call_id,
        "rejection_code": rejection.finding_code,
        "response_sha256": observed.response_sha256,
        "task_result_sha256": task_result_sha256,
        "transcript_sha256": observed.transcript.transcript_sha256,
        "completion_identity_sha256": completion_identity_sha256,
        "durable_completion": True,
        "tool_event_count": 0,
        "tool_policy_validated": True,
    }


def _parse_reflection(response: str) -> SafeReflectionV2:
    if (
        type(response) is not str
        or not 0 < len(response.encode("utf-8")) <= RESPONSE_MAX_UTF8_BYTES
    ):
        raise SafeReflectorError("SAFE_REFLECTOR_RESPONSE_SCHEMA_INVALID")
    try:
        value = json.loads(
            response,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
        return SafeReflectionV2.model_validate(value)
    except SafeReflectorError:
        raise
    except (json.JSONDecodeError, UnicodeError, ValueError, RecursionError) as exc:
        raise SafeReflectorError("SAFE_REFLECTOR_RESPONSE_SCHEMA_INVALID") from exc


def _require_bindings(reflection: SafeReflectionV2, packet: SealedReflectorPacketV2) -> None:
    actual = {
        "run_id": reflection.run_id,
        "fold_id": reflection.fold_id,
        "batch_index": reflection.batch_index,
        "selected_target": reflection.selected_target,
        "predecessor_artifact_sha256": reflection.predecessor_artifact_sha256,
        "prior_evidence_sha256": reflection.prior_evidence_sha256,
        "source_packet_sha256": reflection.source_packet_sha256,
    }
    if actual != packet.required_response_bindings:
        raise SafeReflectorError("SAFE_REFLECTOR_RESPONSE_BINDING_INVALID")


def _merge_reflection(
    reflection: SafeReflectionV2, packet: SealedReflectorPacketV2
) -> tuple[CanonicalEntryV2, ...]:
    existing = {entry.entry_id: entry for entry in packet.active_state.entries}
    merged = dict(existing)
    mandatory = packet.cumulative_evidence.get("mandatory_retirement_entry_ids", [])
    if type(mandatory) is not list or any(type(value) is not str for value in mandatory):
        raise SafeReflectorError("SAFE_REFLECTOR_RESPONSE_SEQUENCE_INVALID")
    evidence_records = packet.cumulative_evidence.get("entry_evidence")
    if type(evidence_records) is not dict:
        raise SafeReflectorError("SAFE_REFLECTOR_RESPONSE_SEQUENCE_INVALID")
    retired_proposals: set[str] = set()
    try:
        for proposal in reflection.proposals:
            entry = proposal.entry
            prior = existing.get(entry.entry_id)
            evidence_record = evidence_records.get(entry.entry_id, {})
            if type(evidence_record) is not dict:
                raise SafeReflectorError("SAFE_REFLECTOR_RESPONSE_SEQUENCE_INVALID")
            expected_positive = _bound_flip_refs(
                evidence_record.get("positive_flip_refs", []),
                prior=() if prior is None else prior.positive_flip_refs,
                allowed=packet.seen_train_uids,
            )
            expected_negative = _bound_flip_refs(
                evidence_record.get("negative_flip_refs", []),
                prior=() if prior is None else prior.negative_flip_refs,
                allowed=packet.seen_train_uids,
            )
            if (
                entry.source_packet_sha256 != packet.packet_sha256
                or entry.last_evaluated_block != packet.batch_index
                or any(batch > packet.batch_index for batch in entry.supporting_batches)
                or entry.positive_flip_refs != expected_positive
                or entry.negative_flip_refs != expected_negative
            ):
                raise SafeReflectorError("SAFE_REFLECTOR_RESPONSE_BINDING_INVALID")
            if proposal.action == "add":
                if (
                    prior is not None
                    or entry.first_seen_batch != packet.batch_index
                    or len(set(entry.support_uid_refs) & packet.current_batch_uids) < 2
                    or not set(entry.support_uid_refs).issubset(packet.current_batch_uids)
                    or not set(entry.contradiction_uid_refs).issubset(
                        packet.current_batch_uids
                    )
                ):
                    raise SafeReflectorError("SAFE_REFLECTOR_RESPONSE_SEQUENCE_INVALID")
            elif prior is None or prior.status == "retired":
                raise SafeReflectorError("SAFE_REFLECTOR_RESPONSE_SEQUENCE_INVALID")
            else:
                new_supports = set(entry.support_uid_refs) - set(prior.support_uid_refs)
                new_contradictions = set(entry.contradiction_uid_refs) - set(
                    prior.contradiction_uid_refs
                )
                new_batches = set(entry.supporting_batches) - set(prior.supporting_batches)
                if (
                    entry.first_seen_batch != prior.first_seen_batch
                    or not set(prior.support_uid_refs).issubset(entry.support_uid_refs)
                    or not set(prior.contradiction_uid_refs).issubset(entry.contradiction_uid_refs)
                    or not set(prior.supporting_batches).issubset(entry.supporting_batches)
                    or not new_supports.issubset(packet.current_batch_uids)
                    or not new_contradictions.issubset(packet.current_batch_uids)
                    or new_batches != ({packet.batch_index} if new_supports else set())
                ):
                    raise SafeReflectorError("SAFE_REFLECTOR_RESPONSE_SEQUENCE_INVALID")
            if proposal.action == "retire":
                assert prior is not None
                retired_proposals.add(entry.entry_id)
                if (
                    entry.content_identity() != prior.content_identity()
                ):
                    raise SafeReflectorError("SAFE_REFLECTOR_RESPONSE_SEQUENCE_INVALID")
            merged[entry.entry_id] = entry
        if not set(mandatory).issubset(retired_proposals):
            raise SafeReflectorError("SAFE_REFLECTOR_RESPONSE_SEQUENCE_INVALID")
        values = tuple(sorted(merged.values(), key=lambda entry: entry.entry_id))
        validate_reflector_entries_v2(
            values,
            selected_target=packet.selected_target,
            allowed_train_uids=packet.seen_train_uids,
            forbidden_normalized_questions=packet.forbidden_normalized_questions,
        )
        return values
    except SafeReflectorError:
        raise
    except (SafeArtifactError, TypeError, ValueError) as exc:
        raise SafeReflectorError("SAFE_REFLECTOR_RESPONSE_LEAKAGE_INVALID") from exc


def _bound_flip_refs(
    value: object, *, prior: tuple[str, ...], allowed: frozenset[str]
) -> tuple[str, ...]:
    if (
        type(value) is not list
        or any(type(item) is not str for item in value)
        or value != sorted(value)
        or len(set(value)) != len(value)
        or not set(value).issubset(allowed)
    ):
        raise SafeReflectorError("SAFE_REFLECTOR_RESPONSE_SEQUENCE_INVALID")
    return tuple(sorted({*prior, *value}))


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise SafeReflectorError("SAFE_REFLECTOR_RESPONSE_SCHEMA_INVALID")
        value[key] = item
    return value


def _reject_nonfinite(_value: str) -> None:
    raise SafeReflectorError("SAFE_REFLECTOR_RESPONSE_SCHEMA_INVALID")


__all__ = [
    "MODEL",
    "PROMPT_MAX_UTF8_BYTES",
    "REASONING_EFFORT",
    "REFLECTOR_TIMEOUT_SECONDS",
    "RESPONSE_MAX_UTF8_BYTES",
    "AcceptedSafeReflectionV2",
    "EntryProposalV2",
    "ReflectorLedgerV2",
    "SafeReflectionV2",
    "SafeReflectorError",
    "SafeReflectorPlanV2",
    "SafeReflectorPromptV2",
    "accept_reflection_v2",
    "observe_reflector_status_v2",
    "prepare_reflector_call_v2",
    "rebuild_accepted_reflection_v2",
    "rejected_completion_payload_v2",
    "render_reflector_prompt_v2",
]
