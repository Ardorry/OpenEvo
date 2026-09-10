"""Fail-closed recovery for a paid paper call with no valid judge result.

The recovery implemented here is deliberately narrower than a retry API.  It
accepts only closed failure classes with an immutable production claim,
terminal receipt, and durable Core evidence.  The failed call remains in the
ledger and each deterministic replacement call ID is authorized before any
replacement dispatch.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .hashing import canonical_sha256
from .paper_evaluator import (
    PAPER_EVALUATOR_DATA_COLLECTION,
    PAPER_EVALUATOR_MAX_OUTPUT_TOKENS,
    PAPER_EVALUATOR_MODEL,
    PAPER_EVALUATOR_PROVIDER,
    PAPER_EVALUATOR_TEMPERATURE,
    PaperEvaluationCall,
    _read_regular_json_no_follow,
    validate_assessment_text,
    validate_paper_evaluation_plan,
    validate_paper_plan_source_audit,
)

PAPER_REPLACEMENT_SCHEMA = "chemcrow_paper_failed_attempt_replacement_v1"
PAPER_INVALID_ASSESSMENT_REPLACEMENT_SCHEMA = (
    "chemcrow_paper_invalid_assessment_replacement_v2"
)
PAPER_PRE_RESPONSE_REPLACEMENT_SCHEMA = "chemcrow_paper_pre_response_replacement_v3"
PAPER_REPLACEMENT_STATUS = "SEALED_FAILED_ATTEMPT_REPLACEMENT_READY"


class PaperNoValidResultPredicates(BaseModel):
    """Closed proof that replacement selection did not inspect a valid score."""

    model_config = ConfigDict(extra="forbid")

    original_result_absent: bool
    original_claim_present_and_bound: bool
    original_terminal_receipt_present_and_bound: bool
    core_session_terminal_error: bool
    core_session_completed: bool = False
    core_session_trajectory_has_zero_traces: bool
    core_session_trajectory_has_one_trace: bool = False
    durable_completion_is_unique: bool
    durable_response_hash_matches_receipt: bool
    durable_response_id_matches_receipt: bool
    upstream_choice_finish_reason_error: bool
    upstream_choice_finish_reason_stop: bool = False
    upstream_choice_error_code_502: bool
    upstream_choice_error_type_provider_unavailable: bool
    upstream_usage_and_cost_are_zero: bool
    upstream_usage_and_cost_are_positive: bool = False
    terminal_failure_receipt: bool = False
    upstream_http_402: bool = False
    upstream_provider_response_absent: bool = False
    upstream_cost_fields_absent: bool = False
    core_completion_absent: bool = False
    durable_assessment_is_invalid: bool
    valid_score_not_observed: bool
    replacement_claim_absent: bool
    replacement_receipt_absent: bool
    replacement_result_absent: bool


class PaperReplacementAuthority(BaseModel):
    """Immutable authority for exactly one fresh-ID replacement call."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[
        "chemcrow_paper_failed_attempt_replacement_v1",
        "chemcrow_paper_invalid_assessment_replacement_v2",
        "chemcrow_paper_pre_response_replacement_v3",
    ]
    status: Literal["SEALED_FAILED_ATTEMPT_REPLACEMENT_READY"]
    failure_class: Literal[
        "zero_cost_provider_failure",
        "invalid_structured_assessment",
        "upstream_http_402_no_response",
    ] = "zero_cost_provider_failure"
    plan_sha256: str
    source_completed_run_audit_sha256: str
    source_aggregate_sha256: str
    source_experiment_id: str
    original_plan_ordinal: int = Field(ge=1, le=42)
    original_call_id: str
    replacement_call_id: str
    replacement_attempt_ordinal: Literal[2]
    original_call_sha256: str
    prompt_sha256: str
    original_claim_sha256: str
    original_receipt_sha256: str
    original_response_sha256: str | None
    original_response_id: str | None
    original_openrouter_reported_cost_usd: float | None = Field(default=None, ge=0.0)
    original_list_price_cost_usd: float | None = Field(default=None, ge=0.0)
    core_session_relative_path: str
    core_session_sha256: str
    core_completion_relative_path: str | None
    core_completion_sha256: str | None
    invalid_assessment_error_types_sha256: str
    no_valid_result_predicates: PaperNoValidResultPredicates
    excluded_attempt_count: Literal[1]
    target_result_count_unchanged: Literal[42]
    prior_replacement_count: int = Field(default=0, ge=0, le=41)
    actual_upstream_call_count_after_success: int = Field(ge=43, le=84)
    failed_call_id_reuse: Literal[False]
    automatic_provider_retry: Literal[False]
    score_driven_retry: Literal[False]
    partial_response_used_as_result: Literal[False]
    prompt_or_response_included: Literal[False]
    recorded_before_replacement_dispatch: Literal[True]
    sealed_at: str

    @model_validator(mode="after")
    def _validate_closed_identity(self) -> PaperReplacementAuthority:
        hashes = tuple(
            value
            for value in (
            self.plan_sha256,
            self.source_completed_run_audit_sha256,
            self.source_aggregate_sha256,
            self.original_call_sha256,
            self.prompt_sha256,
            self.original_claim_sha256,
            self.original_receipt_sha256,
            self.original_response_sha256,
            self.core_session_sha256,
            self.core_completion_sha256,
            self.invalid_assessment_error_types_sha256,
            )
            if value is not None
        )
        if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in hashes):
            raise ValueError("paper replacement authority contains an invalid SHA256")
        if self.replacement_call_id != f"{self.original_call_id}-replacement-01":
            raise ValueError("paper replacement call ID does not bind the original call")
        predicates = self.no_valid_result_predicates
        common_required = (
            predicates.original_result_absent,
            predicates.original_claim_present_and_bound,
            predicates.original_terminal_receipt_present_and_bound,
            predicates.durable_assessment_is_invalid,
            predicates.valid_score_not_observed,
            predicates.replacement_claim_absent,
            predicates.replacement_receipt_absent,
            predicates.replacement_result_absent,
        )
        if not all(common_required):
            raise ValueError("paper replacement common no-valid-result proof differs")
        if self.actual_upstream_call_count_after_success != 43 + self.prior_replacement_count:
            raise ValueError("paper replacement cumulative attempt count differs")
        if self.failure_class == "zero_cost_provider_failure":
            if (
                self.schema_version != PAPER_REPLACEMENT_SCHEMA
                or not predicates.core_session_terminal_error
                or not predicates.core_session_trajectory_has_zero_traces
                or not predicates.upstream_choice_finish_reason_error
                or not predicates.upstream_choice_error_code_502
                or not predicates.upstream_choice_error_type_provider_unavailable
                or not predicates.upstream_usage_and_cost_are_zero
                or predicates.core_session_completed
                or predicates.core_session_trajectory_has_one_trace
                or predicates.upstream_choice_finish_reason_stop
                or predicates.upstream_usage_and_cost_are_positive
                or predicates.terminal_failure_receipt
                or predicates.upstream_http_402
                or predicates.upstream_provider_response_absent
                or predicates.upstream_cost_fields_absent
                or predicates.core_completion_absent
                or not predicates.durable_completion_is_unique
                or not predicates.durable_response_hash_matches_receipt
                or not predicates.durable_response_id_matches_receipt
                or self.original_openrouter_reported_cost_usd != 0
                or self.original_list_price_cost_usd != 0
            ):
                raise ValueError("paper provider-failure replacement proof differs")
        elif self.failure_class == "invalid_structured_assessment" and (
            self.schema_version != PAPER_INVALID_ASSESSMENT_REPLACEMENT_SCHEMA
            or predicates.core_session_terminal_error
            or predicates.core_session_trajectory_has_zero_traces
            or predicates.upstream_choice_finish_reason_error
            or predicates.upstream_choice_error_code_502
            or predicates.upstream_choice_error_type_provider_unavailable
            or predicates.upstream_usage_and_cost_are_zero
            or not predicates.core_session_completed
            or not predicates.core_session_trajectory_has_one_trace
            or not predicates.upstream_choice_finish_reason_stop
            or not predicates.upstream_usage_and_cost_are_positive
            or predicates.terminal_failure_receipt
            or predicates.upstream_http_402
            or predicates.upstream_provider_response_absent
            or predicates.upstream_cost_fields_absent
            or predicates.core_completion_absent
            or not predicates.durable_completion_is_unique
            or not predicates.durable_response_hash_matches_receipt
            or not predicates.durable_response_id_matches_receipt
            or self.original_openrouter_reported_cost_usd <= 0
            or self.original_list_price_cost_usd <= 0
        ):
            raise ValueError("paper invalid-assessment replacement proof differs")
        elif self.failure_class == "upstream_http_402_no_response" and (
            self.schema_version != PAPER_PRE_RESPONSE_REPLACEMENT_SCHEMA
            or not predicates.core_session_terminal_error
            or predicates.core_session_completed
            or predicates.core_session_trajectory_has_zero_traces
            or not predicates.core_session_trajectory_has_one_trace
            or predicates.durable_completion_is_unique
            or predicates.durable_response_hash_matches_receipt
            or predicates.durable_response_id_matches_receipt
            or predicates.upstream_choice_finish_reason_error
            or predicates.upstream_choice_finish_reason_stop
            or predicates.upstream_choice_error_code_502
            or predicates.upstream_choice_error_type_provider_unavailable
            or predicates.upstream_usage_and_cost_are_zero
            or predicates.upstream_usage_and_cost_are_positive
            or not predicates.terminal_failure_receipt
            or not predicates.upstream_http_402
            or not predicates.upstream_provider_response_absent
            or not predicates.upstream_cost_fields_absent
            or not predicates.core_completion_absent
            or self.original_response_sha256 is not None
            or self.original_response_id is not None
            or self.original_openrouter_reported_cost_usd is not None
            or self.original_list_price_cost_usd is not None
            or self.core_completion_relative_path is not None
            or self.core_completion_sha256 is not None
        ):
            raise ValueError("paper pre-response replacement proof differs")
        try:
            sealed_at = datetime.fromisoformat(self.sealed_at)
        except ValueError as exc:
            raise ValueError("paper replacement sealed timestamp is invalid") from exc
        if sealed_at.tzinfo is None:
            raise ValueError("paper replacement sealed timestamp has no timezone")
        return self

    def replacement_call(self, original: PaperEvaluationCall) -> PaperEvaluationCall:
        if (
            original.call_id != self.original_call_id
            or canonical_sha256(original.model_dump(mode="json")) != self.original_call_sha256
            or original.prompt_sha256 != self.prompt_sha256
        ):
            raise ValueError("paper replacement original call authority differs")
        return original.model_copy(update={"call_id": self.replacement_call_id})


