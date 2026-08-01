from __future__ import annotations

from pathlib import Path

import pytest

from openevo_chembench.supervised_transfer_v2 import experiment as v2
from openevo_chembench.supervised_transfer_v2.context_binding import (
    SupervisedAgentRequestV2,
)
from openevo_chembench.supervised_transfer_v2.executor import (
    SupervisedManagedCodexExecutorV2,
)
from openevo_chembench.supervised_transfer_v2.test_ledger import (
    FinalTestConsumptionLedgerV2,
    FinalTestLedgerError,
)
from openevo_chembench.supervised_transfer_v3.composed_control_test_baseline import (
    BASELINE_PROTOCOL_ID,
    EVOLVED_FINAL_TEST_RUN_ID,
    GENERATION_ZERO_CONTEXT_SET_SHA256,
    audit_completed_evolved_final_test_v3,
    build_control_baseline_dry_run_v3,
    build_control_baseline_source_compatibility_receipt_v3,
)
from openevo_chembench.supervised_transfer_v3.config import load_config_v3
from openevo_chembench.supervised_transfer_v3.experiment import (
    load_experiment_inputs_v3,
)

REPOSITORY = Path(__file__).resolve().parents[4]
CONFIG_PATH = (
    REPOSITORY
    / "benchmarks/chembench/configs/supervised_transfer_v3/"
    "chembench_supervised_transfer_v3_two_round_one_evolution.yaml"
)
EVOLVED_RECEIPT = (
    REPOSITORY
    / "results/chembench_supervised_transfer_v3/runs"
    / EVOLVED_FINAL_TEST_RUN_ID
    / "public/final_test_prefix_suffix_composition_receipt_v3.json"
)


@pytest.fixture(scope="module")
def live_inputs():
    if not EVOLVED_RECEIPT.is_file():
        pytest.skip("completed composed evolved Final Test is unavailable")
    return load_experiment_inputs_v3(
        REPOSITORY,
        load_config_v3(CONFIG_PATH),
        require_runtime=False,
    )


@pytest.fixture(scope="module")
def live_audit(live_inputs):
    return audit_completed_evolved_final_test_v3(
        repository_root=REPOSITORY,
        inputs=live_inputs,
    )


def test_completed_evolved_final_test_is_exact_pair_authority(
    live_inputs, live_audit
) -> None:
    assert live_audit.run_id == EVOLVED_FINAL_TEST_RUN_ID
    assert live_audit.correct_count == 382
    assert live_audit.test_order_sha256
    assert len(live_audit.evolved_evaluations) == 450
    assert [row["task_uid"] for row in live_audit.evolved_evaluations] == [
        task.uid for task in live_inputs.test
    ]


def test_control_baseline_dry_run_is_test_only(live_inputs, live_audit) -> None:
    payload = build_control_baseline_dry_run_v3(
        inputs=live_inputs,
        audit=live_audit,
        run_id="stv3-control-baseline-20990101T000000Z",
    )
    assert payload["test_task_count"] == 450
    assert payload["planned_candidate_calls"] == 450
    assert payload["planned_reflector_calls"] == 0
    assert payload["planned_core_jobs"] == 0
    assert payload["planned_context_resolutions"] == 0
    assert payload["planned_artifacts"] == 0
    assert payload["context_mode"] == "generation_zero"
    assert payload["context_target_ids"] == []
    assert payload["context_artifact_ids"] == []
    assert payload["parent_artifacts_imported"] is False
    assert payload["parent_databases_imported"] is False
    assert payload["model_calls"] == 0


def test_runtime_health_fix_has_exact_source_compatibility_receipt(
    live_inputs, live_audit
) -> None:
    receipt = build_control_baseline_source_compatibility_receipt_v3(
        inputs=live_inputs,
        audit=live_audit,
    )
    transition = receipt["reviewed_executor_transition"]
    assert transition["change_class"] == "runtime_health_recovery_only"
    runtime_transition = receipt["reviewed_runtime_services_transition"]
    assert (
        runtime_transition["change_class"]
        == "docker_identity_read_timeout_recovery_only"
    )
    assert receipt["runtime_health_recovery_reviewed"] is True
    assert receipt["docker_identity_read_timeout_recovery_reviewed"] is True
    assert receipt["terminal_completion_semantics_unchanged"] is True
    assert receipt["semantically_compatible"] is True


