"""Read-only formal closure verification for Temperature full-evolve v1.

The aggregate report input is intentionally only a staged, owner-private
``READY_FOR_AUDIT`` document.  This module replays the private SHA-chained
ledger, binds that document to the frozen run and preflight identities, and
issues the only object accepted by the public report publisher.

No benchmark content is projected into the closure receipt.  Questions,
options, targets, per-item predictions, completions, evaluations and
transcripts remain in the owner-private evidence tree.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_private_file,
)
from openevo_chembench.supervised_transfer_v2.context_binding import (
    SupervisedSessionContextBindingV2,
    SupervisedTargetBindingV2,
    issue_supervised_context_binding_receipt_v2,
)
from openevo_chembench.temperature_full_evolve_v1.config import PROTOCOL_ID
from openevo_chembench.temperature_full_evolve_v1.ledger import (
    LEDGER_SCHEMA,
    MAX_EVENT_BYTES,
    MAX_LEDGER_BYTES,
    TemperatureExperimentLedgerV1,
)

CLOSURE_RECEIPT_SCHEMA = "TemperatureFormalClosureReceiptV1"
RUN_MANIFEST_SCHEMA = "TemperatureFullEvolveFormalRunManifestV1"
MAX_RUN_MANIFEST_BYTES = 256 * 1024
MAX_PREFLIGHT_FILE_BYTES = 2 * 1024 * 1024
MAX_AGGREGATE_BYTES = 2 * 1024 * 1024

_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_COMMIT = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,191}\Z", re.ASCII)
_UTC = re.compile(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_PRECHECK_JSON_FILES = {
    "report": "preflight_report.json",
    "split_manifest": "split_manifest.json",
    "historical_manifest": "historical_exclusion_manifest.json",
    "grouping_manifest": "near_duplicate_group_manifest.json",
    "config_manifest": "config_manifest.json",
    "model_identity_receipt": "model_identity_receipt.json",
    "runtime_identity_receipt": "managed_runtime_receipt.json",
    "regression_receipt": "regression_receipt_v1.json",
}
_CLOSURE_TOKEN = object()


class TemperatureClosureError(RuntimeError):
    """Content-free closure admission finding."""

    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


class FormalClosureReceiptV1(BaseModel):
    """Aggregate-only evidence emitted after all private checks pass."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["TemperatureFormalClosureReceiptV1"]
    status: Literal["COMPLETE"]
    protocol_id: Literal["chembench_temperature_full_evolve_v1"]
    issued_at_utc: str
    controller_run_id: str
    evolved_run_id: str
    baseline_run_id: str
    core_run_id: str
    runtime_service_run_id: str
    all_run_ids: tuple[str, ...] = Field(min_length=5, max_length=16)
    source_commit: str
    config_sha256: str
    split_sha256: str
    dataset_sha256: str
    formal_runtime_identity_sha256: str
    formal_runtime_receipt_sha256: str
    source_tree_sha256: str
    core_wheel_sha256: str
    chembench_wheel_sha256: str
    core_editable: Literal[False]
    chembench_editable: Literal[False]
    preflight_bundle_sha256: str
    run_manifest_sha256: str
    runtime_services_identity_sha256: str
    experiment_protocol_sha256: str
    aggregate_input_sha256: str
    aggregate_input_utf8_bytes: int = Field(ge=2, le=MAX_AGGREGATE_BYTES)
    ledger_event_count: int = Field(ge=1)
    ledger_head_sha256: str
    audit_closed_sequence: int = Field(ge=1)
    audit_closed_event_sha256: str
    audit_closed_at_utc: str
    unresolved_call_count: Literal[0]
    run_failed_event_count: Literal[0]
    incident_opened_event_count: Literal[0]
    claimed_logical_call_count: Literal[404]
    managed_runtime_session_attempt_count: int = Field(ge=404)
    readiness_passed_for_accepted_call_count: Literal[404]
    canary_provider_attempt_measurement_status: Literal["NOT_MEASURED"]
    canary_provider_attempt_lower_bound: Literal[404]
    accepted_candidate_count: Literal[400]
    evaluated_candidate_count: Literal[400]
    accepted_reflector_count: Literal[4]
    rejected_reflector_attempt_count: int = Field(ge=0, le=8)
    core_job_count: Literal[12]
    preflight_model_call_count: Literal[0]
    contains_item_identities: Literal[False]
    contains_benchmark_content: Literal[False]
    contains_answers_or_predictions: Literal[False]
    contains_completions_or_transcripts: Literal[False]
    receipt_sha256: str

    @model_validator(mode="after")
    def _closed(self) -> FormalClosureReceiptV1:
        run_ids = {
            self.controller_run_id,
            self.evolved_run_id,
            self.baseline_run_id,
            self.core_run_id,
            self.runtime_service_run_id,
        }
        digests = (
            self.config_sha256,
            self.split_sha256,
            self.dataset_sha256,
            self.formal_runtime_identity_sha256,
            self.formal_runtime_receipt_sha256,
            self.source_tree_sha256,
            self.core_wheel_sha256,
            self.chembench_wheel_sha256,
            self.preflight_bundle_sha256,
            self.run_manifest_sha256,
            self.runtime_services_identity_sha256,
            self.experiment_protocol_sha256,
            self.aggregate_input_sha256,
            self.ledger_head_sha256,
            self.audit_closed_event_sha256,
            self.receipt_sha256,
        )
        if (
            self.schema_version != CLOSURE_RECEIPT_SCHEMA
            or self.protocol_id != PROTOCOL_ID
            or _UTC.fullmatch(self.issued_at_utc) is None
            or _UTC.fullmatch(self.audit_closed_at_utc) is None
            or _COMMIT.fullmatch(self.source_commit) is None
            or any(_SAFE_ID.fullmatch(value) is None for value in (*run_ids, *self.all_run_ids))
            or len(set(self.all_run_ids)) != len(self.all_run_ids)
            or not run_ids <= set(self.all_run_ids)
            or any(_SHA256.fullmatch(value) is None for value in digests)
            or self.ledger_event_count != self.audit_closed_sequence
            or self.ledger_head_sha256 != self.audit_closed_event_sha256
            or self.managed_runtime_session_attempt_count
            < self.claimed_logical_call_count
            or self.readiness_passed_for_accepted_call_count
            != self.accepted_candidate_count + self.accepted_reflector_count
        ):
            raise ValueError("formal closure receipt identity is invalid")
        body = self.model_dump(mode="json", exclude={"receipt_sha256"})
        if sha256_bytes(canonical_json_bytes(body)) != self.receipt_sha256:
            raise ValueError("formal closure receipt digest is invalid")
        return self


