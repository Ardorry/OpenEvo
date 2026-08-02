"""Aggregate-only final reporting for Temperature full-evolve v1.

The public report boundary deliberately accepts no benchmark identities, item
text, answers, predictions, model responses, or transcripts.  The only
item-shaped values admitted are the two ordered boolean correctness vectors
needed for the pre-registered paired Test analysis; those vectors are consumed
in memory and are never serialized to the report package.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import stat
from pathlib import Path
from typing import Literal

import matplotlib
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from openevo_chembench.supervised_transfer_v1.common import (
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_private_file,
    write_public_file,
)
from openevo_chembench.temperature_full_evolve_v1.closure import VerifiedFormalClosureV1
from openevo_chembench.temperature_full_evolve_v1.config import (
    EXPECTED_CANDIDATE_IMAGE_ID,
    EXPECTED_CODEX_SHA256,
    HISTORICAL_EXPOSURE_POLICY,
    PROTOCOL_ID,
)
from openevo_chembench.temperature_full_evolve_v1.statistics import (
    PairedBinaryMetricsV1,
    paired_binary_metrics_v1,
)

matplotlib.use("Agg")
from matplotlib import pyplot as plt

REPORT_INPUT_SCHEMA = "TemperatureFullEvolveAggregateReportInputV1"
REPORT_PACKAGE_SCHEMA = "TemperatureFullEvolveAggregateReportPackageV1"
TEST_SIZE = 100
TRAIN_BATCH_SIZE = 25
TRAIN_BATCH_COUNT = 4
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_COMMIT = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)
_UTC = re.compile(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
MAX_AGGREGATE_INPUT_BYTES = 2 * 1024 * 1024


class TemperatureReportError(RuntimeError):
    """Closed public-report validation or publication failure."""


class _StrictReportModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RunIdentityAggregateV1(_StrictReportModel):
    controller_run_id: str
    evolved_run_id: str
    baseline_run_id: str
    core_run_id: str
    runtime_service_run_id: str
    all_run_ids: tuple[str, ...] = Field(min_length=2, max_length=16)

    @model_validator(mode="after")
    def _closed(self) -> RunIdentityAggregateV1:
        named = {
            self.controller_run_id,
            self.evolved_run_id,
            self.baseline_run_id,
            self.core_run_id,
            self.runtime_service_run_id,
        }
        if any(_SAFE_ID.fullmatch(value) is None for value in (*named, *self.all_run_ids)):
            raise ValueError("run IDs must be safe opaque identifiers")
        if len(set(self.all_run_ids)) != len(self.all_run_ids) or not named <= set(
            self.all_run_ids
        ):
            raise ValueError("all_run_ids must contain every unique named run ID")
        return self


class ExecutionIdentityAggregateV1(_StrictReportModel):
    source_commit: str
    branch: str
    config_sha256: str
    split_sha256: str
    dataset_sha256: str
    group_manifest_sha256: str
    group_assignment_sha256: str
    framework_lock_sha256: str
    formal_runtime_identity_sha256: str
    formal_runtime_receipt_sha256: str
    source_tree_sha256: str
    core_wheel_sha256: str
    chembench_wheel_sha256: str
    core_editable: StrictBool
    chembench_editable: StrictBool
    runtime_services_identity_sha256: str
    model_identity_receipt_sha256: str
    managed_runtime_receipt_sha256: str
    model: Literal["gpt-5.5"]
    reasoning_effort: Literal["medium"]
    managed_codex_executable_sha256: str
    managed_candidate_image_id: str
    capture_mode: Literal["transcript"]
    parser_id: Literal["first_uppercase_opencompass_compatible"]
    strict_parser_id: Literal["strict_single_letter_parser"]
    evaluator_id: Literal["ChemBench4KPrivateEvaluator"]
    candidate_baseline_stack_equal: StrictBool
    evolved_then_baseline_order: StrictBool
    test_feedback_enabled: StrictBool
    test_reflector_calls: int = Field(ge=0)
    test_core_jobs: int = Field(ge=0)
    test_artifact_updates: int = Field(ge=0)

    @model_validator(mode="after")
    def _identity(self) -> ExecutionIdentityAggregateV1:
        digests = (
            self.config_sha256,
            self.split_sha256,
            self.dataset_sha256,
            self.group_manifest_sha256,
            self.group_assignment_sha256,
            self.framework_lock_sha256,
            self.formal_runtime_identity_sha256,
            self.formal_runtime_receipt_sha256,
            self.source_tree_sha256,
            self.core_wheel_sha256,
            self.chembench_wheel_sha256,
            self.runtime_services_identity_sha256,
            self.model_identity_receipt_sha256,
            self.managed_runtime_receipt_sha256,
            self.managed_codex_executable_sha256,
        )
        if (
            _COMMIT.fullmatch(self.source_commit) is None
            or _SAFE_ID.fullmatch(self.branch) is None
            or any(_SHA256.fullmatch(value) is None for value in digests)
            or self.core_editable
            or self.chembench_editable
            or self.managed_codex_executable_sha256 != EXPECTED_CODEX_SHA256
            or self.managed_candidate_image_id != EXPECTED_CANDIDATE_IMAGE_ID
            or not self.candidate_baseline_stack_equal
            or not self.evolved_then_baseline_order
            or self.test_feedback_enabled
            or self.test_reflector_calls
            or self.test_core_jobs
            or self.test_artifact_updates
        ):
            raise ValueError("formal execution identity is not comparison-safe")
        return self


class HistoricalReusePolicyAggregateV1(_StrictReportModel):
    policy: Literal["fixed_official_temperature_pool_fresh_c0"]
    protocol_fixed_at_local_date: Literal["2026-08-02"]
    historical_exclusion_applied: StrictBool
    selected_items_may_have_historical_exposure: StrictBool
    historical_exposure_used_for_item_selection: StrictBool
    prior_artifacts_imported: int = Field(ge=0)
    prior_completions_imported: int = Field(ge=0)
    prior_databases_imported: int = Field(ge=0)
    prior_workspaces_imported: int = Field(ge=0)
    generation_zero_empty_context: StrictBool

    @model_validator(mode="after")
    def _policy(self) -> HistoricalReusePolicyAggregateV1:
        if (
            self.policy != HISTORICAL_EXPOSURE_POLICY
            or self.historical_exclusion_applied
            or not self.selected_items_may_have_historical_exposure
            or self.historical_exposure_used_for_item_selection
            or any(
                value
                for value in (
                    self.prior_artifacts_imported,
                    self.prior_completions_imported,
                    self.prior_databases_imported,
                    self.prior_workspaces_imported,
                )
            )
            or not self.generation_zero_empty_context
        ):
            raise ValueError("historical reuse policy does not describe a fresh C0 run")
        return self


class SplitAggregateV1(_StrictReportModel):
    train_count: Literal[100]
    test_count: Literal[100]
    reserve_count: int = Field(ge=0)
    batch_size: Literal[25]
    batch_count: Literal[4]
    eligible_pool_count: int = Field(ge=200)
    group_count: int = Field(ge=1)
    singleton_group_count: int = Field(ge=0)
    maximum_group_size: int = Field(ge=1)
    uid_overlap_count: Literal[0]
    normalized_text_overlap_count: Literal[0]
    normalized_option_overlap_count: Literal[0]
    source_group_fields_available: StrictBool
    source_group_overlap_count: int | None = Field(default=None, ge=0)
    test_sealed_until_train_complete: Literal[True]
    split_frozen_before_model_calls: Literal[True]

    @model_validator(mode="after")
    def _counts(self) -> SplitAggregateV1:
        if (
            self.eligible_pool_count < self.train_count + self.test_count + self.reserve_count
            or self.singleton_group_count > self.group_count
            or self.maximum_group_size > self.eligible_pool_count
            or (
                self.source_group_fields_available
                and self.source_group_overlap_count != 0
            )
            or (
                not self.source_group_fields_available
                and self.source_group_overlap_count is not None
            )
        ):
            raise ValueError("split aggregate counts are inconsistent")
        return self


class PreflightAggregateV1(_StrictReportModel):
    preflight_id: str
    preflight_bundle_sha256: str
    model_calls: Literal[0]
    focused_test_count: int = Field(ge=1)
    focused_failure_count: Literal[0]
    integration_test_count: int = Field(ge=1)
    integration_failure_count: Literal[0]
    generation_zero_context_bytes: Literal[0]
    source_tree_frozen: Literal[True]
    exactly_once_recovery_passed: Literal[True]
    test_feedback_barrier_passed: Literal[True]

    @model_validator(mode="after")
    def _id(self) -> PreflightAggregateV1:
        if (
            _SAFE_ID.fullmatch(self.preflight_id) is None
            or _SHA256.fullmatch(self.preflight_bundle_sha256) is None
        ):
            raise ValueError("preflight ID is invalid")
        return self


class BatchArtifactAggregateV1(_StrictReportModel):
    text_memory_sha256: str
    skill_bundle_sha256: str
    agent_system_sha256: str
    combined_context_sha256: str
    text_memory_bytes: int = Field(ge=0, le=4096)
    skill_bundle_bytes: int = Field(ge=0, le=1536)
    agent_system_bytes: int = Field(ge=0, le=1024)
    combined_context_bytes: int = Field(ge=0, le=8192)
    validation_passed: Literal[True]

    @model_validator(mode="after")
    def _artifact(self) -> BatchArtifactAggregateV1:
        if any(
            _SHA256.fullmatch(value) is None
            for value in (
                self.text_memory_sha256,
                self.skill_bundle_sha256,
                self.agent_system_sha256,
                self.combined_context_sha256,
            )
        ):
            raise ValueError("artifact digest is invalid")
        if self.combined_context_bytes < (
            self.text_memory_bytes + self.skill_bundle_bytes + self.agent_system_bytes
        ):
            raise ValueError("combined context cannot be smaller than its three targets")
        return self


class RuleEvidenceAggregateV1(_StrictReportModel):
    active_rules: int = Field(ge=0, le=64)
    supported_rules: int = Field(ge=0, le=64)
    conflicted_rules: int = Field(ge=0, le=64)
    retired_rules: int = Field(ge=0, le=64)
    support_count_total: int = Field(ge=0)
    contradiction_count_total: int = Field(ge=0)
    evidence_sha256: str

    @model_validator(mode="after")
    def _evidence(self) -> RuleEvidenceAggregateV1:
        if (
            _SHA256.fullmatch(self.evidence_sha256) is None
            or self.supported_rules > self.active_rules
            or self.conflicted_rules > self.active_rules
        ):
            raise ValueError("rule evidence aggregate is inconsistent")
        return self


class TrainBatchAggregateV1(_StrictReportModel):
    batch_index: int = Field(ge=1, le=4)
    task_count: Literal[25]
    pre_correct: int = Field(ge=0, le=25)
    post_correct: int = Field(ge=0, le=25)
    both_correct: int = Field(ge=0, le=25)
    pre_only_correct: int = Field(ge=0, le=25)
    post_only_correct: int = Field(ge=0, le=25)
    both_wrong: int = Field(ge=0, le=25)
    pre_parser_success: int = Field(ge=0, le=25)
    post_parser_success: int = Field(ge=0, le=25)
    pre_candidate_calls: Literal[25]
    post_candidate_calls: Literal[25]
    reflector_calls: Literal[1]
    core_jobs: Literal[3]
    failed_attempts: int = Field(ge=0)
    retries: int = Field(ge=0)
    rejected_attempts: int = Field(ge=0)
    artifacts: BatchArtifactAggregateV1
    rules: RuleEvidenceAggregateV1

    @model_validator(mode="after")
    def _transitions(self) -> TrainBatchAggregateV1:
        if (
            self.both_correct
            + self.pre_only_correct
            + self.post_only_correct
            + self.both_wrong
            != self.task_count
            or self.pre_correct != self.both_correct + self.pre_only_correct
            or self.post_correct != self.both_correct + self.post_only_correct
        ):
            raise ValueError("Train transition counts are inconsistent")
        return self


class TestArmAggregateV1(_StrictReportModel):
    arm: Literal["evolved", "baseline"]
    correctness: tuple[StrictBool, ...] = Field(min_length=100, max_length=100)
    accepted_completion_count: Literal[100]
    official_parser_success_count: int = Field(ge=0, le=100)
    strict_parser_success_count: int = Field(ge=0, le=100)
    logical_candidate_calls: Literal[100]
    model_attempts: int = Field(ge=100)
    failed_attempts: int = Field(ge=0)
    retries: int = Field(ge=0)
    rejected_attempts: int = Field(ge=0)
    context_bytes: int = Field(ge=0, le=8192)
    context_sha256: str
    feedback_enabled: Literal[False]
    reflector_calls: Literal[0]
    core_jobs: Literal[0]
    artifact_updates: Literal[0]

    @model_validator(mode="after")
    def _arm(self) -> TestArmAggregateV1:
        if _SHA256.fullmatch(self.context_sha256) is None:
            raise ValueError("Test context digest is invalid")
        if self.model_attempts < self.logical_candidate_calls + self.retries:
            raise ValueError("Test model attempt count omits retries")
        if self.arm == "baseline" and self.context_bytes != 0:
            raise ValueError("generation-zero baseline must have empty context")
        return self


class FailureAggregateV1(_StrictReportModel):
    infrastructure_no_completion: int = Field(ge=0)
    parser_or_evaluator: int = Field(ge=0)
    orchestration: int = Field(ge=0)
    runtime_or_credential: int = Field(ge=0)
    duplicate_or_lease: int = Field(ge=0)
    artifact_or_schema: int = Field(ge=0)
    data_integrity: int = Field(ge=0)

    @property
    def total(self) -> int:
        return sum(self.model_dump().values())


class UnmeasuredProviderAttemptsAggregateV1(_StrictReportModel):
    """A conservative bound when raw provider-attempt telemetry is unavailable."""

    status: Literal["NOT_MEASURED"]
    lower_bound: Literal[404]


class CallAndFailureAggregateV1(_StrictReportModel):
    candidate_logical_calls: Literal[400]
    reflector_logical_calls: Literal[4]
    core_jobs: Literal[12]
    experimental_model_logical_calls: Literal[404]
    managed_runtime_session_attempts: int = Field(ge=404)
    readiness_passed_for_accepted_calls: Literal[404]
    canary_provider_attempts: UnmeasuredProviderAttemptsAggregateV1
    failed_attempts: int = Field(ge=0)
    retries: int = Field(ge=0)
    rejected_attempts: int = Field(ge=0)
    recoveries: int = Field(ge=0)
    invalidated_formal_runs: int = Field(ge=0)
    failures: FailureAggregateV1

    @model_validator(mode="after")
    def _totals(self) -> CallAndFailureAggregateV1:
        if (
            self.experimental_model_logical_calls
            != self.candidate_logical_calls + self.reflector_logical_calls
            or self.managed_runtime_session_attempts
            != self.experimental_model_logical_calls + self.retries
            or self.readiness_passed_for_accepted_calls
            != self.experimental_model_logical_calls
            or self.failed_attempts != self.failures.total
        ):
            raise ValueError("call/failure totals are inconsistent")
        return self


class FinalArtifactAggregateV1(BatchArtifactAggregateV1):
    final_state_id: Literal["C4"]
    final_manifest_sha256: str
    lineage_sha256: str
    frozen_before_test: Literal[True]

    @model_validator(mode="after")
    def _final(self) -> FinalArtifactAggregateV1:
        if any(
            _SHA256.fullmatch(value) is None
            for value in (self.final_manifest_sha256, self.lineage_sha256)
        ):
            raise ValueError("final artifact identity is invalid")
        return self


class RegressionAggregateV1(_StrictReportModel):
    focused_passed: int = Field(ge=1)
    focused_failed: Literal[0]
    integration_passed: int = Field(ge=1)
    integration_failed: Literal[0]
    test_command_sha256: str
    test_output_sha256: str
    model_calls_during_tests: Literal[0]

    @model_validator(mode="after")
    def _digests(self) -> RegressionAggregateV1:
        if any(
            _SHA256.fullmatch(value) is None
            for value in (self.test_command_sha256, self.test_output_sha256)
        ):
            raise ValueError("regression receipt digest is invalid")
        return self


class AggregateReportInputV1(_StrictReportModel):
    """The complete aggregate-only boundary consumed by final reporting."""

    schema_version: Literal["TemperatureFullEvolveAggregateReportInputV1"]
    protocol_id: Literal["chembench_temperature_full_evolve_v1"]
    status: Literal["READY_FOR_AUDIT"]
    generated_at_utc: str
    started_at_utc: str
    completed_at_utc: str
    train_post_is_same_item_supervised_diagnostic: Literal[True]
    run_ids: RunIdentityAggregateV1
    identity: ExecutionIdentityAggregateV1
    historical_reuse: HistoricalReusePolicyAggregateV1
    split: SplitAggregateV1
    preflight: PreflightAggregateV1
    train_batches: tuple[TrainBatchAggregateV1, ...] = Field(min_length=4, max_length=4)
    evolved_test: TestArmAggregateV1
    baseline_test: TestArmAggregateV1
    calls: CallAndFailureAggregateV1
    final_artifacts: FinalArtifactAggregateV1
    regression: RegressionAggregateV1

    @model_validator(mode="after")
    def _closed_experiment(self) -> AggregateReportInputV1:
        if (
            self.protocol_id != PROTOCOL_ID
            or any(
                _UTC.fullmatch(value) is None
                for value in (self.generated_at_utc, self.started_at_utc, self.completed_at_utc)
            )
            or tuple(batch.batch_index for batch in self.train_batches) != (1, 2, 3, 4)
            or self.evolved_test.arm != "evolved"
            or self.baseline_test.arm != "baseline"
        ):
            raise ValueError("report input experiment identity is invalid")
        if self.evolved_test.context_sha256 != self.final_artifacts.combined_context_sha256:
            raise ValueError("evolved Test did not use frozen C4 context")
        if self.evolved_test.context_bytes != self.final_artifacts.combined_context_bytes:
            raise ValueError("evolved Test context size does not match frozen C4")
        final_batch = self.train_batches[-1].artifacts
        for field in (
            "text_memory_sha256",
            "skill_bundle_sha256",
            "agent_system_sha256",
            "combined_context_sha256",
            "text_memory_bytes",
            "skill_bundle_bytes",
            "agent_system_bytes",
            "combined_context_bytes",
        ):
            if getattr(final_batch, field) != getattr(self.final_artifacts, field):
                raise ValueError("final artifact identity does not equal C4 checkpoint")
        candidate_calls = sum(
            batch.pre_candidate_calls + batch.post_candidate_calls
            for batch in self.train_batches
        ) + self.evolved_test.logical_candidate_calls + self.baseline_test.logical_candidate_calls
        if (
            candidate_calls != self.calls.candidate_logical_calls
            or sum(batch.reflector_calls for batch in self.train_batches)
            != self.calls.reflector_logical_calls
            or sum(batch.core_jobs for batch in self.train_batches) != self.calls.core_jobs
        ):
            raise ValueError("phase call counts do not match experiment totals")
        return self


def write_aggregate_report_input_v1(*, report: AggregateReportInputV1, path: Path) -> str:
    """Seal the private aggregate input once after the audit closes."""

    if type(report) is not AggregateReportInputV1:
        raise TypeError("report must be the exact aggregate DTO")
    if not isinstance(path, Path) or not path.is_absolute():
        raise TypeError("aggregate input path must be absolute")
    encoded = canonical_pretty_json_bytes(report.model_dump(mode="json"))
    if len(encoded) > MAX_AGGREGATE_INPUT_BYTES:
        raise TemperatureReportError("AGGREGATE_REPORT_INPUT_SIZE_EXCEEDED")
    write_private_file(path, encoded, replace=False)
    return sha256_bytes(encoded)


def load_aggregate_report_input_v1(path: Path) -> AggregateReportInputV1:
    """Load one sealed owner-private aggregate without relaxing strict JSON types."""

    if not isinstance(path, Path) or not path.is_absolute():
        raise TypeError("aggregate input path must be absolute")
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size < 2
            or metadata.st_size > MAX_AGGREGATE_INPUT_BYTES
        ):
            raise TemperatureReportError("AGGREGATE_REPORT_INPUT_IDENTITY_INVALID")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise TemperatureReportError("AGGREGATE_REPORT_INPUT_CHANGED")
            chunks: list[bytes] = []
            remaining = MAX_AGGREGATE_INPUT_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 1024 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            encoded = b"".join(chunks)
            if len(encoded) != metadata.st_size:
                raise TemperatureReportError("AGGREGATE_REPORT_INPUT_CHANGED")
        finally:
            os.close(descriptor)
    except FileNotFoundError as exc:
        raise TemperatureReportError("AGGREGATE_REPORT_INPUT_MISSING") from exc

    def unique_pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        decoded = json.loads(
            encoded,
            object_pairs_hook=unique_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite")),
        )
        if type(decoded) is not dict:
            raise ValueError("aggregate root is not an object")
        return AggregateReportInputV1.model_validate_json(encoded, strict=True)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise TemperatureReportError("AGGREGATE_REPORT_INPUT_INVALID") from exc


def _require_closure_binding(
    *,
    report: AggregateReportInputV1,
    closure: VerifiedFormalClosureV1,
    protocol_bytes: bytes,
) -> None:
    receipt = closure.receipt
    aggregate_bytes = canonical_pretty_json_bytes(report.model_dump(mode="json"))
    named_run_ids = (
        receipt.controller_run_id,
        receipt.evolved_run_id,
        receipt.baseline_run_id,
        receipt.core_run_id,
        receipt.runtime_service_run_id,
    )
    report_run_ids = (
        report.run_ids.controller_run_id,
        report.run_ids.evolved_run_id,
        report.run_ids.baseline_run_id,
        report.run_ids.core_run_id,
        report.run_ids.runtime_service_run_id,
    )
    if (
        report.status != "READY_FOR_AUDIT"
        or receipt.status != "COMPLETE"
        or receipt.protocol_id != report.protocol_id
        or named_run_ids != report_run_ids
        or receipt.all_run_ids != report.run_ids.all_run_ids
        or receipt.source_commit != report.identity.source_commit
        or receipt.config_sha256 != report.identity.config_sha256
        or receipt.split_sha256 != report.identity.split_sha256
        or receipt.dataset_sha256 != report.identity.dataset_sha256
        or receipt.formal_runtime_identity_sha256
        != report.identity.formal_runtime_identity_sha256
        or receipt.formal_runtime_receipt_sha256
        != report.identity.formal_runtime_receipt_sha256
        or receipt.source_tree_sha256 != report.identity.source_tree_sha256
        or receipt.core_wheel_sha256 != report.identity.core_wheel_sha256
        or receipt.chembench_wheel_sha256
        != report.identity.chembench_wheel_sha256
        or receipt.core_editable is not False
        or receipt.chembench_editable is not False
        or receipt.preflight_bundle_sha256 != report.preflight.preflight_bundle_sha256
        or receipt.runtime_services_identity_sha256
        != report.identity.runtime_services_identity_sha256
        or receipt.aggregate_input_sha256 != sha256_bytes(aggregate_bytes)
        or receipt.aggregate_input_utf8_bytes != len(aggregate_bytes)
        or receipt.experiment_protocol_sha256 != sha256_bytes(protocol_bytes)
    ):
        raise TemperatureReportError("FORMAL_CLOSURE_BINDING_INVALID")


def write_final_report_package_v1(
    *,
    report: AggregateReportInputV1,
    closure: VerifiedFormalClosureV1,
    destination: Path,
    protocol_source: Path,
) -> dict[str, object]:
    """Publish a closed aggregate-only report package and complete checksum inventory."""

    if type(report) is not AggregateReportInputV1:
        raise TypeError("report must be the exact aggregate DTO")
    if type(closure) is not VerifiedFormalClosureV1:
        raise TypeError("closure must be verifier-issued")
    if not isinstance(destination, Path) or not destination.is_absolute():
        raise TypeError("destination must be an absolute Path")
    if not isinstance(protocol_source, Path) or not protocol_source.is_absolute():
        raise TypeError("protocol_source must be an absolute Path")
    protocol_bytes = protocol_source.read_bytes()
    if not protocol_bytes or len(protocol_bytes) > 2 * 1024 * 1024:
        raise TemperatureReportError("EXPERIMENT_PROTOCOL_SOURCE_INVALID")
    _require_closure_binding(report=report, closure=closure, protocol_bytes=protocol_bytes)
    if destination.exists():
        raise FileExistsError(destination)

    staging = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    if staging.exists():
        raise FileExistsError(staging)
    staging.mkdir(parents=True, mode=0o755)
    try:
        comparison = paired_binary_metrics_v1(
            tuple(report.baseline_test.correctness),
            tuple(report.evolved_test.correctness),
        )
        batch_statistics = tuple(_batch_statistics(batch) for batch in report.train_batches)
        _write_package_files(
            root=staging,
            report=report,
            comparison=comparison,
            batch_statistics=batch_statistics,
            protocol_bytes=protocol_bytes,
            closure=closure,
        )
        expected = tuple(
            sorted(path.name for path in staging.iterdir() if path.is_file())
        )
        if "SHA256SUMS.txt" in expected or not expected:
            raise TemperatureReportError("REPORT_PACKAGE_FILE_CLOSURE_INVALID")
        sums = "".join(
            f"{sha256_bytes((staging / name).read_bytes())}  {name}\n" for name in expected
        ).encode("utf-8")
        write_public_file(staging / "SHA256SUMS.txt", sums)
        os.rename(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "schema_version": REPORT_PACKAGE_SCHEMA,
        "status": "COMPLETE",
        "report_directory": destination.as_posix(),
        "file_count": len(expected) + 1,
        "sha256sums_sha256": sha256_bytes(sums),
        "paired_delta_percentage_points": comparison.delta_percentage_points,
        "mcnemar_exact_p": comparison.mcnemar_exact_p,
    }


def _batch_statistics(batch: TrainBatchAggregateV1) -> PairedBinaryMetricsV1:
    pre = (
        (True,) * batch.both_correct
        + (True,) * batch.pre_only_correct
        + (False,) * batch.post_only_correct
        + (False,) * batch.both_wrong
    )
    post = (
        (True,) * batch.both_correct
        + (False,) * batch.pre_only_correct
        + (True,) * batch.post_only_correct
        + (False,) * batch.both_wrong
    )
    return paired_binary_metrics_v1(pre, post, bootstrap_replicates=10_000)


def _write_package_files(
    *,
    root: Path,
    report: AggregateReportInputV1,
    comparison: PairedBinaryMetricsV1,
    batch_statistics: tuple[PairedBinaryMetricsV1, ...],
    protocol_bytes: bytes,
    closure: VerifiedFormalClosureV1,
) -> None:
    write_public_file(root / "EXPERIMENT_PROTOCOL.md", protocol_bytes)
    _write_json(root / "split_manifest.json", _split_manifest(report))
    _write_json(root / "historical_exclusion_manifest.json", _historical_manifest(report))
    _write_json(root / "near_duplicate_group_manifest.json", _group_manifest(report))
    _write_json(root / "config_manifest.json", _config_manifest(report))
    _write_json(root / "model_identity_receipt.json", _model_receipt(report))
    _write_json(root / "managed_runtime_receipt.json", _runtime_receipt(report))
    _write_json(root / "evolved_test_metrics.json", _arm_metrics(report.evolved_test, comparison, False))
    _write_json(root / "baseline_test_metrics.json", _arm_metrics(report.baseline_test, comparison, True))
    _write_json(root / "paired_test_comparison.json", _paired_comparison(report, comparison))
    _write_json(root / "call_and_failure_summary.json", _call_summary(report))
    _write_json(root / "final_artifact_manifest.json", _artifact_manifest(report))
    _write_json(root / "regression_receipt.json", report.regression.model_dump(mode="json"))
    _write_json(
        root / "formal_closure_receipt.json",
        closure.receipt.model_dump(mode="json"),
    )
    _write_csvs(root, report, batch_statistics)
    _write_charts(root, report, comparison)
    write_public_file(root / "PRECHECK_REPORT.md", _precheck_markdown(report).encode("utf-8"))
    write_public_file(
        root / "FINAL_REPORT.md",
        _final_report_markdown(report, comparison).encode("utf-8"),
    )
    write_public_file(root / "HANDOFF.md", _handoff_markdown(report, comparison).encode("utf-8"))
    chart_names = sorted(path.name for path in root.glob("*.png"))
    _write_json(
        root / "chart_inventory.json",
        {
            "schema_version": "TemperatureFullEvolveChartInventoryV1",
            "chart_count": len(chart_names),
            "charts": chart_names,
            "aggregate_only": True,
        },
    )
    package_files = sorted(
        [path.name for path in root.iterdir() if path.is_file()] + ["package_manifest.json"]
    )
    _write_json(
        root / "package_manifest.json",
        {
            "schema_version": REPORT_PACKAGE_SCHEMA,
            "status": "COMPLETE",
            "protocol_id": report.protocol_id,
            "controller_run_id": report.run_ids.controller_run_id,
            "generated_at_utc": report.generated_at_utc,
            "public_scope": "AGGREGATE_ONLY",
            "contains_item_identities": False,
            "contains_benchmark_content": False,
            "contains_answers_or_predictions": False,
            "contains_model_responses_or_transcripts": False,
            "files_excluding_sha256sums": package_files,
        },
    )


def _split_manifest(report: AggregateReportInputV1) -> dict[str, object]:
    return {
        "schema_version": "TemperatureFullEvolveSplitManifestV1",
        "status": "FROZEN_AND_COMPLETED",
        **report.split.model_dump(mode="json"),
        "split_sha256": report.identity.split_sha256,
        "dataset_sha256": report.identity.dataset_sha256,
        "contains_item_identities": False,
    }


def _historical_manifest(report: AggregateReportInputV1) -> dict[str, object]:
    policy = report.historical_reuse
    return {
        "schema_version": "TemperatureHistoricalReusePolicyManifestV1",
        **policy.model_dump(mode="json"),
        "disclosure": (
            "The fixed official Temperature pool was split without consulting historical "
            "exposure. Historical non-exposure is not claimed, and no prior learned state "
            "was imported."
        ),
        "contains_item_identities": False,
    }


def _group_manifest(report: AggregateReportInputV1) -> dict[str, object]:
    split = report.split
    return {
        "schema_version": "TemperatureNearDuplicateGroupManifestV1",
        "status": "PASS",
        "group_count": split.group_count,
        "singleton_group_count": split.singleton_group_count,
        "maximum_group_size": split.maximum_group_size,
        "group_manifest_sha256": report.identity.group_manifest_sha256,
        "group_assignment_sha256": report.identity.group_assignment_sha256,
        "uid_overlap_count": split.uid_overlap_count,
        "normalized_text_overlap_count": split.normalized_text_overlap_count,
        "normalized_option_overlap_count": split.normalized_option_overlap_count,
        "source_group_fields_available": split.source_group_fields_available,
        "source_group_overlap_count": split.source_group_overlap_count,
        "contains_group_members": False,
    }


def _config_manifest(report: AggregateReportInputV1) -> dict[str, object]:
    identity = report.identity
    return {
        "schema_version": "TemperatureFullEvolveConfigManifestV1",
        "protocol_id": report.protocol_id,
        "source_commit": identity.source_commit,
        "branch": identity.branch,
        "config_sha256": identity.config_sha256,
        "split_sha256": identity.split_sha256,
        "model": identity.model,
        "reasoning_effort": identity.reasoning_effort,
        "batch_size": TRAIN_BATCH_SIZE,
        "batch_count": TRAIN_BATCH_COUNT,
        "test_order": ["evolved", "baseline"],
        "candidate_baseline_stack_equal": identity.candidate_baseline_stack_equal,
        "test_feedback_enabled": identity.test_feedback_enabled,
        "test_reflector_calls": identity.test_reflector_calls,
        "test_core_jobs": identity.test_core_jobs,
        "test_artifact_updates": identity.test_artifact_updates,
    }


def _model_receipt(report: AggregateReportInputV1) -> dict[str, object]:
    identity = report.identity
    return {
        "schema_version": "TemperatureFullEvolveModelIdentityReceiptV1",
        "status": "PASS",
        "model": identity.model,
        "reasoning_effort": identity.reasoning_effort,
        "capture_mode": identity.capture_mode,
        "candidate_baseline_stack_equal": identity.candidate_baseline_stack_equal,
        "receipt_sha256": identity.model_identity_receipt_sha256,
    }


def _runtime_receipt(report: AggregateReportInputV1) -> dict[str, object]:
    identity = report.identity
    return {
        "schema_version": "TemperatureFullEvolveManagedRuntimeReceiptV1",
        "status": "PASS",
        "service_run_id": report.run_ids.runtime_service_run_id,
        "managed_codex_executable_sha256": identity.managed_codex_executable_sha256,
        "managed_candidate_image_id": identity.managed_candidate_image_id,
        "source_commit": identity.source_commit,
        "source_tree_sha256": identity.source_tree_sha256,
        "formal_runtime_identity_sha256": identity.formal_runtime_identity_sha256,
        "formal_runtime_receipt_sha256": identity.formal_runtime_receipt_sha256,
        "core_wheel_sha256": identity.core_wheel_sha256,
        "chembench_wheel_sha256": identity.chembench_wheel_sha256,
        "core_editable": identity.core_editable,
        "chembench_editable": identity.chembench_editable,
        "framework_lock_sha256": identity.framework_lock_sha256,
        "runtime_services_identity_sha256": identity.runtime_services_identity_sha256,
        "receipt_sha256": identity.managed_runtime_receipt_sha256,
    }


def _arm_metrics(
    arm: TestArmAggregateV1,
    comparison: PairedBinaryMetricsV1,
    reference: bool,
) -> dict[str, object]:
    correct = comparison.reference_correct if reference else comparison.candidate_correct
    accuracy = comparison.reference_accuracy if reference else comparison.candidate_accuracy
    wilson = comparison.reference_wilson_95 if reference else comparison.candidate_wilson_95
    attempts = arm.model_attempts
    return {
        "schema_version": "TemperatureFullEvolveTestArmMetricsV1",
        "status": "COMPLETE",
        "arm": arm.arm,
        "n": TEST_SIZE,
        "correct": correct,
        "accuracy": accuracy,
        "wilson_95": list(wilson),
        "accepted_completion_count": arm.accepted_completion_count,
        "official_parser_success_count": arm.official_parser_success_count,
        "strict_parser_success_count": arm.strict_parser_success_count,
        "logical_candidate_calls": arm.logical_candidate_calls,
        "model_attempts": attempts,
        "failed_attempts": arm.failed_attempts,
        "failure_rate": arm.failed_attempts / attempts,
        "retries": arm.retries,
        "rejected_attempts": arm.rejected_attempts,
        "context_bytes": arm.context_bytes,
        "context_sha256": arm.context_sha256,
        "feedback_enabled": arm.feedback_enabled,
        "reflector_calls": arm.reflector_calls,
        "core_jobs": arm.core_jobs,
        "artifact_updates": arm.artifact_updates,
        "contains_ordered_correctness_vector": False,
    }


def _paired_comparison(
    report: AggregateReportInputV1,
    comparison: PairedBinaryMetricsV1,
) -> dict[str, object]:
    return {
        "schema_version": "TemperatureFullEvolvePairedTestComparisonV1",
        "status": "COMPLETE",
        "primary_comparison": "final_C4_evolved_minus_generation_zero_baseline",
        "n": comparison.n,
        "baseline_correct": comparison.reference_correct,
        "evolved_correct": comparison.candidate_correct,
        "baseline_accuracy": comparison.reference_accuracy,
        "evolved_accuracy": comparison.candidate_accuracy,
        "baseline_wilson_95": list(comparison.reference_wilson_95),
        "evolved_wilson_95": list(comparison.candidate_wilson_95),
        "paired_delta_percentage_points": comparison.delta_percentage_points,
        "paired_bootstrap_delta_95_percentage_points": list(
            comparison.paired_bootstrap_delta_95_percentage_points
        ),
        "both_correct": comparison.both_correct,
        "baseline_only_correct": comparison.reference_only_correct,
        "evolved_only_correct": comparison.candidate_only_correct,
        "both_wrong": comparison.both_wrong,
        "negative_flip_rate": comparison.negative_flip_rate,
        "mcnemar_exact_p": comparison.mcnemar_exact_p,
        "bootstrap_replicates": comparison.bootstrap_replicates,
        "bootstrap_seed": comparison.bootstrap_seed,
        "official_parser_success_difference": (
            report.evolved_test.official_parser_success_count
            - report.baseline_test.official_parser_success_count
        ),
        "strict_parser_success_difference": (
            report.evolved_test.strict_parser_success_count
            - report.baseline_test.strict_parser_success_count
        ),
        "retry_difference": report.evolved_test.retries - report.baseline_test.retries,
        "contains_ordered_correctness_vectors": False,
    }


def _call_summary(report: AggregateReportInputV1) -> dict[str, object]:
    return {
        "schema_version": "TemperatureFullEvolveCallAndFailureSummaryV1",
        **report.calls.model_dump(mode="json"),
        "failure_category_total": report.calls.failures.total,
    }


def _artifact_manifest(report: AggregateReportInputV1) -> dict[str, object]:
    return {
        "schema_version": "TemperatureFullEvolveFinalArtifactManifestV1",
        **report.final_artifacts.model_dump(mode="json"),
        "core_run_id": report.run_ids.core_run_id,
    }


def _write_csvs(
    root: Path,
    report: AggregateReportInputV1,
    batch_statistics: tuple[PairedBinaryMetricsV1, ...],
) -> None:
    cumulative_pre = 0
    cumulative_post = 0
    batch_rows: list[tuple[object, ...]] = []
    transition_rows: list[tuple[object, ...]] = []
    artifact_rows: list[tuple[object, ...]] = []
    rule_rows: list[tuple[object, ...]] = []
    for batch, metrics in zip(report.train_batches, batch_statistics, strict=True):
        cumulative_pre += batch.pre_correct
        cumulative_post += batch.post_correct
        seen = batch.batch_index * TRAIN_BATCH_SIZE
        batch_rows.append(
            (
                batch.batch_index,
                batch.task_count,
                batch.pre_correct,
                batch.post_correct,
                batch.pre_correct / batch.task_count,
                batch.post_correct / batch.task_count,
                metrics.delta_percentage_points,
                cumulative_pre,
                cumulative_post,
                cumulative_pre / seen,
                cumulative_post / seen,
                metrics.mcnemar_exact_p,
                batch.pre_parser_success,
                batch.post_parser_success,
                batch.pre_candidate_calls + batch.post_candidate_calls,
                batch.reflector_calls,
                batch.core_jobs,
                batch.failed_attempts,
                batch.retries,
                batch.rejected_attempts,
            )
        )
        transition_rows.append(
            (
                batch.batch_index,
                batch.both_correct,
                batch.pre_only_correct,
                batch.post_only_correct,
                batch.both_wrong,
                batch.pre_only_correct / batch.task_count,
                metrics.mcnemar_exact_p,
            )
        )
        artifact_rows.append(
            (
                batch.batch_index,
                batch.artifacts.text_memory_bytes,
                batch.artifacts.skill_bundle_bytes,
                batch.artifacts.agent_system_bytes,
                batch.artifacts.combined_context_bytes,
                int(batch.artifacts.validation_passed),
            )
        )
        rule_rows.append(
            (
                batch.batch_index,
                batch.rules.active_rules,
                batch.rules.supported_rules,
                batch.rules.conflicted_rules,
                batch.rules.retired_rules,
                batch.rules.support_count_total,
                batch.rules.contradiction_count_total,
            )
        )
    _write_csv(
        root / "train_batch_metrics.csv",
        (
            "batch_index",
            "task_count",
            "pre_correct",
            "post_correct",
            "pre_accuracy",
            "post_accuracy",
            "delta_percentage_points",
            "cumulative_pre_correct",
            "cumulative_post_correct",
            "cumulative_pre_accuracy",
            "cumulative_post_accuracy",
            "mcnemar_exact_p",
            "pre_parser_success",
            "post_parser_success",
            "candidate_calls",
            "reflector_calls",
            "core_jobs",
            "failed_attempts",
            "retries",
            "rejected_attempts",
        ),
        tuple(batch_rows),
    )
    _write_csv(
        root / "train_transition_metrics.csv",
        (
            "batch_index",
            "both_correct",
            "pre_only_correct",
            "post_only_correct",
            "both_wrong",
            "negative_flip_rate",
            "mcnemar_exact_p",
        ),
        tuple(transition_rows),
    )
    _write_csv(
        root / "artifact_size_by_batch.csv",
        (
            "batch_index",
            "text_memory_bytes",
            "skill_bundle_bytes",
            "agent_system_bytes",
            "combined_context_bytes",
            "validation_passed",
        ),
        tuple(artifact_rows),
    )
    _write_csv(
        root / "rule_evidence_summary.csv",
        (
            "batch_index",
            "active_rules",
            "supported_rules",
            "conflicted_rules",
            "retired_rules",
            "support_count_total",
            "contradiction_count_total",
        ),
        tuple(rule_rows),
    )


def _write_charts(
    root: Path,
    report: AggregateReportInputV1,
    comparison: PairedBinaryMetricsV1,
) -> None:
    batches = [batch.batch_index for batch in report.train_batches]
    pre = [100 * batch.pre_correct / TRAIN_BATCH_SIZE for batch in report.train_batches]
    post = [100 * batch.post_correct / TRAIN_BATCH_SIZE for batch in report.train_batches]
    _grouped_bar(
        root / "train_batch_pre_post_accuracy.png",
        batches,
        (pre, post),
        ("Pre", "Post"),
        "Train batch pre/post accuracy",
        "Accuracy (%)",
    )
    cumulative_pre: list[float] = []
    cumulative_post: list[float] = []
    pre_sum = post_sum = 0
    for batch in report.train_batches:
        pre_sum += batch.pre_correct
        post_sum += batch.post_correct
        denominator = batch.batch_index * TRAIN_BATCH_SIZE
        cumulative_pre.append(100 * pre_sum / denominator)
        cumulative_post.append(100 * post_sum / denominator)
    _line_chart(
        root / "train_cumulative_pre_post_curve.png",
        batches,
        (cumulative_pre, cumulative_post),
        ("Cumulative pre", "Cumulative post"),
        "Train cumulative pre/post curve",
        "Accuracy (%)",
    )
    _stacked_bar(
        root / "train_correctness_transitions.png",
        batches,
        tuple(
            [getattr(batch, field) for batch in report.train_batches]
            for field in ("both_correct", "pre_only_correct", "post_only_correct", "both_wrong")
        ),
        ("Both correct", "Pre only", "Post only", "Both wrong"),
        "Train paired correctness transitions",
        "Count",
    )
    _line_chart(
        root / "artifact_size_by_batch.png",
        batches,
        tuple(
            [getattr(batch.artifacts, field) for batch in report.train_batches]
            for field in (
                "text_memory_bytes",
                "skill_bundle_bytes",
                "agent_system_bytes",
                "combined_context_bytes",
            )
        ),
        ("Text memory", "Skill bundle", "Agent system", "Combined"),
        "Artifact size by batch",
        "UTF-8 bytes",
    )
    _line_chart(
        root / "rule_evidence_curve.png",
        batches,
        tuple(
            [getattr(batch.rules, field) for batch in report.train_batches]
            for field in ("supported_rules", "conflicted_rules", "retired_rules")
        ),
        ("Supported", "Conflicted", "Retired"),
        "Rule evidence by batch",
        "Rule count",
    )
    _simple_bar(
        root / "test_baseline_vs_evolved_accuracy.png",
        ("Baseline", "Evolved C4"),
        (100 * comparison.reference_accuracy, 100 * comparison.candidate_accuracy),
        "Test accuracy",
        "Accuracy (%)",
    )
    _simple_bar(
        root / "test_paired_flips.png",
        ("Baseline only", "Evolved only", "Both correct", "Both wrong"),
        (
            comparison.reference_only_correct,
            comparison.candidate_only_correct,
            comparison.both_correct,
            comparison.both_wrong,
        ),
        "Test paired outcomes",
        "Count",
    )
    _simple_bar(
        root / "call_and_failure_summary.png",
        (
            "Candidate",
            "Reflector",
            "Core jobs",
            "Failures",
            "Retries",
            "Rejected",
            "Recoveries",
        ),
        (
            report.calls.candidate_logical_calls,
            report.calls.reflector_logical_calls,
            report.calls.core_jobs,
            report.calls.failed_attempts,
            report.calls.retries,
            report.calls.rejected_attempts,
            report.calls.recoveries,
        ),
        "Calls and failures",
        "Count",
    )


def _figure() -> tuple[object, object]:
    return plt.subplots(figsize=(8.4, 4.8), dpi=120, constrained_layout=True)


def _save_figure(path: Path, figure: object) -> None:
    stream = io.BytesIO()
    figure.savefig(
        stream,
        format="png",
        dpi=120,
        metadata={"Software": "OpenEvo Temperature Full-Evolve v1"},
    )
    plt.close(figure)
    write_public_file(path, stream.getvalue())


def _grouped_bar(
    path: Path,
    x: list[int],
    series: tuple[list[float], ...],
    labels: tuple[str, ...],
    title: str,
    ylabel: str,
) -> None:
    figure, axes = _figure()
    width = 0.34
    offsets = (-(len(series) - 1) * width / 2, (len(series) - 1) * width / 2)
    for values, label, offset in zip(series, labels, offsets, strict=True):
        axes.bar([value + offset for value in x], values, width=width, label=label)
    axes.set_xticks(x)
    axes.set_xlabel("Batch")
    axes.set_ylabel(ylabel)
    axes.set_title(title)
    axes.set_ylim(0, max(100, max(max(values) for values in series) * 1.1))
    axes.legend()
    axes.grid(axis="y", alpha=0.25)
    _save_figure(path, figure)


def _line_chart(
    path: Path,
    x: list[int],
    series: tuple[list[float], ...],
    labels: tuple[str, ...],
    title: str,
    ylabel: str,
) -> None:
    figure, axes = _figure()
    for values, label in zip(series, labels, strict=True):
        axes.plot(x, values, marker="o", linewidth=2, label=label)
    axes.set_xticks(x)
    axes.set_xlabel("Batch")
    axes.set_ylabel(ylabel)
    axes.set_title(title)
    axes.legend()
    axes.grid(alpha=0.25)
    _save_figure(path, figure)


def _stacked_bar(
    path: Path,
    x: list[int],
    series: tuple[list[int], ...],
    labels: tuple[str, ...],
    title: str,
    ylabel: str,
) -> None:
    figure, axes = _figure()
    bottom = [0] * len(x)
    for values, label in zip(series, labels, strict=True):
        axes.bar(x, values, bottom=bottom, label=label)
        bottom = [left + right for left, right in zip(bottom, values, strict=True)]
    axes.set_xticks(x)
    axes.set_xlabel("Batch")
    axes.set_ylabel(ylabel)
    axes.set_title(title)
    axes.legend(ncols=2)
    axes.grid(axis="y", alpha=0.25)
    _save_figure(path, figure)


def _simple_bar(
    path: Path,
    labels: tuple[str, ...],
    values: tuple[float | int, ...],
    title: str,
    ylabel: str,
) -> None:
    figure, axes = _figure()
    axes.bar(labels, values)
    axes.set_ylabel(ylabel)
    axes.set_title(title)
    axes.grid(axis="y", alpha=0.25)
    axes.tick_params(axis="x", rotation=15)
    _save_figure(path, figure)


def _precheck_markdown(report: AggregateReportInputV1) -> str:
    return f"""# Temperature Full-Evolve v1 Preflight

