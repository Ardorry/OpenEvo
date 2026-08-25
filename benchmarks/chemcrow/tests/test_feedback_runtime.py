from __future__ import annotations

import pytest

from openevo_chemcrow.feedback import reflector_feedback_payload, runtime_feedback
from openevo_chemcrow.models import (
    EvaluatorFeedback,
    FeedbackMode,
    RubricScores,
    Trajectory,
)
from openevo_chemcrow.runtime import (
    CORE_MANAGED_CODEX_ROUTE,
    assert_core_managed_codex_config,
    build_task_request,
    s0_config_hash,
)


def _trajectory(task_id: str = "chemcrow-test-01") -> Trajectory:
    return Trajectory(
        run_id="run-1",
        task_id=task_id,
        role="baseline",
        status="COMPLETED",
        answer="answer",
        candidate_config_sha256="s0",
    )


def test_feedback_ablation_projection():
    trajectory = _trajectory()
    runtime = runtime_feedback(trajectory)
    evaluator = EvaluatorFeedback(
        evaluator_role="evolution_evaluator",
        evaluator_run_id="eval-1",
        scores=RubricScores(chemical_correctness=2, reasoning_quality=3, task_completion=2),
    )
    f0 = reflector_feedback_payload(mode=FeedbackMode.F0, trajectory=trajectory, runtime=runtime, evaluator=evaluator)
    f1 = reflector_feedback_payload(mode=FeedbackMode.F1, trajectory=trajectory, runtime=runtime, evaluator=evaluator)
    f2 = reflector_feedback_payload(mode=FeedbackMode.F2, trajectory=trajectory, runtime=runtime, evaluator=evaluator)
    assert set(f0) == {"mode", "observable_trajectory"}
    assert "runtime_feedback" in f1 and "evaluator_feedback" not in f1
    assert "runtime_feedback" in f2 and "evaluator_feedback" in f2


def test_baseline_evolved_request_parity_except_artifact(task_item, core_candidate_config):
    candidate = core_candidate_config
    baseline = build_task_request(task=task_item, run_id="b", role="baseline", candidate=candidate, artifact_ids=[], mcp_url="http://tools/mcp")
    evolved = build_task_request(task=task_item, run_id="e", role="evolved", candidate=candidate, artifact_ids=["art-1"], mcp_url="http://tools/mcp")
    assert baseline["agent"] == evolved["agent"]
    assert baseline["runtime"] == evolved["runtime"]
    assert baseline["instruction"] == evolved["instruction"]
    assert baseline["metadata"]["evolution"]["context_artifact_ids"] == []
    assert evolved["metadata"]["evolution"]["context_artifact_ids"] == ["art-1"]
    assert baseline["metadata"]["execution_route"] == CORE_MANAGED_CODEX_ROUTE
    assert baseline["metadata"]["host_codex_exec_forbidden"] is True
    assert s0_config_hash(candidate) == s0_config_hash(candidate)


def test_all_codex_routes_fail_closed_without_core_managed_runtime(core_candidate_config):
    assert_core_managed_codex_config(core_candidate_config)
    bypass = {**core_candidate_config, "runtime": None}
    with pytest.raises(TypeError, match="explicit Core-managed runtime"):
        assert_core_managed_codex_config(bypass)
    shell_bypass = {
        **core_candidate_config,
        "agent": {**core_candidate_config["agent"], "custom_shell": "codex exec"},
    }
    with pytest.raises(ValueError, match="custom shell"):
        assert_core_managed_codex_config(shell_bypass)