@dataclass(frozen=True, slots=True)
class VerifiedFormalClosureV1:
    """Process-local authority returned only by the private verifier."""

    receipt: FormalClosureReceiptV1
    _token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.receipt) is not FormalClosureReceiptV1 or self._token is not _CLOSURE_TOKEN:
            raise TypeError("formal closure authority must be verifier-issued")

    def __repr__(self) -> str:
        return "VerifiedFormalClosureV1(<aggregate-only-verified-receipt>)"


def verify_formal_closure_v1(
    *,
    run_root: Path,
    preflight_root: Path,
    aggregate_input_path: Path,
) -> VerifiedFormalClosureV1:
    """Replay private evidence and issue aggregate-only publication authority."""

    run = _private_directory(run_root)
    private = _private_directory(run / "private")
    preflight = _safe_directory(preflight_root)
    aggregate_path = _absolute_path(aggregate_input_path)
    expected_aggregate = private / "aggregate_report_input_v1.json"
    if aggregate_path != expected_aggregate:
        raise TemperatureClosureError("CLOSURE_AGGREGATE_PATH_NOT_RUN_BOUND")

    manifest_path = private / "run_manifest_v1.json"
    ledger_path = private / "events.jsonl"
    manifest_bytes = _read_regular_file(
        manifest_path,
        maximum=MAX_RUN_MANIFEST_BYTES,
        private=True,
    )
    aggregate_bytes = _read_regular_file(
        aggregate_path,
        maximum=MAX_AGGREGATE_BYTES,
        private=True,
    )
    ledger_bytes = _read_regular_file(
        ledger_path,
        maximum=MAX_LEDGER_BYTES,
        private=True,
    )
    manifest = _decode_json_object(manifest_bytes, "CLOSURE_RUN_MANIFEST_INVALID")
    if manifest.get("schema_version") != RUN_MANIFEST_SCHEMA:
        raise TemperatureClosureError("CLOSURE_RUN_MANIFEST_INVALID")

    # Local import keeps the report DTO independent from its publication gate.
    from openevo_chembench.temperature_full_evolve_v1.reporting import (
        AggregateReportInputV1,
    )

    try:
        report = AggregateReportInputV1.model_validate_json(aggregate_bytes, strict=True)
    except Exception as exc:
        raise TemperatureClosureError("CLOSURE_AGGREGATE_INPUT_INVALID") from exc
    if canonical_pretty_json_bytes(report.model_dump(mode="json")) != aggregate_bytes:
        raise TemperatureClosureError("CLOSURE_AGGREGATE_INPUT_NOT_CANONICAL")
    if report.status != "READY_FOR_AUDIT":
        raise TemperatureClosureError("CLOSURE_AGGREGATE_INPUT_STATUS_INVALID")

    events = _decode_and_validate_ledger(
        ledger_bytes,
        controller_run_id=report.run_ids.controller_run_id,
    )
    audit = _require_terminal_audit(
        events,
        aggregate_sha256=sha256_bytes(aggregate_bytes),
    )
    counts = _ledger_counts(events)
    if counts != {
        "unresolved_call_count": 0,
        "run_failed_event_count": 0,
        "incident_opened_event_count": 0,
        "claimed_logical_call_count": 404,
        "accepted_candidate_count": 400,
        "evaluated_candidate_count": 400,
        "accepted_reflector_count": 4,
        "rejected_reflector_attempt_count": counts[
            "rejected_reflector_attempt_count"
        ],
        "core_job_count": 12,
    } or not 0 <= counts["rejected_reflector_attempt_count"] <= 8:
        raise TemperatureClosureError("CLOSURE_LEDGER_COUNTS_INVALID")
    _bind_ledger_aggregates(report=report, events=events)

    preflight_payloads, preflight_digest = _load_preflight_bundle(preflight)
    protocol_bytes = _read_regular_file(
        preflight / "EXPERIMENT_PROTOCOL.md",
        maximum=MAX_PREFLIGHT_FILE_BYTES,
        private=False,
    )
    _bind_preflight_report(
        report=report,
        payloads=preflight_payloads,
        bundle_sha256=preflight_digest,
    )
    _bind_run_manifest(
        manifest=manifest,
        report=report,
        preflight_bundle_sha256=preflight_digest,
    )

    receipt_body: dict[str, object] = {
        "schema_version": CLOSURE_RECEIPT_SCHEMA,
        "status": "COMPLETE",
        "protocol_id": PROTOCOL_ID,
        "issued_at_utc": _utc_seconds(str(audit["recorded_at_utc"])),
        "controller_run_id": report.run_ids.controller_run_id,
        "evolved_run_id": report.run_ids.evolved_run_id,
        "baseline_run_id": report.run_ids.baseline_run_id,
        "core_run_id": report.run_ids.core_run_id,
        "runtime_service_run_id": report.run_ids.runtime_service_run_id,
        "all_run_ids": report.run_ids.all_run_ids,
        "source_commit": report.identity.source_commit,
        "config_sha256": report.identity.config_sha256,
        "split_sha256": report.identity.split_sha256,
        "dataset_sha256": report.identity.dataset_sha256,
        "formal_runtime_identity_sha256": (
            report.identity.formal_runtime_identity_sha256
        ),
        "formal_runtime_receipt_sha256": (
            report.identity.formal_runtime_receipt_sha256
        ),
        "source_tree_sha256": report.identity.source_tree_sha256,
        "core_wheel_sha256": report.identity.core_wheel_sha256,
        "chembench_wheel_sha256": report.identity.chembench_wheel_sha256,
        "core_editable": False,
        "chembench_editable": False,
        "preflight_bundle_sha256": preflight_digest,
        "run_manifest_sha256": sha256_bytes(manifest_bytes),
        "runtime_services_identity_sha256": report.identity.runtime_services_identity_sha256,
        "experiment_protocol_sha256": sha256_bytes(protocol_bytes),
        "aggregate_input_sha256": sha256_bytes(aggregate_bytes),
        "aggregate_input_utf8_bytes": len(aggregate_bytes),
        "ledger_event_count": len(events),
        "ledger_head_sha256": str(audit["event_sha256"]),
        "audit_closed_sequence": int(audit["sequence"]),
        "audit_closed_event_sha256": str(audit["event_sha256"]),
        "audit_closed_at_utc": _utc_seconds(str(audit["recorded_at_utc"])),
        **counts,
        "managed_runtime_session_attempt_count": (
            report.calls.managed_runtime_session_attempts
        ),
        "readiness_passed_for_accepted_call_count": (
            report.calls.readiness_passed_for_accepted_calls
        ),
        "canary_provider_attempt_measurement_status": (
            report.calls.canary_provider_attempts.status
        ),
        "canary_provider_attempt_lower_bound": (
            report.calls.canary_provider_attempts.lower_bound
        ),
        "preflight_model_call_count": 0,
        "contains_item_identities": False,
        "contains_benchmark_content": False,
        "contains_answers_or_predictions": False,
        "contains_completions_or_transcripts": False,
    }
    receipt = FormalClosureReceiptV1.model_validate(
        {
            **receipt_body,
            "receipt_sha256": sha256_bytes(canonical_json_bytes(receipt_body)),
        },
        strict=True,
    )
    return VerifiedFormalClosureV1(receipt=receipt, _token=_CLOSURE_TOKEN)


