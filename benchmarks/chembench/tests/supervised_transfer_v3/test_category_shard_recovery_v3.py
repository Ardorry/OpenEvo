from __future__ import annotations

from pathlib import Path

import pytest

from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
from openevo_chembench.supervised_transfer_v3.category_shard_recovery import (
    ACCEPTED_CATEGORY,
    DISCARDED_CATEGORY,
    PARENT_RUN_ID,
    REMAINING_CATEGORIES,
    AcceptedClosedShardReceiptV3,
    DiscardedPartialShardReceiptV3,
    FinalShardCompositionReceiptV3,
    RemainingShardExecutionPlanV3,
    ShardCompositionEntryV3,
    SupervisedTransferCategoryShardRecoveryV3,
    audit_parent_category_shards_v3,
    build_category_shard_recovery_dry_run_v3,
    remaining_categories_v3,
)
from openevo_chembench.supervised_transfer_v3.config import load_config_v3
from openevo_chembench.supervised_transfer_v3.experiment import (
    SupervisedTransferExperimentV3,
    load_experiment_inputs_v3,
)

REPOSITORY = Path(__file__).resolve().parents[4]
PARENT_STATE = (
    REPOSITORY / "state/chembench_supervised_transfer_v3/runs" / PARENT_RUN_ID / "run_state.json"
)


@pytest.fixture(scope="module")
def live_parent_audit():
    if not PARENT_STATE.is_file():
        pytest.skip("immutable crash parent is not available in this checkout")
    return audit_parent_category_shards_v3(repository_root=REPOSITORY)


@pytest.fixture(scope="module")
def live_inputs():
    config = load_config_v3(
        REPOSITORY
        / "benchmarks/chembench/configs/supervised_transfer_v3/chembench_supervised_transfer_v3.yaml"
    )
    return load_experiment_inputs_v3(REPOSITORY, config, require_runtime=False)


def test_closed_parent_category_is_accepted(live_parent_audit) -> None:
    receipt = live_parent_audit.accepted
    assert type(receipt) is AcceptedClosedShardReceiptV3
    assert receipt.category == ACCEPTED_CATEGORY
    assert receipt.task_start == 0
    assert receipt.task_end == 49
    assert receipt.candidate_completion_count == 150
    assert receipt.reflector_call_count == 100
    assert receipt.core_job_count == 300
    assert receipt.context_resolution_count == 300
    assert receipt.final_category_state_closed is True
    assert receipt.category_state_independent is True


def test_partial_parent_category_is_fully_discarded(live_parent_audit) -> None:
    receipt = live_parent_audit.discarded
    assert type(receipt) is DiscardedPartialShardReceiptV3
    assert receipt.category == DISCARDED_CATEGORY
    assert receipt.discard_entire_category is True
    assert receipt.completed_candidate_calls_discarded == 76
    assert receipt.completed_reflector_calls_discarded == 51
    assert receipt.used_as_recovery_input is False
    assert receipt.included_in_final_statistics is False


def test_recovery_starts_at_category_one_task_zero(live_inputs, live_parent_audit) -> None:
    plan = RemainingShardExecutionPlanV3(
        parent_run_id=PARENT_RUN_ID,
        recovery_run_id="stv3-category-recovery-20990101T000000Z",
        categories=REMAINING_CATEGORIES,
    )
    assert plan.start_category_index == 1
    assert plan.first_category == DISCARDED_CATEGORY
    assert plan.first_task_ordinal == 0
    assert plan.first_round == 0
    dry_run = build_category_shard_recovery_dry_run_v3(
        inputs=live_inputs,
        audit=live_parent_audit,
        recovery_run_id=plan.recovery_run_id,
    )
    assert dry_run["model_calls"] == 0
    assert dry_run["first_category"] == DISCARDED_CATEGORY
    assert dry_run["first_task_ordinal"] == 0
    assert dry_run["first_round"] == 0


def test_generation_zero_targets_used(live_inputs, live_parent_audit) -> None:
    plan = RemainingShardExecutionPlanV3(
        parent_run_id=PARENT_RUN_ID,
        recovery_run_id="stv3-category-recovery-20990101T000001Z",
        categories=REMAINING_CATEGORIES,
    )
    assert plan.generation_zero_targets is True
    source = Path(SupervisedTransferCategoryShardRecoveryV3.__module__.replace(".", "/") + ".py")
    assert "generation_zero_targets" in (
        REPOSITORY / "benchmarks/chembench/src" / source
    ).read_text(encoding="utf-8")
    dry_run = build_category_shard_recovery_dry_run_v3(
        inputs=live_inputs,
        audit=live_parent_audit,
        recovery_run_id=plan.recovery_run_id,
    )
    assert dry_run["generation_zero_targets"] is True