- Preflight ID: `{report.preflight.preflight_id}`
- Status before formal execution: **PASS**
- Preflight model calls: **0**
- Frozen split: {report.split.train_count} Train / {report.split.test_count} Test
- Split SHA-256: `{report.identity.split_sha256}`
- Generation-zero injected context: 0 bytes
- Focused/integration failures: 0/0
- Exactly-once recovery and Test feedback barriers: PASS

The fixed official Temperature pool was split without consulting historical exposure.
Historical non-exposure is not claimed. No prior artifact, completion, database, workspace,
or learned context was imported: C0 was empty.
"""


def _final_report_markdown(
    report: AggregateReportInputV1,
    comparison: PairedBinaryMetricsV1,
) -> str:
    total_pre = sum(batch.pre_correct for batch in report.train_batches)
    total_post = sum(batch.post_correct for batch in report.train_batches)
    delta = comparison.delta_percentage_points
    parser_equal = (
        report.evolved_test.official_parser_success_count
        == report.baseline_test.official_parser_success_count
        and report.evolved_test.strict_parser_success_count
        == report.baseline_test.strict_parser_success_count
    )
    retry_equal = report.evolved_test.retries == report.baseline_test.retries
    test_direction = "提高" if delta > 0 else "下降" if delta < 0 else "持平"
    success_count = sum(
        (
            delta > 0,
            comparison.candidate_only_correct > comparison.reference_only_correct,
            parser_equal and retry_equal,
            True,
            total_post > total_pre and delta > 0,
        )
    )
    return f"""# ChemBench Temperature Full-Evolve v1 最终报告