def write_formal_closure_receipt_v1(
    closure: VerifiedFormalClosureV1,
    *,
    path: Path,
) -> str:
    """Persist one verifier-issued owner-private receipt idempotently."""

    if type(closure) is not VerifiedFormalClosureV1:
        raise TypeError("closure must be verifier-issued")
    target = _absolute_path(path)
    encoded = canonical_pretty_json_bytes(closure.receipt.model_dump(mode="json"))
    if target.exists():
        existing = _read_regular_file(target, maximum=MAX_RUN_MANIFEST_BYTES, private=True)
        if existing != encoded:
            raise TemperatureClosureError("CLOSURE_RECEIPT_DRIFT")
    else:
        write_private_file(target, encoded, replace=False)
    return sha256_bytes(encoded)


def _decode_and_validate_ledger(
    encoded: bytes,
    *,
    controller_run_id: str,
) -> tuple[dict[str, Any], ...]:
    if not encoded or not encoded.endswith(b"\n"):
        raise TemperatureClosureError("CLOSURE_LEDGER_TRUNCATED")
    events: list[dict[str, Any]] = []
    for raw_line in encoded.splitlines(keepends=True):
        if len(raw_line) > MAX_EVENT_BYTES:
            raise TemperatureClosureError("CLOSURE_LEDGER_EVENT_TOO_LARGE")
        value = _decode_json_object(raw_line, "CLOSURE_LEDGER_JSON_INVALID")
        if value.get("schema_version") != LEDGER_SCHEMA or canonical_json_bytes(value) != raw_line:
            raise TemperatureClosureError("CLOSURE_LEDGER_EVENT_NOT_CANONICAL")
        events.append(value)
    if not events:
        raise TemperatureClosureError("CLOSURE_LEDGER_EMPTY")
    try:
        validator = object.__new__(TemperatureExperimentLedgerV1)
        validator._run_id = controller_run_id
        validator._validate_transitions(events)
    except Exception as exc:
        raise TemperatureClosureError("CLOSURE_LEDGER_VALIDATION_FAILED") from exc
    return tuple(events)


