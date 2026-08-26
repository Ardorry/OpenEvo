from __future__ import annotations

import json
import sqlite3

from openevo_chemcrow.hashing import canonical_sha256
from openevo_chemcrow.ledger import PhaseLedger
from openevo_chemcrow.models import EvaluatorFeedback, RubricScores, TaskItem, Trajectory
from openevo_chemcrow.reflector_recovery import (
    load_reflector_boundary_checkpoint,
    reconcile_reflector_no_effect_failure,
)
from openevo_chemcrow.runtime import CORE_MANAGED_CODEX_ROUTE


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def test_reflector_pre_exec_failure_recovers_exact_g1_without_redispatch(
    tmp_path,
    monkeypatch,
):
    experiment_id = "experiment"
    task = TaskItem(
        task_id="chemcrow-04",
        prompt="Find a synthesis and cost.",
        broad_category="synthesis_planning",
        provenance={"source": "test"},
        sanitized_item_sha256="task-hash",
    )
    pair_id = f"{experiment_id}--{task.task_id}"
    run_root = tmp_path / "run"
    ledger_root = run_root / "claims"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("experiment: test\n")
    baseline = Trajectory(
        run_id=f"{pair_id}-baseline-run",
        task_id=task.task_id,
        role="baseline",
        status="COMPLETED",
        answer="baseline answer",
        candidate_config_sha256="candidate-hash",
    )
    evaluation = EvaluatorFeedback(
        evaluator_role="evolution_evaluator",
        evaluator_run_id="evaluator-run",
        scores=RubricScores(
            chemical_correctness=3,
            reasoning_quality=3,
            task_completion=2,
        ),
        actionable_critique=["Add cost evidence."],
    )
    feedback = {
        "mode": "F2",
        "observable_trajectory": {},
        "runtime_feedback": {},
        "evaluator_feedback": evaluation.model_dump(mode="json"),
    }
    evidence = {
        "task": {
            "task_id": task.task_id,
            "prompt": task.prompt,
            "sanitized_item_sha256": task.sanitized_item_sha256,
        },
        "baseline": baseline.model_dump(mode="json"),
        "feedback": feedback,
    }
    evidence_hash = canonical_sha256(evidence)
    ledger = PhaseLedger(ledger_root, pair_id=pair_id)
    ledger.claim(
        "baseline_candidate",
        {
            "task_id": task.task_id,
            "s0_hash": "s0",
            "artifact_ids": [],
            "artifact_inventory": {},
        },
    )
    ledger.terminal(
        "baseline_candidate",
        {
            "run_id": baseline.run_id,
            "trajectory_sha256": canonical_sha256(baseline.model_dump(mode="json")),
            "runtime_context": "bare_s0",
        },
    )
    ledger.claim(
        "baseline_internal_evaluator",
        {
            "task_id": task.task_id,
            "run_id": baseline.run_id,
            "evaluator_id": "evaluator-id",
            "paper_evaluator": False,
        },
    )
    ledger.terminal(
        "baseline_internal_evaluator",
        {
            "evaluator_run_id": evaluation.evaluator_run_id,
            "feedback_sha256": canonical_sha256(evaluation.model_dump(mode="json")),
        },
    )
    for phase, artifact_type in (
        ("reflector_memory", "text_memory"),
        ("reflector_skill_bundle", "skill_bundle"),
        ("reflector_agent_system", "agent_system"),
    ):
        ledger.claim(
            phase,
            {
                "task_id": task.task_id,
                "pair_id": pair_id,
                "parent_run_id": baseline.run_id,
                "artifact_type": artifact_type,
                "input_evidence_hash": evidence_hash,
                "sibling_artifact_ids": [],
                "paper_evaluator_feedback_included": False,
            },
        )

    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps(
            {
                "payload": {
                    "session_result": {
                        "metadata": {
                            "input_evidence": evidence,
                            "input_evidence_sha256": evidence_hash,
                        }
                    }
                }
            }
        )
    )
    completion_path = tmp_path / "completion.json"
    completion_path.write_text(
        json.dumps(
            {
                "session_id": "session",
                "task_id": f"{pair_id}-reflector_memory-core-baseline-abcdef",
                "status": "ERROR",
                "error": (
                    "agent execution failed: [Errno 7] Argument list too long: "
                    "'/usr/bin/docker'"
                ),
                "trajectory": {"metadata": {"record_count": 0}, "traces": []},
                "workspace_result": None,
                "metadata": {
                    "execution_route": CORE_MANAGED_CODEX_ROUTE,
                    "host_codex_exec_forbidden": True,
                },
            }
        )
    )
    db_path = tmp_path / "core.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE jobs (
                job_id TEXT, job_type TEXT, method TEXT, state TEXT,
                lease_id TEXT, error TEXT, config_json TEXT
            )
            """
        )
        conn.execute("CREATE TABLE artifacts (staging_job_id TEXT)")
        conn.execute(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "job-failed",
                "chemcrow_task_local_isolated_reflection",
                "text_memory_reflector",
                "running",
                "lease-id",
                None,
                json.dumps({"input_evidence_sha256": evidence_hash}),
            ),
        )

    def fail_job(url, *, json, timeout, trust_env):
        assert url.endswith("/v1/jobs/job-failed/fail")
        assert json["retryable"] is False
        return _Response({"job_id": "job-failed", "state": "failed"})

    monkeypatch.setattr("openevo_chemcrow.reflector_recovery.httpx.post", fail_job)

    _, _, receipt = reconcile_reflector_no_effect_failure(
        config_path=config_path,
        run_root=run_root,
        ledger_root=ledger_root,
        experiment_id=experiment_id,
        task=task,
        expected_candidate_config_sha256="candidate-hash",
        expected_evaluator_id="evaluator-id",
        backend_url="http://127.0.0.1:8200",
        evolution_db_path=db_path,
        failed_phase="reflector_memory",
        failed_job_id="job-failed",
        core_completion_path=completion_path,
        evidence_event_path=event_path,
    )
    assert receipt["model_calls_during_reconciliation"] == 0
    recovered = load_reflector_boundary_checkpoint(
        run_root=run_root,
        ledger_root=ledger_root,
        pair_id=pair_id,
        task=task,
        expected_candidate_config_sha256="candidate-hash",
        expected_evaluator_id="evaluator-id",
    )
    assert recovered is not None
    recovered_baseline, recovered_evaluation, phases = recovered
    assert recovered_baseline == baseline
    assert recovered_evaluation == evaluation
    assert phases == {
        "reflector_memory",
        "reflector_skill_bundle",
        "reflector_agent_system",
    }
