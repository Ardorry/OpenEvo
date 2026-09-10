from __future__ import annotations

import json
import sqlite3

import pytest

from openevo_chemcrow.hashing import canonical_sha256
from openevo_chemcrow.ledger import PhaseLedger
from openevo_chemcrow.models import (
    ArtifactKind,
    EvaluatorFeedback,
    RubricScores,
    TaskItem,
    Trajectory,
)
from openevo_chemcrow.reflector_recovery import (
    _latest_failed_job_batch,
    load_partial_reflector_boundary_checkpoint,
    load_reflector_boundary_checkpoint,
    reconcile_paused_reflector_batch,
    reconcile_reflector_no_effect_failure,
)
from openevo_chemcrow.runtime import CORE_MANAGED_CODEX_ROUTE
from openevo_chemcrow.three_artifact_evolution import _core_reflector_contract_hash


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def test_latest_failed_job_batch_uses_shared_abort_time_across_create_seconds():
    historical = [
        {
            "job_id": f"old-{index}",
            "method": method,
            "created_at": "2026-08-30T15:00:00Z",
            "updated_at": "2026-08-30T15:01:00Z",
        }
        for index, method in enumerate(
            ("text_memory_reflector", "skill_bundle_reflector", "agent_system_reflector")
        )
    ]
    current = [
        {
            "job_id": f"new-{index}",
            "method": method,
            "created_at": created_at,
            "updated_at": "2026-08-30T15:20:21Z",
        }
        for index, (method, created_at) in enumerate(
            (
                ("text_memory_reflector", "2026-08-30T15:18:44Z"),
                ("skill_bundle_reflector", "2026-08-30T15:18:45Z"),
                ("agent_system_reflector", "2026-08-30T15:18:45Z"),
            )
        )
    ]

    latest, older = _latest_failed_job_batch(historical + current)

    assert {row["job_id"] for row in latest} == {"new-0", "new-1", "new-2"}
    assert {row["job_id"] for row in older} == {"old-0", "old-1", "old-2"}


