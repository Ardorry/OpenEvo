from __future__ import annotations

from pathlib import Path

import pytest

from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
from openevo_chembench.supervised_transfer_v3.composed_final_test import (
    COMPOSITION_RUN_ID,
    _issue_context,
    audit_composed_train_shards_v3,
    build_composed_final_test_dry_run_v3,
)
from openevo_chembench.supervised_transfer_v3.config import load_config_v3
from openevo_chembench.supervised_transfer_v3.experiment import load_experiment_inputs_v3

REPOSITORY = Path(__file__).resolve().parents[4]
CONFIG_PATH = (
    REPOSITORY
    / "benchmarks/chembench/configs/supervised_transfer_v3/"
    "chembench_supervised_transfer_v3_two_round_one_evolution.yaml"
)
COMPOSITION_PATH = (
    REPOSITORY
    / "results/chembench_supervised_transfer_v3/runs"
    / COMPOSITION_RUN_ID
    / "public/continued_final_shard_composition_receipt_v3.json"
)


@pytest.fixture(scope="module")
def live_audit():
    if not COMPOSITION_PATH.is_file():
        pytest.skip("closed nine-shard composition is unavailable")
    return audit_composed_train_shards_v3(
        repository_root=REPOSITORY,
        composition_run_id=COMPOSITION_RUN_ID,
    )


@pytest.fixture(scope="module")
def live_inputs():
    return load_experiment_inputs_v3(
        REPOSITORY,
        load_config_v3(CONFIG_PATH),
        require_runtime=False,
    )


def test_all_nine_closed_shards_are_reaudited(live_audit) -> None:
    assert tuple(item.category for item in live_audit.categories) == CHEMBENCH4K_CATEGORIES
    assert tuple(item.category_index for item in live_audit.categories) == tuple(range(9))
    assert all(item.final_result.task_index == 49 for item in live_audit.categories)
    assert all(item.final_result.update_index == 1 for item in live_audit.categories)
    assert all(item.final_result.global_update_ordinal == 50 for item in live_audit.categories)


def test_composed_audit_is_read_only(live_audit) -> None:
    before = tuple(item.category_tree_sha256 for item in live_audit.categories)
    repeated = audit_composed_train_shards_v3(
        repository_root=REPOSITORY,
        composition_run_id=COMPOSITION_RUN_ID,
    )
    assert tuple(item.category_tree_sha256 for item in repeated.categories) == before
    assert repeated.digest == live_audit.digest


def test_final_contexts_are_exact_final_heads(live_audit) -> None:
    for item in live_audit.categories:
        context = _issue_context(item.final_result)
        assert context.memory.core_artifact_id == item.final_result.core_artifact_id
        assert (
            context.memory.artifact_payload_sha256
            == item.final_result.artifact_payload_sha256
        )
        auxiliary = {
            value.target_id: value
            for value in item.final_result.supervised_auxiliary_artifacts
        }
        assert context.skill.core_artifact_id == auxiliary["skill_bundle"].core_artifact_id
        assert (
            context.agent_system.core_artifact_id
            == auxiliary["agent_system"].core_artifact_id
        )


def test_composed_final_test_dry_run_is_test_only(live_audit, live_inputs) -> None:
    payload = build_composed_final_test_dry_run_v3(
        inputs=live_inputs,
        audit=live_audit,
        run_id="stv3-composed-final-test-20990101T000000Z",
    )
    assert payload["category_order"] == list(CHEMBENCH4K_CATEGORIES)
    assert payload["frozen_target_count"] == 27
    assert payload["test_task_count"] == 450
    assert payload["planned_candidate_calls"] == 450
    assert payload["planned_reflector_calls"] == 0
    assert payload["planned_core_jobs"] == 0
    assert payload["source_artifacts_imported"] is False
    assert payload["source_databases_imported"] is False
    assert payload["model_calls"] == 0


def test_composed_runner_has_no_codex_or_database_bypass() -> None:
    source = (
        REPOSITORY
        / "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/"
        "composed_final_test.py"
    ).read_text(encoding="utf-8")
    assert "codex exec" not in source
    assert "subprocess.Popen" not in source
    assert "ATTACH DATABASE" not in source.upper()
    assert "copyfile" not in source
    assert "copytree" not in source
    assert "self._run_evolved_test_v3(frozen, executor)" in source
    assert 'self._executor_factory("online")' in source


def test_default_v3_execution_remains_unchanged() -> None:
    source = (
        REPOSITORY
        / "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/experiment.py"
    ).read_text(encoding="utf-8")
    assert "composed_final_test" not in source
    assert "for category in CHEMBENCH4K_CATEGORIES:" in source
