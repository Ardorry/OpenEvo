"""Zero-model-call admission gate for Temperature full-evolve v1."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from openevo_chembench.chembench4k_dataset import normalize_benchmark_text
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_public_file,
)
from openevo_chembench.temperature_full_evolve_v1.config import (
    BATCH_SIZE,
    CATEGORY,
    EXPECTED_CANDIDATE_IMAGE_ID,
    EXPECTED_CODEX_SHA256,
    HISTORICAL_EXPOSURE_POLICY,
    PROTOCOL_ID,
    TEST_COUNT,
    TRAIN_COUNT,
    TemperatureFullEvolveConfigV1,
)
from openevo_chembench.temperature_full_evolve_v1.evidence import RuleEvidenceIndexV1
from openevo_chembench.temperature_full_evolve_v1.split import FrozenTemperatureSplitV1

PREFLIGHT_SCHEMA = "TemperatureFullEvolvePreflightV1"
RUNTIME_EVIDENCE_SCHEMA = "TemperatureRuntimePreflightEvidenceV1"
REGRESSION_SCHEMA = "TemperatureRegressionReceiptV1"
MODEL_LOGICAL_CALLS = 404
CANDIDATE_LOGICAL_CALLS = 400
REFLECTOR_LOGICAL_CALLS = 4
CORE_JOBS = 12
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_COMMIT = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)


class TemperaturePreflightError(RuntimeError):
    """Content-free admission failure."""


class RuntimePreflightEvidenceV1(BaseModel):
    """Content-free identity evidence collected without invoking a model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = RUNTIME_EVIDENCE_SCHEMA
    candidate_codex_executable_sha256: str
    reflector_codex_executable_sha256: str
    candidate_image_id: str
    model: str
    reasoning_effort: str
    capture_mode: str
    credential_metadata_passed: bool
    credential_content_read: bool
    managed_runtime_passed: bool
    framework_registry_passed: bool
    framework_lock_sha256: str
    runtime_services_ready: bool
    runtime_services_identity_sha256: str
    completion_persistence_shared: bool
    completion_persistence_identity_sha256: str
    empty_runtime_inventory_sha256: str
    rollout_task_count_before_first_call: int = Field(ge=0)
    persisted_task_directory_count_before_first_call: int = Field(ge=0)
    model_visible_tools_enabled: StrictBool
    provider_transport_network_enabled: StrictBool
    model_calls: int = Field(ge=0)

    @model_validator(mode="after")
    def _identity(self) -> RuntimePreflightEvidenceV1:
        digests = (
            self.candidate_codex_executable_sha256,
            self.reflector_codex_executable_sha256,
            self.framework_lock_sha256,
            self.runtime_services_identity_sha256,
            self.completion_persistence_identity_sha256,
            self.empty_runtime_inventory_sha256,
        )
        if (
            self.schema_version != RUNTIME_EVIDENCE_SCHEMA
            or any(_SHA256.fullmatch(value) is None for value in digests)
            or self.candidate_codex_executable_sha256 != EXPECTED_CODEX_SHA256
            or self.reflector_codex_executable_sha256 != EXPECTED_CODEX_SHA256
            or self.candidate_image_id != EXPECTED_CANDIDATE_IMAGE_ID
            or self.model != "gpt-5.5"
            or self.reasoning_effort != "medium"
            or self.capture_mode != "transcript"
            or not self.credential_metadata_passed
            or self.credential_content_read
            or not self.managed_runtime_passed
            or not self.framework_registry_passed
            or not self.runtime_services_ready
            or not self.completion_persistence_shared
            or self.rollout_task_count_before_first_call
            or self.persisted_task_directory_count_before_first_call
            or self.model_visible_tools_enabled
            or not self.provider_transport_network_enabled
            or self.model_calls != 0
        ):
            raise ValueError("runtime preflight identity is not admissible")
        return self

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.model_dump(mode="json")))


