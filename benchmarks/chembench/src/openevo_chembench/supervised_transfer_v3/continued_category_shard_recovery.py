"""Chained category-atomic recovery for the active two-round v3 profile.

This controller extends the first same-profile recovery without importing any
parent database, artifact, or category head.  It accepts an exact closed
category prefix, discards the parent's current partial category in full, and
starts that category again at task zero with generation-zero targets.
"""

from __future__ import annotations

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
from openevo_chembench.supervised_transfer_v3.category_shard_recovery import (
    _RUN_ID,
    _active_processes,
    _executor_digest,
    _model_digest,
    _read_json,
    _read_jsonl,
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
from openevo_chembench.supervised_transfer_v3.same_profile_category_recovery import (
    EXPECTED_MANAGED_CODEX_SHA256,
    SameProfileAcceptedClosedShardsReceiptV3,
    SameProfileShardCompositionEntryV3,
    _category_events,
    _git,
    _open_immutable_database,
    _require_database_counts,
    _require_same_profile,
    _validate_checkpoint_chain,
)

CONTINUED_CATEGORY_RECOVERY_PROTOCOL_ID = (
    "supervised_transfer_v3_two_round_one_evolution_chained_category_shard_recovery"
)
CONTINUED_PARENT_RUN_ID = "stv3-one-update-recovery-20260731T024430Z"
CONTINUED_START_CATEGORY_INDEX = 7
_SHA256_LENGTH = 64
_COMMIT_LENGTH = 40


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.model_dump(mode="json")))


class ContinuedAcceptedCategoryShardV3(_ClosedModel):
    schema_version: Literal["ContinuedAcceptedCategoryShardV3"] = (
        "ContinuedAcceptedCategoryShardV3"
    )
    category_index: int = Field(ge=0, le=8)
    category: str
    source_run_id: str
    source_commit: str
    config_digest: str
    split_digest: str
    model_digest: str
    executor_digest: str
    managed_codex_digest: str
    task_start: Literal[0] = 0
    task_end: Literal[49] = 49
    candidate_completion_count: Literal[100] = 100
    reflector_call_count: Literal[50] = 50
    core_job_count: Literal[150] = 150
    artifact_count: Literal[150] = 150
    context_resolution_count: Literal[150] = 150
    final_category_state_closed: Literal[True] = True
    category_state_independent: Literal[True] = True
    included_in_final_statistics: Literal[True] = True
    category_tree_sha256: str
    final_head_set_sha256: str
    closed_evidence_sha256: str

    @field_validator("source_commit")
    @classmethod
    def _commit(cls, value: str) -> str:
        if len(value) != _COMMIT_LENGTH or any(
            char not in "0123456789abcdef" for char in value
        ):
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
        "closed_evidence_sha256",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        if len(value) != _SHA256_LENGTH or any(
            char not in "0123456789abcdef" for char in value
        ):
            raise ValueError("closed shard digest must be SHA-256")
        return value


class ContinuedAcceptedClosedShardsReceiptV3(_ClosedModel):
    schema_version: Literal["ContinuedAcceptedClosedShardsReceiptV3"] = (
        "ContinuedAcceptedClosedShardsReceiptV3"
    )
    parent_run_id: str
    start_category_index: int = Field(ge=1, le=8)
    shards: tuple[ContinuedAcceptedCategoryShardV3, ...]
    accepted_category_count: int = Field(ge=1, le=8)
    accepted_task_count: int = Field(ge=50, le=400)
    accepted_candidate_completion_count: int = Field(ge=100, le=800)
    accepted_reflector_call_count: int = Field(ge=50, le=400)
    accepted_core_job_count: int = Field(ge=150, le=1200)
    accepted_context_resolution_count: int = Field(ge=150, le=1200)
    included_in_final_statistics: Literal[True] = True

    @model_validator(mode="after")
    def _closed_prefix(self) -> ContinuedAcceptedClosedShardsReceiptV3:
        count = self.start_category_index
        if (
            tuple(shard.category_index for shard in self.shards) != tuple(range(count))
            or tuple(shard.category for shard in self.shards)
            != CHEMBENCH4K_CATEGORIES[:count]
            or self.accepted_category_count != count
            or self.accepted_task_count != count * 50
            or self.accepted_candidate_completion_count != count * 100
            or self.accepted_reflector_call_count != count * 50
            or self.accepted_core_job_count != count * 150
            or self.accepted_context_resolution_count != count * 150
        ):
            raise ValueError("accepted shards must be the exact closed category prefix")
        identities = {
            (
                shard.config_digest,
                shard.split_digest,
                shard.model_digest,
                shard.executor_digest,
                shard.managed_codex_digest,
            )
            for shard in self.shards
        }
        if len(identities) != 1:
            raise ValueError("accepted category shards are not execution-compatible")
        return self