def _require_terminal_audit(
    events: tuple[dict[str, Any], ...],
    *,
    aggregate_sha256: str,
) -> dict[str, Any]:
    audit_events = [event for event in events if event.get("kind") == "AUDIT_CLOSED"]
    if len(audit_events) != 1 or events[-1] is not audit_events[0]:
        raise TemperatureClosureError("CLOSURE_AUDIT_NOT_TERMINAL")
    audit = audit_events[0]
    payload = audit.get("payload")
    if payload != {
        "aggregate_report_input_sha256": aggregate_sha256,
        "checksums_verified": True,
    }:
        raise TemperatureClosureError("CLOSURE_AUDIT_AGGREGATE_BINDING_INVALID")
    return audit


def _ledger_counts(events: tuple[dict[str, Any], ...]) -> dict[str, int]:
    claims: dict[str, dict[str, Any]] = {}
    accepted: set[str] = set()
    evaluated: set[str] = set()
    run_failed = 0
    incident_opened = 0
    core_jobs = 0
    rejected_reflector_attempts = 0
    for event in events:
        kind = event["kind"]
        payload = event["payload"]
        if kind == "CALL_CLAIMED":
            claims[str(payload["logical_call_id"])] = payload
        elif kind == "CALL_ACCEPTED":
            accepted.add(str(payload["logical_call_id"]))
        elif kind == "CALL_EVALUATED":
            evaluated.add(str(payload["logical_call_id"]))
        elif kind == "TARGET_JOB_COMPLETED":
            core_jobs += 1
        elif kind == "CALL_REJECTED_COMPLETION":
            rejected_reflector_attempts += 1
        elif kind == "RUN_FAILED":
            run_failed += 1
        elif kind == "INCIDENT_OPENED":
            incident_opened += 1
    unresolved = sum(logical not in accepted for logical in claims)
    candidate = {
        logical for logical in accepted if claims.get(logical, {}).get("phase") != "train_reflector"
    }
    reflector = {
        logical for logical in accepted if claims.get(logical, {}).get("phase") == "train_reflector"
    }
    return {
        "unresolved_call_count": unresolved,
        "run_failed_event_count": run_failed,
        "incident_opened_event_count": incident_opened,
        "claimed_logical_call_count": len(claims),
        "accepted_candidate_count": len(candidate),
        "evaluated_candidate_count": len(evaluated & candidate),
        "accepted_reflector_count": len(reflector),
        "rejected_reflector_attempt_count": rejected_reflector_attempts,
        "core_job_count": core_jobs,
    }


