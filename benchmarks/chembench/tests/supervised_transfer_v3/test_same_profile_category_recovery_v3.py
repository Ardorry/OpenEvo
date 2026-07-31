from __future__ import annotations

from pathlib import Path

import pytest

from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
from openevo_chembench.supervised_transfer_v3.config import load_config_v3
from openevo_chembench.supervised_transfer_v3.experiment import (
    SupervisedTransferExperimentV3,
    load_experiment_inputs_v3,
)
from openevo_chembench.supervised_transfer_v3.same_profile_category_recovery import (
    SAME_PROFILE_ACCEPTED_CATEGORIES,
    SAME_PROFILE_DISCARDED_CATEGORY,
    SAME_PROFILE_PARENT_RUN_ID,
    SAME_PROFILE_REMAINING_CATEGORIES,
    SameProfileAcceptedClosedShardsReceiptV3,
    SameProfileCategoryShardRecoveryV3,
    SameProfileDiscardedPartialShardReceiptV3,
    SameProfileFinalShardCompositionReceiptV3,
    SameProfileRemainingShardExecutionPlanV3,
    SameProfileShardCompositionEntryV3,
    audit_same_profile_parent_category_shards_v3,
    build_same_profile_category_recovery_dry_run_v3,
    same_profile_remaining_categories_v3,
)

REPOSITORY = Path(__file__).resolve().parents[4]
PARENT_STATE = (
    REPOSITORY
    / "state/chembench_supervised_transfer_v3/runs"
    / SAME_PROFILE_PARENT_RUN_ID
    / "run_state.json"
)
CONFIG_PATH = (
    REPOSITORY / "benchmarks/chembench/configs/supervised_transfer_v3/"
    "chembench_supervised_transfer_v3_two_round_one_evolution.yaml"
)


@pytest.fixture(scope="module")
def live_parent_audit():
    if not PARENT_STATE.is_file():
        pytest.skip("immutable same-profile parent is unavailable")
    return audit_same_profile_parent_category_shards_v3(repository_root=REPOSITORY)


@pytest.fixture(scope="module")
def live_inputs():
    return load_experiment_inputs_v3(
        REPOSITORY,
        load_config_v3(CONFIG_PATH),
        require_runtime=False,
    )


def test_five_closed_parent_categories_are_accepted(live_parent_audit) -> None:
    receipt = live_parent_audit.accepted
    assert type(receipt) is SameProfileAcceptedClosedShardsReceiptV3
    assert tuple(shard.category for shard in receipt.shards) == (SAME_PROFILE_ACCEPTED_CATEGORIES)
    assert tuple(shard.category_index for shard in receipt.shards) == tuple(range(5))
    assert all(shard.candidate_completion_count == 100 for shard in receipt.shards)
    assert all(shard.reflector_call_count == 50 for shard in receipt.shards)
    assert all(shard.core_job_count == 150 for shard in receipt.shards)
    assert all(shard.context_resolution_count == 150 for shard in receipt.shards)
    assert all(shard.final_category_state_closed for shard in receipt.shards)
    assert all(shard.category_state_independent for shard in receipt.shards)


def test_current_parent_category_is_fully_discarded(live_parent_audit) -> None:
    receipt = live_parent_audit.discarded
    assert type(receipt) is SameProfileDiscardedPartialShardReceiptV3
    assert receipt.category == SAME_PROFILE_DISCARDED_CATEGORY
    assert receipt.discard_entire_category is True
    assert receipt.completed_candidate_calls_discarded == 17
    assert receipt.successful_reflector_calls_discarded == 8
    assert receipt.failed_reflector_invocations_discarded == 1
    assert len(receipt.failed_reflector_receipt_sha256) == 64
    assert len(receipt.failed_reflector_event_stream_sha256) == 64
    assert receipt.completed_response_after_timeout_notice is True
    assert receipt.used_as_recovery_input is False
    assert receipt.artifacts_imported is False
    assert receipt.database_imported is False


def test_recovery_starts_at_stream_five_task_zero(
    live_inputs,
    live_parent_audit,
) -> None:
    plan = SameProfileRemainingShardExecutionPlanV3(
        parent_run_id=SAME_PROFILE_PARENT_RUN_ID,
        recovery_run_id="stv3-same-profile-recovery-20990101T000000Z",
        categories=SAME_PROFILE_REMAINING_CATEGORIES,
    )
    dry_run = build_same_profile_category_recovery_dry_run_v3(
        inputs=live_inputs,
        audit=live_parent_audit,
        recovery_run_id=plan.recovery_run_id,
    )
    assert plan.start_category_index == 5
    assert plan.first_category == "Retrosynthesis"
    assert plan.first_task_ordinal == 0
    assert plan.first_round == 0
    assert dry_run["first_category"] == "Retrosynthesis"
    assert dry_run["first_task_ordinal"] == 0
    assert dry_run["first_round"] == 0
    assert dry_run["generation_zero_targets"] is True
    assert dry_run["model_calls"] == 0


