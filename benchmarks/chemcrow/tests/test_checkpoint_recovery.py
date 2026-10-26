from __future__ import annotations

import json

import pytest

from openevo_chemcrow.checkpoint_recovery import (
    load_completed_baseline_evaluator_checkpoint,
    reconcile_completed_baseline_evaluator_checkpoint,
)
from openevo_chemcrow.compatible_evaluation import build_evolution_judge_task
from openevo_chemcrow.ledger import PhaseLedger
from openevo_chemcrow.models import Trajectory
from openevo_chemcrow.runtime import (
    CORE_MANAGED_CODEX_ROUTE,
    build_task_request,
    s0_config_hash,
)


def _completion(*, run_id: str, answer: str, prompt: str = "prompt", transcript: str = ""):
    return {
        "task_id": run_id,
        "session_id": f"session-{run_id}",
        "status": "COMPLETED",
        "error": None,
        "workspace_result": None,
        "timing": {"agent": 1000},
        "metadata": {
            "execution_route": CORE_MANAGED_CODEX_ROUTE,
            "host_codex_exec_forbidden": True,
        },
        "trajectory": {
            "status": "COMPLETED",
            "metadata": {},
            "traces": [
                {
                    "prompt_messages": [{"role": "user", "content": prompt}],
                    "response_messages": [{"role": "assistant", "content": answer}],
                    "metadata": {"transcript": transcript},
                }
            ],
        },
    }


def test_reconcile_completed_calls_adapts_confidence_without_redispatch(
    tmp_path, task_item, core_candidate_config
):
    experiment_id = "experiment"
    pair_id = f"{experiment_id}--{task_item.task_id}"
    baseline_run_id = f"{pair_id}-baseline-run"
    evaluator_run_id = f"{baseline_run_id}-evolution-eval-baseline-run"
    evaluator_id = "evolution-evaluator-id"
    evaluator_config = json.loads(json.dumps(core_candidate_config))
    baseline_answer = "preserved baseline answer"
    baseline = Trajectory(
        run_id=baseline_run_id,
        task_id=task_item.task_id,
        role="baseline",
        status="COMPLETED",
        answer=baseline_answer,
        candidate_config_sha256=s0_config_hash(core_candidate_config),
        wall_time_seconds=1.0,
    )
    judge_task = build_evolution_judge_task(task=task_item, trajectory=baseline)
    evaluator_prompt = build_task_request(
        task=judge_task,
        run_id=evaluator_run_id,
        role="baseline",
        candidate=evaluator_config,
        artifact_ids=[],
        mcp_url=None,
    )["instruction"]
    evaluator_answer = json.dumps(
        {
            "scores": {
                "chemical_correctness": 3,
                "reasoning_quality": 4,
                "task_completion": 2,
            },
            "strengths": ["grounded"],
            "weaknesses": [],
            "actionable_critique": [],
            "confidence": 4,
        }
    )
    baseline_path = tmp_path / "baseline" / "completion.json"
    evaluator_path = tmp_path / f"task_{evaluator_run_id}" / "completion.json"
    baseline_path.parent.mkdir()
    evaluator_path.parent.mkdir()
    baseline_path.write_text(
        json.dumps(
            _completion(run_id=baseline_run_id, answer=baseline_answer),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    evaluator_path.write_text(
        json.dumps(
            _completion(
                run_id=evaluator_run_id,
                answer=evaluator_answer,
                prompt=evaluator_prompt,
            ),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("artifact_protocol: three_isolated_v1\n", encoding="utf-8")
    ledger_root = tmp_path / "claims"
    ledger = PhaseLedger(ledger_root, pair_id=pair_id)
    ledger.claim(
        "baseline_candidate",
        {"task_id": task_item.task_id, "artifact_ids": []},
    )
    ledger.terminal(
        "baseline_candidate",
        {"run_id": baseline_run_id, "trajectory_sha256": "original"},
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

    checkpoint_path, recovery_path, recovered_baseline, feedback = (
        reconcile_completed_baseline_evaluator_checkpoint(
            config_path=config_path,
            run_root=tmp_path / "runs",
            ledger_root=ledger_root,
            cache_root=tmp_path / "cache",
            experiment_id=experiment_id,
            task=task_item,
            candidate_config=core_candidate_config,
            evaluator_config=evaluator_config,
            evaluator_id=evaluator_id,
            baseline_completion_path=baseline_path,
            evaluator_completion_path=evaluator_path,
        )
    )

    assert checkpoint_path.is_file()
    receipt = json.loads(recovery_path.read_text(encoding="utf-8"))
    assert receipt["candidate_redispatched"] is False
    assert receipt["evaluator_redispatched"] is False
    assert receipt["confidence_parse_receipt"]["adapted"] is True
    assert recovered_baseline.answer == baseline_answer
    assert feedback.scores.model_dump() == {
        "chemical_correctness": 3.0,
        "reasoning_quality": 4.0,
        "task_completion": 2.0,
    }
    assert feedback.confidence == 1.0
    loaded = load_completed_baseline_evaluator_checkpoint(
        run_root=tmp_path / "runs",
        ledger_root=ledger_root,
        pair_id=pair_id,
        task=task_item,
        expected_candidate_config_sha256=s0_config_hash(core_candidate_config),
        expected_evaluator_id=evaluator_id,
    )
    assert loaded == (recovered_baseline, feedback)


def test_checkpoint_recovery_rejects_candidate_shell_execution(
    tmp_path, task_item, core_candidate_config
):
    experiment_id = "experiment"
    pair_id = f"{experiment_id}--{task_item.task_id}"
    baseline_run_id = f"{pair_id}-baseline-run"
    evaluator_id = "evolution-evaluator-id"
    baseline_path = tmp_path / "baseline" / "completion.json"
    baseline_path.parent.mkdir()
    baseline_path.write_text(
        json.dumps(
            _completion(
                run_id=baseline_run_id,
                answer="answer",
                transcript=json.dumps(
                    {"item": {"type": "command_execution", "command": "true"}}
                ),
            )
        ),
        encoding="utf-8",
    )
    evaluator_path = tmp_path / "task_unused-evaluator" / "completion.json"
    evaluator_path.parent.mkdir()
    evaluator_path.write_text("{}", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("config: true\n", encoding="utf-8")
    ledger_root = tmp_path / "claims"
    ledger = PhaseLedger(ledger_root, pair_id=pair_id)
    ledger.claim("baseline_candidate", {"task_id": task_item.task_id})
    ledger.terminal(
        "baseline_candidate", {"run_id": baseline_run_id, "trajectory_sha256": "x"}
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

    with pytest.raises(ValueError, match="shell executions"):
        reconcile_completed_baseline_evaluator_checkpoint(
            config_path=config_path,
            run_root=tmp_path / "runs",
            ledger_root=ledger_root,
            cache_root=tmp_path / "cache",
            experiment_id=experiment_id,
            task=task_item,
            candidate_config=core_candidate_config,
            evaluator_config=core_candidate_config,
            evaluator_id=evaluator_id,
            baseline_completion_path=baseline_path,
            evaluator_completion_path=evaluator_path,
        )
