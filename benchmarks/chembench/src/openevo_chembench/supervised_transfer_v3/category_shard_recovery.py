"""Category-atomic recovery for the supervised transfer v3 experiment.

The recovery runner never opens or imports a parent Core store.  A separate
read-only audit accepts only completely closed parent category shards.  Every
remaining category starts in a new namespace at task zero with a fresh Core
bridge and generation-zero targets.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import subprocess
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from openevo.evolution.framework import canonical_digest
from openevo.runtime.managed import MANAGED_RUNTIME_RELEASES
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
from openevo_chembench.core_evolution_v2 import (
    _read_bounded_regular_file,
    _safe_core_file_path,
)
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_public_file,
)
from openevo_chembench.supervised_transfer_v2 import experiment as v2
from openevo_chembench.supervised_transfer_v2.core import TaskwiseCoreUpdateResultV1
from openevo_chembench.supervised_transfer_v3.config import (
    EVOLUTION_CYCLES,
    PROTOCOL_ID,
    TEST_COUNT,
)
from openevo_chembench.supervised_transfer_v3.experiment import (
    SupervisedExperimentV3Error,
    SupervisedTransferExperimentV3,
)

CATEGORY_SHARD_RECOVERY_PROTOCOL_ID = "supervised_transfer_v3_category_shard_recovery"
PARENT_RUN_ID = "stv3-online-formal-20260730T031520Z"
ACCEPTED_CATEGORY = "Name_Conversion"
DISCARDED_CATEGORY = "Property_Prediction"
START_CATEGORY_INDEX = 1
REMAINING_CATEGORIES = CHEMBENCH4K_CATEGORIES[START_CATEGORY_INDEX:]
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_MAX_ARTIFACT_PAYLOAD_BYTES = 64 * 1024


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.model_dump(mode="json")))


class CategoryShardRecoveryAmendmentV3(_ClosedModel):
    schema_version: Literal["CategoryShardRecoveryAmendmentV3"] = (
        "CategoryShardRecoveryAmendmentV3"
    )
    original_protocol_id: Literal["chembench_supervised_transfer_v3_three_answer_online_only"] = (
        PROTOCOL_ID
    )
    recovery_protocol_id: Literal["supervised_transfer_v3_category_shard_recovery"] = (
        CATEGORY_SHARD_RECOVERY_PROTOCOL_ID
    )
    amendment_reason: Literal["HOST_OR_WSL_CRASH"] = "HOST_OR_WSL_CRASH"
    authorization: Literal["explicit_user_authorization"] = "explicit_user_authorization"
    parent_run_id: str
    recovery_run_id: str
    accepted_parent_category: Literal["Name_Conversion"] = ACCEPTED_CATEGORY
    discarded_parent_category: Literal["Property_Prediction"] = DISCARDED_CATEGORY
    start_category_index: Literal[1] = START_CATEGORY_INDEX
    start_category: Literal["Property_Prediction"] = DISCARDED_CATEGORY
    start_task_ordinal: Literal[0] = 0
    start_round: Literal[0] = 0
    generation_zero_targets: Literal[True] = True
    parent_artifacts_imported: Literal[False] = False
    parent_database_imported: Literal[False] = False
    category_shard_recovery: Literal[True] = True
    one_parent_closed_shard_reused: Literal[True] = True
    partial_parent_shard_fully_discarded: Literal[True] = True
    uninterrupted_single_process_run: Literal[False] = False


class AcceptedClosedShardReceiptV3(_ClosedModel):
    schema_version: Literal["AcceptedClosedShardReceiptV3"] = "AcceptedClosedShardReceiptV3"
    parent_run_id: str
    category_index: Literal[0] = 0
    category: Literal["Name_Conversion"] = ACCEPTED_CATEGORY
    task_start: Literal[0] = 0
    task_end: Literal[49] = 49
    status: Literal["fully_closed"] = "fully_closed"
    source_commit: str
    config_digest: str
    split_digest: str
    model_digest: str
    executor_digest: str
    managed_codex_digest: str
    candidate_completion_count: Literal[150] = 150
    reflector_call_count: Literal[100] = 100
    core_job_count: Literal[300] = 300
    text_memory_artifact_count: Literal[100] = 100
    skill_bundle_artifact_count: Literal[100] = 100
    agent_system_artifact_count: Literal[100] = 100
    context_resolution_count: Literal[300] = 300
    active_lease_count: Literal[0] = 0
    staged_job_count: Literal[0] = 0
    final_category_state_closed: Literal[True] = True
    category_state_independent: Literal[True] = True
    included_in_final_statistics: Literal[True] = True
    category_tree_sha256: str
    final_head_set_sha256: str

    @field_validator(
        "source_commit",
    )
    @classmethod
    def _commit(cls, value: str) -> str:
        if _GIT_COMMIT.fullmatch(value) is None:
            raise ValueError("source commit must be a full Git commit")
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
        if _SHA256.fullmatch(value) is None:
            raise ValueError("closed shard digest must be SHA-256")
        return value


class DiscardedPartialShardReceiptV3(_ClosedModel):
    schema_version: Literal["DiscardedPartialShardReceiptV3"] = "DiscardedPartialShardReceiptV3"
    parent_run_id: str
    category_index: Literal[1] = 1
    category: Literal["Property_Prediction"] = DISCARDED_CATEGORY
    discard_entire_category: Literal[True] = True
    included_in_final_statistics: Literal[False] = False
    used_as_recovery_input: Literal[False] = False
    artifacts_imported: Literal[False] = False
    database_imported: Literal[False] = False
    completed_candidate_calls_discarded: Literal[76] = 76
    completed_reflector_calls_discarded: Literal[51] = 51
    completed_core_jobs_discarded: Literal[153] = 153
    text_memory_artifacts_discarded: Literal[51] = 51
    skill_bundle_artifacts_discarded: Literal[51] = 51
    agent_system_artifacts_discarded: Literal[51] = 51
    context_resolutions_discarded: Literal[153] = 153
    actual_paid_usage_retained: Literal[True] = True
    parent_evidence_preserved: Literal[True] = True
    category_tree_sha256: str

    @field_validator("category_tree_sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("discarded shard tree digest must be SHA-256")
        return value


class RemainingShardExecutionPlanV3(_ClosedModel):
    schema_version: Literal["RemainingShardExecutionPlanV3"] = "RemainingShardExecutionPlanV3"
    parent_run_id: str
    recovery_run_id: str
    start_category_index: Literal[1] = 1
    categories: tuple[str, ...]
    first_category: Literal["Property_Prediction"] = DISCARDED_CATEGORY
    first_task_ordinal: Literal[0] = 0
    first_round: Literal[0] = 0
    generation_zero_targets: Literal[True] = True
    train_task_count: Literal[400] = 400
    train_candidate_calls: Literal[1200] = 1200
    reflector_calls: Literal[800] = 800
    core_jobs: Literal[2400] = 2400
    typed_artifacts: Literal[2400] = 2400
    context_resolutions: Literal[2400] = 2400
    eventual_final_test_candidate_calls: Literal[450] = TEST_COUNT
    planned_recovery_candidate_calls: Literal[1650] = 1650
    parent_artifacts_imported: Literal[False] = False
    parent_database_imported: Literal[False] = False

    @model_validator(mode="after")
    def _order(self) -> RemainingShardExecutionPlanV3:
        if self.categories != REMAINING_CATEGORIES:
            raise ValueError("remaining category order differs from frozen v3 order")
        return self


class ShardCompositionEntryV3(_ClosedModel):
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
    completion_count: Literal[150] = 150
    reflector_count: Literal[100] = 100
    artifact_count: Literal[300] = 300
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
        if _SHA256.fullmatch(value) is None:
            raise ValueError("composition digest must be SHA-256")
        return value


class FinalShardCompositionReceiptV3(_ClosedModel):
    schema_version: Literal["FinalShardCompositionReceiptV3"] = "FinalShardCompositionReceiptV3"
    protocol_id: str = CATEGORY_SHARD_RECOVERY_PROTOCOL_ID
    categories: tuple[ShardCompositionEntryV3, ...]
    category_shard_recovery: Literal[True] = True
    one_parent_closed_shard_reused: Literal[True] = True
    partial_parent_shard_fully_discarded: Literal[True] = True
    uninterrupted_single_process_run: Literal[False] = False
    parent_name_conversion_calls_included: Literal[250] = 250
    parent_property_prediction_calls_discarded: Literal[127] = 127
    new_recovery_train_calls_included: Literal[2000] = 2000
    planned_composed_final_test_calls: Literal[450] = 450
    new_recovery_calls_in_final_result_after_test: Literal[2450] = 2450
    total_actual_calls_at_train_composition_minimum: Literal[2377] = 2377
    projected_total_actual_calls_after_final_test_minimum: Literal[2827] = 2827
    total_calls_in_train_shard_composition: Literal[2250] = 2250
    total_calls_in_final_result_after_test: Literal[2700] = 2700

    @model_validator(mode="after")
    def _identity(self) -> FinalShardCompositionReceiptV3:
        if (
            len(self.categories) != len(CHEMBENCH4K_CATEGORIES)
            or tuple(item.category for item in self.categories) != CHEMBENCH4K_CATEGORIES
            or tuple(item.category_index for item in self.categories) != tuple(range(9))
        ):
            raise ValueError("final composition must contain each frozen category exactly once")
        identity = {
            (
                item.config_digest,
                item.split_digest,
                item.model_digest,
                item.executor_digest,
                item.managed_codex_digest,
            )
            for item in self.categories
        }
        if len(identity) != 1:
            raise ValueError("category shards are not execution-compatible")
        return self


@dataclass(frozen=True, slots=True)
class AuditedParentShardsV3:
    repository_root: Path
    parent_run_id: str
    parent_state_root: Path
    parent_result_root: Path
    parent_tree_sha256: str
    parent_tree_file_count: int
    parent_tree_size_bytes: int
    accepted: AcceptedClosedShardReceiptV3
    discarded: DiscardedPartialShardReceiptV3


def remaining_categories_v3(start_category_index: int = START_CATEGORY_INDEX) -> tuple[str, ...]:
    if type(start_category_index) is not int or start_category_index != START_CATEGORY_INDEX:
        raise SupervisedExperimentV3Error("CATEGORY_SHARD_START_INDEX_INVALID")
    return CHEMBENCH4K_CATEGORIES[start_category_index:]


def _require_legacy_category_recovery_profile(inputs: v2.ExperimentInputsV2) -> None:
    config = inputs.config
    if (
        getattr(config, "protocol_id", None) != PROTOCOL_ID
        or getattr(config, "train_rounds", None) != 3
        or getattr(config, "evolution_cycles", None) != EVOLUTION_CYCLES
    ):
        raise SupervisedExperimentV3Error("CATEGORY_SHARD_RECOVERY_PROFILE_INCOMPATIBLE")


def build_category_shard_recovery_dry_run_v3(
    *,
    inputs: v2.ExperimentInputsV2,
    audit: AuditedParentShardsV3,
    recovery_run_id: str,
) -> dict[str, object]:
    """Build the remaining-shard schedule without creating a run or calling a model."""

    if type(inputs) is not v2.ExperimentInputsV2:
        raise TypeError("inputs must be exact ExperimentInputsV2")
    _require_legacy_category_recovery_profile(inputs)
    if type(audit) is not AuditedParentShardsV3:
        raise TypeError("audit must be exact AuditedParentShardsV3")
    if _RUN_ID.fullmatch(recovery_run_id) is None:
        raise ValueError("invalid recovery run ID")
    if inputs.repository_root != audit.repository_root:
        raise SupervisedExperimentV3Error("CATEGORY_SHARD_REPOSITORY_MISMATCH")
    complete = v2._by_category(inputs.train)
    schedule = []
    ordered_uids: list[str] = []
    for category_index, category in enumerate(REMAINING_CATEGORIES, start=START_CATEGORY_INDEX):
        tasks = complete[category]
        if len(tasks) != 50:
            raise SupervisedExperimentV3Error("CATEGORY_SHARD_TASK_COUNT_INVALID")
        ordered_uids.extend(task.uid for task in tasks)
        schedule.append(
            {
                "category_index": category_index,
                "category": category,
                "task_start": 0,
                "task_end": 49,
                "task_count": 50,
                "sessions_per_task": 3,
                "cycles_per_task": EVOLUTION_CYCLES,
                "generation_zero_at_task_zero": True,
            }
        )
    if len(ordered_uids) != 400 or len(set(ordered_uids)) != 400:
        raise SupervisedExperimentV3Error("CATEGORY_SHARD_SCHEDULE_INVALID")
    return {
        "schema_version": "CategoryShardRecoveryDryRunV3",
        "protocol_id": CATEGORY_SHARD_RECOVERY_PROTOCOL_ID,
        "status": "PASS",
        "model_calls": 0,
        "parent_run_id": audit.parent_run_id,
        "recovery_run_id": recovery_run_id,
        "accepted_parent_category": ACCEPTED_CATEGORY,
        "discarded_parent_category": DISCARDED_CATEGORY,
        "first_category": DISCARDED_CATEGORY,
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
        "planned_remaining_train_candidate_calls": 1200,
        "planned_remaining_train_reflector_calls": 800,
        "planned_remaining_train_core_jobs": 2400,
    }


def audit_parent_category_shards_v3(
    *, repository_root: Path, parent_run_id: str = PARENT_RUN_ID
) -> AuditedParentShardsV3:
    """Read-only audit of the accepted and discarded parent category shards."""

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
        raise SupervisedExperimentV3Error("CATEGORY_SHARD_PARENT_PROCESS_ACTIVE")
    if tuple(state_root.rglob("*-wal")) or tuple(state_root.rglob("*-shm")):
        raise SupervisedExperimentV3Error("CATEGORY_SHARD_PARENT_SQLITE_RESIDUAL")
    state = _read_json(state_root / "run_state.json")
    paid = _read_json(result_root / "public/planned_paid_execution_receipt_v3.json")
    if (
        state.get("run_id") != parent_run_id
        or state.get("protocol_id") != PROTOCOL_ID
        or state.get("status") != "RUNNING"
        or state.get("stage") != "ONLINE_TRAIN"
        or state.get("category") != DISCARDED_CATEGORY
        or state.get("task") != 25
        or state.get("round_or_cycle") != 1
        or paid.get("source_commit") != state.get("source_commit")
        or paid.get("config_sha256") != state.get("config_sha256")
        or paid.get("split_reference_sha256") != state.get("split_receipt_sha256")
        or state.get("reflector_codex_executable_sha256")
        != "a96f944d1a596dbfb7fdd84f482be5c50e34b04bb371126840d873e4ebf26902"
    ):
        raise SupervisedExperimentV3Error("CATEGORY_SHARD_PARENT_IDENTITY_INVALID")
    public_events = _read_jsonl(result_root / "public/events.jsonl")
    private_events = _read_jsonl(state_root / "private/events.jsonl")
    accepted = _audit_accepted_category(
        state_root=state_root,
        state=state,
        paid=paid,
        public_events=public_events,
        private_events=private_events,
        parent_run_id=parent_run_id,
    )
    discarded = _audit_discarded_category(
        state_root=state_root,
        public_events=public_events,
        private_events=private_events,
        parent_run_id=parent_run_id,
    )
    after = _tree_identity((state_root, result_root))
    if after != before:
        raise SupervisedExperimentV3Error("CATEGORY_SHARD_PARENT_MUTATED_DURING_AUDIT")
    return AuditedParentShardsV3(
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


def _audit_accepted_category(
    *,
    state_root: Path,
    state: dict[str, Any],
    paid: dict[str, Any],
    public_events: list[dict[str, Any]],
    private_events: list[dict[str, Any]],
    parent_run_id: str,
) -> AcceptedClosedShardReceiptV3:
    category_root = state_root / "private/core/formal" / ACCEPTED_CATEGORY
    checkpoint = category_root / "private_lineage_checkpoints_v2.jsonl"
    rows = _read_jsonl(checkpoint)
    if len(rows) != 100:
        raise SupervisedExperimentV3Error("ACCEPTED_SHARD_CHECKPOINT_COUNT_INVALID")
    results: list[TaskwiseCoreUpdateResultV1] = []
    prior = None
    for ordinal, row in enumerate(rows, start=1):
        try:
            result = TaskwiseCoreUpdateResultV1.model_validate(row["result"])
            lineage = result.required_lineage_receipt()
        except (KeyError, TypeError, ValueError) as exc:
            raise SupervisedExperimentV3Error("ACCEPTED_SHARD_CHECKPOINT_INVALID") from exc
        if (
            result.supervised_category != ACCEPTED_CATEGORY
            or result.updates_per_task != EVOLUTION_CYCLES
            or result.global_update_ordinal != ordinal
            or result.task_index != (ordinal - 1) // EVOLUTION_CYCLES
            or result.update_index != (ordinal - 1) % EVOLUTION_CYCLES + 1
            or result.predecessor != prior
            or row.get("required_lineage") != lineage.model_dump(mode="json")
            or row.get("required_lineage_sha256") != lineage.digest
            or not result.validation_receipt.passed
            or result.validation_receipt.finding_codes
            or not result.memory_inspection.passed
            or tuple(value.target_id for value in result.supervised_auxiliary_artifacts)
            != ("skill_bundle", "agent_system")
            or any(not value.inspection.passed for value in result.supervised_auxiliary_artifacts)
        ):
            raise SupervisedExperimentV3Error("ACCEPTED_SHARD_CHAIN_INVALID")
        prior = result.predecessor_identity()
        results.append(result)
    final = results[-1]
    if final.task_index != 49 or final.update_index != 2 or final.global_update_ordinal != 100:
        raise SupervisedExperimentV3Error("ACCEPTED_SHARD_FINAL_HEAD_INVALID")
    database = category_root / "evolution.sqlite3"
    connection = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SupervisedExperimentV3Error("ACCEPTED_SHARD_SQLITE_INVALID")
        states = {
            str(row["state"]): int(row["count"])
            for row in connection.execute(
                "SELECT state, COUNT(*) AS count FROM jobs GROUP BY state"
            )
        }
        leases = int(
            connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE claimed_by IS NOT NULL "
                "OR lease_id IS NOT NULL OR lease_expires_at IS NOT NULL"
            ).fetchone()[0]
        )
        staged_jobs = int(
            connection.execute(
                "SELECT COUNT(*) FROM artifacts WHERE staging_job_id IS NOT NULL"
            ).fetchone()[0]
        )
        contexts = int(
            connection.execute("SELECT COUNT(*) FROM context_materializations").fetchone()[0]
        )
        if states != {"succeeded": 300} or leases or staged_jobs or contexts != 300:
            raise SupervisedExperimentV3Error("ACCEPTED_SHARD_CORE_COUNTS_INVALID")
        type_counts = {
            str(row["type"]): (int(row["count"]), int(row["promoted"]))
            for row in connection.execute(
                "SELECT type, COUNT(*) AS count, SUM(promoted) AS promoted "
                "FROM artifacts GROUP BY type"
            )
        }
        if type_counts != {
            "dataset": (100, 100),
            "text_memory": (100, 100),
            "skill_bundle": (100, 100),
            "agent_system": (100, 100),
        }:
            raise SupervisedExperimentV3Error("ACCEPTED_SHARD_ARTIFACT_COUNTS_INVALID")
        for result in results:
            _verify_result_lifecycle_read_only(connection, category_root, result)
    finally:
        connection.close()
    evaluations = [
        row
        for row in private_events
        if row.get("kind") == "PRIVATE_EVALUATED"
        and row.get("stage") == "ONLINE_TRAIN"
        and row.get("category") == ACCEPTED_CATEGORY
    ]
    cycles = [
        row
        for row in public_events
        if row.get("kind") == "REFLECTOR_SUPERVISED"
        and row.get("stage") == "ONLINE_TRAIN"
        and row.get("category") == ACCEPTED_CATEGORY
    ]
    if len(evaluations) != 150 or len(cycles) != 100:
        raise SupervisedExperimentV3Error("ACCEPTED_SHARD_EVENT_COUNTS_INVALID")
    for task_ordinal in range(50):
        task_evaluations = [row for row in evaluations if row.get("task_ordinal") == task_ordinal]
        task_cycles = [row for row in cycles if row.get("task_index") == task_ordinal]
        if sorted(row.get("round_index") for row in task_evaluations) != [0, 1, 2] or sorted(
            row.get("cycle") for row in task_cycles
        ) != [1, 2]:
            raise SupervisedExperimentV3Error("ACCEPTED_SHARD_TASK_NOT_CLOSED")
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
    return AcceptedClosedShardReceiptV3(
        parent_run_id=parent_run_id,
        source_commit=str(state["source_commit"]),
        config_digest=str(state["config_sha256"]),
        split_digest=str(state["split_receipt_sha256"]),
        model_digest=_model_digest(paid),
        executor_digest=_executor_digest(paid),
        managed_codex_digest=str(state["reflector_codex_executable_sha256"]),
        category_tree_sha256=_tree_identity((category_root,))[0],
        final_head_set_sha256=sha256_bytes(canonical_json_bytes(target_receipts)),
    )


def _audit_discarded_category(
    *,
    state_root: Path,
    public_events: list[dict[str, Any]],
    private_events: list[dict[str, Any]],
    parent_run_id: str,
) -> DiscardedPartialShardReceiptV3:
    category_root = state_root / "private/core/formal" / DISCARDED_CATEGORY
    rows = _read_jsonl(category_root / "private_lineage_checkpoints_v2.jsonl")
    evaluations = [
        row
        for row in private_events
        if row.get("kind") == "PRIVATE_EVALUATED"
        and row.get("stage") == "ONLINE_TRAIN"
        and row.get("category") == DISCARDED_CATEGORY
    ]
    cycles = [
        row
        for row in public_events
        if row.get("kind") == "REFLECTOR_SUPERVISED"
        and row.get("stage") == "ONLINE_TRAIN"
        and row.get("category") == DISCARDED_CATEGORY
    ]
    if len(rows) != 51 or len(evaluations) != 76 or len(cycles) != 51:
        raise SupervisedExperimentV3Error("DISCARDED_SHARD_EVIDENCE_COUNTS_INVALID")
    if any(row.get("round_index") == 1 and row.get("task_ordinal") == 25 for row in evaluations):
        raise SupervisedExperimentV3Error("DISCARDED_SHARD_UNEXPECTED_COMPLETION")
    database = category_root / "evolution.sqlite3"
    connection = sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SupervisedExperimentV3Error("DISCARDED_SHARD_SQLITE_INVALID")
        states = {
            str(row["state"]): int(row["count"])
            for row in connection.execute(
                "SELECT state, COUNT(*) AS count FROM jobs GROUP BY state"
            )
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
        if (
            states != {"succeeded": 153}
            or leases
            or staged
            or contexts != 153
            or type_counts
            != {
                "dataset": (51, 51),
                "text_memory": (51, 51),
                "skill_bundle": (51, 51),
                "agent_system": (51, 51),
            }
        ):
            raise SupervisedExperimentV3Error("DISCARDED_SHARD_CORE_COUNTS_INVALID")
    finally:
        connection.close()
    return DiscardedPartialShardReceiptV3(
        parent_run_id=parent_run_id,
        category_tree_sha256=_tree_identity((category_root,))[0],
    )


def _verify_result_lifecycle_read_only(
    connection: sqlite3.Connection,
    category_root: Path,
    result: TaskwiseCoreUpdateResultV1,
) -> None:
    targets = (
        (
            "text_memory",
            result.job_id,
            result.core_artifact_id,
            result.artifact_payload_sha256,
            result.artifact_lineage_sha256,
            result.core_artifact_manifest_sha256,
            result.core_context_id,
            result.context_resolution_digest,
        ),
        *(
            (
                value.target_id,
                value.job_id,
                value.core_artifact_id,
                value.artifact_payload_sha256,
                value.artifact_lineage_sha256,
                value.core_artifact_manifest_sha256,
                value.core_context_id,
                value.context_resolution_digest,
            )
            for value in result.supervised_auxiliary_artifacts
        ),
    )
    for (
        target_id,
        job_id,
        artifact_id,
        payload_digest,
        lineage_digest,
        manifest_digest,
        context_id,
        context_digest,
    ) in targets:
        job = connection.execute(
            "SELECT state FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        artifact = connection.execute(
            "SELECT type, state, promoted, staging_job_id, uri, manifest_path, lineage_json "
            "FROM artifacts WHERE artifact_id=?",
            (artifact_id,),
        ).fetchone()
        context = connection.execute(
            "SELECT cm.manifest_json, c.selected_artifact_ids_json "
            "FROM context_materializations cm JOIN contexts c USING(context_id) "
            "WHERE cm.context_id=?",
            (context_id,),
        ).fetchone()
        if (
            job is None
            or job["state"] != "succeeded"
            or artifact is None
            or artifact["type"] != target_id
            or artifact["state"] != "active"
            or artifact["promoted"] != 1
            or artifact["staging_job_id"] is not None
            or context is None
        ):
            raise SupervisedExperimentV3Error("ACCEPTED_SHARD_LIFECYCLE_INVALID")
        payload_root = _safe_core_file_path(
            str(artifact["uri"]), allowed_root=category_root / "artifacts"
        )
        payload_path = payload_root / "SKILL.md" if target_id == "skill_bundle" else payload_root
        payload = _read_bounded_regular_file(payload_path, maximum=_MAX_ARTIFACT_PAYLOAD_BYTES)
        try:
            lineage = json.loads(str(artifact["lineage_json"]))
            materialized = json.loads(str(context["manifest_json"]))
            selected = json.loads(str(context["selected_artifact_ids_json"]))
        except json.JSONDecodeError as exc:
            raise SupervisedExperimentV3Error("ACCEPTED_SHARD_LIFECYCLE_JSON_INVALID") from exc
        if (
            sha256_bytes(payload) != payload_digest
            or sha256_bytes(_read_regular(Path(str(artifact["manifest_path"])))) != manifest_digest
            or canonical_digest(lineage) != lineage_digest
            or canonical_digest(materialized) != context_digest
            or selected != [artifact_id]
            or materialized.get("context_id") != context_id
        ):
            raise SupervisedExperimentV3Error("ACCEPTED_SHARD_LIFECYCLE_DIGEST_INVALID")


class SupervisedTransferCategoryShardRecoveryV3(SupervisedTransferExperimentV3):
    """Run only fresh category shards 1..8; never attach a parent Core store."""

    def __init__(
        self,
        *,
        inputs: v2.ExperimentInputsV2,
        run_id: str,
        parent_audit: AuditedParentShardsV3,
        executor_factory: Any | None = None,
        bridge_factory: Any | None = None,
    ) -> None:
        _require_legacy_category_recovery_profile(inputs)
        if type(parent_audit) is not AuditedParentShardsV3:
            raise TypeError("parent_audit must be exact AuditedParentShardsV3")
        if parent_audit.repository_root != inputs.repository_root:
            raise SupervisedExperimentV3Error("CATEGORY_SHARD_REPOSITORY_MISMATCH")
        self.parent_audit = parent_audit
        self.start_category_index = START_CATEGORY_INDEX
        self.remaining_categories = remaining_categories_v3()
        parent_state = _read_json(parent_audit.parent_state_root / "run_state.json")
        super().__init__(
            inputs=inputs,
            run_id=run_id,
            run_mode="formal_online",
            smoke_run_id=str(parent_state["smoke_run_id"]),
            executor_factory=executor_factory,
            bridge_factory=bridge_factory,
        )
        self._state.update(
            {
                "schema_version": "ChemBenchCategoryShardRecoveryRunStateV3",
                "protocol_id": CATEGORY_SHARD_RECOVERY_PROTOCOL_ID,
                "parent_run_id": parent_audit.parent_run_id,
                "start_category_index": START_CATEGORY_INDEX,
                "first_category": DISCARDED_CATEGORY,
                "first_task": 0,
                "generation_zero_targets": True,
                "parent_artifacts_imported": False,
                "parent_database_imported": False,
                "accepted_parent_shard_sha256": parent_audit.accepted.digest,
                "discarded_parent_shard_sha256": parent_audit.discarded.digest,
            }
        )
        self._write_state()
        self._write_recovery_protocol_receipts()

    def run_category_shard_recovery(self) -> dict[str, object]:
        """Execute every remaining Train category from generation zero."""

        try:
            self._verify_parent_tree_unchanged()
            self._verify_parent_smoke_compatibility()
            with ExitStack() as stack:
                executor = v2._enter_if_context(stack, self._executor_factory("online"))
                self._open_remaining_bridges(stack)
                self._run_remaining_train(executor)
                self._require_counts(task_sessions=1200, reflector_calls=800, core_jobs=2400)
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
            raise SupervisedExperimentV3Error("CATEGORY_SHARD_BRIDGE_SET_INVALID")

    def _run_remaining_train(self, executor: v2.TaskExecutorV2) -> None:
        self._set_stage("ONLINE_TRAIN")
        by_category = v2._by_category(self.inputs.train)
        for category in self.remaining_categories:
            bridge = self._bridges[category]
            for task_ordinal, task in enumerate(by_category[category]):
                if task_ordinal == 0 and self._heads[category] is not None:
                    raise SupervisedExperimentV3Error("CATEGORY_SHARD_GENERATION_ZERO_INVALID")
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
        task_ordinal = kwargs.get("task_ordinal")
        round_index = kwargs.get("round_index")
        stage = kwargs.get("stage")
        context = kwargs.get("context")
        if (
            stage == "ONLINE_TRAIN"
            and getattr(task, "category", None) == DISCARDED_CATEGORY
            and task_ordinal == 0
            and round_index == 0
            and context is not None
        ):
            raise SupervisedExperimentV3Error("CATEGORY_SHARD_GENERATION_ZERO_INVALID")
        return super()._execute_single_session(*args, **kwargs)

    def _verify_parent_smoke_compatibility(self) -> None:
        parent_state = _read_json(self.parent_audit.parent_state_root / "run_state.json")
        smoke_path = (
            self.inputs.repository_root
            / self.inputs.config.payload["roots"]["results"]
            / "runs"
            / str(parent_state["smoke_run_id"])
            / "public/smoke_authority_v3.json"
        )
        smoke = _read_json(smoke_path)
        if (
            smoke.get("status") != "PASS"
            or smoke.get("source_commit") != self.parent_audit.accepted.source_commit
            or smoke.get("config_sha256") != self.inputs.config.digest
            or smoke.get("split_reference_sha256") != self.inputs.split_receipt_sha256
            or smoke.get("candidate_codex_identity_sha256") != self.inputs.candidate_codex.digest
            or smoke.get("reflector_codex_identity_sha256") != self.inputs.managed_codex.digest
        ):
            raise SupervisedExperimentV3Error("CATEGORY_SHARD_SMOKE_AUTHORITY_INVALID")
        self._state["parent_smoke_authority_sha256"] = sha256_bytes(_read_regular(smoke_path))
        self._write_state()

    def _verify_parent_tree_unchanged(self) -> None:
        current = _tree_identity(
            (self.parent_audit.parent_state_root, self.parent_audit.parent_result_root)
        )
        expected = (
            self.parent_audit.parent_tree_sha256,
            self.parent_audit.parent_tree_file_count,
            self.parent_audit.parent_tree_size_bytes,
        )
        if current != expected:
            raise SupervisedExperimentV3Error("CATEGORY_SHARD_PARENT_TREE_DRIFT")

    def _write_recovery_protocol_receipts(self) -> None:
        public = self.result_root / "public"
        amendment = CategoryShardRecoveryAmendmentV3(
            parent_run_id=self.parent_audit.parent_run_id,
            recovery_run_id=self.run_id,
        )
        plan = RemainingShardExecutionPlanV3(
            parent_run_id=self.parent_audit.parent_run_id,
            recovery_run_id=self.run_id,
            categories=self.remaining_categories,
        )
        for name, model in (
            ("category_shard_recovery_amendment_v3.json", amendment),
            ("accepted_closed_shard_receipt_v3.json", self.parent_audit.accepted),
            ("discarded_partial_shard_receipt_v3.json", self.parent_audit.discarded),
            ("remaining_shard_execution_plan_v3.json", plan),
        ):
            write_public_file(
                public / name, canonical_pretty_json_bytes(model.model_dump(mode="json"))
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
            "schema_version": "CategoryShardRecoveryPaidExecutionPlanV3",
            "protocol_id": CATEGORY_SHARD_RECOVERY_PROTOCOL_ID,
            "run_id": self.run_id,
            "parent_run_id": self.parent_audit.parent_run_id,
            "source_commit": self.inputs.source_commit,
            "config_sha256": self.inputs.config.digest,
            "split_reference_sha256": self.inputs.split_receipt_sha256,
            "source_manifest_sha256": self.inputs.source_manifest_sha256,
            "model": self.inputs.config.model,
            "reasoning_effort": self.inputs.config.reasoning_effort,
            "task_timeout_seconds": self.inputs.config.task_timeout_seconds,
            "reflector_timeout_seconds": self.inputs.config.reflector_timeout_seconds,
            "start_category_index": START_CATEGORY_INDEX,
            "categories": list(self.remaining_categories),
            "first_task_ordinal": 0,
            "generation_zero_targets": True,
            "planned_calls": {
                "remaining_train_candidate_calls": 1200,
                "remaining_train_reflector_calls": 800,
                "remaining_train_core_jobs": 2400,
                "eventual_final_test_candidate_calls": TEST_COUNT,
                "recovery_candidate_calls_total": 1650,
                "answer_and_reflector_calls": 2450,
            },
            "parent_usage": {
                "name_conversion_calls_included": 250,
                "property_prediction_calls_discarded": 127,
            },
            "candidate_execution_path": "TaskRequest->Rollout->Gateway->CodexHarness",
            "reflector_execution_path": "Core planned job->worker->codex_cli provider",
            "candidate_codex_identity_sha256": self.inputs.candidate_codex.digest,
            "candidate_codex_executable_sha256": self.inputs.candidate_codex.executable_sha256,
            "reflector_codex_identity_sha256": self.inputs.managed_codex.digest,
            "reflector_codex_executable_sha256": self.inputs.managed_codex.executable_sha256,
            "runtime_services_identity_sha256": self.inputs.runtime_services.digest,
            "parent_artifacts_imported": False,
            "parent_database_imported": False,
            "written_before_first_model_call": True,
            "issued_at_utc": v2.utc_now(),
        }
        write_public_file(
            self.result_root / "public/category_shard_paid_execution_plan_v3.json",
            canonical_pretty_json_bytes(payload),
        )

    def _write_remaining_closed_shard_receipts(self) -> tuple[ShardCompositionEntryV3, ...]:
        identity = {
            "source_commit": self.inputs.source_commit,
            "config_digest": self.inputs.config.digest,
            "split_digest": self.inputs.split_receipt_sha256,
            "model_digest": _model_digest_from_inputs(self),
            "executor_digest": _executor_digest_from_inputs(self),
            "managed_codex_digest": self.inputs.managed_codex.executable_sha256,
        }
        entries: list[ShardCompositionEntryV3] = []
        for category_index, category in enumerate(CHEMBENCH4K_CATEGORIES):
            if category_index < START_CATEGORY_INDEX:
                continue
            head = self._heads[category]
            if head is None or head.task_index != 49 or head.global_update_ordinal != 100:
                raise SupervisedExperimentV3Error("REMAINING_SHARD_FINAL_HEAD_INVALID")
            bridge = self._bridges[category]
            bridge.verify_update_result(head)
            public_rows = [
                row
                for row in _read_jsonl(self.public_events)
                if row.get("kind") == "REFLECTOR_SUPERVISED"
                and row.get("stage") == "ONLINE_TRAIN"
                and row.get("category") == category
            ]
            private_rows = [
                row
                for row in _read_jsonl(self.private_events)
                if row.get("kind") == "PRIVATE_EVALUATED"
                and row.get("stage") == "ONLINE_TRAIN"
                and row.get("category") == category
            ]
            if len(public_rows) != 100 or len(private_rows) != 150:
                raise SupervisedExperimentV3Error("REMAINING_SHARD_EVENT_COUNTS_INVALID")
            closed = {
                "schema_version": "ClosedRecoveryCategoryShardReceiptV3",
                "protocol_id": CATEGORY_SHARD_RECOVERY_PROTOCOL_ID,
                "run_id": self.run_id,
                "category_index": category_index,
                "category": category,
                "task_start": 0,
                "task_end": 49,
                "task_count": 50,
                "completion_count": 150,
                "reflector_count": 100,
                "core_job_count": 300,
                "artifact_count": 300,
                "final_global_update_ordinal": 100,
                "final_head_artifact_id": head.core_artifact_id,
                "final_head_payload_sha256": head.artifact_payload_sha256,
                "final_head_context_sha256": head.context_resolution_digest,
                "source_commit": identity["source_commit"],
                "config_digest": identity["config_digest"],
                "split_digest": identity["split_digest"],
                "model_digest": identity["model_digest"],
                "executor_digest": identity["executor_digest"],
                "managed_codex_digest": identity["managed_codex_digest"],
                "closed": True,
                "included_in_statistics": True,
                "issued_at_utc": v2.utc_now(),
            }
            path = self.result_root / "public" / f"closed_shard_{category}_v3.json"
            write_public_file(path, canonical_pretty_json_bytes(closed))
            entries.append(
                ShardCompositionEntryV3(
                    category=category,
                    category_index=category_index,
                    source_run_id=self.run_id,
                    closed_receipt_sha256=sha256_bytes(path.read_bytes()),
                    **identity,
                )
            )
        return tuple(entries)

    def _write_final_shard_composition(
        self, remaining: tuple[ShardCompositionEntryV3, ...]
    ) -> FinalShardCompositionReceiptV3:
        parent = self.parent_audit.accepted
        accepted_entry = ShardCompositionEntryV3(
            category=ACCEPTED_CATEGORY,
            category_index=0,
            source_run_id=parent.parent_run_id,
            source_commit=parent.source_commit,
            config_digest=parent.config_digest,
            split_digest=parent.split_digest,
            model_digest=parent.model_digest,
            executor_digest=parent.executor_digest,
            managed_codex_digest=parent.managed_codex_digest,
            closed_receipt_sha256=parent.digest,
        )
        receipt = FinalShardCompositionReceiptV3(categories=(accepted_entry, *remaining))
        write_public_file(
            self.result_root / "public/final_shard_composition_receipt_v3.json",
            canonical_pretty_json_bytes(receipt.model_dump(mode="json")),
        )
        return receipt


def build_source_compatibility_receipt_v3(
    *, audit: AuditedParentShardsV3, recovery_run_id: str
) -> dict[str, object]:
    repository = audit.repository_root
    parent_commit = audit.accepted.source_commit
    recovery_commit = _git(repository, "rev-parse", "HEAD")
    protected = (
        "benchmarks/chembench/configs/supervised_transfer_v3/chembench_supervised_transfer_v3.yaml",
        "benchmarks/chembench/src/openevo_chembench/chembench4k_dataset.py",
        "benchmarks/chembench/src/openevo_chembench/chembench4k_models.py",
        "benchmarks/chembench/src/openevo_chembench/chembench4k_prompt.py",
        "benchmarks/chembench/src/openevo_chembench/chembench4k_evaluation.py",
        "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2/core.py",
        "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2/executor.py",
        "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2/packet.py",
        "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2/reflector_boundary.py",
        "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2/memory.py",
        "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2/artifacts.py",
        "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v2/managed_codex.py",
        "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/config.py",
        "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/experiment.py",
        "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/reporting.py",
        "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/split_reference.py",
        "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/test_ledger.py",
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
        "benchmarks/chembench/configs/supervised_transfer_v3/category_shard_recovery.md",
        "benchmarks/chembench/manifests/supervised_transfer_v3/source_manifest_v3.json",
        "benchmarks/chembench/scripts/supervised_transfer_v3/main.py",
        "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/category_shard_recovery.py",
        "benchmarks/chembench/tests/supervised_transfer_v3/test_category_shard_recovery_v3.py",
    }
    if set(changed) - allowed_changed:
        raise SupervisedExperimentV3Error("RECOVERY_SOURCE_NOT_SEMANTICALLY_COMPATIBLE")
    return {
        "schema_version": "CategoryShardRecoveryCompatibilityReceiptV3",
        "parent_run_id": audit.parent_run_id,
        "recovery_run_id": recovery_run_id,
        "parent_source_commit": parent_commit,
        "recovery_source_commit": recovery_commit,
        "git_diff_sha256": sha256_bytes(diff),
        "files_changed": changed,
        "unchanged_semantic_inputs": unchanged,
        "allowed_changes": [
            "category_start_selection",
            "category_shard_orchestration",
            "shard_receipts_and_composition",
        ],
        "candidate_execution_path_unchanged": True,
        "reflector_execution_path_unchanged": True,
        "category_internal_protocol_unchanged": True,
        "semantically_compatible": True,
    }


def _model_digest(paid: dict[str, Any]) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "model": paid.get("model"),
                "reasoning_effort": paid.get("reasoning_effort"),
                "managed_runtime_image": MANAGED_RUNTIME_RELEASES[
                    "managed_science"
                ].loaded_image_id,
            }
        )
    )


def _executor_digest(paid: dict[str, Any]) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "candidate_execution_path": paid.get("candidate_execution_path"),
                "reflector_execution_path": paid.get("reflector_execution_path"),
                "candidate_codex_identity_sha256": paid.get("candidate_codex_identity_sha256"),
                "reflector_codex_identity_sha256": paid.get("reflector_codex_identity_sha256"),
                "task_timeout_seconds": paid.get("task_timeout_seconds"),
                "reflector_timeout_seconds": paid.get("reflector_timeout_seconds"),
            }
        )
    )


def _model_digest_from_inputs(experiment: SupervisedTransferCategoryShardRecoveryV3) -> str:
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
    experiment: SupervisedTransferCategoryShardRecoveryV3,
) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "candidate_execution_path": "TaskRequest->Rollout->Gateway->CodexHarness",
                "reflector_execution_path": "Core planned job->worker->codex_cli provider",
                "candidate_codex_identity_sha256": experiment.inputs.candidate_codex.digest,
                "reflector_codex_identity_sha256": experiment.inputs.managed_codex.digest,
                "task_timeout_seconds": experiment.inputs.config.task_timeout_seconds,
                "reflector_timeout_seconds": experiment.inputs.config.reflector_timeout_seconds,
            }
        )
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_read_regular(path).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SupervisedExperimentV3Error("CATEGORY_SHARD_JSON_INVALID") from exc
    if not isinstance(value, dict):
        raise SupervisedExperimentV3Error("CATEGORY_SHARD_JSON_INVALID")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    encoded = _read_regular(path)
    if encoded and not encoded.endswith(b"\n"):
        raise SupervisedExperimentV3Error("CATEGORY_SHARD_JSONL_TRUNCATED")
    rows: list[dict[str, Any]] = []
    try:
        for line in encoded.splitlines():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise TypeError
            rows.append(value)
    except (TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise SupervisedExperimentV3Error("CATEGORY_SHARD_JSONL_INVALID") from exc
    return rows


def _read_regular(path: Path) -> bytes:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise SupervisedExperimentV3Error("CATEGORY_SHARD_FILE_UNSAFE")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    noatime = getattr(os, "O_NOATIME", 0)
    try:
        descriptor = os.open(path, flags | noatime)
    except PermissionError:
        descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        return stream.read()


def _tree_identity(roots: tuple[Path, ...]) -> tuple[str, int, int]:
    entries: list[dict[str, object]] = []
    total = 0
    common = Path(os.path.commonpath([str(root) for root in roots]))
    for root in roots:
        for path in sorted(root.rglob("*")):
            metadata = path.lstat()
            relative = path.relative_to(common).as_posix()
            if stat.S_ISLNK(metadata.st_mode):
                raise SupervisedExperimentV3Error("CATEGORY_SHARD_PARENT_SYMLINK")
            if stat.S_ISDIR(metadata.st_mode):
                entries.append(
                    {"path": relative, "kind": "directory", "mode": stat.S_IMODE(metadata.st_mode)}
                )
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise SupervisedExperimentV3Error("CATEGORY_SHARD_PARENT_FILE_UNSAFE")
            payload = _read_regular(path)
            total += len(payload)
            entries.append(
                {
                    "path": relative,
                    "kind": "file",
                    "mode": stat.S_IMODE(metadata.st_mode),
                    "size": len(payload),
                    "sha256": sha256_bytes(payload),
                }
            )
    return (
        sha256_bytes(canonical_json_bytes(entries)),
        sum(1 for entry in entries if entry["kind"] == "file"),
        total,
    )


def _active_processes(run_id: str) -> tuple[int, ...]:
    own = {os.getpid()}
    parent = os.getppid()
    while parent > 1 and parent not in own:
        own.add(parent)
        try:
            status = (Path("/proc") / str(parent) / "status").read_text()
            parent = int(
                next(line.split()[1] for line in status.splitlines() if line.startswith("PPid:"))
            )
        except (OSError, StopIteration, ValueError):
            break
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) in own:
            continue
        try:
            arguments = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if not arguments or Path(arguments[0].decode(errors="ignore")).name == "tmux":
            continue
        command = b" ".join(arguments)
        if run_id.encode() in command:
            found.append(int(entry.name))
    return tuple(sorted(found))


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


__all__ = [
    "ACCEPTED_CATEGORY",
    "CATEGORY_SHARD_RECOVERY_PROTOCOL_ID",
    "AcceptedClosedShardReceiptV3",
    "AuditedParentShardsV3",
    "CategoryShardRecoveryAmendmentV3",
    "DiscardedPartialShardReceiptV3",
    "FinalShardCompositionReceiptV3",
    "RemainingShardExecutionPlanV3",
    "ShardCompositionEntryV3",
    "SupervisedTransferCategoryShardRecoveryV3",
    "audit_parent_category_shards_v3",
    "build_category_shard_recovery_dry_run_v3",
    "build_source_compatibility_receipt_v3",
    "remaining_categories_v3",
]