## 结论

实验已完整闭合。Test 主比较为固定最终状态 C4 减 generation-zero baseline：baseline
{comparison.reference_correct}/{comparison.n}（{100 * comparison.reference_accuracy:.2f}%），
evolved {comparison.candidate_correct}/{comparison.n}（{100 * comparison.candidate_accuracy:.2f}%），
配对差值 {delta:+.2f} percentage points，方向为**{test_direction}**。配对 bootstrap 95% CI
为 [{comparison.paired_bootstrap_delta_95_percentage_points[0]:+.2f},
{comparison.paired_bootstrap_delta_95_percentage_points[1]:+.2f}] pp；McNemar exact p =
{comparison.mcnemar_exact_p:.6g}。

这是一次单类别探索性实验；一次点估计或 p 值都不能证明 full-evolve 普遍有效。

## 数据与合规边界

- Train/Test: {report.split.train_count}/{report.split.test_count}；batch size 25，共 4 批。
- split SHA-256: `{report.identity.split_sha256}`。
- UID、标准化题面和选项集合的 Train/Test 交集均为 0。上游数据未提供可核验的
  source/paper/template group 字段，因此 source-group overlap 为 `NOT_ASSESSED`，不能虚报为 0；
  本实验仅报告基于可用题面与选项特征的确定性近重复分组。