def paper_replacement_authority_path(*, plan_path: Path, original_call_id: str) -> Path:
    """Return the only accepted in-ledger authority location."""

    return plan_path.parent / "recovery" / f"{original_call_id}.replacement.json"


def authorize_paper_failed_attempt_replacement(
    *,
    plan_path: Path,
    result_root: Path,
    receipt_root: Path,
    source_run_root: Path,
    completed_run_audit: Path,
    core_completion_root: Path,
    original_call_id: str,
    existing_authority_paths: list[Path] | None = None,
) -> tuple[Path, PaperReplacementAuthority]:
    """Seal one failed attempt and authorize one replacement without model calls."""

    from .paper_core import (
        _validate_existing_result,
        _validate_production_ledger_layout,
    )

    plan, calls, plan_sha256 = _load_plan(plan_path)
    validate_paper_plan_source_audit(
        plan=plan,
        run_root=source_run_root,
        completed_run_audit=completed_run_audit,
    )
    _validate_production_ledger_layout(
        plan_path=plan_path,
        result_root=result_root,
        shim_receipt_root=receipt_root,
    )
    existing_authorities = [
        load_paper_replacement_authority(
            authority_path=path,
            plan_path=plan_path,
            result_root=result_root,
            receipt_root=receipt_root,
            source_run_root=source_run_root,
            completed_run_audit=completed_run_audit,
            core_completion_root=core_completion_root,
        )
        for path in (existing_authority_paths or [])
    ]
    if len({authority.original_call_id for authority in existing_authorities}) != len(
        existing_authorities
    ):
        raise ValueError("paper existing replacement authorities are not unique")
    call_by_id = {call.call_id: call for call in calls}
    if original_call_id not in call_by_id:
        raise ValueError("paper recovery call is outside the frozen plan")
    original = call_by_id[original_call_id]
    original_ordinal = calls.index(original) + 1
    replacement_call_id = f"{original_call_id}-replacement-01"
    if not re.fullmatch(r"[a-z0-9_-]+-replacement-01", replacement_call_id):
        raise ValueError("paper replacement call ID is invalid")

    _validate_exact_pre_replacement_inventory(
        calls=calls,
        original_ordinal=original_ordinal,
        result_root=result_root,
        receipt_root=receipt_root,
        replacement_call_id=replacement_call_id,
        validate_existing_result=_validate_existing_result,
        existing_authorities=existing_authorities,
        existing_authority_paths=list(existing_authority_paths or []),
    )
    evidence = _validate_failed_attempt_evidence(
        original=original,
        receipt_root=receipt_root,
        result_root=result_root,
        core_completion_root=core_completion_root,
    )
    payload = {
        "schema_version": {
            "zero_cost_provider_failure": PAPER_REPLACEMENT_SCHEMA,
            "invalid_structured_assessment": PAPER_INVALID_ASSESSMENT_REPLACEMENT_SCHEMA,
            "upstream_http_402_no_response": PAPER_PRE_RESPONSE_REPLACEMENT_SCHEMA,
        }[evidence["failure_class"]],
        "status": PAPER_REPLACEMENT_STATUS,
        "plan_sha256": plan_sha256,
        "source_completed_run_audit_sha256": plan["source_completed_run_audit_sha256"],
        "source_aggregate_sha256": plan["source_aggregate_sha256"],
        "source_experiment_id": plan["source_experiment_id"],
        "original_plan_ordinal": original_ordinal,
        "original_call_id": original_call_id,
        "replacement_call_id": replacement_call_id,
        "replacement_attempt_ordinal": 2,
        "original_call_sha256": canonical_sha256(original.model_dump(mode="json")),
        "prompt_sha256": original.prompt_sha256,
        **evidence,
        "excluded_attempt_count": 1,
        "target_result_count_unchanged": 42,
        "prior_replacement_count": len(existing_authorities),
        "actual_upstream_call_count_after_success": 43 + len(existing_authorities),
        "failed_call_id_reuse": False,
        "automatic_provider_retry": False,
        "score_driven_retry": False,
        "partial_response_used_as_result": False,
        "prompt_or_response_included": False,
        "recorded_before_replacement_dispatch": True,
        "sealed_at": datetime.now(UTC).isoformat(),
    }
    authority = PaperReplacementAuthority.model_validate(payload)
    output = paper_replacement_authority_path(
        plan_path=plan_path,
        original_call_id=original_call_id,
    )
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = output.parent / ".replacement.lock"
    lock_fd = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        lock_stat = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
            raise ValueError("paper replacement lock is not a link-count-one regular file")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if output.exists():
            loaded = load_paper_replacement_authority(
                authority_path=output,
                plan_path=plan_path,
                result_root=result_root,
                receipt_root=receipt_root,
                source_run_root=source_run_root,
                completed_run_audit=completed_run_audit,
                core_completion_root=core_completion_root,
            )
            return output, loaded
        _atomic_no_replace_json(output, authority.model_dump(mode="json"))
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    return output, authority