def _bind_ledger_aggregates(*, report: Any, events: tuple[dict[str, Any], ...]) -> None:
    claims = [event["payload"] for event in events if event["kind"] == "CALL_CLAIMED"]
    accepted = {
        str(event["payload"]["logical_call_id"])
        for event in events
        if event["kind"] == "CALL_ACCEPTED"
    }
    no_completion = [
        event["payload"] for event in events if event["kind"] == "CALL_NO_COMPLETION_FAILURE"
    ]
    rejected_completion = [
        event["payload"] for event in events if event["kind"] == "CALL_REJECTED_COMPLETION"
    ]
    logical = {str(claim["logical_call_id"]) for claim in claims}
    claim_by_logical = {
        str(claim["logical_call_id"]): claim
        for claim in claims
    }
    frozen_context_targets = _frozen_context_targets(events)
    recoveries = sum(event["kind"] == "RECOVERY_COMPLETED" for event in events)
    # The managed harness admits an accepted completion only after credential
    # readiness passes.  The ledger does not expose raw canary-provider
    # telemetry, so closure binds the accepted-call count and a lower bound;
    # it deliberately does not infer an exact provider-attempt total.
    if (
        report.calls.experimental_model_logical_calls != len(logical)
        or report.calls.managed_runtime_session_attempts != len(claims)
        or report.calls.readiness_passed_for_accepted_calls != len(accepted)
        or report.calls.canary_provider_attempts.status != "NOT_MEASURED"
        or report.calls.canary_provider_attempts.lower_bound != len(accepted)
        or report.calls.retries != len(claims) - len(logical)
        or report.calls.failed_attempts
        != len(no_completion) + len(rejected_completion)
        or report.calls.failures.infrastructure_no_completion != len(no_completion)
        or report.calls.recoveries != recoveries
        or report.calls.rejected_attempts != len(rejected_completion)
        or report.calls.failures.artifact_or_schema != len(rejected_completion)
        or len(claims) - len(logical)
        != len(no_completion) + len(rejected_completion)
        or any(
            (
                report.calls.failures.parser_or_evaluator,
                report.calls.failures.orchestration,
                report.calls.failures.runtime_or_credential,
                report.calls.failures.duplicate_or_lease,
                report.calls.failures.data_integrity,
            )
        )
    ):
        raise TemperatureClosureError("CLOSURE_CALL_AGGREGATE_MISMATCH")

    failures_by_phase: dict[str, int] = {}
    rejections_by_phase: dict[str, int] = {}
    claim_by_call = {str(claim["call_id"]): claim for claim in claims}
    for failure in no_completion:
        claim = claim_by_call.get(str(failure["call_id"]))
        if claim is None:
            raise TemperatureClosureError("CLOSURE_CALL_AGGREGATE_MISMATCH")
        phase = str(claim["phase"])
        failures_by_phase[phase] = failures_by_phase.get(phase, 0) + 1
    for rejection in rejected_completion:
        claim = claim_by_call.get(str(rejection["call_id"]))
        if claim is None or claim.get("phase") != "train_reflector":
            raise TemperatureClosureError("CLOSURE_CALL_AGGREGATE_MISMATCH")
        phase = str(claim["phase"])
        failures_by_phase[phase] = failures_by_phase.get(phase, 0) + 1
        rejections_by_phase[phase] = rejections_by_phase.get(phase, 0) + 1
    for phase, arm in (("evolved-test", report.evolved_test), ("baseline-test", report.baseline_test)):
        attempts = sum(claim["phase"] == phase for claim in claims)
        if (
            arm.model_attempts != attempts
            or arm.retries != attempts - 100
            or arm.failed_attempts != failures_by_phase.get(phase, 0)
            or arm.rejected_attempts != rejections_by_phase.get(phase, 0)
        ):
            raise TemperatureClosureError("CLOSURE_TEST_CALL_AGGREGATE_MISMATCH")

    evaluations: dict[tuple[str, int | None], dict[int, dict[str, Any]]] = {}
    for event in events:
        if event["kind"] != "CALL_EVALUATED":
            continue
        event_logical = str(event["payload"]["logical_call_id"])
        value = _decode_json_object(
            str(event["payload"]["evaluation_json"]).encode("utf-8"),
            "CLOSURE_EVALUATION_JSON_INVALID",
        )
        phase = value.get("phase")
        batch_index = value.get("batch_index")
        ordinal = value.get("task_ordinal")
        if (
            type(phase) is not str
            or (batch_index is not None and type(batch_index) is not int)
            or type(ordinal) is not int
            or value.get("logical_call_id") != event_logical
            or type(value.get("correct")) is not bool
            or type(value.get("official_parse_status")) is not str
            or type(value.get("strict_parse_status")) is not str
        ):
            raise TemperatureClosureError("CLOSURE_EVALUATION_AGGREGATE_INVALID")
        bucket = evaluations.setdefault((phase, batch_index), {})
        if ordinal in bucket:
            raise TemperatureClosureError("CLOSURE_EVALUATION_AGGREGATE_INVALID")
        bucket[ordinal] = value

    _bind_test_evaluations(
        report.evolved_test,
        evaluations.get(("evolved_test", None), {}),
        claims=claim_by_logical,
        expected_targets=frozen_context_targets,
    )
    _bind_test_evaluations(
        report.baseline_test,
        evaluations.get(("baseline_test", None), {}),
        claims=claim_by_logical,
        expected_targets=(),
    )
    for batch in report.train_batches:
        batch_claims = [
            claim
            for claim in claims
            if claim["phase"] in {"train-pre", "train-post", "train_reflector"}
            and _logical_batch(str(claim["logical_call_id"])) == batch.batch_index
        ]
        batch_call_ids = {str(claim["call_id"]) for claim in batch_claims}
        batch_logical_ids = {str(claim["logical_call_id"]) for claim in batch_claims}
        if (
            batch.retries != len(batch_claims) - len(batch_logical_ids)
            or batch.failed_attempts
            != sum(str(failure["call_id"]) in batch_call_ids for failure in no_completion)
            + sum(
                str(rejection["call_id"]) in batch_call_ids
                for rejection in rejected_completion
            )
            or batch.rejected_attempts
            != sum(
                str(rejection["call_id"]) in batch_call_ids
                for rejection in rejected_completion
            )
        ):
            raise TemperatureClosureError("CLOSURE_TRAIN_CALL_AGGREGATE_MISMATCH")
        pre = evaluations.get(("train_pre", batch.batch_index), {})
        post = evaluations.get(("train_post", batch.batch_index), {})
        expected_ordinals = set(
            range((batch.batch_index - 1) * 25, batch.batch_index * 25)
        )
        if set(pre) != expected_ordinals or set(post) != expected_ordinals:
            raise TemperatureClosureError("CLOSURE_TRAIN_EVALUATION_AGGREGATE_MISMATCH")
        pairs = [(pre[index], post[index]) for index in sorted(expected_ordinals)]
        both_correct = sum(left["correct"] and right["correct"] for left, right in pairs)
        pre_only = sum(left["correct"] and not right["correct"] for left, right in pairs)
        post_only = sum(not left["correct"] and right["correct"] for left, right in pairs)
        both_wrong = sum(not left["correct"] and not right["correct"] for left, right in pairs)
        if (
            batch.pre_correct != both_correct + pre_only
            or batch.post_correct != both_correct + post_only
            or batch.both_correct != both_correct
            or batch.pre_only_correct != pre_only
            or batch.post_only_correct != post_only
            or batch.both_wrong != both_wrong
            or batch.pre_parser_success
            != sum(value["official_parse_status"] == "parsed" for value in pre.values())
            or batch.post_parser_success
            != sum(value["official_parse_status"] == "parsed" for value in post.values())
        ):
            raise TemperatureClosureError("CLOSURE_TRAIN_EVALUATION_AGGREGATE_MISMATCH")


