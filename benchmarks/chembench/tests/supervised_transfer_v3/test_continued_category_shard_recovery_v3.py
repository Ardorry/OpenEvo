from __future__ import annotations

from pathlib import Path

import pytest

from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
from openevo_chembench.supervised_transfer_v3.config import load_config_v3
from openevo_chembench.supervised_transfer_v3.continued_category_shard_recovery import (
    CONTINUED_PARENT_RUN_ID,
    CONTINUED_START_CATEGORY_INDEX,
    ContinuedAcceptedClosedShardsReceiptV3,
    ContinuedCategoryShardRecoveryV3,
    ContinuedDiscardedArtifactValidationShardReceiptV3,
    ContinuedDiscardedPartialShardReceiptV3,
    ContinuedRemainingShardExecutionPlanV3,
    audit_continued_parent_category_shards_v3,
    build_continued_category_recovery_dry_run_v3,
    continued_remaining_categories_v3,
)
from openevo_chembench.supervised_transfer_v3.experiment import (
    SupervisedTransferExperimentV3,
    load_experiment_inputs_v3,
)
from openevo_chembench.supervised_transfer_v3.same_profile_category_recovery import (
    SAME_PROFILE_REMAINING_CATEGORIES,
    same_profile_remaining_categories_v3,
)

REPOSITORY = Path(__file__).resolve().parents[4]
PARENT_STATE = (
    REPOSITORY
    / "state/chembench_supervised_transfer_v3/runs"
    / CONTINUED_PARENT_RUN_ID
    / "run_state.json"
)
CONFIG_PATH = (
    REPOSITORY / "benchmarks/chembench/configs/supervised_transfer_v3/"
    "chembench_supervised_transfer_v3_two_round_one_evolution.yaml"
)
ARTIFACT_FAILURE_PARENT_RUN_ID = "stv3-temperature-recovery-20260731T071432Z"
ARTIFACT_FAILURE_PARENT_STATE = (
    REPOSITORY
    / "state/chembench_supervised_transfer_v3/runs"
    / ARTIFACT_FAILURE_PARENT_RUN_ID
    / "run_state.json"
)


@pytest.fixture(scope="module")
def live_parent_audit():
    if not PARENT_STATE.is_file():
        pytest.skip("immutable continued-recovery parent is unavailable")
    return audit_continued_parent_category_shards_v3(
        repository_root=REPOSITORY,
        start_category_index=CONTINUED_START_CATEGORY_INDEX,
    )


@pytest.fixture(scope="module")
def live_inputs():
    return load_experiment_inputs_v3(
        REPOSITORY,
        load_config_v3(CONFIG_PATH),
        require_runtime=False,
    )


@pytest.fixture(scope="module")
def artifact_failure_parent_audit():
    if not ARTIFACT_FAILURE_PARENT_STATE.is_file():
        pytest.skip("immutable artifact-validation recovery parent is unavailable")
    return audit_continued_parent_category_shards_v3(
        repository_root=REPOSITORY,
        parent_run_id=ARTIFACT_FAILURE_PARENT_RUN_ID,
        start_category_index=7,
    )


def test_closed_parent_prefix_zero_through_six_is_accepted(
    live_parent_audit,
) -> None:
    receipt = live_parent_audit.accepted
    assert type(receipt) is ContinuedAcceptedClosedShardsReceiptV3
    assert receipt.start_category_index == 7
    assert tuple(shard.category_index for shard in receipt.shards) == tuple(range(7))
    assert tuple(shard.category for shard in receipt.shards) == CHEMBENCH4K_CATEGORIES[:7]
    assert receipt.accepted_task_count == 350
    assert receipt.accepted_candidate_completion_count == 700
    assert receipt.accepted_reflector_call_count == 350
    assert receipt.accepted_core_job_count == 1050
    assert all(shard.final_category_state_closed for shard in receipt.shards)


def test_temperature_parent_shard_is_fully_discarded(live_parent_audit) -> None:
    receipt = live_parent_audit.discarded
    assert type(receipt) is ContinuedDiscardedPartialShardReceiptV3
    assert receipt.category_index == 7
    assert receipt.category == "Temperature_Prediction"
    assert receipt.completed_task_count == 11
    assert receipt.partial_task_ordinal == 11
    assert receipt.candidate_attempt_count_discarded == 24
    assert receipt.candidate_completion_count_discarded == 23
    assert receipt.candidate_attempts_without_completion_discarded == 1
    assert receipt.reflector_call_count_discarded == 12
    assert receipt.core_job_count_discarded == 36
    assert receipt.artifact_count_discarded == 36
    assert receipt.context_resolution_count_discarded == 36
    assert receipt.used_as_recovery_input is False
    assert receipt.artifacts_imported is False
    assert receipt.database_imported is False


def test_recovery_starts_at_temperature_task_zero(
    live_inputs,
    live_parent_audit,
) -> None:
    dry_run = build_continued_category_recovery_dry_run_v3(
        inputs=live_inputs,
        audit=live_parent_audit,
        recovery_run_id="stv3-temperature-recovery-20990101T000000Z",
    )
    assert dry_run["first_category"] == "Temperature_Prediction"
    assert dry_run["first_task_ordinal"] == 0
    assert dry_run["first_round"] == 0
    assert dry_run["generation_zero_targets"] is True
    assert dry_run["model_calls"] == 0
    assert [row["category"] for row in dry_run["remaining_schedule"]] == [
        "Temperature_Prediction",
        "Solvent_Prediction",
    ]
    assert all(row["task_start"] == 0 for row in dry_run["remaining_schedule"])


