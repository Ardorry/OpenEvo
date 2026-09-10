from __future__ import annotations

import json

from openevo_chemcrow.baseline_evaluator_recovery import (
    load_baseline_before_evaluator_checkpoint,
    reconcile_baseline_evaluator_no_effect_failure,
)
from openevo_chemcrow.ledger import PhaseLedger
from openevo_chemcrow.runtime import CORE_MANAGED_CODEX_ROUTE, s0_config_hash


def _baseline_completion(run_id: str) -> dict:
    return {
        "task_id": run_id,
        "session_id": "baseline-session",
        "status": "COMPLETED",
        "error": None,
        "workspace_result": None,
        "timing": {"run_ms": 1000},
        "metadata": {
            "execution_route": CORE_MANAGED_CODEX_ROUTE,
            "host_codex_exec_forbidden": True,
        },
        "trajectory": {
            "status": "COMPLETED",
            "metadata": {},
            "traces": [
                {
                    "prompt_messages": [{"role": "user", "content": "task"}],
                    "response_messages": [
                        {"role": "assistant", "content": "preserved G1 answer"}
                    ],
                    "metadata": {"transcript": ""},
                }
            ],
        },
    }


def _evaluator_failure(run_id: str) -> dict:
    return {
        "task_id": run_id,
        "session_id": "evaluator-session",
        "status": "ERROR",
        "error": (
            "agent execution failed: Codex subscription credential isolation could "
            "not be proven (validation_failed)"
        ),
        "workspace_result": None,
        "metadata": {
            "execution_route": CORE_MANAGED_CODEX_ROUTE,
            "host_codex_exec_forbidden": True,
            "evolution": {"context_artifact_ids": []},
            "openevo": {"credential_isolation": {"policy_sha256": "policy"}},
        },
        "trajectory": {"metadata": {"record_count": 0}, "traces": []},
    }


def test_reconcile_baseline_evaluator_failure_preserves_g1(tmp_path, task_item, core_candidate_config):
    experiment_id = "experiment"
    pair_id = f"{experiment_id}--{task_item.task_id}"
    baseline_run_id = f"{pair_id}-baseline-0123456789"
    evaluator_run_id = f"{baseline_run_id}-evolution-eval-baseline-abcdef0123"
    evaluator_id = "evaluator-id"
    ledger_root = tmp_path / "claims"
    ledger = PhaseLedger(ledger_root, pair_id=pair_id)
    ledger.claim(
        "baseline_candidate",
        {
            "task_id": task_item.task_id,
            "s0_hash": s0_config_hash(core_candidate_config),
            "artifact_ids": [],
            "artifact_inventory": {},
        },
    )
    ledger.terminal(
        "baseline_candidate",
        {"run_id": baseline_run_id, "trajectory_sha256": "original", "runtime_context": "bare_s0"},
    )
    ledger.claim(
        "baseline_internal_evaluator",
        {
            "task_id": task_item.task_id,
            "run_id": baseline_run_id,
            "evaluator_id": evaluator_id,
            "paper_evaluator": False,
        },
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("experiment: test\n", encoding="utf-8")
    baseline_path = tmp_path / "baseline.json"
    evaluator_path = tmp_path / "evaluator.json"
    baseline_path.write_text(json.dumps(_baseline_completion(baseline_run_id)))
    evaluator_path.write_text(json.dumps(_evaluator_failure(evaluator_run_id)))

    checkpoint_path, receipt_path, baseline, receipt = (
        reconcile_baseline_evaluator_no_effect_failure(
            config_path=config_path,
            run_root=tmp_path / "run",
            ledger_root=ledger_root,
            cache_root=tmp_path / "cache",
            experiment_id=experiment_id,
            task=task_item,
            candidate_config=core_candidate_config,
            evaluator_id=evaluator_id,
            baseline_completion_path=baseline_path,
            evaluator_completion_path=evaluator_path,
        )
    )

    assert checkpoint_path.is_file() and receipt_path.is_file()
    assert baseline.answer == "preserved G1 answer"
    assert receipt["candidate_redispatched"] is False
    assert receipt["evaluator_model_call_proven_absent"] is True
    assert receipt["baseline_tool_receipt_count"] == 0
    assert load_baseline_before_evaluator_checkpoint(
        run_root=tmp_path / "run",
        ledger_root=ledger_root,
        pair_id=pair_id,
        task=task_item,
        expected_candidate_config_sha256=s0_config_hash(core_candidate_config),
        expected_evaluator_id=evaluator_id,
    ) == baseline