def _bind_test_evaluations(
    arm: Any,
    values: dict[int, dict[str, Any]],
    *,
    claims: dict[str, dict[str, Any]],
    expected_targets: tuple[SupervisedTargetBindingV2, ...],
) -> None:
    if set(values) != set(range(100)):
        raise TemperatureClosureError("CLOSURE_TEST_EVALUATION_AGGREGATE_MISMATCH")
    ordered = [values[index] for index in range(100)]
    context_bindings_valid = True
    for value in ordered:
        logical = str(value["logical_call_id"])
        claim = claims.get(logical)
        if claim is None or value.get("context_binding_sha256") != (
            _expected_context_binding_receipt_sha256(
                call_id=str(claim["call_id"]),
                targets=expected_targets,
            )
        ):
            context_bindings_valid = False
            break
    if (
        tuple(value["correct"] for value in ordered) != arm.correctness
        or sum(value["official_parse_status"] == "parsed" for value in ordered)
        != arm.official_parser_success_count
        or sum(value["strict_parse_status"] == "parsed" for value in ordered)
        != arm.strict_parser_success_count
        or not context_bindings_valid
    ):
        raise TemperatureClosureError("CLOSURE_TEST_EVALUATION_AGGREGATE_MISMATCH")


def _frozen_context_targets(
    events: tuple[dict[str, Any], ...],
) -> tuple[SupervisedTargetBindingV2, ...]:
    frozen = [event for event in events if event["kind"] == "FINAL_STATE_FROZEN"]
    if len(frozen) != 1:
        raise TemperatureClosureError("CLOSURE_FINAL_CONTEXT_BINDING_INVALID")
    raw = frozen[0]["payload"].get("frozen_context_targets")
    if type(raw) is not list:
        raise TemperatureClosureError("CLOSURE_FINAL_CONTEXT_BINDING_INVALID")
    try:
        targets = tuple(
            SupervisedTargetBindingV2(**value)
            for value in raw
            if type(value) is dict
        )
    except (TypeError, ValueError) as exc:
        raise TemperatureClosureError("CLOSURE_FINAL_CONTEXT_BINDING_INVALID") from exc
    if len(targets) != 3:
        raise TemperatureClosureError("CLOSURE_FINAL_CONTEXT_BINDING_INVALID")
    return targets


