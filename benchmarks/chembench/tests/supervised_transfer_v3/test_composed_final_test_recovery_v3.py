from __future__ import annotations

from pathlib import Path

import pytest

from openevo_chembench.supervised_transfer_v3.composed_final_test import (
    COMPOSITION_RUN_ID,
    audit_composed_train_shards_v3,
)
from openevo_chembench.supervised_transfer_v3.composed_final_test_recovery import (
    DEFAULT_PARENT_FINAL_TEST_RUN_ID,
    FINAL_TEST_RECOVERY_PROTOCOL_ID,
    audit_failed_final_test_prefix_v3,
    build_final_test_recovery_dry_run_v3,
)
from openevo_chembench.supervised_transfer_v3.config import load_config_v3
from openevo_chembench.supervised_transfer_v3.experiment import load_experiment_inputs_v3

REPOSITORY = Path(__file__).resolve().parents[4]
CONFIG_PATH = (
    REPOSITORY
    / "benchmarks/chembench/configs/supervised_transfer_v3/"
    "chembench_supervised_transfer_v3_two_round_one_evolution.yaml"
)
PARENT_STATE = (
    REPOSITORY
    / "state/chembench_supervised_transfer_v3/runs"
    / DEFAULT_PARENT_FINAL_TEST_RUN_ID
    / "run_state.json"
)


@pytest.fixture(scope="module")
def live_inputs():
    if not PARENT_STATE.is_file():
        pytest.skip("interrupted composed Final Test evidence is unavailable")
    return load_experiment_inputs_v3(
        REPOSITORY,
        load_config_v3(CONFIG_PATH),
        require_runtime=False,
    )


@pytest.fixture(scope="module")
def live_composition():
    if not PARENT_STATE.is_file():
        pytest.skip("interrupted composed Final Test evidence is unavailable")
    return audit_composed_train_shards_v3(
        repository_root=REPOSITORY,
        composition_run_id=COMPOSITION_RUN_ID,
    )


@pytest.fixture(scope="module")
def live_prefix(live_inputs, live_composition):
    return audit_failed_final_test_prefix_v3(
        repository_root=REPOSITORY,
        inputs=live_inputs,
        composition_audit=live_composition,
        parent_run_id=DEFAULT_PARENT_FINAL_TEST_RUN_ID,
    )


def test_parent_prefix_is_exactly_158_closed_items(live_prefix) -> None:
    assert live_prefix.accepted.completion_count == 158
    assert live_prefix.accepted.task_start_ordinal == 0
    assert live_prefix.accepted.task_end_ordinal == 157
    assert live_prefix.accepted.correct_count == 139
    assert len(live_prefix.parent_private_evaluations) == 158


def test_first_incomplete_item_is_fully_excluded(live_prefix) -> None:
    assert live_prefix.discarded.task_ordinal == 158
    assert live_prefix.discarded.category == "Caption2mol"
    assert live_prefix.discarded.incomplete_attempt_count == 15
    assert live_prefix.discarded.completion_exists is False
    assert live_prefix.discarded.included_in_final_statistics is False
    assert live_prefix.discarded.used_as_recovery_input is False


def test_prefix_audit_is_read_only(live_inputs, live_composition, live_prefix) -> None:
    before = (
        live_prefix.accepted.parent_run_tree_sha256,
        live_prefix.accepted.parent_ledger_sha256,
    )
    repeated = audit_failed_final_test_prefix_v3(
        repository_root=REPOSITORY,
        inputs=live_inputs,
        composition_audit=live_composition,
        parent_run_id=DEFAULT_PARENT_FINAL_TEST_RUN_ID,
    )
    assert (
        repeated.accepted.parent_run_tree_sha256,
        repeated.accepted.parent_ledger_sha256,
    ) == before


def test_recovery_dry_run_starts_at_first_uncompleted_uid(
    live_inputs, live_composition, live_prefix
) -> None:
    payload = build_final_test_recovery_dry_run_v3(
        inputs=live_inputs,
        composition_audit=live_composition,
        prefix_audit=live_prefix,
        recovery_run_id="stv3-composed-final-test-recovery-20990101T000000Z",
    )
    assert payload["recovery_protocol_id"] == FINAL_TEST_RECOVERY_PROTOCOL_ID
    assert payload["accepted_parent_completion_count"] == 158
    assert payload["start_task_ordinal"] == 158
    assert payload["first_category"] == "Caption2mol"
    assert payload["first_category_task_ordinal"] == 8
    assert payload["remaining_task_count"] == 292
    assert payload["planned_candidate_calls"] == 292
    assert payload["parent_completion_reexecuted"] is False
    assert payload["parent_ledger_modified"] is False
    assert payload["model_calls"] == 0


def test_recovery_runner_has_no_codex_or_database_bypass() -> None:
    source = (
        REPOSITORY
        / "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/"
        "composed_final_test_recovery.py"
    ).read_text(encoding="utf-8")
    assert "codex exec" not in source
    assert "subprocess.Popen" not in source
    assert "ATTACH DATABASE" not in source.upper()
    assert "copyfile" not in source
    assert "copytree" not in source
    assert 'self._executor_factory("online")' in source
    assert "self._execute_session(" in source


def test_existing_final_test_and_ledger_are_not_modified_by_recovery_source() -> None:
    source = (
        REPOSITORY
        / "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/"
        "composed_final_test_recovery.py"
    ).read_text(encoding="utf-8")
    assert "final_test_suffix_ledger_v3.jsonl" in source
    assert "_load_parent_ledger_read_only(parent_ledger_path)" in source
    assert source.count("FinalTestConsumptionLedgerV3(") == 1
    assert "run_composed_final_test" not in source