class ContinuedDiscardedPartialShardReceiptV3(_ClosedModel):
    schema_version: Literal["ContinuedDiscardedPartialShardReceiptV3"] = (
        "ContinuedDiscardedPartialShardReceiptV3"
    )
    parent_run_id: str
    category_index: int = Field(ge=1, le=8)
    category: str
    discard_entire_category: Literal[True] = True
    completed_task_count: int = Field(ge=0, le=49)
    partial_task_ordinal: int = Field(ge=0, le=49)
    partial_task_round_zero_completion_exists: Literal[True] = True
    partial_task_round_one_attempt_exists: Literal[True] = True
    partial_task_round_one_completion_exists: Literal[False] = False
    candidate_attempt_count_discarded: int = Field(ge=2)
    candidate_completion_count_discarded: int = Field(ge=1)
    candidate_attempts_without_completion_discarded: Literal[1] = 1
    reflector_call_count_discarded: int = Field(ge=1)
    core_job_count_discarded: int = Field(ge=3)
    artifact_count_discarded: int = Field(ge=3)
    context_resolution_count_discarded: int = Field(ge=3)
    failure_code: Literal["RUNTIME_SERVICE_HEALTH_INVALID"] = (
        "RUNTIME_SERVICE_HEALTH_INVALID"
    )
    included_in_final_statistics: Literal[False] = False
    used_as_recovery_input: Literal[False] = False
    artifacts_imported: Literal[False] = False
    database_imported: Literal[False] = False
    actual_paid_usage_retained: Literal[True] = True
    parent_evidence_preserved: Literal[True] = True
    category_tree_sha256: str

    @model_validator(mode="after")
    def _counts(self) -> ContinuedDiscardedPartialShardReceiptV3:
        reflected = self.completed_task_count + 1
        if (
            self.partial_task_ordinal != self.completed_task_count
            or self.candidate_completion_count_discarded
            != self.completed_task_count * 2 + 1
            or self.candidate_attempt_count_discarded
            != self.candidate_completion_count_discarded + 1
            or self.reflector_call_count_discarded != reflected
            or self.core_job_count_discarded != reflected * 3
            or self.artifact_count_discarded != reflected * 3
            or self.context_resolution_count_discarded != reflected * 3
        ):
            raise ValueError("discarded partial category counts are inconsistent")
        return self

    @field_validator("category_tree_sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        if len(value) != _SHA256_LENGTH or any(
            char not in "0123456789abcdef" for char in value
        ):
            raise ValueError("discarded category tree digest must be SHA-256")
        return value


class ContinuedCategoryRecoveryAmendmentV3(_ClosedModel):
    schema_version: Literal["ContinuedCategoryRecoveryAmendmentV3"] = (
        "ContinuedCategoryRecoveryAmendmentV3"
    )
    original_protocol_id: Literal[
        "chembench_supervised_transfer_v3_two_round_one_evolution_online_only"
    ] = TWO_ROUND_ONE_EVOLUTION_PROTOCOL_ID
    recovery_protocol_id: Literal[
        "supervised_transfer_v3_two_round_one_evolution_chained_category_shard_recovery"
    ] = CONTINUED_CATEGORY_RECOVERY_PROTOCOL_ID
    amendment_reason: Literal["RUNTIME_SERVICE_HEALTH_INVALID"] = (
        "RUNTIME_SERVICE_HEALTH_INVALID"
    )
    authorization: Literal["explicit_user_authorization"] = "explicit_user_authorization"
    parent_run_id: str
    recovery_run_id: str
    accepted_parent_categories: tuple[str, ...]
    discarded_parent_category: str
    start_category_index: int = Field(ge=1, le=8)
    start_category: str
    start_task_ordinal: Literal[0] = 0
    start_round: Literal[0] = 0
    generation_zero_targets: Literal[True] = True
    parent_artifacts_imported: Literal[False] = False
    parent_database_imported: Literal[False] = False
    category_shard_recovery: Literal[True] = True
    partial_parent_shard_fully_discarded: Literal[True] = True
    uninterrupted_single_process_run: Literal[False] = False

    @model_validator(mode="after")
    def _boundary(self) -> ContinuedCategoryRecoveryAmendmentV3:
        if (
            self.accepted_parent_categories
            != CHEMBENCH4K_CATEGORIES[: self.start_category_index]
            or self.discarded_parent_category
            != CHEMBENCH4K_CATEGORIES[self.start_category_index]
            or self.start_category != self.discarded_parent_category
        ):
            raise ValueError("continued recovery amendment boundary is inconsistent")
        return self