class RegressionReceiptV1(BaseModel):
    """Test evidence whose command/output digests are fixed before paid execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = REGRESSION_SCHEMA
    focused_test_count: int = Field(ge=1)
    focused_failure_count: int = Field(ge=0)
    integration_test_count: int = Field(ge=1)
    integration_failure_count: int = Field(ge=0)
    command_sha256: str
    output_sha256: str
    model_calls: int = Field(ge=0)

    @model_validator(mode="after")
    def _passed(self) -> RegressionReceiptV1:
        if (
            self.schema_version != REGRESSION_SCHEMA
            or _SHA256.fullmatch(self.command_sha256) is None
            or _SHA256.fullmatch(self.output_sha256) is None
            or self.focused_failure_count
            or self.integration_failure_count
            or self.model_calls
        ):
            raise ValueError("regression receipt is not a zero-call pass")
        return self

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.model_dump(mode="json")))


@dataclass(frozen=True, slots=True)
class TemperaturePreflightBundleV1:
    report: dict[str, Any]
    split_manifest: dict[str, Any]
    historical_manifest: dict[str, Any]
    grouping_manifest: dict[str, Any]
    config_manifest: dict[str, Any]
    model_identity_receipt: dict[str, Any]
    runtime_identity_receipt: dict[str, Any]
    regression_receipt: dict[str, Any]

    @property
    def digest(self) -> str:
        return sha256_bytes(
            canonical_json_bytes(
                {
                    "report": self.report,
                    "split_manifest": self.split_manifest,
                    "historical_manifest": self.historical_manifest,
                    "grouping_manifest": self.grouping_manifest,
                    "config_manifest": self.config_manifest,
                    "model_identity_receipt": self.model_identity_receipt,
                    "runtime_identity_receipt": self.runtime_identity_receipt,
                    "regression_receipt": self.regression_receipt,
                }
            )
        )


def build_zero_model_preflight_v1(
    *,
    config: TemperatureFullEvolveConfigV1,
    split: FrozenTemperatureSplitV1,
    runtime: RuntimePreflightEvidenceV1,
    regression: RegressionReceiptV1,
    source_commit: str,
    source_tree_clean: bool,
) -> TemperaturePreflightBundleV1:
    """Recompute every static admission property and return aggregate evidence."""

    if type(config) is not TemperatureFullEvolveConfigV1:
        raise TypeError("preflight requires the exact Temperature config")
    if type(split) is not FrozenTemperatureSplitV1:
        raise TypeError("preflight requires the exact frozen split")
    if type(runtime) is not RuntimePreflightEvidenceV1 or type(regression) is not RegressionReceiptV1:
        raise TypeError("preflight identity receipts must be exact DTOs")
    if _COMMIT.fullmatch(source_commit) is None or source_tree_clean is not True:
        raise TemperaturePreflightError("SOURCE_IDENTITY_NOT_FROZEN")
    _require_current_run_split_isolation(split)
    zero = RuleEvidenceIndexV1.generation_zero()
    if zero.rules or zero.batch_index != 0:
        raise TemperaturePreflightError("GENERATION_ZERO_EVIDENCE_INVALID")
    schedule = _build_call_schedule()
    if (
        len(schedule) != MODEL_LOGICAL_CALLS
        or len(set(schedule)) != MODEL_LOGICAL_CALLS
        or sum(value.startswith("reflector-") for value in schedule) != REFLECTOR_LOGICAL_CALLS
    ):
        raise TemperaturePreflightError("LOGICAL_CALL_SCHEDULE_INVALID")

    split_manifest = {
        "schema_version": "TemperatureSplitManifestAggregateV1",
        "status": "PASS",
        "category": CATEGORY,
        "train_count": len(split.train),
        "test_count": len(split.test),
        "reserve_count": len(split.reserve),
        "batch_size": BATCH_SIZE,
        "batch_count": len(split.train) // BATCH_SIZE,
        "split_sha256": split.plan.split_sha256,
        "train_order_sha256": _order_digest(tuple(task.uid for task in split.train)),
        "test_order_sha256": _order_digest(tuple(task.uid for task in split.test)),
        "dataset_sha256": split.dataset_combined_sha256,
        "dev_demonstration_count": split.dev_demonstration_count,
        "dev_demonstration_order_sha256": split.dev_demonstration_set_sha256,
        "dev_demonstration_content_set_sha256": (
            split.dev_demonstration_content_set_sha256
        ),
        "dev_train_uid_overlap_count": 0,
        "dev_test_uid_overlap_count": 0,
        "dev_reserve_uid_overlap_count": 0,
        "dev_partition_ordered_prompt_overlap_count": 0,
        "dev_partition_unordered_prompt_overlap_count": 0,
        "current_run_train_test_isolated": True,
        "contains_item_identities": False,
    }
    historical_manifest = {
        "schema_version": "TemperatureHistoricalExposurePolicyManifestV1",
        "policy": HISTORICAL_EXPOSURE_POLICY,
        "historical_exposure_used_for_eligibility": False,
        "historical_items_excluded": 0,
        "eligible_source_pool_count": len(split.train) + len(split.test) + len(split.reserve),
        "prior_artifacts_imported": 0,
        "prior_completions_imported": 0,
        "prior_databases_imported": 0,
        "generation_zero_is_fresh": True,
        "contains_item_identities": False,
    }
    grouping_manifest = {
        "schema_version": "TemperatureNearDuplicateGroupingAggregateV1",
        "status": "PASS",
        "group_count": split.plan.group_count,
        "singleton_group_count": split.plan.singleton_group_count,
        "maximum_group_size": split.plan.maximum_group_size,
        "edge_counts": dict(split.plan.edge_counts),
        "group_manifest_sha256": split.plan.group_manifest_sha256,
        "group_assignment_sha256": split.plan.group_assignment_sha256,
        "whole_group_assignment": True,
        "source_group_fields_available": False,
        "contains_item_identities": False,
    }
    config_manifest = {
        "schema_version": "TemperatureConfigManifestV1",
        "protocol_id": PROTOCOL_ID,
        "config_sha256": config.digest,
        "source_commit": source_commit,
        "source_tree_clean": True,
        "model": config.model,
        "reasoning_effort": config.reasoning_effort,
        "candidate_and_baseline_prompt_template_equal": True,
        "candidate_and_baseline_parser_equal": True,
        "candidate_and_baseline_evaluator_equal": True,
        "candidate_and_baseline_retry_policy_equal": True,
        "candidate_and_baseline_timeout_equal": True,
        "test_order": ["evolved", "baseline"],
        "test_feedback_enabled": False,
        "test_reflector_calls": 0,
        "test_core_jobs": 0,
        "generation_zero_context_bytes": 0,
        "logical_call_schedule_sha256": sha256_bytes(canonical_json_bytes(schedule)),
    }
    model_receipt = {
        "schema_version": "TemperatureModelIdentityReceiptV1",
        "status": "PASS_ZERO_MODEL_CALLS",
        "model": runtime.model,
        "reasoning_effort": runtime.reasoning_effort,
        "candidate_codex_executable_sha256": runtime.candidate_codex_executable_sha256,
        "reflector_codex_executable_sha256": runtime.reflector_codex_executable_sha256,
        "candidate_image_id": runtime.candidate_image_id,
        "candidate_baseline_identity_equal": True,
        "model_calls": 0,
    }
    runtime_receipt = {
        **runtime.model_dump(mode="json"),
        "runtime_preflight_sha256": runtime.digest,
    }
    findings = {
        "data_count": "PASS",
        "uid_overlap": "PASS",
        "normalized_question_option_overlap": "PASS",
        "group_isolation": "PASS",
        "generation_zero_empty_context": "PASS",
        "managed_codex_identity": "PASS",
        "credential_metadata": "PASS",
        "parser_evaluator_identity": "PASS",
        "formal_harness_only": "PASS",
        "model_visible_execution_and_search_tools_disabled": "PASS",
        "fresh_runtime_task_and_completion_inventory_empty": "PASS",
        "test_feedback_disabled": "PASS",
        "exactly_once_ledger_tests": "PASS",
        "checkpoint_idempotency_tests": "PASS",
        "three_target_schema_lineage_budget_tests": "PASS",
        "regression_tests": "PASS",
        "shared_completion_persistence": "PASS",
    }
    report = {
        "schema_version": PREFLIGHT_SCHEMA,
        "status": "PASS_READY_FOR_FORMAL_EXECUTION",
        "protocol_id": PROTOCOL_ID,
        "source_commit": source_commit,
        "config_sha256": config.digest,
        "split_sha256": split.plan.split_sha256,
        "runtime_preflight_sha256": runtime.digest,
        "regression_receipt_sha256": regression.digest,
        "findings": findings,
        "planned": {
            "train_count": TRAIN_COUNT,
            "test_count": TEST_COUNT,
            "train_batch_count": TRAIN_COUNT // BATCH_SIZE,
            "candidate_logical_calls": CANDIDATE_LOGICAL_CALLS,
            "reflector_logical_calls": REFLECTOR_LOGICAL_CALLS,
            "model_logical_calls": MODEL_LOGICAL_CALLS,
            "core_jobs": CORE_JOBS,
        },
        "preflight_model_calls": 0,
    }
    return TemperaturePreflightBundleV1(
        report=report,
        split_manifest=split_manifest,
        historical_manifest=historical_manifest,
        grouping_manifest=grouping_manifest,
        config_manifest=config_manifest,
        model_identity_receipt=model_receipt,
        runtime_identity_receipt=runtime_receipt,
        regression_receipt=regression.model_dump(mode="json"),
    )


def write_public_preflight_bundle_v1(
    bundle: TemperaturePreflightBundleV1,
    *,
    destination: Path,
) -> None:
    if type(bundle) is not TemperaturePreflightBundleV1:
        raise TypeError("preflight bundle must be exact")
    if not isinstance(destination, Path) or not destination.is_absolute():
        raise TypeError("preflight destination must be absolute")
    destination.mkdir(parents=True, exist_ok=False)
    payloads = {
        "preflight_report.json": bundle.report,
        "split_manifest.json": bundle.split_manifest,
        "historical_exclusion_manifest.json": bundle.historical_manifest,
        "near_duplicate_group_manifest.json": bundle.grouping_manifest,
        "config_manifest.json": bundle.config_manifest,
        "model_identity_receipt.json": bundle.model_identity_receipt,
        "managed_runtime_receipt.json": bundle.runtime_identity_receipt,
        "regression_receipt_v1.json": bundle.regression_receipt,
    }
    for name, payload in payloads.items():
        write_public_file(destination / name, canonical_pretty_json_bytes(payload))
    write_public_file(
        destination / "PRECHECK_REPORT.md",
        _render_precheck_markdown(bundle).encode("utf-8"),
    )


def _require_current_run_split_isolation(split: FrozenTemperatureSplitV1) -> None:
    partitions = (split.train, split.test, split.reserve)
    uid_sets = tuple({task.uid for task in values} for values in partitions)
    if (
        tuple(map(len, partitions)) != (TRAIN_COUNT, TEST_COUNT, 2)
        or uid_sets[0] & uid_sets[1]
        or uid_sets[0] & uid_sets[2]
        or uid_sets[1] & uid_sets[2]
    ):
        raise TemperaturePreflightError("CURRENT_RUN_UID_ISOLATION_INVALID")
    signatures: list[set[str]] = []
    unordered: list[set[str]] = []
    for values in partitions:
        signatures.append(
            {
                _task_signature(task.question, (task.A, task.B, task.C, task.D), unordered=False)
                for task in values
            }
        )
        unordered.append(
            {
                _task_signature(task.question, (task.A, task.B, task.C, task.D), unordered=True)
                for task in values
            }
        )
    if (
        signatures[0] & signatures[1]
        or signatures[0] & signatures[2]
        or signatures[1] & signatures[2]
        or unordered[0] & unordered[1]
        or unordered[0] & unordered[2]
        or unordered[1] & unordered[2]
    ):
        raise TemperaturePreflightError("CURRENT_RUN_CONTENT_ISOLATION_INVALID")


def _task_signature(question: str, options: tuple[str, ...], *, unordered: bool) -> str:
    normalized_options = tuple(normalize_benchmark_text(value) for value in options)
    if unordered:
        normalized_options = tuple(sorted(normalized_options))
    return sha256_bytes(
        canonical_json_bytes(
            {
                "question": normalize_benchmark_text(question),
                "options": normalized_options,
            }
        )
    )


def _build_call_schedule() -> tuple[str, ...]:
    values: list[str] = []
    for batch in range(1, TRAIN_COUNT // BATCH_SIZE + 1):
        values.extend(f"train-pre-b{batch:02d}-i{item:03d}" for item in range(1, 26))
        values.append(f"reflector-b{batch:02d}")
        values.extend(f"train-post-b{batch:02d}-i{item:03d}" for item in range(1, 26))
    values.extend(f"evolved-test-i{item:03d}" for item in range(1, TEST_COUNT + 1))
    values.extend(f"baseline-test-i{item:03d}" for item in range(1, TEST_COUNT + 1))
    return tuple(values)


def _order_digest(values: tuple[str, ...]) -> str:
    return sha256_bytes(canonical_json_bytes(values))


def _render_precheck_markdown(bundle: TemperaturePreflightBundleV1) -> str:
    report = bundle.report
    lines = [
        "# Temperature full-evolve v1 preflight",
        "",
        f"Status: `{report['status']}`",
        "",
        (
            "This admission used zero model calls. The current-run split is 100 Train / "
            "100 Test with four fixed batches of 25. Generation zero has empty text memory, "
            "skill, agent-system, and evidence state."
        ),
        "",
        (
            "The fixed official Temperature pool is split without consulting historical "
            "exposure. Historical non-exposure is not claimed; no prior completion, artifact, "
            "database, workspace, or cache is imported."
        ),
        "",
        f"Split SHA-256: `{report['split_sha256']}`",
        f"Config SHA-256: `{report['config_sha256']}`",
        f"Source commit: `{report['source_commit']}`",
        f"Preflight bundle SHA-256: `{bundle.digest}`",
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "CANDIDATE_LOGICAL_CALLS",
    "CORE_JOBS",
    "MODEL_LOGICAL_CALLS",
    "PREFLIGHT_SCHEMA",
    "REFLECTOR_LOGICAL_CALLS",
    "RegressionReceiptV1",
    "RuntimePreflightEvidenceV1",
    "TemperaturePreflightBundleV1",
    "TemperaturePreflightError",
    "build_zero_model_preflight_v1",
    "write_public_preflight_bundle_v1",
]