def test_no_parent_artifact_import() -> None:
    source = (
        REPOSITORY
        / "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/category_shard_recovery.py"
    ).read_text(encoding="utf-8")
    assert "register_artifact(" not in source
    assert "TaskwiseCoreRecoverySeed" not in source
    assert 'parent_artifacts_imported": True' not in source


def test_no_parent_database_copy() -> None:
    source = (
        REPOSITORY
        / "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/category_shard_recovery.py"
    ).read_text(encoding="utf-8")
    assert "ATTACH DATABASE" not in source.upper()
    assert "copyfile" not in source
    assert "copytree" not in source
    assert 'parent_database_imported": True' not in source


def test_remaining_category_order(live_inputs, live_parent_audit) -> None:
    assert remaining_categories_v3() == CHEMBENCH4K_CATEGORIES[1:]
    assert remaining_categories_v3()[0] == DISCARDED_CATEGORY
    assert len(remaining_categories_v3()) == 8
    dry_run = build_category_shard_recovery_dry_run_v3(
        inputs=live_inputs,
        audit=live_parent_audit,
        recovery_run_id="stv3-category-recovery-20990101T000002Z",
    )
    schedule = dry_run["remaining_schedule"]
    assert [row["category"] for row in schedule] == list(CHEMBENCH4K_CATEGORIES[1:])
    assert all(row["task_start"] == 0 and row["task_end"] == 49 for row in schedule)


def test_default_v3_execution_unchanged() -> None:
    assert SupervisedTransferCategoryShardRecoveryV3 is not SupervisedTransferExperimentV3
    original = (
        REPOSITORY
        / "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/experiment.py"
    ).read_text(encoding="utf-8")
    assert "start_category_index" not in original
    assert "for category in CHEMBENCH4K_CATEGORIES:" in original


def test_final_shard_composition() -> None:
    common = {
        "source_commit": "1" * 40,
        "config_digest": "2" * 64,
        "split_digest": "3" * 64,
        "model_digest": "4" * 64,
        "executor_digest": "5" * 64,
        "managed_codex_digest": "6" * 64,
    }
    categories = tuple(
        ShardCompositionEntryV3(
            category=category,
            category_index=index,
            source_run_id=("parent-run-0001" if index == 0 else "recovery-run-0001"),
            closed_receipt_sha256=f"{index + 1:x}" * 64,
            **common,
        )
        for index, category in enumerate(CHEMBENCH4K_CATEGORIES)
    )
    receipt = FinalShardCompositionReceiptV3(categories=categories)
    assert len(receipt.categories) == 9
    assert receipt.categories[0].source_run_id == "parent-run-0001"
    assert all(item.included_in_statistics for item in receipt.categories)


def test_paid_usage_counts_include_discarded_calls() -> None:
    common = {
        "source_commit": "1" * 40,
        "config_digest": "2" * 64,
        "split_digest": "3" * 64,
        "model_digest": "4" * 64,
        "executor_digest": "5" * 64,
        "managed_codex_digest": "6" * 64,
    }
    categories = tuple(
        ShardCompositionEntryV3(
            category=category,
            category_index=index,
            source_run_id=f"source-run-{index:04d}",
            closed_receipt_sha256=f"{index + 1:x}" * 64,
            **common,
        )
        for index, category in enumerate(CHEMBENCH4K_CATEGORIES)
    )
    receipt = FinalShardCompositionReceiptV3(categories=categories)
    assert receipt.parent_name_conversion_calls_included == 250
    assert receipt.parent_property_prediction_calls_discarded == 127
    assert receipt.new_recovery_train_calls_included == 2000
    assert receipt.planned_composed_final_test_calls == 450
    assert receipt.new_recovery_calls_in_final_result_after_test == 2450
    assert receipt.total_actual_calls_at_train_composition_minimum == 2377
    assert receipt.projected_total_actual_calls_after_final_test_minimum == 2827
    assert receipt.total_calls_in_train_shard_composition == 2250
    assert receipt.total_calls_in_final_result_after_test == 2700