class ContinuedRemainingShardExecutionPlanV3(_ClosedModel):
    schema_version: Literal["ContinuedRemainingShardExecutionPlanV3"] = (
        "ContinuedRemainingShardExecutionPlanV3"
    )
    parent_run_id: str
    recovery_run_id: str
    start_category_index: int = Field(ge=1, le=8)
    categories: tuple[str, ...]
    first_category: str
    first_task_ordinal: Literal[0] = 0
    first_round: Literal[0] = 0
    generation_zero_targets: Literal[True] = True
    train_task_count: int = Field(ge=50, le=400)
    train_candidate_calls: int = Field(ge=100, le=800)
    reflector_calls: int = Field(ge=50, le=400)
    core_jobs: int = Field(ge=150, le=1200)
    typed_artifacts: int = Field(ge=150, le=1200)
    context_resolutions: int = Field(ge=150, le=1200)
    answer_and_reflector_model_calls: int = Field(ge=150, le=1200)
    parent_artifacts_imported: Literal[False] = False
    parent_database_imported: Literal[False] = False

    @model_validator(mode="after")
    def _order_and_counts(self) -> ContinuedRemainingShardExecutionPlanV3:
        remaining = 9 - self.start_category_index
        if (
            self.categories != CHEMBENCH4K_CATEGORIES[self.start_category_index :]
            or self.first_category != self.categories[0]
            or self.train_task_count != remaining * 50
            or self.train_candidate_calls != remaining * 100
            or self.reflector_calls != remaining * 50
            or self.core_jobs != remaining * 150
            or self.typed_artifacts != remaining * 150
            or self.context_resolutions != remaining * 150
            or self.answer_and_reflector_model_calls != remaining * 150
        ):
            raise ValueError("continued recovery remaining plan is inconsistent")
        return self


class ContinuedFinalShardCompositionReceiptV3(_ClosedModel):
    schema_version: Literal["ContinuedFinalShardCompositionReceiptV3"] = (
        "ContinuedFinalShardCompositionReceiptV3"
    )
    protocol_id: str = CONTINUED_CATEGORY_RECOVERY_PROTOCOL_ID
    categories: tuple[SameProfileShardCompositionEntryV3, ...]
    accepted_parent_category_count: int = Field(ge=1, le=8)
    parent_partial_candidate_completions_discarded: int = Field(ge=1)
    parent_partial_reflector_calls_discarded: int = Field(ge=1)
    recovery_train_candidate_calls_included: int = Field(ge=100)
    recovery_train_reflector_calls_included: int = Field(ge=50)
    category_shard_recovery: Literal[True] = True
    partial_parent_shard_fully_discarded: Literal[True] = True
    uninterrupted_single_process_run: Literal[False] = False
    planned_composed_final_test_calls: Literal[450] = TEST_COUNT

    @model_validator(mode="after")
    def _identity(self) -> ContinuedFinalShardCompositionReceiptV3:
        if (
            tuple(item.category_index for item in self.categories) != tuple(range(9))
            or tuple(item.category for item in self.categories)
            != CHEMBENCH4K_CATEGORIES
            or self.recovery_train_candidate_calls_included
            != (9 - self.accepted_parent_category_count) * 100
            or self.recovery_train_reflector_calls_included
            != (9 - self.accepted_parent_category_count) * 50
        ):
            raise ValueError("continued composition must contain all categories exactly once")
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
            raise ValueError("continued category shards are not execution-compatible")
        return self


@dataclass(frozen=True, slots=True)
class ContinuedAuditedParentShardsV3:
    repository_root: Path
    parent_run_id: str
    parent_source_commit: str
    start_category_index: int
    parent_state_root: Path
    parent_result_root: Path
    parent_tree_sha256: str
    parent_tree_file_count: int
    parent_tree_size_bytes: int
    accepted: ContinuedAcceptedClosedShardsReceiptV3
    discarded: ContinuedDiscardedPartialShardReceiptV3


def continued_remaining_categories_v3(start_category_index: int) -> tuple[str, ...]:
    if type(start_category_index) is not int or not 1 <= start_category_index <= 8:
        raise SupervisedExperimentV3Error("CONTINUED_RECOVERY_START_INDEX_INVALID")
    return CHEMBENCH4K_CATEGORIES[start_category_index:]


def _load_parent_paid_plan(result_root: Path) -> dict[str, Any]:
    candidates = (
        result_root / "public/continued_category_shard_paid_execution_plan_v3.json",
        result_root / "public/same_profile_category_shard_paid_execution_plan_v3.json",
    )
    existing = [path for path in candidates if path.is_file()]
    if len(existing) != 1:
        raise SupervisedExperimentV3Error("CONTINUED_PARENT_PAID_PLAN_INVALID")
    return _read_json(existing[0])


def _load_inherited_prefix(
    result_root: Path,
) -> tuple[ContinuedAcceptedCategoryShardV3, ...]:
    continued_path = result_root / "public/continued_accepted_closed_shards_receipt_v3.json"
    if continued_path.is_file():
        receipt = ContinuedAcceptedClosedShardsReceiptV3.model_validate(
            _read_json(continued_path)
        )
        return receipt.shards
    original = SameProfileAcceptedClosedShardsReceiptV3.model_validate(
        _read_json(result_root / "public/same_profile_accepted_closed_shards_receipt_v3.json")
    )
    return tuple(
        ContinuedAcceptedCategoryShardV3(
            category_index=shard.category_index,
            category=shard.category,
            source_run_id=shard.parent_run_id,
            source_commit=shard.source_commit,
            config_digest=shard.config_digest,
            split_digest=shard.split_digest,
            model_digest=shard.model_digest,
            executor_digest=shard.executor_digest,
            managed_codex_digest=shard.managed_codex_digest,
            category_tree_sha256=shard.category_tree_sha256,
            final_head_set_sha256=shard.final_head_set_sha256,
            closed_evidence_sha256=shard.digest,
        )
        for shard in original.shards
    )