def test_no_parent_artifact_or_database_import() -> None:
    source = (
        REPOSITORY / "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/"
        "same_profile_category_recovery.py"
    ).read_text(encoding="utf-8")
    assert "register_artifact(" not in source
    assert "TaskwiseCoreRecoverySeed" not in source
    assert "ATTACH DATABASE" not in source.upper()
    assert "copyfile" not in source
    assert "copytree" not in source
    assert 'parent_artifacts_imported": True' not in source
    assert 'parent_database_imported": True' not in source


def test_remaining_category_order(live_inputs, live_parent_audit) -> None:
    assert same_profile_remaining_categories_v3() == CHEMBENCH4K_CATEGORIES[5:]
    assert SAME_PROFILE_REMAINING_CATEGORIES[0] == "Retrosynthesis"
    dry_run = build_same_profile_category_recovery_dry_run_v3(
        inputs=live_inputs,
        audit=live_parent_audit,
        recovery_run_id="stv3-same-profile-recovery-20990101T000001Z",
    )
    schedule = dry_run["remaining_schedule"]
    assert [row["category"] for row in schedule] == list(CHEMBENCH4K_CATEGORIES[5:])
    assert all(row["task_start"] == 0 and row["task_end"] == 49 for row in schedule)
    assert all(row["sessions_per_task"] == 2 for row in schedule)
    assert all(row["cycles_per_task"] == 1 for row in schedule)


def test_two_round_one_evolution_call_plan() -> None:
    plan = SameProfileRemainingShardExecutionPlanV3(
        parent_run_id=SAME_PROFILE_PARENT_RUN_ID,
        recovery_run_id="stv3-same-profile-recovery-20990101T000002Z",
        categories=SAME_PROFILE_REMAINING_CATEGORIES,
    )
    assert plan.train_task_count == 200
    assert plan.train_candidate_calls == 400
    assert plan.reflector_calls == 200
    assert plan.core_jobs == 600
    assert plan.typed_artifacts == 600
    assert plan.context_resolutions == 600
    assert plan.total_model_calls_minimum == 1000
    assert plan.total_model_calls_maximum == 1400


def test_default_and_legacy_v3_execution_are_unchanged() -> None:
    assert SameProfileCategoryShardRecoveryV3 is not SupervisedTransferExperimentV3
    original = (
        REPOSITORY / "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/"
        "experiment.py"
    ).read_text(encoding="utf-8")
    legacy = (
        REPOSITORY / "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/"
        "category_shard_recovery.py"
    ).read_text(encoding="utf-8")
    assert "start_category_index" not in original
    assert "for category in CHEMBENCH4K_CATEGORIES:" in original
    assert "START_CATEGORY_INDEX = 1" in legacy


def test_final_shard_composition_accepts_different_source_commits() -> None:
    common = {
        "config_digest": "2" * 64,
        "split_digest": "3" * 64,
        "model_digest": "4" * 64,
        "executor_digest": "5" * 64,
        "managed_codex_digest": "6" * 64,
    }
    categories = tuple(
        SameProfileShardCompositionEntryV3(
            category=category,
            category_index=index,
            source_run_id=(
                SAME_PROFILE_PARENT_RUN_ID
                if index < 5
                else "stv3-same-profile-recovery-20990101T000003Z"
            ),
            source_commit=("1" * 40 if index < 5 else "7" * 40),
            closed_receipt_sha256=f"{index + 1:x}" * 64,
            **common,
        )
        for index, category in enumerate(CHEMBENCH4K_CATEGORIES)
    )
    receipt = SameProfileFinalShardCompositionReceiptV3(categories=categories)
    assert len(receipt.categories) == 9
    assert receipt.closed_parent_shards_reused == 5
    assert receipt.parent_partial_shard_answer_and_reflector_invocations_discarded == 26
    assert receipt.actual_answer_and_reflector_calls_at_train_composition == 1376
    assert receipt.answer_and_reflector_calls_in_train_result == 1350


def test_parent_tree_is_unchanged_by_read_only_audit(live_parent_audit) -> None:
    repeated = audit_same_profile_parent_category_shards_v3(repository_root=REPOSITORY)
    assert repeated.parent_tree_sha256 == live_parent_audit.parent_tree_sha256
    assert repeated.parent_tree_file_count == live_parent_audit.parent_tree_file_count
    assert repeated.parent_tree_size_bytes == live_parent_audit.parent_tree_size_bytes