def test_two_remaining_categories_have_exact_call_plan(live_parent_audit) -> None:
    plan = ContinuedRemainingShardExecutionPlanV3(
        parent_run_id=live_parent_audit.parent_run_id,
        recovery_run_id="stv3-temperature-recovery-20990101T000001Z",
        start_category_index=7,
        categories=CHEMBENCH4K_CATEGORIES[7:],
        first_category="Temperature_Prediction",
        train_task_count=100,
        train_candidate_calls=200,
        reflector_calls=100,
        core_jobs=300,
        typed_artifacts=300,
        context_resolutions=300,
        answer_and_reflector_model_calls=300,
    )
    assert plan.categories == ("Temperature_Prediction", "Solvent_Prediction")
    assert plan.train_candidate_calls == 200
    assert plan.reflector_calls == 100
    assert plan.core_jobs == 300


def test_no_parent_artifact_or_database_import() -> None:
    source = (
        REPOSITORY / "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/"
        "continued_category_shard_recovery.py"
    ).read_text(encoding="utf-8")
    assert "register_artifact(" not in source
    assert "TaskwiseCoreRecoverySeed" not in source
    assert "ATTACH DATABASE" not in source.upper()
    assert "copyfile" not in source
    assert "copytree" not in source
    assert 'parent_artifacts_imported": True' not in source
    assert 'parent_database_imported": True' not in source


def test_default_and_first_recovery_behaviors_are_unchanged() -> None:
    assert ContinuedCategoryShardRecoveryV3 is not SupervisedTransferExperimentV3
    assert same_profile_remaining_categories_v3() == SAME_PROFILE_REMAINING_CATEGORIES
    assert continued_remaining_categories_v3(7) == CHEMBENCH4K_CATEGORIES[7:]
    original = (
        REPOSITORY / "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/"
        "experiment.py"
    ).read_text(encoding="utf-8")
    assert "start_category_index" not in original
    assert "for category in CHEMBENCH4K_CATEGORIES:" in original


def test_parent_tree_is_unchanged_by_continued_audit(live_parent_audit) -> None:
    repeated = audit_continued_parent_category_shards_v3(
        repository_root=REPOSITORY,
        start_category_index=CONTINUED_START_CATEGORY_INDEX,
    )
    assert repeated.parent_tree_sha256 == live_parent_audit.parent_tree_sha256
    assert repeated.parent_tree_file_count == live_parent_audit.parent_tree_file_count
    assert repeated.parent_tree_size_bytes == live_parent_audit.parent_tree_size_bytes


def test_artifact_validation_parent_accepts_only_closed_prefix(
    artifact_failure_parent_audit,
) -> None:
    assert tuple(
        shard.category for shard in artifact_failure_parent_audit.accepted.shards
    ) == CHEMBENCH4K_CATEGORIES[:7]
    receipt = artifact_failure_parent_audit.discarded
    assert type(receipt) is ContinuedDiscardedArtifactValidationShardReceiptV3
    assert receipt.category == "Temperature_Prediction"
    assert receipt.completed_task_count == 20
    assert receipt.partial_task_ordinal == 20
    assert receipt.candidate_attempt_count_discarded == 44
    assert receipt.candidate_completion_count_discarded == 41
    assert receipt.candidate_attempts_without_completion_discarded == 3
    assert receipt.closed_reflector_cycle_count_discarded == 20
    assert receipt.reflector_call_count_discarded == 21
    assert receipt.core_job_count_discarded == 61
    assert receipt.artifact_count_discarded == 61
    assert receipt.promoted_artifact_count_discarded == 60
    assert receipt.rejected_unpromoted_artifact_count_discarded == 1
    assert receipt.context_resolution_count_discarded == 60
    assert receipt.validator_finding_codes == ("memory_structure_invalid",)
    assert receipt.root_cause == "NUL_CONTROL_CHARACTER_IN_RENDERED_TEXT_MEMORY"
    assert receipt.used_as_recovery_input is False
    assert receipt.artifacts_imported is False
    assert receipt.database_imported is False


def test_artifact_validation_recovery_restarts_temperature_at_generation_zero(
    live_inputs,
    artifact_failure_parent_audit,
) -> None:
    dry_run = build_continued_category_recovery_dry_run_v3(
        inputs=live_inputs,
        audit=artifact_failure_parent_audit,
        recovery_run_id="stv3-temperature-controlfix-recovery-20990101T000000Z",
    )
    assert dry_run["first_category"] == "Temperature_Prediction"
    assert dry_run["first_task_ordinal"] == 0
    assert dry_run["first_round"] == 0
    assert dry_run["generation_zero_targets"] is True
    assert dry_run["parent_artifacts_imported"] is False
    assert dry_run["parent_database_imported"] is False


def test_artifact_validation_parent_tree_is_unchanged_by_audit(
    artifact_failure_parent_audit,
) -> None:
    repeated = audit_continued_parent_category_shards_v3(
        repository_root=REPOSITORY,
        parent_run_id=ARTIFACT_FAILURE_PARENT_RUN_ID,
        start_category_index=7,
    )
    assert repeated.parent_tree_sha256 == artifact_failure_parent_audit.parent_tree_sha256
    assert (
        repeated.parent_tree_file_count
        == artifact_failure_parent_audit.parent_tree_file_count
    )
    assert repeated.parent_tree_size_bytes == artifact_failure_parent_audit.parent_tree_size_bytes