def _audit_closed_category(
    *,
    state_root: Path,
    state: dict[str, Any],
    paid: dict[str, Any],
    public_events: list[dict[str, Any]],
    private_events: list[dict[str, Any]],
    source_run_id: str,
    category_index: int,
    category: str,
) -> ContinuedAcceptedCategoryShardV3:
    category_root = state_root / "private/core/formal" / category
    rows = _read_jsonl(category_root / "private_lineage_checkpoints_v2.jsonl")
    results = _validate_checkpoint_chain(rows, category=category, expected_tasks=50)
    final = results[-1]
    if final.task_index != 49 or final.update_index != 1 or final.global_update_ordinal != 50:
        raise SupervisedExperimentV3Error("CONTINUED_CLOSED_FINAL_HEAD_INVALID")
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
        raise SupervisedExperimentV3Error("CONTINUED_CLOSED_EVENT_COUNTS_INVALID")
    for task_ordinal in range(50):
        if sorted(
            row.get("round_index")
            for row in evaluations
            if row.get("task_ordinal") == task_ordinal
        ) != [0, 1] or [
            row.get("cycle") for row in cycles if row.get("task_index") == task_ordinal
        ] != [1]:
            raise SupervisedExperimentV3Error("CONTINUED_CLOSED_TASK_INVALID")
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
    category_tree_sha256 = _tree_identity((category_root,))[0]
    final_head_set_sha256 = sha256_bytes(canonical_json_bytes(target_receipts))
    closed_evidence_sha256 = sha256_bytes(
        canonical_json_bytes(
            {
                "category": category,
                "category_index": category_index,
                "category_tree_sha256": category_tree_sha256,
                "final_head_set_sha256": final_head_set_sha256,
                "candidate_completion_count": 100,
                "reflector_call_count": 50,
                "core_job_count": 150,
            }
        )
    )
    return ContinuedAcceptedCategoryShardV3(
        category_index=category_index,
        category=category,
        source_run_id=source_run_id,
        source_commit=str(state["source_commit"]),
        config_digest=str(state["config_sha256"]),
        split_digest=str(state["split_receipt_sha256"]),
        model_digest=_model_digest(paid),
        executor_digest=_executor_digest(paid),
        managed_codex_digest=str(state["reflector_codex_executable_sha256"]),
        category_tree_sha256=category_tree_sha256,
        final_head_set_sha256=final_head_set_sha256,
        closed_evidence_sha256=closed_evidence_sha256,
    )