def load_paper_replacement_authority(
    *,
    authority_path: Path,
    plan_path: Path,
    result_root: Path,
    receipt_root: Path,
    source_run_root: Path,
    completed_run_audit: Path,
    core_completion_root: Path,
) -> PaperReplacementAuthority:
    """Revalidate an immutable replacement authority and all source evidence."""

    plan, calls, plan_sha256 = _load_plan(plan_path)
    validate_paper_plan_source_audit(
        plan=plan,
        run_root=source_run_root,
        completed_run_audit=completed_run_audit,
    )
    raw, _ = _read_regular_json_no_follow(
        authority_path,
        label="paper replacement authority",
    )
    authority = PaperReplacementAuthority.model_validate(raw)
    expected_path = paper_replacement_authority_path(
        plan_path=plan_path,
        original_call_id=authority.original_call_id,
    )
    if Path(os.path.abspath(os.fspath(authority_path))) != Path(
        os.path.abspath(os.fspath(expected_path))
    ):
        raise ValueError("paper replacement authority is outside the plan ledger")
    call_by_id = {call.call_id: call for call in calls}
    original = call_by_id.get(authority.original_call_id)
    if original is None:
        raise ValueError("paper replacement original call is outside the frozen plan")
    if (
        authority.plan_sha256 != plan_sha256
        or authority.source_completed_run_audit_sha256 != plan["source_completed_run_audit_sha256"]
        or authority.source_aggregate_sha256 != plan["source_aggregate_sha256"]
        or authority.source_experiment_id != plan["source_experiment_id"]
        or authority.original_plan_ordinal != calls.index(original) + 1
        or authority.original_call_sha256 != canonical_sha256(original.model_dump(mode="json"))
        or authority.prompt_sha256 != original.prompt_sha256
        or authority.replacement_call_id != f"{original.call_id}-replacement-01"
    ):
        raise ValueError("paper replacement plan/source binding differs")
    evidence = _validate_failed_attempt_evidence(
        original=original,
        receipt_root=receipt_root,
        result_root=result_root,
        core_completion_root=core_completion_root,
    )
    for key, value in evidence.items():
        observed = getattr(authority, key)
        if hasattr(observed, "model_dump"):
            observed = observed.model_dump(mode="json")
        if observed != value:
            raise ValueError(f"paper replacement evidence binding differs: {key}")
    return authority