- Test 在 Train 闭合前保持 sealed；Test feedback、Reflector、Core job、artifact update 均为 0。
- Candidate 与 baseline 使用相同模型、reasoning、prompt/harness policy、parser/evaluator 与 retry
  语义；managed Codex SHA-256 为 `{report.identity.managed_codex_executable_sha256}`。
- 历史暴露没有作为 eligibility exclusion，且没有用于本次固定池内的题目选择；
  因此被选项目可能曾在其他实验中暴露。与此同时，旧 artifact、completion、database、workspace
  的导入数量都为 0，C0 注入上下文为 0 bytes。该 provenance 限制不能被表述为 never-exposed
  验证。

## Train 同题诊断

四批 Pre 合计 {total_pre}/100，监督演化后同题 Post 合计 {total_post}/100，差值
{total_post - total_pre:+d} 题。Post 是看过本批 GT 的 Reflector 更新后的同题 replay，只衡量训练
适配，**不是独立泛化证据**。每批 transitions、McNemar、parser、调用、artifact 大小和规则
support/conflict 见 CSV 与图表。

最终 artifacts：text memory {report.final_artifacts.text_memory_bytes} bytes
(`{report.final_artifacts.text_memory_sha256}`)，skill bundle
{report.final_artifacts.skill_bundle_bytes} bytes (`{report.final_artifacts.skill_bundle_sha256}`)，
agent system {report.final_artifacts.agent_system_bytes} bytes
(`{report.final_artifacts.agent_system_sha256}`)；组合注入
{report.final_artifacts.combined_context_bytes} bytes。最终 Test 固定使用 C4，没有 sweep 或按 Test
挑选 checkpoint。

