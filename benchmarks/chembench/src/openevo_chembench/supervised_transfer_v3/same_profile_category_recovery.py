"""Category-atomic recovery for the active two-round v3 profile.

This amendment accepts only fully closed category shards from an immutable
failed parent.  The current partial category is discarded in full, and a new
run starts that category at task zero with fresh Core state and generation-zero
targets.  Parent databases and artifacts are audit inputs only.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from openevo.runtime.managed import MANAGED_RUNTIME_RELEASES
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_public_file,
)
from openevo_chembench.supervised_transfer_v2 import experiment as v2
from openevo_chembench.supervised_transfer_v2.core import TaskwiseCoreUpdateResultV1
from openevo_chembench.supervised_transfer_v3.category_shard_recovery import (
    _RUN_ID,
    _active_processes,
    _executor_digest,
    _model_digest,
    _read_json,
    _read_jsonl,
    _read_regular,
    _tree_identity,
    _verify_result_lifecycle_read_only,
)
from openevo_chembench.supervised_transfer_v3.config import (
    TEST_COUNT,
    TWO_ROUND_ONE_EVOLUTION_PROTOCOL_ID,
)
from openevo_chembench.supervised_transfer_v3.experiment import (
    SupervisedExperimentV3Error,
    SupervisedTransferExperimentV3,
)

SAME_PROFILE_CATEGORY_RECOVERY_PROTOCOL_ID = (
    "supervised_transfer_v3_two_round_one_evolution_category_shard_recovery"
)
SAME_PROFILE_PARENT_RUN_ID = "stv3-one-update-online-20260730T165823Z"
SAME_PROFILE_START_CATEGORY_INDEX = 5
SAME_PROFILE_ACCEPTED_CATEGORIES = CHEMBENCH4K_CATEGORIES[:SAME_PROFILE_START_CATEGORY_INDEX]
SAME_PROFILE_DISCARDED_CATEGORY = CHEMBENCH4K_CATEGORIES[SAME_PROFILE_START_CATEGORY_INDEX]
SAME_PROFILE_REMAINING_CATEGORIES = CHEMBENCH4K_CATEGORIES[SAME_PROFILE_START_CATEGORY_INDEX:]
EXPECTED_MANAGED_CODEX_SHA256 = "a96f944d1a596dbfb7fdd84f482be5c50e34b04bb371126840d873e4ebf26902"
_SHA256_LENGTH = 64
_COMMIT_LENGTH = 40


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.model_dump(mode="json")))


class SameProfileAcceptedCategoryShardV3(_ClosedModel):
    schema_version: Literal["SameProfileAcceptedCategoryShardV3"] = (
        "SameProfileAcceptedCategoryShardV3"
    )
    parent_run_id: str
    category_index: int = Field(ge=0, le=4)
    category: str
    task_start: Literal[0] = 0
    task_end: Literal[49] = 49
    status: Literal["fully_closed"] = "fully_closed"
    source_commit: str
    config_digest: str
    split_digest: str
    model_digest: str
    executor_digest: str
    managed_codex_digest: str
    candidate_completion_count: Literal[100] = 100
    reflector_call_count: Literal[50] = 50
    core_job_count: Literal[150] = 150
    text_memory_artifact_count: Literal[50] = 50
    skill_bundle_artifact_count: Literal[50] = 50
    agent_system_artifact_count: Literal[50] = 50
    context_resolution_count: Literal[150] = 150
    active_lease_count: Literal[0] = 0
    staged_job_count: Literal[0] = 0
    final_category_state_closed: Literal[True] = True
    category_state_independent: Literal[True] = True
    included_in_final_statistics: Literal[True] = True
    category_tree_sha256: str
    final_head_set_sha256: str

    @field_validator("source_commit")
    @classmethod
    def _commit(cls, value: str) -> str:
        if len(value) != _COMMIT_LENGTH or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("source commit must be a full lowercase Git commit")
        return value

    @field_validator(
        "config_digest",
        "split_digest",
        "model_digest",
        "executor_digest",
        "managed_codex_digest",
        "category_tree_sha256",
        "final_head_set_sha256",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        if len(value) != _SHA256_LENGTH or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("closed shard digest must be SHA-256")
        return value


class SameProfileAcceptedClosedShardsReceiptV3(_ClosedModel):
    schema_version: Literal["SameProfileAcceptedClosedShardsReceiptV3"] = (
        "SameProfileAcceptedClosedShardsReceiptV3"
    )
    parent_run_id: str
    shards: tuple[SameProfileAcceptedCategoryShardV3, ...]
    accepted_category_count: Literal[5] = 5
    accepted_task_count: Literal[250] = 250
    accepted_candidate_completion_count: Literal[500] = 500
    accepted_reflector_call_count: Literal[250] = 250
    accepted_core_job_count: Literal[750] = 750
    accepted_context_resolution_count: Literal[750] = 750
    included_in_final_statistics: Literal[True] = True

    @model_validator(mode="after")
    def _closed_prefix(self) -> SameProfileAcceptedClosedShardsReceiptV3:
        if (
            tuple(shard.category_index for shard in self.shards) != tuple(range(5))
            or tuple(shard.category for shard in self.shards) != SAME_PROFILE_ACCEPTED_CATEGORIES
            or any(shard.parent_run_id != self.parent_run_id for shard in self.shards)
        ):
            raise ValueError("accepted shards must be the exact closed five-category prefix")
        return self


class SameProfileDiscardedPartialShardReceiptV3(_ClosedModel):
    schema_version: Literal["SameProfileDiscardedPartialShardReceiptV3"] = (
        "SameProfileDiscardedPartialShardReceiptV3"
    )
    parent_run_id: str
    category_index: Literal[5] = SAME_PROFILE_START_CATEGORY_INDEX
    category: Literal["Retrosynthesis"] = SAME_PROFILE_DISCARDED_CATEGORY
    discard_entire_category: Literal[True] = True
    task_zero_through_seven_closed: Literal[True] = True
    task_eight_round_zero_completion_exists: Literal[True] = True
    task_eight_reflector_failed: Literal[True] = True
    task_eight_round_one_completion_exists: Literal[False] = False
    completed_candidate_calls_discarded: Literal[17] = 17
    successful_reflector_calls_discarded: Literal[8] = 8
    failed_reflector_invocations_discarded: Literal[1] = 1
    successful_core_jobs_discarded: Literal[24] = 24
    failed_core_jobs_discarded: Literal[1] = 1
    text_memory_artifacts_discarded: Literal[8] = 8
    skill_bundle_artifacts_discarded: Literal[8] = 8
    agent_system_artifacts_discarded: Literal[8] = 8
    context_resolutions_discarded: Literal[24] = 24
    included_in_final_statistics: Literal[False] = False
    used_as_recovery_input: Literal[False] = False
    artifacts_imported: Literal[False] = False
    database_imported: Literal[False] = False
    actual_paid_usage_retained: Literal[True] = True
    parent_evidence_preserved: Literal[True] = True
    category_tree_sha256: str
    failed_reflector_receipt_sha256: str
    failed_reflector_event_stream_sha256: str
    completed_response_after_timeout_notice: Literal[True] = True

    @field_validator(
        "category_tree_sha256",
        "failed_reflector_receipt_sha256",
        "failed_reflector_event_stream_sha256",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        if len(value) != _SHA256_LENGTH or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("discarded shard tree digest must be SHA-256")
        return value


class SameProfileCategoryShardRecoveryAmendmentV3(_ClosedModel):
    schema_version: Literal["SameProfileCategoryShardRecoveryAmendmentV3"] = (
        "SameProfileCategoryShardRecoveryAmendmentV3"
    )
    original_protocol_id: Literal[
        "chembench_supervised_transfer_v3_two_round_one_evolution_online_only"
    ] = TWO_ROUND_ONE_EVOLUTION_PROTOCOL_ID
    recovery_protocol_id: Literal[
        "supervised_transfer_v3_two_round_one_evolution_category_shard_recovery"
    ] = SAME_PROFILE_CATEGORY_RECOVERY_PROTOCOL_ID
    amendment_reason: Literal["NETWORK_SWITCH_TRANSIENT_TIMEOUT"] = (
        "NETWORK_SWITCH_TRANSIENT_TIMEOUT"
    )
    authorization: Literal["explicit_user_authorization"] = "explicit_user_authorization"
    parent_run_id: str
    recovery_run_id: str
    accepted_parent_categories: tuple[str, ...] = SAME_PROFILE_ACCEPTED_CATEGORIES
    discarded_parent_category: Literal["Retrosynthesis"] = SAME_PROFILE_DISCARDED_CATEGORY
    start_category_index: Literal[5] = SAME_PROFILE_START_CATEGORY_INDEX
    start_category: Literal["Retrosynthesis"] = SAME_PROFILE_DISCARDED_CATEGORY
    start_task_ordinal: Literal[0] = 0
    start_round: Literal[0] = 0
    generation_zero_targets: Literal[True] = True
    parent_artifacts_imported: Literal[False] = False
    parent_database_imported: Literal[False] = False
    category_shard_recovery: Literal[True] = True
    closed_parent_shards_reused: Literal[5] = 5
    partial_parent_shard_fully_discarded: Literal[True] = True
    uninterrupted_single_process_run: Literal[False] = False


class SameProfileRemainingShardExecutionPlanV3(_ClosedModel):
    schema_version: Literal["SameProfileRemainingShardExecutionPlanV3"] = (
        "SameProfileRemainingShardExecutionPlanV3"
    )
    parent_run_id: str
    recovery_run_id: str
    start_category_index: Literal[5] = SAME_PROFILE_START_CATEGORY_INDEX
    categories: tuple[str, ...]
    first_category: Literal["Retrosynthesis"] = SAME_PROFILE_DISCARDED_CATEGORY
    first_task_ordinal: Literal[0] = 0
    first_round: Literal[0] = 0
    generation_zero_targets: Literal[True] = True
    train_task_count: Literal[200] = 200
    train_candidate_calls: Literal[400] = 400
    reflector_calls: Literal[200] = 200
    core_jobs: Literal[600] = 600
    typed_artifacts: Literal[600] = 600
    context_resolutions: Literal[600] = 600
    candidate_readiness_calls_minimum: Literal[400] = 400
    candidate_readiness_calls_maximum: Literal[800] = 800
    answer_and_reflector_model_calls: Literal[600] = 600
    total_model_calls_minimum: Literal[1000] = 1000
    total_model_calls_maximum: Literal[1400] = 1400
    eventual_final_test_candidate_calls: Literal[450] = TEST_COUNT
    parent_artifacts_imported: Literal[False] = False
    parent_database_imported: Literal[False] = False

    @model_validator(mode="after")
    def _order(self) -> SameProfileRemainingShardExecutionPlanV3:
        if self.categories != SAME_PROFILE_REMAINING_CATEGORIES:
            raise ValueError("remaining categories differ from the frozen v3 order")
        return self


class SameProfileShardCompositionEntryV3(_ClosedModel):
    category: str
    category_index: int = Field(ge=0, le=8)
    source_run_id: str
    source_commit: str
    config_digest: str
    split_digest: str
    model_digest: str
    executor_digest: str
    managed_codex_digest: str
    task_count: Literal[50] = 50
    completion_count: Literal[100] = 100
    reflector_count: Literal[50] = 50
    artifact_count: Literal[150] = 150
    closed_receipt_sha256: str
    included_in_statistics: Literal[True] = True

    @field_validator(
        "config_digest",
        "split_digest",
        "model_digest",
        "executor_digest",
        "managed_codex_digest",
        "closed_receipt_sha256",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        if len(value) != _SHA256_LENGTH or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("composition digest must be SHA-256")
        return value


class SameProfileFinalShardCompositionReceiptV3(_ClosedModel):
    schema_version: Literal["SameProfileFinalShardCompositionReceiptV3"] = (
        "SameProfileFinalShardCompositionReceiptV3"
    )
    protocol_id: str = SAME_PROFILE_CATEGORY_RECOVERY_PROTOCOL_ID
    categories: tuple[SameProfileShardCompositionEntryV3, ...]
    category_shard_recovery: Literal[True] = True
    closed_parent_shards_reused: Literal[5] = 5
    partial_parent_shard_fully_discarded: Literal[True] = True
    uninterrupted_single_process_run: Literal[False] = False
    parent_closed_shard_answer_and_reflector_calls_included: Literal[750] = 750
    parent_partial_shard_answer_and_reflector_invocations_discarded: Literal[26] = 26
    recovery_train_answer_and_reflector_calls_included: Literal[600] = 600
    actual_answer_and_reflector_calls_at_train_composition: Literal[1376] = 1376
    answer_and_reflector_calls_in_train_result: Literal[1350] = 1350
    planned_composed_final_test_calls: Literal[450] = 450

    @model_validator(mode="after")
    def _identity(self) -> SameProfileFinalShardCompositionReceiptV3:
        if (
            tuple(item.category_index for item in self.categories) != tuple(range(9))
            or tuple(item.category for item in self.categories) != CHEMBENCH4K_CATEGORIES
        ):
            raise ValueError("composition must contain all nine categories exactly once")
        identities = {
            (
                item.config_digest,
                item.split_digest,
                item.model_digest,
                item.executor_digest,
                item.managed_codex_digest,
            )
            for item in self.categories
        }
        if len(identities) != 1:
            raise ValueError("category shards are not execution-compatible")
        return self


@dataclass(frozen=True, slots=True)
class SameProfileAuditedParentShardsV3:
    repository_root: Path
    parent_run_id: str
    parent_state_root: Path
    parent_result_root: Path
    parent_tree_sha256: str
    parent_tree_file_count: int
    parent_tree_size_bytes: int
    accepted: SameProfileAcceptedClosedShardsReceiptV3
    discarded: SameProfileDiscardedPartialShardReceiptV3


def _require_same_profile(inputs: v2.ExperimentInputsV2) -> None:
    config = inputs.config
    if (
        getattr(config, "protocol_id", None) != TWO_ROUND_ONE_EVOLUTION_PROTOCOL_ID
        or getattr(config, "train_rounds", None) != 2
        or getattr(config, "evolution_cycles", None) != 1
    ):
        raise SupervisedExperimentV3Error("SAME_PROFILE_RECOVERY_CONFIG_INVALID")


def same_profile_remaining_categories_v3(
    start_category_index: int = SAME_PROFILE_START_CATEGORY_INDEX,
) -> tuple[str, ...]:
    if (
        type(start_category_index) is not int
        or start_category_index != SAME_PROFILE_START_CATEGORY_INDEX
    ):
        raise SupervisedExperimentV3Error("SAME_PROFILE_RECOVERY_START_INDEX_INVALID")
    return CHEMBENCH4K_CATEGORIES[start_category_index:]


def build_same_profile_category_recovery_dry_run_v3(
    *,
    inputs: v2.ExperimentInputsV2,
    audit: SameProfileAuditedParentShardsV3,
    recovery_run_id: str,
) -> dict[str, object]:
    """Build the fresh remaining-category schedule without model calls or run state."""

    if type(inputs) is not v2.ExperimentInputsV2:
        raise TypeError("inputs must be exact ExperimentInputsV2")
    if type(audit) is not SameProfileAuditedParentShardsV3:
        raise TypeError("audit must be exact SameProfileAuditedParentShardsV3")
    _require_same_profile(inputs)
    if _RUN_ID.fullmatch(recovery_run_id) is None:
        raise ValueError("invalid recovery run ID")
    if inputs.repository_root != audit.repository_root:
        raise SupervisedExperimentV3Error("SAME_PROFILE_RECOVERY_REPOSITORY_MISMATCH")
    by_category = v2._by_category(inputs.train)
    schedule: list[dict[str, object]] = []
    ordered_uids: list[str] = []
    for category_index, category in enumerate(
        SAME_PROFILE_REMAINING_CATEGORIES,
        start=SAME_PROFILE_START_CATEGORY_INDEX,
    ):
        tasks = by_category[category]
        if len(tasks) != 50:
            raise SupervisedExperimentV3Error("SAME_PROFILE_RECOVERY_TASK_COUNT_INVALID")
        ordered_uids.extend(task.uid for task in tasks)
        schedule.append(
            {
                "category_index": category_index,
                "category": category,
                "task_start": 0,
                "task_end": 49,
                "task_count": 50,
                "sessions_per_task": 2,
                "cycles_per_task": 1,
                "generation_zero_at_task_zero": True,
            }
        )
    if len(ordered_uids) != 200 or len(set(ordered_uids)) != 200:
        raise SupervisedExperimentV3Error("SAME_PROFILE_RECOVERY_SCHEDULE_INVALID")
    return {
        "schema_version": "SameProfileCategoryShardRecoveryDryRunV3",
        "protocol_id": SAME_PROFILE_CATEGORY_RECOVERY_PROTOCOL_ID,
        "status": "PASS",
        "model_calls": 0,
        "parent_run_id": audit.parent_run_id,
        "recovery_run_id": recovery_run_id,
        "accepted_parent_categories": list(SAME_PROFILE_ACCEPTED_CATEGORIES),
        "discarded_parent_category": SAME_PROFILE_DISCARDED_CATEGORY,
        "first_category": SAME_PROFILE_DISCARDED_CATEGORY,
        "first_task_ordinal": 0,
        "first_round": 0,
        "generation_zero_targets": True,
        "parent_artifacts_imported": False,
        "parent_database_imported": False,
        "remaining_schedule": schedule,
        "remaining_train_order_sha256": sha256_bytes(canonical_json_bytes(ordered_uids)),
        "source_commit": inputs.source_commit,
        "config_sha256": inputs.config.digest,
        "split_reference_sha256": inputs.split_receipt_sha256,
        "source_manifest_sha256": inputs.source_manifest_sha256,
        "candidate_execution_path": "TaskRequest->Rollout->Gateway->CodexHarness",
        "reflector_execution_path": "Core planned job->worker->codex_cli provider",
        "planned_remaining_train_candidate_calls": 400,
        "planned_remaining_train_reflector_calls": 200,
        "planned_remaining_train_core_jobs": 600,
    }


def audit_same_profile_parent_category_shards_v3(
    *,
    repository_root: Path,
    parent_run_id: str = SAME_PROFILE_PARENT_RUN_ID,
) -> SameProfileAuditedParentShardsV3:
    """Audit the failed parent read-only and accept only its five closed shards."""

    repository = repository_root.resolve(strict=True)
    if _RUN_ID.fullmatch(parent_run_id) is None:
        raise ValueError("invalid parent run ID")
    state_root = (
        repository / "state/chembench_supervised_transfer_v3/runs" / parent_run_id
    ).resolve(strict=True)
    result_root = (
        repository / "results/chembench_supervised_transfer_v3/runs" / parent_run_id
    ).resolve(strict=True)
    before = _tree_identity((state_root, result_root))
    if _active_processes(parent_run_id):
        raise SupervisedExperimentV3Error("SAME_PROFILE_PARENT_PROCESS_ACTIVE")
    state = _read_json(state_root / "run_state.json")
    paid = _read_json(result_root / "public/planned_paid_execution_receipt_v3.json")
    if (
        state.get("run_id") != parent_run_id
        or state.get("protocol_id") != TWO_ROUND_ONE_EVOLUTION_PROTOCOL_ID
        or state.get("status") != "FAIL_CLOSED"
        or state.get("stage") != "ONLINE_TRAIN"
        or state.get("category") != SAME_PROFILE_DISCARDED_CATEGORY
        or state.get("task") != 8
        or state.get("round_or_cycle") != 0
        or state.get("failure_code") != "TASKWISE_REFLECTOR_BOUNDARY_FAILED"
        or state.get("attempts_per_train_task") != 2
        or state.get("evolution_cycles_per_train_task") != 1
        or state.get("task_sessions") != 517
        or state.get("reflector_calls") != 258
        or state.get("core_jobs") != 774
        or state.get("context_resolutions") != 774
        or state.get("security_findings") != 0
        or state.get("context_findings") != 0
        or state.get("artifact_findings") != 0
        or state.get("results_reusable") is not False
        or state.get("resume_allowed") is not False
        or paid.get("source_commit") != state.get("source_commit")
        or paid.get("config_sha256") != state.get("config_sha256")
        or paid.get("split_reference_sha256") != state.get("split_receipt_sha256")
        or paid.get("protocol_id") != TWO_ROUND_ONE_EVOLUTION_PROTOCOL_ID
        or paid.get("attempts_per_train_task") != 2
        or paid.get("evolution_cycles_per_train_task") != 1
        or state.get("candidate_codex_executable_sha256") != EXPECTED_MANAGED_CODEX_SHA256
        or state.get("reflector_codex_executable_sha256") != EXPECTED_MANAGED_CODEX_SHA256
    ):
        raise SupervisedExperimentV3Error("SAME_PROFILE_PARENT_IDENTITY_INVALID")
    public_events = _read_jsonl(result_root / "public/events.jsonl")
    private_events = _read_jsonl(state_root / "private/events.jsonl")
    accepted_shards = tuple(
        _audit_closed_category(
            state_root=state_root,
            state=state,
            paid=paid,
            public_events=public_events,
            private_events=private_events,
            parent_run_id=parent_run_id,
            category_index=category_index,
            category=category,
        )
        for category_index, category in enumerate(SAME_PROFILE_ACCEPTED_CATEGORIES)
    )
    accepted = SameProfileAcceptedClosedShardsReceiptV3(
        parent_run_id=parent_run_id,
        shards=accepted_shards,
    )
    discarded = _audit_discarded_current_category(
        state_root=state_root,
        public_events=public_events,
        private_events=private_events,
        parent_run_id=parent_run_id,
    )
    later_categories = set(CHEMBENCH4K_CATEGORIES[SAME_PROFILE_START_CATEGORY_INDEX + 1 :])
    if any(
        row.get("stage") == "ONLINE_TRAIN"
        and row.get("category") in later_categories
        and row.get("kind") in {"REFLECTOR_SUPERVISED", "PRIVATE_EVALUATED"}
        for row in (*public_events, *private_events)
    ):
        raise SupervisedExperimentV3Error("SAME_PROFILE_PARENT_LATER_CATEGORY_TOUCHED")
    after = _tree_identity((state_root, result_root))
    if after != before:
        raise SupervisedExperimentV3Error("SAME_PROFILE_PARENT_MUTATED_DURING_AUDIT")
    return SameProfileAuditedParentShardsV3(
        repository_root=repository,
        parent_run_id=parent_run_id,
        parent_state_root=state_root,
        parent_result_root=result_root,
        parent_tree_sha256=before[0],
        parent_tree_file_count=before[1],
        parent_tree_size_bytes=before[2],
        accepted=accepted,
        discarded=discarded,
    )


def _audit_closed_category(
    *,
    state_root: Path,
    state: dict[str, Any],
    paid: dict[str, Any],
    public_events: list[dict[str, Any]],
    private_events: list[dict[str, Any]],
    parent_run_id: str,
    category_index: int,
    category: str,
) -> SameProfileAcceptedCategoryShardV3:
    category_root = state_root / "private/core/formal" / category
    rows = _read_jsonl(category_root / "private_lineage_checkpoints_v2.jsonl")
    if len(rows) != 50:
        raise SupervisedExperimentV3Error("SAME_PROFILE_CLOSED_CHECKPOINT_COUNT_INVALID")
    results = _validate_checkpoint_chain(rows, category=category, expected_tasks=50)
    final = results[-1]
    if final.task_index != 49 or final.update_index != 1 or final.global_update_ordinal != 50:
        raise SupervisedExperimentV3Error("SAME_PROFILE_CLOSED_FINAL_HEAD_INVALID")
    connection = _open_immutable_database(category_root / "evolution.sqlite3")
    try:
        _require_database_counts(
            connection,
            expected_states={"succeeded": 150},
            dataset_count=50,
            typed_target_count=50,
            context_count=150,
        )
        for result in results:
            _verify_result_lifecycle_read_only(connection, category_root, result)
    finally:
        connection.close()
    evaluations = _category_events(
        private_events,
        kind="PRIVATE_EVALUATED",
        category=category,
    )
    cycles = _category_events(
        public_events,
        kind="REFLECTOR_SUPERVISED",
        category=category,
    )
    if len(evaluations) != 100 or len(cycles) != 50:
        raise SupervisedExperimentV3Error("SAME_PROFILE_CLOSED_EVENT_COUNTS_INVALID")
    for task_ordinal in range(50):
        rounds = sorted(
            row.get("round_index")
            for row in evaluations
            if row.get("task_ordinal") == task_ordinal
        )
        updates = sorted(
            row.get("cycle") for row in cycles if row.get("task_index") == task_ordinal
        )
        if rounds != [0, 1] or updates != [1]:
            raise SupervisedExperimentV3Error("SAME_PROFILE_CLOSED_TASK_INVALID")
    target_receipts = [
        {
            "target": "text_memory",
            "artifact_id": final.core_artifact_id,
            "payload_sha256": final.artifact_payload_sha256,
            "context_sha256": final.context_resolution_digest,
        },
        *[
            {
                "target": value.target_id,
                "artifact_id": value.core_artifact_id,
                "payload_sha256": value.artifact_payload_sha256,
                "context_sha256": value.context_resolution_digest,
            }
            for value in final.supervised_auxiliary_artifacts
        ],
    ]
    return SameProfileAcceptedCategoryShardV3(
        parent_run_id=parent_run_id,
        category_index=category_index,
        category=category,
        source_commit=str(state["source_commit"]),
        config_digest=str(state["config_sha256"]),
        split_digest=str(state["split_receipt_sha256"]),
        model_digest=_model_digest(paid),
        executor_digest=_executor_digest(paid),
        managed_codex_digest=str(state["reflector_codex_executable_sha256"]),
        category_tree_sha256=_tree_identity((category_root,))[0],
        final_head_set_sha256=sha256_bytes(canonical_json_bytes(target_receipts)),
    )


def _audit_discarded_current_category(
    *,
    state_root: Path,
    public_events: list[dict[str, Any]],
    private_events: list[dict[str, Any]],
    parent_run_id: str,
) -> SameProfileDiscardedPartialShardReceiptV3:
    category = SAME_PROFILE_DISCARDED_CATEGORY
    category_root = state_root / "private/core/formal" / category
    rows = _read_jsonl(category_root / "private_lineage_checkpoints_v2.jsonl")
    if len(rows) != 8:
        raise SupervisedExperimentV3Error("SAME_PROFILE_DISCARDED_CHECKPOINT_COUNT_INVALID")
    results = _validate_checkpoint_chain(rows, category=category, expected_tasks=8)
    connection = _open_immutable_database(category_root / "evolution.sqlite3")
    try:
        _require_database_counts(
            connection,
            expected_states={"failed": 1, "succeeded": 24},
            dataset_count=9,
            typed_target_count=8,
            context_count=24,
        )
        for result in results:
            _verify_result_lifecycle_read_only(connection, category_root, result)
    finally:
        connection.close()
    evaluations = _category_events(
        private_events,
        kind="PRIVATE_EVALUATED",
        category=category,
    )
    cycles = _category_events(
        public_events,
        kind="REFLECTOR_SUPERVISED",
        category=category,
    )
    if len(evaluations) != 17 or len(cycles) != 8:
        raise SupervisedExperimentV3Error("SAME_PROFILE_DISCARDED_EVENT_COUNTS_INVALID")
    for task_ordinal in range(8):
        if sorted(
            row.get("round_index")
            for row in evaluations
            if row.get("task_ordinal") == task_ordinal
        ) != [0, 1] or [
            row.get("cycle") for row in cycles if row.get("task_index") == task_ordinal
        ] != [1]:
            raise SupervisedExperimentV3Error("SAME_PROFILE_DISCARDED_PREFIX_INVALID")
    task_eight_rounds = [
        row.get("round_index") for row in evaluations if row.get("task_ordinal") == 8
    ]
    if task_eight_rounds != [0] or any(row.get("task_index") == 8 for row in cycles):
        raise SupervisedExperimentV3Error("SAME_PROFILE_DISCARDED_BOUNDARY_INVALID")
    failure_receipt_sha256, failure_events_sha256 = _audit_failed_reflector_receipt(category_root)
    return SameProfileDiscardedPartialShardReceiptV3(
        parent_run_id=parent_run_id,
        category_tree_sha256=_tree_identity((category_root,))[0],
        failed_reflector_receipt_sha256=failure_receipt_sha256,
        failed_reflector_event_stream_sha256=failure_events_sha256,
    )


def _audit_failed_reflector_receipt(category_root: Path) -> tuple[str, str]:
    receipts = sorted((category_root / "private_reflector_events").glob("*/receipt.json"))
    if len(receipts) != 9:
        raise SupervisedExperimentV3Error("SAME_PROFILE_REFLECTOR_RECEIPT_COUNT_INVALID")
    loaded = [(path, _read_json(path)) for path in receipts]
    failed = [(path, payload) for path, payload in loaded if payload.get("status") != "COMPLETED"]
    if len(failed) != 1 or sum(payload.get("status") == "COMPLETED" for _, payload in loaded) != 8:
        raise SupervisedExperimentV3Error("SAME_PROFILE_REFLECTOR_FAILURE_INVALID")
    receipt_path, receipt = failed[0]
    if (
        receipt.get("status") != "INVALID_INVOCATION"
        or receipt.get("codex_returncode") != 0
        or receipt.get("cleanup_complete") is not True
        or receipt.get("event_counts") != {}
        or receipt.get("real_codex_sha256") != EXPECTED_MANAGED_CODEX_SHA256
        or receipt.get("retry_allowed") is not False
        or receipt.get("resume_allowed") is not False
        or receipt.get("replacement_completion_allowed") is not False
        or receipt.get("output_normalization_applied") is not False
    ):
        raise SupervisedExperimentV3Error("SAME_PROFILE_REFLECTOR_FAILURE_INVALID")
    events_path = receipt_path.parent / "events.jsonl"
    encoded = _read_regular(events_path)
    events = _read_jsonl(events_path)
    if receipt.get("event_stream_sha256") != sha256_bytes(encoded) or [
        event.get("type") for event in events
    ] != [
        "thread.started",
        "turn.started",
        "error",
        "item.completed",
        "turn.completed",
    ]:
        raise SupervisedExperimentV3Error("SAME_PROFILE_REFLECTOR_EVENT_STREAM_INVALID")
    error = events[2]
    item = events[3].get("item")
    usage = events[4].get("usage")
    if (
        set(error) != {"type", "message"}
        or type(error.get("message")) is not str
        or re.fullmatch(
            r"Reconnecting\.\.\. [1-5]/5 \(request timed out\)",
            str(error["message"]),
        )
        is None
        or not isinstance(item, dict)
        or set(item) != {"id", "text", "type"}
        or item.get("type") != "agent_message"
        or type(item.get("text")) is not str
        or not str(item["text"]).strip()
        or not isinstance(usage, dict)
        or not usage
        or not all(type(value) is int and value >= 0 for value in usage.values())
    ):
        raise SupervisedExperimentV3Error("SAME_PROFILE_REFLECTOR_RECOVERED_TIMEOUT_INVALID")
    return sha256_bytes(_read_regular(receipt_path)), sha256_bytes(encoded)


def _validate_checkpoint_chain(
    rows: list[dict[str, Any]],
    *,
    category: str,
    expected_tasks: int,
) -> tuple[TaskwiseCoreUpdateResultV1, ...]:
    results: list[TaskwiseCoreUpdateResultV1] = []
    predecessor = None
    for task_index, row in enumerate(rows):
        try:
            result = TaskwiseCoreUpdateResultV1.model_validate(row["result"])
            lineage = result.required_lineage_receipt()
        except (KeyError, TypeError, ValueError) as exc:
            raise SupervisedExperimentV3Error("SAME_PROFILE_CHECKPOINT_INVALID") from exc
        if (
            result.supervised_category != category
            or result.updates_per_task != 1
            or result.task_index != task_index
            or result.update_index != 1
            or result.global_update_ordinal != task_index + 1
            or result.predecessor != predecessor
            or row.get("required_lineage") != lineage.model_dump(mode="json")
            or row.get("required_lineage_sha256") != lineage.digest
            or not result.validation_receipt.passed
            or result.validation_receipt.finding_codes
            or not result.memory_inspection.passed
            or tuple(value.target_id for value in result.supervised_auxiliary_artifacts)
            != ("skill_bundle", "agent_system")
            or any(not value.inspection.passed for value in result.supervised_auxiliary_artifacts)
        ):
            raise SupervisedExperimentV3Error("SAME_PROFILE_CHECKPOINT_CHAIN_INVALID")
        predecessor = result.predecessor_identity()
        results.append(result)
    if len(results) != expected_tasks:
        raise SupervisedExperimentV3Error("SAME_PROFILE_CHECKPOINT_TASK_COUNT_INVALID")
    return tuple(results)


def _open_immutable_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        connection.close()
        raise SupervisedExperimentV3Error("SAME_PROFILE_SQLITE_INTEGRITY_INVALID")
    return connection


def _require_database_counts(
    connection: sqlite3.Connection,
    *,
    expected_states: dict[str, int],
    dataset_count: int,
    typed_target_count: int,
    context_count: int,
) -> None:
    states = {
        str(row["state"]): int(row["count"])
        for row in connection.execute("SELECT state, COUNT(*) AS count FROM jobs GROUP BY state")
    }
    leases = int(
        connection.execute(
            "SELECT COUNT(*) FROM jobs WHERE claimed_by IS NOT NULL "
            "OR lease_id IS NOT NULL OR lease_expires_at IS NOT NULL"
        ).fetchone()[0]
    )
    staged = int(
        connection.execute(
            "SELECT COUNT(*) FROM artifacts WHERE staging_job_id IS NOT NULL"
        ).fetchone()[0]
    )
    contexts = int(
        connection.execute("SELECT COUNT(*) FROM context_materializations").fetchone()[0]
    )
    type_counts = {
        str(row["type"]): (int(row["count"]), int(row["promoted"]))
        for row in connection.execute(
            "SELECT type, COUNT(*) AS count, SUM(promoted) AS promoted "
            "FROM artifacts GROUP BY type"
        )
    }
    expected_types = {
        "dataset": (dataset_count, dataset_count),
        "text_memory": (typed_target_count, typed_target_count),
        "skill_bundle": (typed_target_count, typed_target_count),
        "agent_system": (typed_target_count, typed_target_count),
    }
    if (
        states != expected_states
        or leases != 0
        or staged != 0
        or contexts != context_count
        or type_counts != expected_types
    ):
        raise SupervisedExperimentV3Error("SAME_PROFILE_CORE_COUNTS_INVALID")


def _category_events(
    rows: list[dict[str, Any]],
    *,
    kind: str,
    category: str,
) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("kind") == kind
        and row.get("stage") == "ONLINE_TRAIN"
        and row.get("category") == category
    ]


class SameProfileCategoryShardRecoveryV3(SupervisedTransferExperimentV3):
    """Run fresh category shards 5..8 without attaching any parent Core state."""

    def __init__(
        self,
        *,
        inputs: v2.ExperimentInputsV2,
        run_id: str,
        smoke_run_id: str,
        parent_audit: SameProfileAuditedParentShardsV3,
        executor_factory: Any | None = None,
        bridge_factory: Any | None = None,
    ) -> None:
        _require_same_profile(inputs)
        if type(parent_audit) is not SameProfileAuditedParentShardsV3:
            raise TypeError("parent_audit must be exact SameProfileAuditedParentShardsV3")
        if parent_audit.repository_root != inputs.repository_root:
            raise SupervisedExperimentV3Error("SAME_PROFILE_RECOVERY_REPOSITORY_MISMATCH")
        self.parent_audit = parent_audit
        self.start_category_index = SAME_PROFILE_START_CATEGORY_INDEX
        self.remaining_categories = SAME_PROFILE_REMAINING_CATEGORIES
        super().__init__(
            inputs=inputs,
            run_id=run_id,
            run_mode="formal_online",
            smoke_run_id=smoke_run_id,
            executor_factory=executor_factory,
            bridge_factory=bridge_factory,
        )
        self._state.update(
            {
                "schema_version": "ChemBenchSameProfileCategoryRecoveryRunStateV3",
                "protocol_id": SAME_PROFILE_CATEGORY_RECOVERY_PROTOCOL_ID,
                "parent_run_id": parent_audit.parent_run_id,
                "start_category_index": SAME_PROFILE_START_CATEGORY_INDEX,
                "first_category": SAME_PROFILE_DISCARDED_CATEGORY,
                "first_task": 0,
                "generation_zero_targets": True,
                "parent_artifacts_imported": False,
                "parent_database_imported": False,
                "accepted_parent_shards_sha256": parent_audit.accepted.digest,
                "discarded_parent_shard_sha256": parent_audit.discarded.digest,
            }
        )
        self._write_state()
        self._write_recovery_protocol_receipts()

    def run_category_shard_recovery(self) -> dict[str, object]:
        """Run all remaining Train categories from fresh generation-zero heads."""

        try:
            self._verify_parent_tree_unchanged()
            self._verify_smoke_authority()
            with ExitStack() as stack:
                executor = v2._enter_if_context(stack, self._executor_factory("online"))
                self._open_remaining_bridges(stack)
                self._run_remaining_train(executor)
                self._require_counts(
                    task_sessions=400,
                    reflector_calls=200,
                    core_jobs=600,
                )
                closed = self._write_remaining_closed_shard_receipts()
                composition = self._write_final_shard_composition(closed)
            self._verify_parent_tree_unchanged()
            self._state.update(
                {
                    "stage": "REMAINING_SHARDS_COMPLETED",
                    "status": "REMAINING_SHARDS_COMPLETED",
                    "completed_at_utc": v2.utc_now(),
                    "final_shard_composition_sha256": composition.digest,
                    "final_test_status": "PENDING_COMPOSED_SHARD_EXECUTION",
                }
            )
            self._write_state()
            return {
                "status": "REMAINING_SHARDS_COMPLETED",
                "run_id": self.run_id,
                "parent_run_id": self.parent_audit.parent_run_id,
                "closed_remaining_shards": len(closed),
                "final_shard_composition_sha256": composition.digest,
                "parent_artifacts_imported": False,
                "parent_database_imported": False,
            }
        except BaseException as exc:
            self._fail_closed(exc)
            raise

    def _open_remaining_bridges(self, stack: ExitStack) -> None:
        self._set_stage("ONLINE_CORE_INITIALIZATION")
        for category in self.remaining_categories:
            path = self.state_root / "private/core/formal" / category
            bridge = self._bridge_factory(path, category)
            self._bridges[category] = stack.enter_context(bridge)
        if set(self._bridges) != set(self.remaining_categories):
            raise SupervisedExperimentV3Error("SAME_PROFILE_RECOVERY_BRIDGE_SET_INVALID")

    def _run_remaining_train(self, executor: v2.TaskExecutorV2) -> None:
        self._set_stage("ONLINE_TRAIN")
        by_category = v2._by_category(self.inputs.train)
        for category in self.remaining_categories:
            bridge = self._bridges[category]
            for task_ordinal, task in enumerate(by_category[category]):
                if task_ordinal == 0 and self._heads[category] is not None:
                    raise SupervisedExperimentV3Error(
                        "SAME_PROFILE_RECOVERY_GENERATION_ZERO_INVALID"
                    )
                self._run_one_online_task_v3(
                    executor,
                    task=task,
                    category_ordinal=task_ordinal,
                    bridge=bridge,
                    stage="ONLINE_TRAIN",
                    persist_head=True,
                )
            self._verify_parent_tree_unchanged()

    def _execute_single_session(self, *args: Any, **kwargs: Any) -> v2.SessionOutcomeV2:
        task = kwargs.get("task")
        if (
            kwargs.get("stage") == "ONLINE_TRAIN"
            and getattr(task, "category", None) == SAME_PROFILE_DISCARDED_CATEGORY
            and kwargs.get("task_ordinal") == 0
            and kwargs.get("round_index") == 0
            and kwargs.get("context") is not None
        ):
            raise SupervisedExperimentV3Error("SAME_PROFILE_RECOVERY_GENERATION_ZERO_INVALID")
        return super()._execute_single_session(*args, **kwargs)

    def _verify_parent_tree_unchanged(self) -> None:
        current = _tree_identity(
            (
                self.parent_audit.parent_state_root,
                self.parent_audit.parent_result_root,
            )
        )
        expected = (
            self.parent_audit.parent_tree_sha256,
            self.parent_audit.parent_tree_file_count,
            self.parent_audit.parent_tree_size_bytes,
        )
        if current != expected:
            raise SupervisedExperimentV3Error("SAME_PROFILE_PARENT_TREE_DRIFT")

    def _write_recovery_protocol_receipts(self) -> None:
        amendment = SameProfileCategoryShardRecoveryAmendmentV3(
            parent_run_id=self.parent_audit.parent_run_id,
            recovery_run_id=self.run_id,
        )
        plan = SameProfileRemainingShardExecutionPlanV3(
            parent_run_id=self.parent_audit.parent_run_id,
            recovery_run_id=self.run_id,
            categories=self.remaining_categories,
        )
        public = self.result_root / "public"
        for name, model in (
            ("same_profile_category_recovery_amendment_v3.json", amendment),
            ("same_profile_accepted_closed_shards_receipt_v3.json", self.parent_audit.accepted),
            ("same_profile_discarded_partial_shard_receipt_v3.json", self.parent_audit.discarded),
            ("same_profile_remaining_shard_execution_plan_v3.json", plan),
        ):
            write_public_file(
                public / name,
                canonical_pretty_json_bytes(model.model_dump(mode="json")),
            )
        self._state.update(
            {
                "category_shard_recovery_amendment_sha256": amendment.digest,
                "remaining_shard_execution_plan_sha256": plan.digest,
            }
        )
        self._write_state()

    def _write_paid_plan(self) -> None:
        payload = {
            "schema_version": "SameProfileCategoryShardRecoveryPaidExecutionPlanV3",
            "protocol_id": SAME_PROFILE_CATEGORY_RECOVERY_PROTOCOL_ID,
            "original_protocol_id": TWO_ROUND_ONE_EVOLUTION_PROTOCOL_ID,
            "run_id": self.run_id,
            "parent_run_id": self.parent_audit.parent_run_id,
            "smoke_run_id": self.smoke_run_id,
            "source_commit": self.inputs.source_commit,
            "config_sha256": self.inputs.config.digest,
            "split_reference_sha256": self.inputs.split_receipt_sha256,
            "source_manifest_sha256": self.inputs.source_manifest_sha256,
            "model": self.inputs.config.model,
            "reasoning_effort": self.inputs.config.reasoning_effort,
            "task_timeout_seconds": self.inputs.config.task_timeout_seconds,
            "reflector_timeout_seconds": self.inputs.config.reflector_timeout_seconds,
            "start_category_index": SAME_PROFILE_START_CATEGORY_INDEX,
            "categories": list(self.remaining_categories),
            "first_task_ordinal": 0,
            "first_round": 0,
            "generation_zero_targets": True,
            "planned_calls": {
                "remaining_train_candidate_calls": 400,
                "remaining_train_reflector_calls": 200,
                "remaining_train_core_jobs": 600,
                "candidate_readiness_calls_minimum": 400,
                "candidate_readiness_calls_maximum": 800,
                "answer_and_reflector_model_calls": 600,
                "total_model_calls_minimum": 1000,
                "total_model_calls_maximum": 1400,
                "eventual_final_test_candidate_calls": TEST_COUNT,
            },
            "parent_usage": {
                "closed_shard_candidate_completions_included": 500,
                "closed_shard_reflector_calls_included": 250,
                "discarded_candidate_completions": 17,
                "discarded_successful_reflector_calls": 8,
                "discarded_failed_reflector_invocations": 1,
            },
            "candidate_execution_path": "TaskRequest->Rollout->Gateway->CodexHarness",
            "reflector_execution_path": "Core planned job->worker->codex_cli provider",
            "candidate_codex_identity_sha256": self.inputs.candidate_codex.digest,
            "candidate_codex_executable_sha256": (self.inputs.candidate_codex.executable_sha256),
            "reflector_codex_identity_sha256": self.inputs.managed_codex.digest,
            "reflector_codex_executable_sha256": (self.inputs.managed_codex.executable_sha256),
            "runtime_services_identity_sha256": self.inputs.runtime_services.digest,
            "parent_artifacts_imported": False,
            "parent_database_imported": False,
            "written_before_first_model_call": True,
            "issued_at_utc": v2.utc_now(),
        }
        write_public_file(
            self.result_root / "public/same_profile_category_shard_paid_execution_plan_v3.json",
            canonical_pretty_json_bytes(payload),
        )

    def _write_remaining_closed_shard_receipts(
        self,
    ) -> tuple[SameProfileShardCompositionEntryV3, ...]:
        identity = {
            "source_commit": self.inputs.source_commit,
            "config_digest": self.inputs.config.digest,
            "split_digest": self.inputs.split_receipt_sha256,
            "model_digest": _model_digest_from_inputs(self),
            "executor_digest": _executor_digest_from_inputs(self),
            "managed_codex_digest": self.inputs.managed_codex.executable_sha256,
        }
        public_events = _read_jsonl(self.public_events)
        private_events = _read_jsonl(self.private_events)
        entries: list[SameProfileShardCompositionEntryV3] = []
        for category_index, category in enumerate(CHEMBENCH4K_CATEGORIES):
            if category_index < SAME_PROFILE_START_CATEGORY_INDEX:
                continue
            head = self._heads[category]
            if (
                head is None
                or head.task_index != 49
                or head.update_index != 1
                or head.global_update_ordinal != 50
            ):
                raise SupervisedExperimentV3Error("SAME_PROFILE_RECOVERY_FINAL_HEAD_INVALID")
            self._bridges[category].verify_update_result(head)
            public_rows = _category_events(
                public_events,
                kind="REFLECTOR_SUPERVISED",
                category=category,
            )
            private_rows = _category_events(
                private_events,
                kind="PRIVATE_EVALUATED",
                category=category,
            )
            if len(public_rows) != 50 or len(private_rows) != 100:
                raise SupervisedExperimentV3Error("SAME_PROFILE_RECOVERY_EVENT_COUNTS_INVALID")
            closed = {
                "schema_version": "SameProfileClosedRecoveryCategoryShardReceiptV3",
                "protocol_id": SAME_PROFILE_CATEGORY_RECOVERY_PROTOCOL_ID,
                "run_id": self.run_id,
                "category_index": category_index,
                "category": category,
                "task_start": 0,
                "task_end": 49,
                "task_count": 50,
                "completion_count": 100,
                "reflector_count": 50,
                "core_job_count": 150,
                "artifact_count": 150,
                "final_global_update_ordinal": 50,
                "final_head_artifact_id": head.core_artifact_id,
                "final_head_payload_sha256": head.artifact_payload_sha256,
                "final_head_context_sha256": head.context_resolution_digest,
                **identity,
                "closed": True,
                "included_in_statistics": True,
                "issued_at_utc": v2.utc_now(),
            }
            path = self.result_root / "public" / f"same_profile_closed_shard_{category}_v3.json"
            write_public_file(path, canonical_pretty_json_bytes(closed))
            entries.append(
                SameProfileShardCompositionEntryV3(
                    category=category,
                    category_index=category_index,
                    source_run_id=self.run_id,
                    closed_receipt_sha256=sha256_bytes(path.read_bytes()),
                    **identity,
                )
            )
        return tuple(entries)

    def _write_final_shard_composition(
        self,
        remaining: tuple[SameProfileShardCompositionEntryV3, ...],
    ) -> SameProfileFinalShardCompositionReceiptV3:
        accepted_entries = tuple(
            SameProfileShardCompositionEntryV3(
                category=shard.category,
                category_index=shard.category_index,
                source_run_id=shard.parent_run_id,
                source_commit=shard.source_commit,
                config_digest=shard.config_digest,
                split_digest=shard.split_digest,
                model_digest=shard.model_digest,
                executor_digest=shard.executor_digest,
                managed_codex_digest=shard.managed_codex_digest,
                closed_receipt_sha256=shard.digest,
            )
            for shard in self.parent_audit.accepted.shards
        )
        receipt = SameProfileFinalShardCompositionReceiptV3(
            categories=(*accepted_entries, *remaining)
        )
        write_public_file(
            self.result_root / "public/same_profile_final_shard_composition_receipt_v3.json",
            canonical_pretty_json_bytes(receipt.model_dump(mode="json")),
        )
        return receipt


def build_same_profile_source_compatibility_receipt_v3(
    *,
    audit: SameProfileAuditedParentShardsV3,
    recovery_run_id: str,
) -> dict[str, object]:
    """Prove that the recovery commit preserves category-internal semantics."""

    repository = audit.repository_root
    parent_commit = audit.accepted.shards[0].source_commit
    recovery_commit = _git(repository, "rev-parse", "HEAD")
    protected = (
        (
            "benchmarks/chembench/configs/supervised_transfer_v3/"
            "chembench_supervised_transfer_v3_two_round_one_evolution.yaml"
        ),
        "benchmarks/chembench/src/openevo_chembench/chembench4k_dataset.py",
        "benchmarks/chembench/src/openevo_chembench/chembench4k_models.py",
        "benchmarks/chembench/src/openevo_chembench/chembench4k_prompt.py",
        "benchmarks/chembench/src/openevo_chembench/chembench4k_evaluation.py",
        "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2/core.py",
        "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2/executor.py",
        "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2/packet.py",
        (
            "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2/"
            "reflector_boundary.py"
        ),
        "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2/memory.py",
        "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2/artifacts.py",
        ("benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2/managed_codex.py"),
        "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/config.py",
        "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/experiment.py",
        "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/reporting.py",
        ("benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/split_reference.py"),
        ("benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/test_ledger.py"),
    )
    unchanged: dict[str, str] = {}
    for relative in protected:
        current = (repository / relative).read_bytes()
        parent = subprocess.run(
            ("git", "show", f"{parent_commit}:{relative}"),
            cwd=repository,
            check=True,
            capture_output=True,
        ).stdout
        if current != parent:
            raise SupervisedExperimentV3Error("RECOVERY_SOURCE_NOT_SEMANTICALLY_COMPATIBLE")
        unchanged[relative] = sha256_bytes(current)
    if _git(repository, "diff", "--", "src/openevo"):
        raise SupervisedExperimentV3Error("SRC_OPENEVO_NOT_PRISTINE")
    diff = subprocess.run(
        (
            "git",
            "diff",
            "--binary",
            parent_commit,
            recovery_commit,
            "--",
            "benchmarks/chembench",
        ),
        cwd=repository,
        check=True,
        capture_output=True,
    ).stdout
    changed = _git(
        repository,
        "diff",
        "--name-only",
        parent_commit,
        recovery_commit,
        "--",
        "benchmarks/chembench",
    ).splitlines()
    allowed_changed = {
        "benchmarks/chembench/README.md",
        (
            "benchmarks/chembench/configs/supervised_transfer_v3/"
            "same_profile_category_shard_recovery.md"
        ),
        "benchmarks/chembench/manifests/supervised_transfer_v3/source_manifest_v3.json",
        "benchmarks/chembench/scripts/supervised_transfer_v3/main.py",
        "benchmarks/chembench/src/openevo_chembench/local_codex_executor.py",
        (
            "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/"
            "same_profile_category_recovery.py"
        ),
        ("benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/source_identity.py"),
        (
            "benchmarks/chembench/tests/supervised_transfer_v3/"
            "test_same_profile_category_recovery_v3.py"
        ),
        "benchmarks/chembench/tests/test_local_codex_security_v2.py",
    }
    if set(changed) - allowed_changed:
        raise SupervisedExperimentV3Error("RECOVERY_SOURCE_NOT_SEMANTICALLY_COMPATIBLE")
    return {
        "schema_version": "SameProfileCategoryShardRecoveryCompatibilityReceiptV3",
        "parent_run_id": audit.parent_run_id,
        "recovery_run_id": recovery_run_id,
        "parent_source_commit": parent_commit,
        "recovery_source_commit": recovery_commit,
        "git_diff_sha256": sha256_bytes(diff),
        "files_changed": changed,
        "unchanged_semantic_inputs": unchanged,
        "allowed_changes": [
            "strict_completed_timeout_notice_compatibility",
            "source_manifest_execution_closure",
            "category_start_selection",
            "category_shard_orchestration",
            "shard_receipts_and_composition",
        ],
        "timeout_notice_contract": {
            "accepted_message_shape": ("Reconnecting... <attempt 1..5>/5 (request timed out)"),
            "complete_agent_message_required": True,
            "turn_completed_required": True,
            "incomplete_or_arbitrary_errors_fail_closed": True,
        },
        "candidate_execution_path_unchanged": True,
        "reflector_execution_path_unchanged": True,
        "category_internal_protocol_unchanged": True,
        "model_prompt_evaluator_answer_parser_artifact_semantics_unchanged": True,
        "semantically_compatible": True,
    }


def _model_digest_from_inputs(
    experiment: SameProfileCategoryShardRecoveryV3,
) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "model": experiment.inputs.config.model,
                "reasoning_effort": experiment.inputs.config.reasoning_effort,
                "managed_runtime_image": MANAGED_RUNTIME_RELEASES[
                    "managed_science"
                ].loaded_image_id,
            }
        )
    )


def _executor_digest_from_inputs(
    experiment: SameProfileCategoryShardRecoveryV3,
) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "candidate_execution_path": "TaskRequest->Rollout->Gateway->CodexHarness",
                "reflector_execution_path": ("Core planned job->worker->codex_cli provider"),
                "candidate_codex_identity_sha256": (experiment.inputs.candidate_codex.digest),
                "reflector_codex_identity_sha256": (experiment.inputs.managed_codex.digest),
                "task_timeout_seconds": (experiment.inputs.config.task_timeout_seconds),
                "reflector_timeout_seconds": (experiment.inputs.config.reflector_timeout_seconds),
            }
        )
    )


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


__all__ = [
    "SAME_PROFILE_ACCEPTED_CATEGORIES",
    "SAME_PROFILE_CATEGORY_RECOVERY_PROTOCOL_ID",
    "SAME_PROFILE_DISCARDED_CATEGORY",
    "SAME_PROFILE_PARENT_RUN_ID",
    "SAME_PROFILE_REMAINING_CATEGORIES",
    "SAME_PROFILE_START_CATEGORY_INDEX",
    "SameProfileAcceptedCategoryShardV3",
    "SameProfileAcceptedClosedShardsReceiptV3",
    "SameProfileAuditedParentShardsV3",
    "SameProfileCategoryShardRecoveryAmendmentV3",
    "SameProfileCategoryShardRecoveryV3",
    "SameProfileDiscardedPartialShardReceiptV3",
    "SameProfileFinalShardCompositionReceiptV3",
    "SameProfileRemainingShardExecutionPlanV3",
    "SameProfileShardCompositionEntryV3",
    "audit_same_profile_parent_category_shards_v3",
    "build_same_profile_category_recovery_dry_run_v3",
    "build_same_profile_source_compatibility_receipt_v3",
    "same_profile_remaining_categories_v3",
]