def replacement_claim_metadata(
    authority: PaperReplacementAuthority, *, authority_path: Path
) -> dict[str, Any]:
    """Return value-free metadata which binds a replacement claim to authority."""

    raw, authority_sha256 = _read_regular_json_no_follow(
        authority_path,
        label="paper replacement authority",
    )
    if PaperReplacementAuthority.model_validate(raw) != authority:
        raise ValueError("paper replacement authority bytes differ")
    return {
        "replacement_authority_sha256": authority_sha256,
        "replacement_of_call_id": authority.original_call_id,
        "replacement_attempt_ordinal": authority.replacement_attempt_ordinal,
        "explicit_failed_attempt_replacement": True,
    }


def validate_replacement_claim_metadata(
    *,
    payload: dict[str, Any],
    authority: PaperReplacementAuthority,
    authority_path: Path,
) -> None:
    expected = replacement_claim_metadata(authority, authority_path=authority_path)
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError("paper replacement claim/receipt authority differs")
    timestamp = payload.get("claimed_at", payload.get("completed_at"))
    if not isinstance(timestamp, str):
        raise TypeError("paper replacement claim/receipt timestamp is absent")
    try:
        observed_at = datetime.fromisoformat(timestamp)
        sealed_at = datetime.fromisoformat(authority.sealed_at)
    except ValueError as exc:
        raise ValueError("paper replacement claim/receipt timestamp is invalid") from exc
    if observed_at.tzinfo is None or observed_at <= sealed_at:
        raise ValueError("paper replacement dispatch was not recorded after authorization")