## Test 配对审查

| 配对状态 | 数量 |
|---|---:|
| Both correct | {comparison.both_correct} |
| Baseline only | {comparison.reference_only_correct} |
| Evolved only | {comparison.candidate_only_correct} |
| Both wrong | {comparison.both_wrong} |

Negative flip rate 为 {100 * comparison.negative_flip_rate:.2f}%。Baseline/Evolved Wilson 95% CI
分别为 [{100 * comparison.reference_wilson_95[0]:.2f}%,
{100 * comparison.reference_wilson_95[1]:.2f}%] 和
[{100 * comparison.candidate_wilson_95[0]:.2f}%,
{100 * comparison.candidate_wilson_95[1]:.2f}%]。Official parser 成功数为
{report.baseline_test.official_parser_success_count}/{report.evolved_test.official_parser_success_count}，
strict parser 为 {report.baseline_test.strict_parser_success_count}/
{report.evolved_test.strict_parser_success_count}；retry 为
{report.baseline_test.retries}/{report.evolved_test.retries}（baseline/evolved）。

预注册的五项成功方向同时满足 {success_count}/5：evolved 点估计更高；evolved-only 多于
baseline-only；增益不依赖 parser/retry 差异；Test 隔离且无反馈/演化；Train 同题提升与独立
Test 提升方向一致。未同时满足时应按未证实或负结果汇报，不能改用中间 checkpoint。

