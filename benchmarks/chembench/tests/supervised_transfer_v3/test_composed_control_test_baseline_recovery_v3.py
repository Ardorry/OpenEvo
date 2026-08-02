from __future__ import annotations

from pathlib import Path

import pytest

from openevo_chembench.supervised_transfer_v3.composed_control_test_baseline import (
    EVOLVED_FINAL_TEST_RUN_ID,
    audit_completed_evolved_final_test_v3,
)
from openevo_chembench.supervised_transfer_v3.composed_control_test_baseline_recovery import (
    CONTROL_BASELINE_RECOVERY_PROTOCOL_ID,
    DEFAULT_PARENT_CONTROL_BASELINE_RUN_ID,
    EXPECTED_EXCLUDED_CONTROL_BASELINE_RUN_IDS,
    audit_failed_control_baseline_prefix_v3,
    build_control_baseline_recovery_dry_run_v3,
    build_control_baseline_recovery_source_compatibility_receipt_v3,
)
from openevo_chembench.supervised_transfer_v3.config import load_config_v3
from openevo_chembench.supervised_transfer_v3.experiment import (
    load_experiment_inputs_v3,
)

REPOSITORY = Path(__file__).resolve().parents[4]
CONFIG_PATH = (
    REPOSITORY / "benchmarks/chembench/configs/supervised_transfer_v3/"
    "chembench_supervised_transfer_v3_two_round_one_evolution.yaml"
)
PARENT_STATE = (
    REPOSITORY
    / "state/chembench_supervised_transfer_v3/runs"
    / DEFAULT_PARENT_CONTROL_BASELINE_RUN_ID
    / "run_state.json"
)


@pytest.fixture(scope="module")
def live_inputs():
    if not PARENT_STATE.is_file():
        pytest.skip("interrupted control baseline evidence is unavailable")
    return load_experiment_inputs_v3(
        REPOSITORY,
        load_config_v3(CONFIG_PATH),
        require_runtime=False,
    )


@pytest.fixture(scope="module")
def live_evolved(live_inputs):
    return audit_completed_evolved_final_test_v3(
        repository_root=REPOSITORY,
        inputs=live_inputs,
        run_id=EVOLVED_FINAL_TEST_RUN_ID,
    )


@pytest.fixture(scope="module")
def live_prefix(live_inputs, live_evolved):
    return audit_failed_control_baseline_prefix_v3(
        repository_root=REPOSITORY,
        inputs=live_inputs,
        evolved_audit=live_evolved,
        parent_run_id=DEFAULT_PARENT_CONTROL_BASELINE_RUN_ID,
    )


def test_selected_parent_is_exact_contiguous_144_item_prefix(live_prefix) -> None:
    assert live_prefix.accepted.completion_count == 144
    assert live_prefix.accepted.task_start_ordinal == 0
    assert live_prefix.accepted.task_end_ordinal == 143
    assert live_prefix.accepted.accepted_attempt_count == 180
    assert live_prefix.accepted.correct_count == 127
    assert live_prefix.accepted.selected_without_correctness is True
    assert len(live_prefix.parent_private_evaluations) == 144


def test_first_incomplete_parent_item_is_excluded(live_inputs, live_prefix) -> None:
    discarded = live_prefix.discarded
    assert discarded.task_ordinal == 144
    assert discarded.task_uid == live_inputs.test[144].uid
    assert discarded.category == live_inputs.test[144].category
    assert discarded.incomplete_attempt_count == 1
    assert discarded.completion_exists is False
    assert discarded.included_in_final_statistics is False
    assert discarded.used_as_recovery_input is False


def test_all_historical_attempts_are_disclosed_and_excluded(live_prefix) -> None:
    excluded = live_prefix.excluded
    assert tuple(item.run_id for item in excluded.historical_runs) == (
        EXPECTED_EXCLUDED_CONTROL_BASELINE_RUN_IDS
    )
    assert [item.completion_count for item in excluded.historical_runs] == [
        27,
        0,
        0,
        0,
        16,
    ]
    assert [item.attempt_event_count for item in excluded.historical_runs] == [
        32,
        3,
        1,
        1,
        53,
    ]
    assert [item.no_completion_attempt_count for item in excluded.historical_runs] == [
        5,
        3,
        1,
        1,
        37,
    ]
    assert [item.public_infrastructure_retry_event_count for item in excluded.historical_runs] == [
        4,
        3,
        1,
        1,
        36,
    ]
    assert excluded.excluded_completion_event_count == 43
    assert excluded.excluded_unique_task_count == 27
    assert excluded.excluded_attempt_event_count == 90
    assert excluded.excluded_no_completion_attempt_count == 47
    assert excluded.excluded_public_infrastructure_retry_event_count == 45
    assert excluded.correctness_read_for_selection is False
    assert excluded.all_historical_completions_are_selected_parent_subset is True