def _load_plan(
    plan_path: Path,
) -> tuple[dict[str, Any], list[PaperEvaluationCall], str]:
    plan, plan_sha256 = _read_regular_json_no_follow(
        plan_path,
        label="paper production plan",
    )
    return plan, validate_paper_evaluation_plan(plan), plan_sha256


def _validate_exact_pre_replacement_inventory(
    *,
    calls: list[PaperEvaluationCall],
    original_ordinal: int,
    result_root: Path,
    receipt_root: Path,
    replacement_call_id: str,
    validate_existing_result: Any,
    existing_authorities: list[PaperReplacementAuthority] | None = None,
    existing_authority_paths: list[Path] | None = None,
) -> None:
    expected_completed = calls[: original_ordinal - 1]
    original = calls[original_ordinal - 1]
    authorities = list(existing_authorities or [])
    authority_paths = list(existing_authority_paths or [])
    if len(authorities) != len(authority_paths):
        raise ValueError("paper existing replacement authority path inventory differs")
    authority_by_original = {
        authority.original_call_id: (authority, path)
        for authority, path in zip(authorities, authority_paths, strict=True)
    }
    if not set(authority_by_original) <= {call.call_id for call in expected_completed}:
        raise ValueError("paper existing replacement authority is outside completed prefix")
    expected_result_ids = {
        authority_by_original.get(call.call_id, (None, None))[0].replacement_call_id
        if call.call_id in authority_by_original
        else call.call_id
        for call in expected_completed
    }
    expected_attempt_ids = expected_result_ids | set(authority_by_original) | {original.call_id}
    observed_results = {
        path.name.removesuffix(".result.json") for path in result_root.glob("*.result.json")
    }
    observed_claims = {
        path.name.removesuffix(".claim.json") for path in receipt_root.glob("*.claim.json")
    }
    observed_receipts = {
        path.name.removesuffix(".receipt.json") for path in receipt_root.glob("*.receipt.json")
    }
    if (
        observed_results != expected_result_ids
        or observed_claims != expected_attempt_ids
        or observed_receipts != expected_attempt_ids
        or (result_root / "aggregate.json").exists()
        or (result_root / f"{replacement_call_id}.result.json").exists()
        or (receipt_root / f"{replacement_call_id}.claim.json").exists()
        or (receipt_root / f"{replacement_call_id}.receipt.json").exists()
    ):
        raise ValueError("paper recovery pre-dispatch ledger inventory differs")
    for call in expected_completed:
        authority_and_path = authority_by_original.get(call.call_id)
        authority = authority_and_path[0] if authority_and_path is not None else None
        authority_path = authority_and_path[1] if authority_and_path is not None else None
        effective_call = authority.replacement_call(call) if authority is not None else call
        validate_existing_result(
            result_root / f"{effective_call.call_id}.result.json",
            effective_call,
            claim_path=receipt_root / f"{effective_call.call_id}.claim.json",
            receipt_path=receipt_root / f"{effective_call.call_id}.receipt.json",
            replacement_authority=authority,
            replacement_authority_path=authority_path,
        )