## 为什么 baseline 可能反而更好

Generation-zero 已有较强先验时，新增规则的上行空间有限，而少数错误规则即可制造更多 negative
flips。Batch supervision 虽比逐题更新更稳定，仍可能把局部反应模式、选项相对关系或本批分布
固化成过宽规则。三个 target 即使职责分开，额外上下文也会竞争注意力、改变原模型原本正确的
判断路径。Train Post 又是监督后的同题重跑，容易高估可迁移性；累计使用最终 C4 会保留后期
错误，不能靠一次 Test 后选择 C1-C3 来补救。Parser 100% 成功也只说明格式有效，不等于化学
判断正确。

## 下一步设计建议

1. 预注册独立 forward validation：batch i 的规则先在 batch i+1 反馈前评估，只用该 validation
   guard 退役高 negative-flip 规则；Test 仍不可参与选择。
2. 把规则晋升门槛从“两个支持”扩展为跨 source-group 支持、最低效应和反例覆盖；对高 baseline
   置信题设置 conservative override，只有多证据一致才允许改变判断。
3. 做 target ablation（memory only、skill only、agent-system only、三者组合）及固定多 split
   重复；所有 arms 预先注册并校正多重比较，避免事后挑最好。
4. 继续压缩重复上下文并记录 rule-to-flip 归因；优先退役高冲突、低覆盖、引发 negative flip 的
   规则，但只能依据 Train/validation 证据。