def test_parent_and_sibling_audit_is_read_only(live_inputs, live_evolved, live_prefix) -> None:
    before = (
        live_prefix.accepted.parent_run_tree_sha256,
        live_prefix.accepted.parent_ledger_sha256,
        live_prefix.excluded.digest,
    )
    repeated = audit_failed_control_baseline_prefix_v3(
        repository_root=REPOSITORY,
        inputs=live_inputs,
        evolved_audit=live_evolved,
        parent_run_id=DEFAULT_PARENT_CONTROL_BASELINE_RUN_ID,
    )
    assert (
        repeated.accepted.parent_run_tree_sha256,
        repeated.accepted.parent_ledger_sha256,
        repeated.excluded.digest,
    ) == before


def test_recovery_dry_run_starts_at_first_uid_without_completion(
    live_inputs, live_evolved, live_prefix
) -> None:
    payload = build_control_baseline_recovery_dry_run_v3(
        inputs=live_inputs,
        evolved_audit=live_evolved,
        prefix_audit=live_prefix,
        recovery_run_id="stv3-control-baseline-recovery-20990101T000000Z",
    )
    assert payload["recovery_protocol_id"] == CONTROL_BASELINE_RECOVERY_PROTOCOL_ID
    assert payload["accepted_parent_completion_count"] == 144
    assert payload["start_task_ordinal"] == 144
    assert payload["first_category"] == live_inputs.test[144].category
    assert payload["first_category_task_ordinal"] == 44
    assert payload["remaining_task_count"] == 306
    assert payload["planned_candidate_calls"] == 306
    assert payload["planned_reflector_calls"] == 0
    assert payload["planned_core_jobs"] == 0
    assert payload["planned_context_resolutions"] == 0
    assert payload["planned_artifacts"] == 0
    assert payload["context_mode"] == "generation_zero"
    assert payload["context_target_ids"] == []
    assert payload["context_artifact_ids"] == []
    assert payload["parent_completion_reexecuted"] is False
    assert payload["parent_ledger_modified"] is False
    assert payload["correctness_used_for_parent_selection"] is False
    assert payload["model_calls"] == 0


def test_recovery_source_transition_preserves_execution_semantics(
    live_inputs, live_evolved, live_prefix
) -> None:
    receipt = build_control_baseline_recovery_source_compatibility_receipt_v3(
        inputs=live_inputs,
        evolved_audit=live_evolved,
        prefix_audit=live_prefix,
    )
    assert receipt["changes_limited_to_control_suffix_recovery_orchestration"] is True
    assert receipt["candidate_prompt_unchanged"] is True
    assert receipt["candidate_model_unchanged"] is True
    assert receipt["candidate_harness_unchanged"] is True
    assert receipt["runtime_identity_checks_unchanged"] is True
    assert receipt["evaluator_unchanged"] is True
    assert receipt["strict_parser_unchanged"] is True
    assert receipt["test_order_unchanged"] is True
    assert receipt["generation_zero_context_unchanged"] is True
    assert receipt["parent_selection_uses_correctness"] is False
    assert receipt["semantically_compatible"] is True


def test_recovery_runner_has_no_codex_database_or_parent_write_bypass() -> None:
    source = (
        REPOSITORY / "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/"
        "composed_control_test_baseline_recovery.py"
    ).read_text(encoding="utf-8")
    assert "codex exec" not in source
    assert "subprocess.Popen" not in source
    assert "ATTACH DATABASE" not in source.upper()
    assert "copyfile" not in source
    assert "copytree" not in source
    assert 'self._executor_factory("control")' in source
    assert "self._execute_session(" in source
    assert "self.inputs.test[start:]" in source
    assert "control_test_suffix_ledger_v3.jsonl" in source
    assert source.count("FinalTestConsumptionLedgerV2(") == 1
    assert "run_control_baseline()" not in source