def _validate_pre_response_402_claim_and_receipt(
    *,
    original: PaperEvaluationCall,
    claim: dict[str, Any],
    receipt: dict[str, Any],
) -> None:
    """Prove an OpenRouter credit guardrail stopped before a provider response."""

    request_sha256 = claim.get("request_sha256")
    descriptor = receipt.get("upstream_request_descriptor")
    proxy = receipt.get("environment_proxy")
    endpoints = receipt.get("openrouter_endpoints")
    provider_route = {
        "only": [PAPER_EVALUATOR_PROVIDER],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": PAPER_EVALUATOR_DATA_COLLECTION,
    }
    absent_response_and_cost_fields = (
        "response_sha256",
        "response_id",
        "model",
        "provider",
        "temperature",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "list_price_cost_usd",
        "openrouter_reported_cost_usd",
    )
    if (
        claim.get("schema_version") != "chemcrow_paper_openrouter_claim_v1"
        or claim.get("call_id") != original.call_id
        or re.fullmatch(r"[0-9a-f]{64}", str(request_sha256)) is None
        or claim.get("prompt_or_response_included") is not False
        or claim.get("ledger_class") != "production"
        or claim.get("production_ledger_included") is not True
        or receipt.get("schema_version") != "chemcrow_paper_openrouter_receipt_v1"
        or receipt.get("status") != "terminal_failure_or_ambiguous"
        or receipt.get("call_id") != original.call_id
        or receipt.get("request_sha256") != request_sha256
        or receipt.get("failure") != "upstream_http_402"
        or receipt.get("upstream_http_status") != 402
        or receipt.get("upstream_error_code") != 402
        or receipt.get("upstream_error_category") != "budget_or_credit_guardrail"
        or receipt.get("upstream_response_body_included") is not False
        or receipt.get("upstream_error_metadata_body_included") is not False
        or receipt.get("openrouter_metadata_body_included") is not False
        or receipt.get("prompt_or_response_included") is not False
        or receipt.get("credential_included") is not False
        or receipt.get("ledger_class") != "production"
        or receipt.get("production_ledger_included") is not True
        or receipt.get("internal_call_identity_validated") is not True
        or receipt.get("internal_response_format_validated") is not True
        or receipt.get("upstream_response_format_omitted") is not True
        or receipt.get("upstream_user_omitted") is not True
        or receipt.get("openrouter_requested") != PAPER_EVALUATOR_MODEL
        or receipt.get("openrouter_strategy") != "direct"
        or receipt.get("openrouter_is_byok") is not False
        or any(field in receipt for field in absent_response_and_cost_fields)
        or not isinstance(endpoints, list)
        or not endpoints
        or any(
            not isinstance(endpoint, dict)
            or endpoint.get("model") != PAPER_EVALUATOR_MODEL
            or str(endpoint.get("provider", "")).lower() != PAPER_EVALUATOR_PROVIDER
            or endpoint.get("selected") is not False
            for endpoint in endpoints
        )
        or not isinstance(descriptor, dict)
        or descriptor.get("model") != PAPER_EVALUATOR_MODEL
        or descriptor.get("temperature") != PAPER_EVALUATOR_TEMPERATURE
        or descriptor.get("max_tokens") != PAPER_EVALUATOR_MAX_OUTPUT_TOKENS
        or descriptor.get("stream") is not False
        or descriptor.get("provider") != provider_route
        or descriptor.get("user_present") is not False
        or descriptor.get("response_format_present") is not False
        or descriptor.get("message_count") != 2
        or descriptor.get("message_roles") != ["system", "user"]
        or not isinstance(proxy, dict)
        or proxy.get("mode") != "environment_proxy"
        or proxy.get("credentials_present") is not False
    ):
        raise ValueError("paper HTTP 402 claim/receipt authority differs")
    for field in (
        "upstream_error_message_sha256",
        "upstream_error_metadata_sha256",
        "upstream_response_sha256",
        "openrouter_metadata_sha256",
        "openrouter_summary_sha256",
    ):
        if re.fullmatch(r"[0-9a-f]{64}", str(receipt.get(field))) is None:
            raise ValueError(f"paper HTTP 402 receipt {field} is invalid")