5. 增加与现有 Temperature 分布不同的外部 held-out 数据，并把历史复用实验定位为机制探索，
   不把它当作 never-exposed 泛化证明。
6. 用多次独立 managed runs 估计 sampling variance；同时报告 effect size、paired CI、McNemar 和
   parser/retry 完整性，不以一次 p 值作为 go/no-go 的唯一依据。

## 调用、故障与恢复

Candidate/Reflector/Core logical calls/jobs 为 {report.calls.candidate_logical_calls}/
{report.calls.reflector_logical_calls}/{report.calls.core_jobs}；实验模型逻辑调用为
{report.calls.experimental_model_logical_calls}，managed runtime session attempts 为
{report.calls.managed_runtime_session_attempts}。404 个 accepted experimental calls 均已通过
credential-readiness；readiness canary 自身的原始 provider attempt 精确总数未被权威采集，状态为
`{report.calls.canary_provider_attempts.status}`，只能报告保守下界
{report.calls.canary_provider_attempts.lower_bound}。因此不能把 404 解释为本实验全部 provider
调用的精确总数。Failed attempts/retries/rejected attempts/recoveries/invalidated runs 为
{report.calls.failed_attempts}/{report.calls.retries}/{report.calls.rejected_attempts}/
{report.calls.recoveries}/
{report.calls.invalidated_formal_runs}。完整闭集分类见 `call_and_failure_summary.json`。