def _expected_context_binding_receipt_sha256(
    *,
    call_id: str,
    targets: tuple[SupervisedTargetBindingV2, ...],
) -> str:
    binding = SupervisedSessionContextBindingV2(
        session_id="temp-" + sha256_bytes(call_id.encode("utf-8"))[:40],
        targets=targets,
    )
    return issue_supervised_context_binding_receipt_v2(
        expected=binding,
        actual=binding,
    ).digest


def _logical_batch(logical_call_id: str) -> int | None:
    match = re.search(r"(?:^|-)b(0[1-4])(?:-|$)", logical_call_id, re.ASCII)
    return None if match is None else int(match.group(1))


def _load_preflight_bundle(root: Path) -> tuple[dict[str, dict[str, Any]], str]:
    payloads: dict[str, dict[str, Any]] = {}
    for key, name in _PRECHECK_JSON_FILES.items():
        encoded = _read_regular_file(
            root / name,
            maximum=MAX_PREFLIGHT_FILE_BYTES,
            private=False,
        )
        payloads[key] = _decode_json_object(encoded, "CLOSURE_PREFLIGHT_JSON_INVALID")
    digest = sha256_bytes(canonical_json_bytes(payloads))
    return payloads, digest


def _bind_preflight_report(
    *,
    report: Any,
    payloads: dict[str, dict[str, Any]],
    bundle_sha256: str,
) -> None:
    precheck = payloads["report"]
    config = payloads["config_manifest"]
    model = payloads["model_identity_receipt"]
    runtime = payloads["runtime_identity_receipt"]
    regression = payloads["regression_receipt"]
    regression_digest = sha256_bytes(canonical_json_bytes(regression))
    runtime_file_digest = sha256_bytes(canonical_pretty_json_bytes(runtime))
    model_file_digest = sha256_bytes(canonical_pretty_json_bytes(model))
    runtime_body = {key: value for key, value in runtime.items() if key != "runtime_preflight_sha256"}
    runtime_preflight_digest = sha256_bytes(canonical_json_bytes(runtime_body))
    if (
        precheck.get("status") != "PASS_READY_FOR_FORMAL_EXECUTION"
        or precheck.get("protocol_id") != PROTOCOL_ID
        or precheck.get("source_commit") != report.identity.source_commit
        or precheck.get("config_sha256") != report.identity.config_sha256
        or precheck.get("split_sha256") != report.identity.split_sha256
        or precheck.get("preflight_model_calls") != 0
        or precheck.get("regression_receipt_sha256") != regression_digest
        or payloads["split_manifest"].get("dataset_sha256") != report.identity.dataset_sha256
        or config.get("source_tree_clean") is not True
        or config.get("generation_zero_context_bytes") != 0
        or model.get("model_calls") != 0
        or model.get("model") != report.identity.model
        or model.get("reasoning_effort") != report.identity.reasoning_effort
        or model_file_digest != report.identity.model_identity_receipt_sha256
        or runtime.get("runtime_preflight_sha256") != runtime_preflight_digest
        or runtime_file_digest != report.identity.managed_runtime_receipt_sha256
        or runtime.get("runtime_services_identity_sha256")
        != report.identity.runtime_services_identity_sha256
        or runtime.get("formal_runtime_identity_sha256")
        != report.identity.formal_runtime_identity_sha256
        or runtime.get("formal_runtime_receipt_sha256")
        != report.identity.formal_runtime_receipt_sha256
        or runtime.get("formal_runtime_source_commit")
        != report.identity.source_commit
        or runtime.get("formal_runtime_source_tree_sha256")
        != report.identity.source_tree_sha256
        or runtime.get("core_wheel_sha256") != report.identity.core_wheel_sha256
        or runtime.get("chembench_wheel_sha256")
        != report.identity.chembench_wheel_sha256
        or runtime.get("core_editable") is not False
        or runtime.get("chembench_editable") is not False
        or runtime.get("formal_runtime_python_isolated") is not True
        or regression.get("model_calls") != 0
        or regression.get("focused_test_count") != report.preflight.focused_test_count
        or regression.get("focused_failure_count") != report.preflight.focused_failure_count
        or regression.get("integration_test_count") != report.preflight.integration_test_count
        or regression.get("integration_failure_count")
        != report.preflight.integration_failure_count
        or report.preflight.preflight_bundle_sha256 != bundle_sha256
    ):
        raise TemperatureClosureError("CLOSURE_PREFLIGHT_BINDING_INVALID")