def test_control_request_uses_managed_codex_harness_without_context(
    live_inputs,
) -> None:
    task = live_inputs.test[0]
    prompt = v2._render_prompt(live_inputs.loader, task)
    executor = SupervisedManagedCodexExecutorV2(
        arm="control",
        timeout_seconds=1200,
        rollout_client=object(),  # build-only test; no submission is performed
        runtime_services=None,
        protocol_id=BASELINE_PROTOCOL_ID,
    )
    request = SupervisedAgentRequestV2(
        rendered_public_prompt=prompt.text,
        resolved_context=None,
        session_id="stv3-control-baseline-task-00000001",
        arm="control",
        task_ordinal=0,
        round_index=0,
        run_id="stv3-control-baseline-20990101T000000Z",
        task_uid=task.uid,
    )
    compiled = executor.build_task_request(request)
    metadata = compiled.metadata["openevo_chembench"]
    assert compiled.agent.harness == "codex"
    assert compiled.agent.model_name == "gpt-5.5"
    assert compiled.agent.settings["reasoning_effort"] == "medium"
    assert compiled.agent.settings["capture_mode"] == "transcript"
    assert compiled.agent.skills_path is None
    assert all(action.type != "upload_dir" for action in compiled.runtime.prepare)
    assert metadata["arm"] == "control"
    assert metadata["context_target_ids"] == []
    assert metadata["context_artifact_ids"] == []


def test_control_ledger_permits_exactly_one_completion_per_test_item(
    tmp_path: Path, live_inputs
) -> None:
    ledger = FinalTestConsumptionLedgerV2(
        path=(tmp_path / "private/control.jsonl").resolve(),
        source_commit="1" * 40,
        artifact_set_digest=GENERATION_ZERO_CONTEXT_SET_SHA256,
        config_digest="2" * 64,
        model_digest="3" * 64,
    )
    for ordinal, task in enumerate(live_inputs.test):
        attempt_id = f"control-baseline-attempt-{ordinal:04d}"
        ledger.claim(
            task_uid=task.uid,
            arm="control",
            attempt_id=attempt_id,
            timestamp="2099-01-01T00:00:00Z",
        )
        ledger.complete(
            task_uid=task.uid,
            arm="control",
            attempt_id=attempt_id,
            completion="A",
            timestamp="2099-01-01T00:00:01Z",
        )
    summary = ledger.summary()
    assert summary["control_completion_count"] == 450
    assert summary["evolved_completion_count"] == 0
    with pytest.raises(FinalTestLedgerError, match="TEST_ITEM_ALREADY_COMPLETED"):
        ledger.claim(
            task_uid=live_inputs.test[0].uid,
            arm="control",
            attempt_id="control-baseline-attempt-duplicate",
            timestamp="2099-01-01T00:00:02Z",
        )


def test_baseline_adapter_has_no_codex_or_artifact_import_bypass() -> None:
    source = (
        REPOSITORY
        / "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/"
        "composed_control_test_baseline.py"
    ).read_text(encoding="utf-8")
    assert "codex exec" not in source
    assert "subprocess.Popen" not in source
    assert "ATTACH DATABASE" not in source.upper()
    assert "copytree" not in source
    assert 'self._executor_factory("control")' in source
    assert 'logical_arm="test_control"' in source
    assert 'executor_arm="control"' in source
    assert "context=None" in source
    assert "FinalTestConsumptionLedgerV2(" in source


def test_existing_evolved_final_test_path_is_unchanged() -> None:
    source = (
        REPOSITORY
        / "benchmarks/chembench/src/openevo_chembench/supervised_transfer_v3/"
        "experiment.py"
    ).read_text(encoding="utf-8")
    assert "composed_control_test_baseline" not in source
    assert "V3_EVOLVED_TEST_CONTEXT_REQUIRED" in source