@pytest.mark.parametrize(
    (
        "completion_error",
        "credential_contract",
        "expected_failure_kind",
        "canary_model_call_may_have_occurred",
        "batch_prepared",
    ),
    [
        (
            "agent execution failed: [Errno 7] Argument list too long: '/usr/bin/docker'",
            None,
            "docker_argv_limit_before_reflector_process_start",
            False,
            False,
        ),
        (
            (
                "agent execution failed: Codex subscription credential isolation could not be "
                "proven (validation_failed)"
            ),
            {"policy_sha256": "policy"},
            "credential_isolation_validation_failed_before_reflector_process_start",
            True,
            False,
        ),
        (
            (
                "agent execution failed: Codex subscription credential isolation could not be "
                "proven (validation_failed)"
            ),
            {"policy_sha256": "policy"},
            "credential_isolation_validation_failed_before_reflector_process_start",
            True,
            True,
        ),
    ],
)
def test_reflector_pre_exec_failure_recovers_exact_g1_without_redispatch(
    tmp_path,
    monkeypatch,
    completion_error,
    credential_contract,
    expected_failure_kind,
    canary_model_call_may_have_occurred,
    batch_prepared,
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
    completion_metadata = {
        "execution_route": CORE_MANAGED_CODEX_ROUTE,
        "host_codex_exec_forbidden": True,
    }
    if credential_contract is not None:
        completion_metadata["openevo"] = {
            "credential_isolation": credential_contract,
        }
    completion_path.write_text(
        json.dumps(
            {
                "session_id": "session",
                "task_id": f"{pair_id}-reflector_memory-core-baseline-abcdef",
                "status": "ERROR",
                "error": completion_error,
                "trajectory": {"metadata": {"record_count": 0}, "traces": []},
                "workspace_result": None,
                "metadata": completion_metadata,
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
                "failed" if batch_prepared else "running",
                None if batch_prepared else "lease-id",
                (
                    "Core-managed reflector_memory returned no completed artifact"
                    if batch_prepared
                    else None
                ),
                json.dumps({"input_evidence_sha256": evidence_hash}),
            ),
        )
        if batch_prepared:
            for job_id, method in (
                ("job-skill", "skill_bundle_reflector"),
                ("job-system", "agent_system_reflector"),
            ):
                conn.execute(
                    "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        job_id,
                        "chemcrow_task_local_isolated_reflection",
                        method,
                        "failed",
                        None,
                        "Core-managed reflector_memory returned no completed artifact",
                        json.dumps({"input_evidence_sha256": evidence_hash}),
                    ),
                )

    def fail_job(url, *, json, timeout, trust_env):
        assert not batch_prepared
        assert url.endswith("/v1/jobs/job-failed/fail")
        assert json["retryable"] is False
        assert json["error"].endswith(expected_failure_kind)
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
    recovered_claim = json.loads((ledger_root / pair_id / "reflector_memory.json").read_text())
    failed_receipt = recovered_claim["failed_attempts"][-1]["receipt"]
    assert failed_receipt["failure_kind"] == expected_failure_kind
    assert (
        failed_receipt["infrastructure_canary_model_call_may_have_occurred"]
        is canary_model_call_may_have_occurred
    )


def test_paused_reflector_batch_preserves_completed_prefix_and_replaces_suffix(
    tmp_path, monkeypatch
):
    experiment_id = "paused-experiment"
    task = TaskItem(
        task_id="chemcrow-08",
        prompt="Plan a catalyst synthesis.",
        broad_category="catalyst_synthesis",
        provenance={"source": "test"},
        sanitized_item_sha256="task-hash",
    )
    pair_id = f"{experiment_id}--{task.task_id}"
    run_root = tmp_path / "run"
    ledger_root = run_root / "claims"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("experiment: paused\n")
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
            task_completion=3,
        ),
        actionable_critique=["Add operational detail."],
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
    phase_kinds = {
        "reflector_memory": ArtifactKind.TEXT_MEMORY,
        "reflector_skill_bundle": ArtifactKind.SKILL_BUNDLE,
        "reflector_agent_system": ArtifactKind.AGENT_SYSTEM,
    }
    for phase, kind in phase_kinds.items():
        ledger.claim(
            phase,
            {
                "task_id": task.task_id,
                "pair_id": pair_id,
                "parent_run_id": baseline.run_id,
                "artifact_type": kind.value,
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
    prompt_root = run_root / pair_id / "reflector_prompts"
    prompt_root.mkdir(parents=True)
    prompts = {kind: f"full prompt for {kind.value}\n" for kind in phase_kinds.values()}
    for kind, prompt in prompts.items():
        (prompt_root / f"{kind.value}.md").write_text(prompt)
    (prompt_root / "freeze.receipt.json").write_text(
        json.dumps(
            {
                "prompt_sha256_by_type": {
                    kind.value: canonical_sha256({"prompt": prompt})
                    for kind, prompt in prompts.items()
                }
            }
        )
    )
    completion_root = tmp_path / "completions"
    completion_path = (
        completion_root
        / f"task_{pair_id}-reflector_memory-core-baseline-abcdef"
        / "ses_memory.json"
    )
    completion_path.parent.mkdir(parents=True)
    memory_content = "# Reusable memory\n\nCheck stereochemistry and workup details."
    completion_path.write_text(
        json.dumps(
            {
                "session_id": "session-memory",
                "task_id": f"{pair_id}-reflector_memory-core-baseline-abcdef",
                "status": "COMPLETED",
                "error": None,
                "trajectory": {
                    "metadata": {"task_metadata": {"agent_model_name": "gpt-5.5"}},
                    "traces": [
                        {
                            "prompt_messages": [
                                {
                                    "role": "user",
                                    "content": prompts[ArtifactKind.TEXT_MEMORY],
                                }
                            ],
                            "response_messages": [
                                {"role": "assistant", "content": memory_content}
                            ],
                        }
                    ],
                },
                "timing": {"run_ms": 1250.0},
                "metadata": {
                    "execution_route": CORE_MANAGED_CODEX_ROUTE,
                    "host_codex_exec_forbidden": True,
                },
                "workspace_result": None,
            }
        )
    )
    db_path = tmp_path / "core.sqlite3"
    methods = {
        "reflector_memory": "text_memory_reflector",
        "reflector_skill_bundle": "skill_bundle_reflector",
        "reflector_agent_system": "agent_system_reflector",
    }
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
        for phase, method in methods.items():
            kind = phase_kinds[phase]
            conn.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    f"job-{phase}",
                    "chemcrow_task_local_isolated_reflection",
                    method,
                    "running",
                    f"lease-{phase}",
                    None,
                    json.dumps(
                        {
                            "input_evidence_sha256": evidence_hash,
                            "reflector_system_sha256": _core_reflector_contract_hash(
                                kind, "core_full_worker_v1"
                            ),
                        }
                    ),
                ),
            )

    failed_jobs = []

    def fail_job(url, *, json, timeout, trust_env):
        failed_jobs.append((url, json))
        return _Response({"job_id": url.rsplit("/", 2)[-2], "state": "failed"})

    monkeypatch.setattr("openevo_chemcrow.reflector_recovery.httpx.post", fail_job)
    _, _, receipt = reconcile_paused_reflector_batch(
        config_path=config_path,
        run_root=run_root,
        ledger_root=ledger_root,
        experiment_id=experiment_id,
        task=task,
        expected_candidate_config_sha256="candidate-hash",
        expected_evaluator_id="evaluator-id",
        backend_url="http://127.0.0.1:8200",
        evolution_db_path=db_path,
        evidence_event_path=event_path,
        core_completion_root=completion_root,
        completed_sibling_paths={"reflector_memory": completion_path},
    )
    assert len(failed_jobs) == 3
    assert receipt["model_calls_during_reconciliation"] == 0
    assert receipt["preserved_model_call_phases"] == ["reflector_memory"]
    assert receipt["replacement_phases"] == [
        "reflector_skill_bundle",
        "reflector_agent_system",
    ]
    recovered = load_partial_reflector_boundary_checkpoint(
        run_root=run_root,
        ledger_root=ledger_root,
        pair_id=pair_id,
        task=task,
        expected_candidate_config_sha256="candidate-hash",
        expected_evaluator_id="evaluator-id",
    )
    assert recovered is not None
    recovered_baseline, recovered_evaluation, replacements, outputs = recovered
    assert recovered_baseline == baseline
    assert recovered_evaluation == evaluation
    assert replacements == {"reflector_skill_bundle", "reflector_agent_system"}
    assert outputs[ArtifactKind.TEXT_MEMORY].content == memory_content
    memory_claim = json.loads((ledger_root / pair_id / "reflector_memory.json").read_text())
    skill_claim = json.loads(
        (ledger_root / pair_id / "reflector_skill_bundle.json").read_text()
    )
    system_claim = json.loads(
        (ledger_root / pair_id / "reflector_agent_system.json").read_text()
    )
    assert memory_claim["status"] == "claimed"
    assert skill_claim["status"] == "replacement_ready"
    assert system_claim["status"] == "replacement_ready"