def _audit_discarded_current_category(
    *,
    state_root: Path,
    public_events: list[dict[str, Any]],
    private_events: list[dict[str, Any]],
    parent_run_id: str,
    category_index: int,
) -> ContinuedDiscardedPartialShardReceiptV3:
    category = CHEMBENCH4K_CATEGORIES[category_index]
    category_root = state_root / "private/core/formal" / category
    rows = _read_jsonl(category_root / "private_lineage_checkpoints_v2.jsonl")
    reflected_task_count = len(rows)
    if not 1 <= reflected_task_count <= 49:
        raise SupervisedExperimentV3Error("CONTINUED_DISCARDED_CHECKPOINT_COUNT_INVALID")
    results = _validate_checkpoint_chain(
        rows,
        category=category,
        expected_tasks=reflected_task_count,
    )
    connection = _open_immutable_database(category_root / "evolution.sqlite3")
    try:
        _require_database_counts(
            connection,
            expected_states={"succeeded": reflected_task_count * 3},
            dataset_count=reflected_task_count,
            typed_target_count=reflected_task_count,
            context_count=reflected_task_count * 3,
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
    attempts = _category_events(
        private_events,
        kind="TASK_MODEL_ATTEMPTED",
        category=category,
    )
    completed_task_count = reflected_task_count - 1
    for task_ordinal in range(completed_task_count):
        if sorted(
            row.get("round_index")
            for row in evaluations
            if row.get("task_ordinal") == task_ordinal
        ) != [0, 1] or [
            row.get("cycle") for row in cycles if row.get("task_index") == task_ordinal
        ] != [1]:
            raise SupervisedExperimentV3Error("CONTINUED_DISCARDED_PREFIX_INVALID")
    partial = completed_task_count
    if (
        [
            row.get("round_index")
            for row in evaluations
            if row.get("task_ordinal") == partial
        ]
        != [0]
        or [
            row.get("cycle") for row in cycles if row.get("task_index") == partial
        ]
        != [1]
        or sorted(
            row.get("round_index")
            for row in attempts
            if row.get("task_ordinal") == partial
        )
        != [0, 1]
        or len(evaluations) != completed_task_count * 2 + 1
        or len(cycles) != reflected_task_count
        or len(attempts) != len(evaluations) + 1
    ):
        raise SupervisedExperimentV3Error("CONTINUED_DISCARDED_BOUNDARY_INVALID")
    return ContinuedDiscardedPartialShardReceiptV3(
        parent_run_id=parent_run_id,
        category_index=category_index,
        category=category,
        completed_task_count=completed_task_count,
        partial_task_ordinal=partial,
        candidate_attempt_count_discarded=len(attempts),
        candidate_completion_count_discarded=len(evaluations),
        reflector_call_count_discarded=len(cycles),
        core_job_count_discarded=len(cycles) * 3,
        artifact_count_discarded=len(cycles) * 3,
        context_resolution_count_discarded=len(cycles) * 3,
        category_tree_sha256=_tree_identity((category_root,))[0],
    )


def audit_continued_parent_category_shards_v3(
    *,
    repository_root: Path,
    parent_run_id: str = CONTINUED_PARENT_RUN_ID,
    start_category_index: int = CONTINUED_START_CATEGORY_INDEX,
) -> ContinuedAuditedParentShardsV3:
    """Audit a failed recovery parent and accept only its closed prefix."""

    continued_remaining_categories_v3(start_category_index)
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
        raise SupervisedExperimentV3Error("CONTINUED_PARENT_PROCESS_ACTIVE")
    state = _read_json(state_root / "run_state.json")
    paid = _load_parent_paid_plan(result_root)
    expected_category = CHEMBENCH4K_CATEGORIES[start_category_index]
    if (
        state.get("run_id") != parent_run_id
        or state.get("status") != "FAIL_CLOSED"
        or state.get("stage") != "ONLINE_TRAIN"
        or state.get("category") != expected_category
        or state.get("failure_code") != "RUNTIME_SERVICE_HEALTH_INVALID"
        or state.get("attempts_per_train_task") != 2
        or state.get("evolution_cycles_per_train_task") != 1
        or state.get("security_findings") != 0
        or state.get("context_findings") != 0
        or state.get("artifact_findings") != 0
        or state.get("results_reusable") is not False
        or state.get("resume_allowed") is not False
        or state.get("parent_artifacts_imported") is not False
        or state.get("parent_database_imported") is not False
        or paid.get("run_id") != parent_run_id
        or paid.get("source_commit") != state.get("source_commit")
        or paid.get("config_sha256") != state.get("config_sha256")
        or paid.get("split_reference_sha256") != state.get("split_receipt_sha256")
        or paid.get("model") is None
        or paid.get("reasoning_effort") is None
        or state.get("candidate_codex_executable_sha256")
        != EXPECTED_MANAGED_CODEX_SHA256
        or state.get("reflector_codex_executable_sha256")
        != EXPECTED_MANAGED_CODEX_SHA256
    ):
        raise SupervisedExperimentV3Error("CONTINUED_PARENT_IDENTITY_INVALID")
    public_events = _read_jsonl(result_root / "public/events.jsonl")
    private_events = _read_jsonl(state_root / "private/events.jsonl")
    inherited = _load_inherited_prefix(result_root)
    if len(inherited) >= start_category_index:
        accepted_shards = inherited[:start_category_index]
    else:
        accepted_shards = (
            *inherited,
            *(
                _audit_closed_category(
                    state_root=state_root,
                    state=state,
                    paid=paid,
                    public_events=public_events,
                    private_events=private_events,
                    source_run_id=parent_run_id,
                    category_index=category_index,
                    category=CHEMBENCH4K_CATEGORIES[category_index],
                )
                for category_index in range(len(inherited), start_category_index)
            ),
        )
    accepted = ContinuedAcceptedClosedShardsReceiptV3(
        parent_run_id=parent_run_id,
        start_category_index=start_category_index,
        shards=tuple(accepted_shards),
        accepted_category_count=start_category_index,
        accepted_task_count=start_category_index * 50,
        accepted_candidate_completion_count=start_category_index * 100,
        accepted_reflector_call_count=start_category_index * 50,
        accepted_core_job_count=start_category_index * 150,
        accepted_context_resolution_count=start_category_index * 150,
    )
    discarded = _audit_discarded_current_category(
        state_root=state_root,
        public_events=public_events,
        private_events=private_events,
        parent_run_id=parent_run_id,
        category_index=start_category_index,
    )
    later_categories = set(CHEMBENCH4K_CATEGORIES[start_category_index + 1 :])
    if any(
        row.get("stage") == "ONLINE_TRAIN"
        and row.get("category") in later_categories
        and row.get("kind") in {"REFLECTOR_SUPERVISED", "PRIVATE_EVALUATED"}
        for row in (*public_events, *private_events)
    ):
        raise SupervisedExperimentV3Error("CONTINUED_PARENT_LATER_CATEGORY_TOUCHED")
    after = _tree_identity((state_root, result_root))
    if after != before:
        raise SupervisedExperimentV3Error("CONTINUED_PARENT_MUTATED_DURING_AUDIT")
    return ContinuedAuditedParentShardsV3(
        repository_root=repository,
        parent_run_id=parent_run_id,
        parent_source_commit=str(state["source_commit"]),
        start_category_index=start_category_index,
        parent_state_root=state_root,
        parent_result_root=result_root,
        parent_tree_sha256=before[0],
        parent_tree_file_count=before[1],
        parent_tree_size_bytes=before[2],
        accepted=accepted,
        discarded=discarded,
    )


def build_continued_category_recovery_dry_run_v3(
    *,
    inputs: v2.ExperimentInputsV2,
    audit: ContinuedAuditedParentShardsV3,
    recovery_run_id: str,
) -> dict[str, object]:
    if type(inputs) is not v2.ExperimentInputsV2:
        raise TypeError("inputs must be exact ExperimentInputsV2")
    if type(audit) is not ContinuedAuditedParentShardsV3:
        raise TypeError("audit must be exact ContinuedAuditedParentShardsV3")
    _require_same_profile(inputs)
    if _RUN_ID.fullmatch(recovery_run_id) is None:
        raise ValueError("invalid recovery run ID")
    if inputs.repository_root != audit.repository_root:
        raise SupervisedExperimentV3Error("CONTINUED_RECOVERY_REPOSITORY_MISMATCH")
    categories = continued_remaining_categories_v3(audit.start_category_index)
    by_category = v2._by_category(inputs.train)
    schedule: list[dict[str, object]] = []
    ordered_uids: list[str] = []
    for category_index, category in enumerate(
        categories,
        start=audit.start_category_index,
    ):
        tasks = by_category[category]
        if len(tasks) != 50:
            raise SupervisedExperimentV3Error("CONTINUED_RECOVERY_TASK_COUNT_INVALID")
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
    expected_tasks = len(categories) * 50
    if len(ordered_uids) != expected_tasks or len(set(ordered_uids)) != expected_tasks:
        raise SupervisedExperimentV3Error("CONTINUED_RECOVERY_SCHEDULE_INVALID")
    return {
        "schema_version": "ContinuedCategoryShardRecoveryDryRunV3",
        "protocol_id": CONTINUED_CATEGORY_RECOVERY_PROTOCOL_ID,
        "status": "PASS",
        "model_calls": 0,
        "parent_run_id": audit.parent_run_id,
        "recovery_run_id": recovery_run_id,
        "accepted_parent_categories": list(
            CHEMBENCH4K_CATEGORIES[: audit.start_category_index]
        ),
        "discarded_parent_category": categories[0],
        "first_category": categories[0],
        "first_task_ordinal": 0,
        "first_round": 0,
        "generation_zero_targets": True,
        "parent_artifacts_imported": False,
        "parent_database_imported": False,
        "remaining_schedule": schedule,
        "remaining_train_order_sha256": sha256_bytes(
            canonical_json_bytes(ordered_uids)
        ),
        "source_commit": inputs.source_commit,
        "config_sha256": inputs.config.digest,
        "split_reference_sha256": inputs.split_receipt_sha256,
        "source_manifest_sha256": inputs.source_manifest_sha256,
        "candidate_execution_path": "TaskRequest->Rollout->Gateway->CodexHarness",
        "reflector_execution_path": "Core planned job->worker->codex_cli provider",
        "planned_remaining_train_candidate_calls": len(categories) * 100,
        "planned_remaining_train_reflector_calls": len(categories) * 50,
        "planned_remaining_train_core_jobs": len(categories) * 150,
    }


class ContinuedCategoryShardRecoveryV3(SupervisedTransferExperimentV3):
    """Run the current partial category and all later categories from task zero."""

    def __init__(
        self,
        *,
        inputs: v2.ExperimentInputsV2,
        run_id: str,
        smoke_run_id: str,
        parent_audit: ContinuedAuditedParentShardsV3,
        executor_factory: Any | None = None,
        bridge_factory: Any | None = None,
    ) -> None:
        _require_same_profile(inputs)
        if type(parent_audit) is not ContinuedAuditedParentShardsV3:
            raise TypeError("parent_audit must be exact ContinuedAuditedParentShardsV3")
        if parent_audit.repository_root != inputs.repository_root:
            raise SupervisedExperimentV3Error("CONTINUED_RECOVERY_REPOSITORY_MISMATCH")
        self.parent_audit = parent_audit
        self.start_category_index = parent_audit.start_category_index
        self.remaining_categories = continued_remaining_categories_v3(
            self.start_category_index
        )
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
                "schema_version": "ChemBenchContinuedCategoryRecoveryRunStateV3",
                "protocol_id": CONTINUED_CATEGORY_RECOVERY_PROTOCOL_ID,
                "parent_run_id": parent_audit.parent_run_id,
                "start_category_index": self.start_category_index,
                "first_category": self.remaining_categories[0],
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
        try:
            self._verify_parent_tree_unchanged()
            self._verify_smoke_authority()
            with ExitStack() as stack:
                executor = v2._enter_if_context(stack, self._executor_factory("online"))
                self._open_remaining_bridges(stack)
                self._run_remaining_train(executor)
                category_count = len(self.remaining_categories)
                self._require_counts(
                    task_sessions=category_count * 100,
                    reflector_calls=category_count * 50,
                    core_jobs=category_count * 150,
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
            raise SupervisedExperimentV3Error("CONTINUED_RECOVERY_BRIDGE_SET_INVALID")

    def _run_remaining_train(self, executor: v2.TaskExecutorV2) -> None:
        self._set_stage("ONLINE_TRAIN")
        by_category = v2._by_category(self.inputs.train)
        for category in self.remaining_categories:
            bridge = self._bridges[category]
            for task_ordinal, task in enumerate(by_category[category]):
                if task_ordinal == 0 and self._heads[category] is not None:
                    raise SupervisedExperimentV3Error(
                        "CONTINUED_RECOVERY_GENERATION_ZERO_INVALID"
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
            and getattr(task, "category", None) == self.remaining_categories[0]
            and kwargs.get("task_ordinal") == 0
            and kwargs.get("round_index") == 0
            and kwargs.get("context") is not None
        ):
            raise SupervisedExperimentV3Error(
                "CONTINUED_RECOVERY_GENERATION_ZERO_INVALID"
            )
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
            raise SupervisedExperimentV3Error("CONTINUED_PARENT_TREE_DRIFT")

    def _write_recovery_protocol_receipts(self) -> None:
        amendment = ContinuedCategoryRecoveryAmendmentV3(
            parent_run_id=self.parent_audit.parent_run_id,
            recovery_run_id=self.run_id,
            accepted_parent_categories=CHEMBENCH4K_CATEGORIES[
                : self.start_category_index
            ],
            discarded_parent_category=self.remaining_categories[0],
            start_category_index=self.start_category_index,
            start_category=self.remaining_categories[0],
        )
        remaining_count = len(self.remaining_categories)
        plan = ContinuedRemainingShardExecutionPlanV3(
            parent_run_id=self.parent_audit.parent_run_id,
            recovery_run_id=self.run_id,
            start_category_index=self.start_category_index,
            categories=self.remaining_categories,
            first_category=self.remaining_categories[0],
            train_task_count=remaining_count * 50,
            train_candidate_calls=remaining_count * 100,
            reflector_calls=remaining_count * 50,
            core_jobs=remaining_count * 150,
            typed_artifacts=remaining_count * 150,
            context_resolutions=remaining_count * 150,
            answer_and_reflector_model_calls=remaining_count * 150,
        )
        public = self.result_root / "public"
        for name, model in (
            ("continued_category_recovery_amendment_v3.json", amendment),
            ("continued_accepted_closed_shards_receipt_v3.json", self.parent_audit.accepted),
            ("continued_discarded_partial_shard_receipt_v3.json", self.parent_audit.discarded),
            ("continued_remaining_shard_execution_plan_v3.json", plan),
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
        remaining_count = len(self.remaining_categories)
        candidate_calls = remaining_count * 100
        reflector_calls = remaining_count * 50
        answer_and_reflector = candidate_calls + reflector_calls
        payload = {
            "schema_version": "ContinuedCategoryShardRecoveryPaidExecutionPlanV3",
            "protocol_id": CONTINUED_CATEGORY_RECOVERY_PROTOCOL_ID,
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
            "start_category_index": self.start_category_index,
            "categories": list(self.remaining_categories),
            "first_task_ordinal": 0,
            "first_round": 0,
            "generation_zero_targets": True,
            "planned_calls": {
                "remaining_train_candidate_calls": candidate_calls,
                "remaining_train_reflector_calls": reflector_calls,
                "remaining_train_core_jobs": remaining_count * 150,
                "candidate_readiness_calls_minimum": candidate_calls,
                "candidate_readiness_calls_maximum": candidate_calls * 2,
                "answer_and_reflector_model_calls": answer_and_reflector,
                "total_model_calls_minimum": answer_and_reflector + candidate_calls,
                "total_model_calls_maximum": answer_and_reflector + candidate_calls * 2,
                "eventual_final_test_candidate_calls": TEST_COUNT,
            },
            "parent_usage": {
                "closed_shard_candidate_completions_included": (
                    self.start_category_index * 100
                ),
                "closed_shard_reflector_calls_included": (
                    self.start_category_index * 50
                ),
                "discarded_candidate_completions": (
                    self.parent_audit.discarded.candidate_completion_count_discarded
                ),
                "discarded_candidate_attempts_without_completion": 1,
                "discarded_successful_reflector_calls": (
                    self.parent_audit.discarded.reflector_call_count_discarded
                ),
            },
            "candidate_execution_path": "TaskRequest->Rollout->Gateway->CodexHarness",
            "reflector_execution_path": "Core planned job->worker->codex_cli provider",
            "candidate_codex_identity_sha256": self.inputs.candidate_codex.digest,
            "candidate_codex_executable_sha256": (
                self.inputs.candidate_codex.executable_sha256
            ),
            "reflector_codex_identity_sha256": self.inputs.managed_codex.digest,
            "reflector_codex_executable_sha256": (
                self.inputs.managed_codex.executable_sha256
            ),
            "runtime_services_identity_sha256": self.inputs.runtime_services.digest,
            "parent_artifacts_imported": False,
            "parent_database_imported": False,
            "written_before_first_model_call": True,
            "issued_at_utc": v2.utc_now(),
        }
        write_public_file(
            self.result_root / "public/continued_category_shard_paid_execution_plan_v3.json",
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
            if category_index < self.start_category_index:
                continue
            head = self._heads[category]
            if (
                head is None
                or head.task_index != 49
                or head.update_index != 1
                or head.global_update_ordinal != 50
            ):
                raise SupervisedExperimentV3Error(
                    "CONTINUED_RECOVERY_FINAL_HEAD_INVALID"
                )
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
                raise SupervisedExperimentV3Error(
                    "CONTINUED_RECOVERY_EVENT_COUNTS_INVALID"
                )
            closed = {
                "schema_version": "ContinuedClosedRecoveryCategoryShardReceiptV3",
                "protocol_id": CONTINUED_CATEGORY_RECOVERY_PROTOCOL_ID,
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
            path = self.result_root / "public" / f"continued_closed_shard_{category}_v3.json"
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
    ) -> ContinuedFinalShardCompositionReceiptV3:
        accepted_entries = tuple(
            SameProfileShardCompositionEntryV3(
                category=shard.category,
                category_index=shard.category_index,
                source_run_id=shard.source_run_id,
                source_commit=shard.source_commit,
                config_digest=shard.config_digest,
                split_digest=shard.split_digest,
                model_digest=shard.model_digest,
                executor_digest=shard.executor_digest,
                managed_codex_digest=shard.managed_codex_digest,
                closed_receipt_sha256=shard.closed_evidence_sha256,
            )
            for shard in self.parent_audit.accepted.shards
        )
        receipt = ContinuedFinalShardCompositionReceiptV3(
            categories=(*accepted_entries, *remaining),
            accepted_parent_category_count=self.start_category_index,
            parent_partial_candidate_completions_discarded=(
                self.parent_audit.discarded.candidate_completion_count_discarded
            ),
            parent_partial_reflector_calls_discarded=(
                self.parent_audit.discarded.reflector_call_count_discarded
            ),
            recovery_train_candidate_calls_included=(
                len(self.remaining_categories) * 100
            ),
            recovery_train_reflector_calls_included=(
                len(self.remaining_categories) * 50
            ),
        )
        write_public_file(
            self.result_root / "public/continued_final_shard_composition_receipt_v3.json",
            canonical_pretty_json_bytes(receipt.model_dump(mode="json")),
        )
        return receipt


def _model_digest_from_inputs(
    experiment: ContinuedCategoryShardRecoveryV3,
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
    experiment: ContinuedCategoryShardRecoveryV3,
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


def build_continued_source_compatibility_receipt_v3(
    *,
    audit: ContinuedAuditedParentShardsV3,
    recovery_run_id: str,
) -> dict[str, object]:
    repository = audit.repository_root
    parent_commit = audit.parent_source_commit
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
            raise SupervisedExperimentV3Error(
                "RECOVERY_SOURCE_NOT_SEMANTICALLY_COMPATIBLE"
            )
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
        (
            "benchmarks/chembench/configs/supervised_transfer_v3/"
            "continued_category_shard_recovery.md"
        ),
        "benchmarks/chembench/manifests/supervised_transfer_v3/source_manifest_v3.json",
        "benchmarks/chembench/scripts/supervised_transfer_v3/main.py",
        (
            "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/"
            "continued_category_shard_recovery.py"
        ),
        (
            "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/"
            "source_identity.py"
        ),
        (
            "benchmarks/chembench/tests/supervised_transfer_v3/"
            "test_continued_category_shard_recovery_v3.py"
        ),
    }
    if set(changed) - allowed_changed:
        raise SupervisedExperimentV3Error(
            "RECOVERY_SOURCE_NOT_SEMANTICALLY_COMPATIBLE"
        )
    return {
        "schema_version": "ContinuedCategoryShardRecoveryCompatibilityReceiptV3",
        "parent_run_id": audit.parent_run_id,
        "recovery_run_id": recovery_run_id,
        "parent_source_commit": parent_commit,
        "recovery_source_commit": recovery_commit,
        "git_diff_sha256": sha256_bytes(diff),
        "files_changed": changed,
        "unchanged_semantic_inputs": unchanged,
        "allowed_changes": [
            "category_start_selection",
            "chained_category_shard_orchestration",
            "shard_receipts_and_composition",
        ],
        "candidate_execution_path_unchanged": True,
        "reflector_execution_path_unchanged": True,
        "category_internal_protocol_unchanged": True,
        "model_prompt_evaluator_answer_parser_artifact_semantics_unchanged": True,
        "semantically_compatible": True,
    }


__all__ = [
    "CONTINUED_CATEGORY_RECOVERY_PROTOCOL_ID",
    "CONTINUED_PARENT_RUN_ID",
    "CONTINUED_START_CATEGORY_INDEX",
    "ContinuedAcceptedCategoryShardV3",
    "ContinuedAcceptedClosedShardsReceiptV3",
    "ContinuedAuditedParentShardsV3",
    "ContinuedCategoryRecoveryAmendmentV3",
    "ContinuedCategoryShardRecoveryV3",
    "ContinuedDiscardedPartialShardReceiptV3",
    "ContinuedFinalShardCompositionReceiptV3",
    "ContinuedRemainingShardExecutionPlanV3",
    "audit_continued_parent_category_shards_v3",
    "build_continued_category_recovery_dry_run_v3",
    "build_continued_source_compatibility_receipt_v3",
    "continued_remaining_categories_v3",
]