Run IDs: {", ".join(f"`{value}`" for value in report.run_ids.all_run_ids)}。
生成时间：{report.generated_at_utc}。
"""


def _handoff_markdown(
    report: AggregateReportInputV1,
    comparison: PairedBinaryMetricsV1,
) -> str:
    return f"""# Temperature Full-Evolve v1 Handoff

- Status: `COMPLETE`
- Train/Test: 100/100; Train batches: 4
- Split SHA-256: `{report.identity.split_sha256}`
- Source commit: `{report.identity.source_commit}`
- Model/reasoning: `{report.identity.model}` / `{report.identity.reasoning_effort}`
- Managed Codex SHA-256: `{report.identity.managed_codex_executable_sha256}`
- Final text memory: `{report.final_artifacts.text_memory_sha256}`
  ({report.final_artifacts.text_memory_bytes} bytes)
- Final skill bundle: `{report.final_artifacts.skill_bundle_sha256}`
  ({report.final_artifacts.skill_bundle_bytes} bytes)
- Final agent system: `{report.final_artifacts.agent_system_sha256}`
  ({report.final_artifacts.agent_system_bytes} bytes)
- Evolved Test: {comparison.candidate_correct}/100
- Baseline Test: {comparison.reference_correct}/100
- Evolved - Baseline: {comparison.delta_percentage_points:+.2f} pp
- Paired flips baseline-only/evolved-only:
  {comparison.reference_only_correct}/{comparison.candidate_only_correct}
- McNemar exact p: {comparison.mcnemar_exact_p:.6g}
- Calls Candidate/Reflector/Core: {report.calls.candidate_logical_calls}/
  {report.calls.reflector_logical_calls}/{report.calls.core_jobs}
- Experimental model logical calls / managed runtime session attempts:
  {report.calls.experimental_model_logical_calls}/
  {report.calls.managed_runtime_session_attempts}
- Credential-readiness passed for accepted calls:
  {report.calls.readiness_passed_for_accepted_calls}; canary provider attempts:
  `{report.calls.canary_provider_attempts.status}` (lower bound
  {report.calls.canary_provider_attempts.lower_bound})
- Failures/retries/rejected/recoveries: {report.calls.failed_attempts}/
  {report.calls.retries}/{report.calls.rejected_attempts}/{report.calls.recoveries}
- Controller run: `{report.run_ids.controller_run_id}`
- Evolved/baseline runs: `{report.run_ids.evolved_run_id}` /
  `{report.run_ids.baseline_run_id}`

The package is aggregate-only. Verify every file except `SHA256SUMS.txt` against
`SHA256SUMS.txt` before copying or publishing it.
"""


def _write_json(path: Path, payload: object) -> None:
    write_public_file(path, canonical_pretty_json_bytes(payload))


def _write_csv(
    path: Path,
    columns: tuple[str, ...],
    rows: tuple[tuple[object, ...], ...],
) -> None:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows)
    write_public_file(path, stream.getvalue().encode("utf-8"))


__all__ = [
    "MAX_AGGREGATE_INPUT_BYTES",
    "REPORT_INPUT_SCHEMA",
    "REPORT_PACKAGE_SCHEMA",
    "AggregateReportInputV1",
    "BatchArtifactAggregateV1",
    "CallAndFailureAggregateV1",
    "ExecutionIdentityAggregateV1",
    "FailureAggregateV1",
    "FinalArtifactAggregateV1",
    "HistoricalReusePolicyAggregateV1",
    "PreflightAggregateV1",
    "RegressionAggregateV1",
    "RuleEvidenceAggregateV1",
    "RunIdentityAggregateV1",
    "SplitAggregateV1",
    "TemperatureReportError",
    "TestArmAggregateV1",
    "TrainBatchAggregateV1",
    "UnmeasuredProviderAttemptsAggregateV1",
    "load_aggregate_report_input_v1",
    "write_aggregate_report_input_v1",
    "write_final_report_package_v1",
]