def _validate_failed_attempt_evidence(
    *,
    original: PaperEvaluationCall,
    receipt_root: Path,
    result_root: Path,
    core_completion_root: Path,
) -> dict[str, Any]:
    from .paper_core import _validate_claim_and_receipt

    result_path = result_root / f"{original.call_id}.result.json"
    claim_path = receipt_root / f"{original.call_id}.claim.json"
    receipt_path = receipt_root / f"{original.call_id}.receipt.json"
    if result_path.exists():
        raise ValueError("paper failed attempt already has a result")
    claim, claim_sha256 = _read_regular_json_no_follow(
        claim_path,
        label="paper failed OpenRouter claim",
    )
    receipt, receipt_sha256 = _read_regular_json_no_follow(
        receipt_path,
        label="paper failed OpenRouter receipt",
    )
    pre_response_402 = receipt.get("status") == "terminal_failure_or_ambiguous"
    if pre_response_402:
        _validate_pre_response_402_claim_and_receipt(
            original=original,
            claim=claim,
            receipt=receipt,
        )
    else:
        validated_claim, validated_receipt = _validate_claim_and_receipt(
            call=original,
            claim_path=claim_path,
            receipt_path=receipt_path,
        )
        if validated_claim != claim or validated_receipt != receipt:
            raise ValueError("paper failed claim/receipt changed while validating")

    task_root = core_completion_root / f"task_{original.call_id}"
    session_paths = sorted(task_root.glob("ses_*.json"))
    completion_paths = sorted(task_root.glob("sessions/*/completions/*.json"))
    expected_completion_count = 0 if pre_response_402 else 1
    if len(session_paths) != 1 or len(completion_paths) != expected_completion_count:
        raise ValueError("paper failed attempt durable Core inventory is not unique")
    session_path = session_paths[0]
    session, session_sha256 = _read_regular_json_no_follow(
        session_path,
        label="paper failed Core session",
    )
    completion_path = completion_paths[0] if completion_paths else None
    if completion_path is not None:
        completion, completion_sha256 = _read_regular_json_no_follow(
            completion_path,
            label="paper failed Gateway completion",
        )
    else:
        completion = None
        completion_sha256 = None
    trajectory = session.get("trajectory")
    traces = trajectory.get("traces") if isinstance(trajectory, dict) else None
    provider_failure_session = (
        session.get("task_id") == original.call_id
        and
        session.get("status") == "ERROR"
        and isinstance(session.get("error"), str)
        and bool(session["error"])
        and traces == []
        and session.get("workspace_result") is None
    )
    invalid_assessment_session = (
        session.get("task_id") == original.call_id
        and session.get("status") == "COMPLETED"
        and session.get("error") is None
        and isinstance(traces, list)
        and len(traces) == 1
        and session.get("workspace_result") is None
    )
    pre_response_402_session = (
        session.get("task_id") == original.call_id
        and session.get("status") == "ERROR"
        and isinstance(session.get("error"), str)
        and bool(session["error"])
        and isinstance(traces, list)
        and len(traces) == 1
        and session.get("workspace_result") is None
        and completion is None
    )
    if pre_response_402 != pre_response_402_session:
        raise ValueError("paper HTTP 402 Core/receipt evidence differs")
    if not any(
        (
            provider_failure_session,
            invalid_assessment_session,
            pre_response_402_session,
        )
    ):
        raise ValueError("paper failed Core session does not prove an allowed failure")
    session_id = session.get("session_id")
    if not isinstance(session_id, str):
        raise TypeError("paper failed Core session identity differs")
    if completion is not None and (
        completion.get("task_id") != original.call_id
        or completion.get("session_id") != session_id
        or completion_path is None
        or completion_path.parent.parent.name != session_id
        or completion.get("api_type") != "openai_chat"
        or completion.get("model_requested") != PAPER_EVALUATOR_MODEL
        or completion.get("model_used") != PAPER_EVALUATOR_MODEL
    ):
        raise ValueError("paper failed Gateway completion identity differs")
    expected_request = {
        "model": PAPER_EVALUATOR_MODEL,
        "temperature": 0.1,
        "max_tokens": 1200,
        "stream": False,
        "user": original.call_id,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "ChemCrow sealed-output paper evaluator. Return strict JSON only.",
            },
            {"role": "user", "content": original.prompt},
        ],
    }
    if completion is not None:
        original_request = completion.get("original_request")
        transformed_request = completion.get("transformed_request")
        if original_request != expected_request or transformed_request != expected_request:
            raise ValueError("paper failed Gateway request differs from frozen call")
    claimed_request = {
        **expected_request,
        "logprobs": True,
        "top_logprobs": 0,
        "return_token_ids": True,
    }
    if canonical_sha256(claimed_request) != claim.get("request_sha256"):
        raise ValueError("paper failed Gateway request hash differs from shim claim")

    if pre_response_402:
        session_relative = session_path.relative_to(core_completion_root)
        return {
            "failure_class": "upstream_http_402_no_response",
            "original_claim_sha256": claim_sha256,
            "original_receipt_sha256": receipt_sha256,
            "original_response_sha256": None,
            "original_response_id": None,
            "original_openrouter_reported_cost_usd": None,
            "original_list_price_cost_usd": None,
            "core_session_relative_path": session_relative.as_posix(),
            "core_session_sha256": session_sha256,
            "core_completion_relative_path": None,
            "core_completion_sha256": None,
            "invalid_assessment_error_types_sha256": canonical_sha256(
                ["upstream_http_402_no_response"]
            ),
            "no_valid_result_predicates": {
                "original_result_absent": True,
                "original_claim_present_and_bound": True,
                "original_terminal_receipt_present_and_bound": True,
                "core_session_terminal_error": True,
                "core_session_completed": False,
                "core_session_trajectory_has_zero_traces": False,
                "core_session_trajectory_has_one_trace": True,
                "durable_completion_is_unique": False,
                "durable_response_hash_matches_receipt": False,
                "durable_response_id_matches_receipt": False,
                "upstream_choice_finish_reason_error": False,
                "upstream_choice_finish_reason_stop": False,
                "upstream_choice_error_code_502": False,
                "upstream_choice_error_type_provider_unavailable": False,
                "upstream_usage_and_cost_are_zero": False,
                "upstream_usage_and_cost_are_positive": False,
                "terminal_failure_receipt": True,
                "upstream_http_402": True,
                "upstream_provider_response_absent": True,
                "upstream_cost_fields_absent": True,
                "core_completion_absent": True,
                "durable_assessment_is_invalid": True,
                "valid_score_not_observed": True,
                "replacement_claim_absent": True,
                "replacement_receipt_absent": True,
                "replacement_result_absent": True,
            },
        }

    if completion is None:
        raise ValueError("paper failed Gateway completion is absent")
    response = completion.get("response")
    if not isinstance(response, dict) or canonical_sha256(response) != receipt.get(
        "response_sha256"
    ):
        raise ValueError("paper failed durable response hash differs from receipt")
    if response.get("id") != receipt.get("response_id"):
        raise ValueError("paper failed durable response ID differs from receipt")
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ValueError("paper failed durable response choice inventory differs")
    choice = choices[0]
    choice_error = choice.get("error")
    error_metadata = choice_error.get("metadata") if isinstance(choice_error, dict) else None
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    usage = response.get("usage")
    provider_failure_response = (
        choice.get("finish_reason") == "error"
        and
        isinstance(choice_error, dict)
        and choice_error.get("code") == 502
        and isinstance(error_metadata, dict)
        and error_metadata.get("error_type") == "provider_unavailable"
        and isinstance(content, str)
        and bool(content)
        and isinstance(usage, dict)
        and usage.get("prompt_tokens") == 0
        and usage.get("completion_tokens") == 0
        and usage.get("total_tokens") == 0
        and usage.get("cost") == 0
        and receipt.get("prompt_tokens") == 0
        and receipt.get("completion_tokens") == 0
        and receipt.get("total_tokens") == 0
        and receipt.get("openrouter_reported_cost_usd") == 0
        and receipt.get("list_price_cost_usd") == 0
    )
    invalid_assessment_response = (
        choice.get("finish_reason") == "stop"
        and choice_error is None
        and isinstance(content, str)
        and bool(content)
        and isinstance(usage, dict)
        and isinstance(usage.get("prompt_tokens"), int)
        and usage["prompt_tokens"] > 0
        and isinstance(usage.get("completion_tokens"), int)
        and usage["completion_tokens"] > 0
        and usage.get("total_tokens")
        == usage["prompt_tokens"] + usage["completion_tokens"]
        and usage.get("prompt_tokens") == receipt.get("prompt_tokens")
        and usage.get("completion_tokens") == receipt.get("completion_tokens")
        and usage.get("total_tokens") == receipt.get("total_tokens")
        and usage.get("cost") == receipt.get("openrouter_reported_cost_usd")
        and isinstance(receipt.get("list_price_cost_usd"), (int, float))
        and receipt["list_price_cost_usd"] > 0
        and isinstance(receipt.get("openrouter_reported_cost_usd"), (int, float))
        and receipt["openrouter_reported_cost_usd"] > 0
    )
    if provider_failure_session != provider_failure_response:
        raise ValueError("paper failed Core/provider failure classes differ")
    if invalid_assessment_session != invalid_assessment_response:
        raise ValueError("paper invalid-assessment Core/provider evidence differs")
    if not provider_failure_response and not invalid_assessment_response:
        raise ValueError("paper failed response is outside the allowed recovery classes")
    try:
        validate_assessment_text(content)
    except ValidationError as exc:
        error_types = [
            item["type"]
            for item in exc.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            )
        ]
    except (TypeError, ValueError) as exc:
        error_types = [type(exc).__name__]
    else:
        raise ValueError("paper failed response unexpectedly contains a valid assessment")
    if not error_types:
        raise ValueError("paper failed assessment error classification is absent")

    session_relative = session_path.relative_to(core_completion_root)
    completion_relative = completion_path.relative_to(core_completion_root)
    provider_failure = provider_failure_response
    return {
        "failure_class": (
            "zero_cost_provider_failure"
            if provider_failure
            else "invalid_structured_assessment"
        ),
        "original_claim_sha256": claim_sha256,
        "original_receipt_sha256": receipt_sha256,
        "original_response_sha256": receipt["response_sha256"],
        "original_response_id": receipt["response_id"],
        "original_openrouter_reported_cost_usd": float(
            receipt["openrouter_reported_cost_usd"]
        ),
        "original_list_price_cost_usd": float(receipt["list_price_cost_usd"]),
        "core_session_relative_path": session_relative.as_posix(),
        "core_session_sha256": session_sha256,
        "core_completion_relative_path": completion_relative.as_posix(),
        "core_completion_sha256": completion_sha256,
        "invalid_assessment_error_types_sha256": canonical_sha256(error_types),
        "no_valid_result_predicates": {
            "original_result_absent": True,
            "original_claim_present_and_bound": True,
            "original_terminal_receipt_present_and_bound": True,
            "core_session_terminal_error": provider_failure,
            "core_session_completed": not provider_failure,
            "core_session_trajectory_has_zero_traces": provider_failure,
            "core_session_trajectory_has_one_trace": not provider_failure,
            "durable_completion_is_unique": True,
            "durable_response_hash_matches_receipt": True,
            "durable_response_id_matches_receipt": True,
            "upstream_choice_finish_reason_error": provider_failure,
            "upstream_choice_finish_reason_stop": not provider_failure,
            "upstream_choice_error_code_502": provider_failure,
            "upstream_choice_error_type_provider_unavailable": provider_failure,
            "upstream_usage_and_cost_are_zero": provider_failure,
            "upstream_usage_and_cost_are_positive": not provider_failure,
            "terminal_failure_receipt": False,
            "upstream_http_402": False,
            "upstream_provider_response_absent": False,
            "upstream_cost_fields_absent": False,
            "core_completion_absent": False,
            "durable_assessment_is_invalid": True,
            "valid_score_not_observed": True,
            "replacement_claim_absent": True,
            "replacement_receipt_absent": True,
            "replacement_result_absent": True,
        },
    }


def _atomic_no_replace_json(path: Path, payload: dict[str, Any]) -> None:
    """Publish canonical JSON without overwrite and fsync file plus directory."""

    serialized = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o600,
    )
    try:
        offset = 0
        while offset < len(serialized):
            offset += os.write(descriptor, serialized[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, path, follow_symlinks=False)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
