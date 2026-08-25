from __future__ import annotations

import json
import time

import httpx
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
    OpenEvoRolloutError,
    _normalize_rollout,
    _poll_rollout_until_terminal,
    assert_core_managed_codex_config,
    build_task_request,
    s0_config_hash,
)


class _StatusResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _TransientStatusClient:
    def __init__(self):
        self.calls = 0

    def get(self, _url):
        self.calls += 1
        if self.calls == 1:
            raise httpx.RemoteProtocolError("server disconnected without a response")
        if self.calls == 2:
            return _StatusResponse({"status": "running"})
        return _StatusResponse({"status": "completed", "results": []})


def test_rollout_poll_transport_error_retries_read_only_without_redispatch():
    client = _TransientStatusClient()
    status, retries = _poll_rollout_until_terminal(
        client=client,
        url="http://rollout/rollout/task/already-submitted",
        started=time.monotonic(),
        admitted_timeout_seconds=10.0,
        poll_seconds=0.0,
    )
    assert status["status"] == "completed"
    assert retries == 1
    assert client.calls == 3


def test_resolved_model_identity_changes_s0_hash(core_candidate_config):
    unresolved = {
        **core_candidate_config,
        "agent": {**core_candidate_config["agent"], "model_name": "${CANDIDATE_MODEL}"},
    }
    assert s0_config_hash(unresolved) != s0_config_hash(core_candidate_config)


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
    assert "BENCHMARK-ONLY SAFETY BOUNDARY" in baseline["instruction"]
    assert "Do not perform physical experiments" in baseline["instruction"]


def test_safety_case_instruction_preserves_refusal(task_item, core_candidate_config):
    safety_task = task_item.model_copy(
        update={
            "safety_metadata": {
                **task_item.safety_metadata,
                "safety_behavior_must_be_preserved": True,
                "safety_case": True,
            }
        }
    )
    request = build_task_request(
        task=safety_task,
        run_id="safety-baseline",
        role="baseline",
        candidate=core_candidate_config,
        artifact_ids=[],
        mcp_url="http://tools/mcp",
    )
    assert "intentional safety case" in request["instruction"]
    assert "do not convert unsafe operational assistance" in request["instruction"]


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


def _rollout_payload(*, evolution=None, transcript_events=()):
    transcript = "\n".join(json.dumps(event) for event in transcript_events)
    return {
        "results": [
            {
                "status": "COMPLETED",
                "trajectory": {
                    "status": "COMPLETED",
                    "metadata": {"task_metadata": {"evolution": evolution or {}}},
                    "traces": [
                        {
                            "response_messages": [
                                {"role": "assistant", "content": "answer"}
                            ],
                            "metadata": {"transcript": transcript},
                        }
                    ],
                },
            }
        ]
    }


def test_evolved_rollout_requires_exact_core_injection_receipt():
    payload = _rollout_payload(evolution={"context_injected": False})
    with pytest.raises(OpenEvoRolloutError, match="artifact-injection receipt"):
        _normalize_rollout(
            payload,
            run_id="evolved-1",
            task_id="task-1",
            role="evolved",
            artifact_ids=["art-1"],
            config_sha256="s0",
            wall_time=1.0,
        )

    payload = _rollout_payload(
        evolution={
            "context_injected": True,
            "runtime_injection_receipt": {"artifacts": [{"artifact_id": "art-1"}]},
        }
    )
    trajectory = _normalize_rollout(
        payload,
        run_id="evolved-1",
        task_id="task-1",
        role="evolved",
        artifact_ids=["art-1"],
        config_sha256="s0",
        wall_time=1.0,
    )
    assert trajectory.artifact_ids == ["art-1"]


def test_declared_tool_surface_rejects_builtin_web_search_and_receipt_mismatch():
    web_payload = _rollout_payload(
        transcript_events=[{"item": {"id": "web-1", "type": "web_search"}}]
    )
    with pytest.raises(OpenEvoRolloutError, match="built-in web search"):
        _normalize_rollout(
            web_payload,
            run_id="baseline-1",
            task_id="task-1",
            role="baseline",
            artifact_ids=[],
            config_sha256="s0",
            wall_time=1.0,
            declared_tool_bridge=True,
        )

    bridge_payload = _rollout_payload(
        transcript_events=[
            {
                "item": {
                    "id": "cmd-1",
                    "type": "command_execution",
                    "command": "curl http://tools/tool/SMILES2Weight",
                }
            }
        ]
    )
    with pytest.raises(OpenEvoRolloutError, match="receipt stream"):
        _normalize_rollout(
            bridge_payload,
            run_id="baseline-1",
            task_id="task-1",
            role="baseline",
            artifact_ids=[],
            config_sha256="s0",
            wall_time=1.0,
            tool_receipts=[],
            declared_tool_bridge=True,
        )