def _bind_run_manifest(
    *,
    manifest: dict[str, Any],
    report: Any,
    preflight_bundle_sha256: str,
) -> None:
    expected_keys = {
        "schema_version",
        "protocol_id",
        "controller_run_id",
        "evolved_run_id",
        "baseline_run_id",
        "core_run_id",
        "runtime_service_run_id",
        "source_commit",
        "branch",
        "config_sha256",
        "split_sha256",
        "preflight_bundle_sha256",
        "runtime_services_identity_sha256",
        "generation_zero_context_empty",
        "old_artifact_imported",
        "old_completion_imported",
        "old_database_imported",
        "old_workspace_imported",
        "started_at_utc",
    }
    expected = {
        "protocol_id": PROTOCOL_ID,
        "controller_run_id": report.run_ids.controller_run_id,
        "evolved_run_id": report.run_ids.evolved_run_id,
        "baseline_run_id": report.run_ids.baseline_run_id,
        "core_run_id": report.run_ids.core_run_id,
        "runtime_service_run_id": report.run_ids.runtime_service_run_id,
        "source_commit": report.identity.source_commit,
        "config_sha256": report.identity.config_sha256,
        "split_sha256": report.identity.split_sha256,
        "preflight_bundle_sha256": preflight_bundle_sha256,
        "runtime_services_identity_sha256": report.identity.runtime_services_identity_sha256,
    }
    if (
        set(manifest) != expected_keys
        or any(manifest.get(key) != value for key, value in expected.items())
        or manifest.get("branch") != report.identity.branch
        or manifest.get("started_at_utc") != report.started_at_utc
    ):
        raise TemperatureClosureError("CLOSURE_RUN_MANIFEST_BINDING_INVALID")
    if (
        manifest.get("generation_zero_context_empty") is not True
        or manifest.get("old_artifact_imported") is not False
        or manifest.get("old_completion_imported") is not False
        or manifest.get("old_database_imported") is not False
        or manifest.get("old_workspace_imported") is not False
    ):
        raise TemperatureClosureError("CLOSURE_RUN_MANIFEST_BINDING_INVALID")


def _read_regular_file(path: Path, *, maximum: int, private: bool) -> bytes:
    target = _absolute_path(path)
    try:
        metadata = target.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or metadata.st_size < 2
            or metadata.st_size > maximum
            or (private and mode != 0o600)
            or (not private and mode & 0o022)
        ):
            raise TemperatureClosureError("CLOSURE_FILE_IDENTITY_INVALID")
        descriptor = os.open(
            target,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise TemperatureClosureError("CLOSURE_FILE_CHANGED")
            chunks: list[bytes] = []
            remaining = maximum + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 1024 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            encoded = b"".join(chunks)
            final = os.fstat(descriptor)
            if (
                len(encoded) != metadata.st_size
                or (final.st_dev, final.st_ino, final.st_size)
                != (metadata.st_dev, metadata.st_ino, metadata.st_size)
            ):
                raise TemperatureClosureError("CLOSURE_FILE_CHANGED")
            return encoded
        finally:
            os.close(descriptor)
    except FileNotFoundError as exc:
        raise TemperatureClosureError("CLOSURE_FILE_MISSING") from exc


def _decode_json_object(encoded: bytes, finding: str) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            encoded,
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite")),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise TemperatureClosureError(finding) from exc
    if type(value) is not dict:
        raise TemperatureClosureError(finding)
    return value


def _private_directory(path: Path) -> Path:
    root = _safe_directory(path)
    metadata = root.lstat()
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise TemperatureClosureError("CLOSURE_RUN_ROOT_NOT_PRIVATE")
    return root


def _safe_directory(path: Path) -> Path:
    target = _absolute_path(path)
    try:
        metadata = target.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o022
        ):
            raise TemperatureClosureError("CLOSURE_DIRECTORY_IDENTITY_INVALID")
    except FileNotFoundError as exc:
        raise TemperatureClosureError("CLOSURE_DIRECTORY_MISSING") from exc
    return target


def _absolute_path(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise TypeError("closure paths must be absolute pathlib.Path values")
    return Path(os.path.abspath(path))


def _utc_seconds(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise TemperatureClosureError("CLOSURE_AUDIT_TIME_INVALID") from exc
    if parsed.tzinfo is None:
        raise TemperatureClosureError("CLOSURE_AUDIT_TIME_INVALID")
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "CLOSURE_RECEIPT_SCHEMA",
    "FormalClosureReceiptV1",
    "TemperatureClosureError",
    "VerifiedFormalClosureV1",
    "verify_formal_closure_v1",
    "write_formal_closure_receipt_v1",
]
