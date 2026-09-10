from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest
from openevo.runtime.codex_isolation import codex_subscription_contract

from openevo_chemcrow.compatible_evaluation import (
    build_evolution_judge_task,
    normalize_internal_confidence,
)
from openevo_chemcrow.evolved_candidate_recovery import (
    _recovery_source_paths,
    load_evolved_candidate_boundary_checkpoint,
    reconcile_evolved_candidate_no_effect_failure,
)
from openevo_chemcrow.feedback import reflector_feedback_payload, runtime_feedback
from openevo_chemcrow.hashing import canonical_sha256
from openevo_chemcrow.ledger import PhaseLedger
from openevo_chemcrow.models import (
    ArtifactKind,
    EvaluatorFeedback,
    FeedbackMode,
    RubricScores,
    Trajectory,
)
from openevo_chemcrow.replacement_ledger import VerifiedReplacementPhaseLedger
from openevo_chemcrow.runtime import (
    CORE_MANAGED_CODEX_ROUTE,
    build_task_request,
    s0_config_hash,
)
from openevo_chemcrow.three_artifact_evolution import (
    ThreeIsolatedEvolutionEngine,
    render_core_native_reflector_prompt,
)
from openevo_chemcrow.three_artifact_models import (
    THREE_ARTIFACT_ORDER,
    ArtifactSeparationPolicy,
)

_CONTENTS = {
    ArtifactKind.TEXT_MEMORY: (
        "# Verified task memory\n\nThe baseline established one observable molecular fact "
        "and should retain the exact unit and source boundary."
    ),
    ArtifactKind.SKILL_BUNDLE: (
        "# Evidence workflow\n\n1. Parse the requested property.\n2. Invoke only the "
        "declared tool.\n3. Reconcile units before drafting the answer."
    ),
    ArtifactKind.AGENT_SYSTEM: (
        "# Task-local execution policy\n\nAnswer every explicit obligation using observable "
        "evidence, label uncertainty, and avoid claiming physical execution."
    ),
}


class _ManagedReflector:
    def __init__(self, *, kind: ArtifactKind, candidate: dict) -> None:
        self.kind = kind
        self.candidate = deepcopy(candidate)
        self.config_sha256 = s0_config_hash(self.candidate)

    def run_candidate(self, *, task, role, artifact_ids, pair_id, mcp_url):
        assert role == "baseline"
        assert artifact_ids == []
        assert mcp_url is None
        suffix = {
            ArtifactKind.TEXT_MEMORY: "1111111111",
            ArtifactKind.SKILL_BUNDLE: "2222222222",
            ArtifactKind.AGENT_SYSTEM: "3333333333",
        }[self.kind]
        return Trajectory(
            run_id=f"{pair_id}-baseline-{suffix}",
            task_id=task.task_id,
            role="baseline",
            status="COMPLETED",
            answer=_CONTENTS[self.kind],
            candidate_config_sha256=self.config_sha256,
        )


def _successful_completion(*, run_id: str, prompt: str, answer: str) -> dict:
    return {
        "task_id": run_id,
        "session_id": f"session-{canonical_sha256({'run_id': run_id})[:16]}",
        "status": "COMPLETED",
        "error": None,
        "workspace_result": None,
        "timing": {"agent": 1250},
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
                    "metadata": {},
                }
            ],
        },
    }


def _write_completion(root: Path, payload: dict) -> Path:
    directory = root / f"task_{payload['task_id']}"
    directory.mkdir(parents=True)
    path = directory / f"{payload['session_id']}.json"
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


def _setup_recoverable_boundary(tmp_path, task_item, core_candidate_config):
    experiment_id = "experiment"
    pair_id = f"{experiment_id}--{task_item.task_id}"
    run_root = tmp_path / "run"
    ledger_root = run_root / "claims"
    completion_root = tmp_path / "completions"
    artifact_root = tmp_path / "artifacts"
    db_path = tmp_path / "core.sqlite3"
    config_path = tmp_path / "config.yaml"
    config_path.write_text("artifact_protocol: three_isolated_core_native_v2\n")
    candidate_hash = s0_config_hash(core_candidate_config)
    baseline = Trajectory(
        run_id=f"{pair_id}-baseline-0000000000",
        task_id=task_item.task_id,
        role="baseline",
        status="COMPLETED",
        answer="A preserved baseline answer with one observable result.",
        candidate_config_sha256=candidate_hash,
        wall_time_seconds=1.0,
    )
    evaluation = EvaluatorFeedback(
        evaluator_role="evolution_evaluator",
        evaluator_run_id=f"{baseline.run_id}-evolution-eval-baseline-4444444444",
        scores=RubricScores(
            chemical_correctness=3,
            reasoning_quality=3,
            task_completion=2,
        ),
        strengths=["Uses the observable result."],
        weaknesses=["One obligation is incomplete."],
        actionable_critique=["Complete the missing obligation with explicit evidence."],
        confidence=0.8,
    )
    feedback = reflector_feedback_payload(
        mode=FeedbackMode.F2,
        trajectory=baseline,
        runtime=runtime_feedback(baseline),
        evaluator=evaluation,
    )
    reflector_configs = {kind: deepcopy(core_candidate_config) for kind in THREE_ARTIFACT_ORDER}
    ports = {
        kind: _ManagedReflector(kind=kind, candidate=reflector_configs[kind])
        for kind in THREE_ARTIFACT_ORDER
    }
    separation = ArtifactSeparationPolicy()
    engine = ThreeIsolatedEvolutionEngine(
        run_root=run_root,
        reflector_rollouts=ports,
        evolution_db_path=db_path,
        evolution_artifact_root=artifact_root,
        separation_policy=separation,
    )
    bundle = engine.evolve_all(
        task=task_item,
        baseline=baseline,
        feedback_payload=feedback,
        pair_id=pair_id,
    )

    ledger = PhaseLedger(ledger_root, pair_id=pair_id)
    ledger.claim(
        "baseline_candidate",
        {
            "task_id": task_item.task_id,
            "s0_hash": candidate_hash,
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
    evaluator_id = "evolution-evaluator-config"
    ledger.claim(
        "baseline_internal_evaluator",
        {
            "task_id": task_item.task_id,
            "run_id": baseline.run_id,
            "evaluator_id": evaluator_id,
            "paper_evaluator": False,
        },
    )
    _, parse_receipt = normalize_internal_confidence(0.8)
    ledger.terminal(
        "baseline_internal_evaluator",
        {
            "evaluator_run_id": evaluation.evaluator_run_id,
            "feedback_sha256": canonical_sha256(evaluation.model_dump(mode="json")),
            "confidence_parse_receipt": parse_receipt,
        },
    )
    phase_by_kind = {
        ArtifactKind.TEXT_MEMORY: "reflector_memory",
        ArtifactKind.SKILL_BUNDLE: "reflector_skill_bundle",
        ArtifactKind.AGENT_SYSTEM: "reflector_agent_system",
    }
    for receipt in bundle.artifacts:
        phase = phase_by_kind[receipt.artifact_type]
        ledger.claim(
            phase,
            {
                "task_id": task_item.task_id,
                "pair_id": pair_id,
                "parent_run_id": baseline.run_id,
                "artifact_type": receipt.artifact_type.value,
                "input_evidence_hash": bundle.input_evidence_hash,
                "sibling_artifact_ids": [],
                "paper_evaluator_feedback_included": False,
            },
        )
        ledger.terminal(
            phase,
            {
                "reflector_job_id": receipt.reflector_job_id,
                "reflector_run_id": receipt.reflector_run_id,
                "model": receipt.model,
                "prompt_hash": receipt.prompt_hash,
                "input_evidence_hash": receipt.input_evidence_hash,
                "artifact_id": receipt.artifact_id,
                "artifact_hash": receipt.artifact_hash,
                "artifact_type": receipt.artifact_type.value,
                "registration_receipt_sha256": (receipt.registration_receipt_sha256),
            },
        )
    ledger.claim(
        "evolved_candidate",
        {
            "task_id": task_item.task_id,
            "s0_hash": candidate_hash,
            "artifact_ids_by_type": bundle.artifact_id_by_type(),
            "artifact_count": 3,
        },
    )

    _write_completion(
        completion_root,
        _successful_completion(
            run_id=baseline.run_id,
            prompt="baseline prompt is preserved independently by the evidence event",
            answer=baseline.answer,
        ),
    )
    judge_task = build_evolution_judge_task(task=task_item, trajectory=baseline)
    evaluator_prompt = build_task_request(
        task=judge_task,
        run_id=evaluation.evaluator_run_id,
        role="baseline",
        candidate=core_candidate_config,
        artifact_ids=[],
        mcp_url=None,
    )["instruction"]
    evaluator_answer = json.dumps(
        {
            "scores": evaluation.scores.model_dump(mode="json"),
            "strengths": evaluation.strengths,
            "weaknesses": evaluation.weaknesses,
            "actionable_critique": evaluation.actionable_critique,
            "confidence": evaluation.confidence,
        }
    )
    _write_completion(
        completion_root,
        _successful_completion(
            run_id=evaluation.evaluator_run_id,
            prompt=evaluator_prompt,
            answer=evaluator_answer,
        ),
    )
    for receipt in bundle.artifacts:
        prompt = render_core_native_reflector_prompt(
            receipt.artifact_type,
            {
                "task": {
                    "task_id": task_item.task_id,
                    "prompt": task_item.prompt,
                    "sanitized_item_sha256": task_item.sanitized_item_sha256,
                },
                "baseline": baseline.model_dump(mode="json"),
                "feedback": feedback,
            },
        )
        reflected_task = task_item.model_copy(
            update={
                "task_id": f"{task_item.task_id}-{phase_by_kind[receipt.artifact_type]}",
                "prompt": prompt,
                "sanitized_item_sha256": canonical_sha256({"prompt": prompt}),
            }
        )
        reflected_prompt = build_task_request(
            task=reflected_task,
            run_id=receipt.reflector_run_id,
            role="baseline",
            candidate=reflector_configs[receipt.artifact_type],
            artifact_ids=[],
            mcp_url=None,
        )["instruction"]
        _write_completion(
            completion_root,
            _successful_completion(
                run_id=receipt.reflector_run_id,
                prompt=reflected_prompt,
                answer=_CONTENTS[receipt.artifact_type],
            ),
        )

    artifact_ids = bundle.artifact_ids()
    failed_run_id = f"{pair_id}-evolved-5555555555"
    failed_metadata = {
        "execution_route": CORE_MANAGED_CODEX_ROUTE,
        "host_codex_exec_forbidden": True,
        "run_id": failed_run_id,
        "evolution": {
            "context_id": "ctx-recovered",
            "context_injected": True,
            "context_artifact_ids": artifact_ids,
        },
        "openevo": {
            "credential_isolation": codex_subscription_contract(),
            "revision_id": "chemcrow-task-local-three:" + canonical_sha256(artifact_ids),
        },
    }
    failed = {
        "task_id": failed_run_id,
        "session_id": "session-failed-g2",
        "status": "ERROR",
        "error": (
            "agent execution failed: Codex subscription credential isolation could "
            "not be proven (validation_failed)"
        ),
        "workspace_result": None,
        "timing": {"agent": 100},
        "metadata": failed_metadata,
        "trajectory": {
            "metadata": {
                "builder": "agent_transcript",
                "record_count": 0,
                "task_metadata": deepcopy(failed_metadata),
            },
            "traces": [],
        },
    }
    failed_path = _write_completion(completion_root, failed)
    return {
        "experiment_id": experiment_id,
        "pair_id": pair_id,
        "run_root": run_root,
        "ledger_root": ledger_root,
        "completion_root": completion_root,
        "artifact_root": artifact_root,
        "db_path": db_path,
        "config_path": config_path,
        "candidate_hash": candidate_hash,
        "baseline": baseline,
        "evaluation": evaluation,
        "evaluator_id": evaluator_id,
        "reflector_configs": reflector_configs,
        "separation": separation,
        "bundle": bundle,
        "failed_path": failed_path,
    }


def _reconcile(boundary, task_item, *, failed_path=None):
    return reconcile_evolved_candidate_no_effect_failure(
        config_path=boundary["config_path"],
        run_root=boundary["run_root"],
        ledger_root=boundary["ledger_root"],
        experiment_id=boundary["experiment_id"],
        task=task_item,
        candidate_config_sha256=boundary["candidate_hash"],
        evaluator_config=next(iter(boundary["reflector_configs"].values())),
        evaluator_id=boundary["evaluator_id"],
        feedback_mode=FeedbackMode.F2,
        reflector_configs=boundary["reflector_configs"],
        separation_policy=boundary["separation"],
        evolution_db_path=boundary["db_path"],
        evolution_artifact_root=boundary["artifact_root"],
        core_completion_root=boundary["completion_root"],
        failed_core_completion_path=failed_path or boundary["failed_path"],
    )


def _record_second_no_effect_attempt(boundary, task_item):
    claim_path = boundary["ledger_root"] / boundary["pair_id"] / "evolved_candidate.json"
    claim = json.loads(claim_path.read_text())
    ledger = VerifiedReplacementPhaseLedger(
        boundary["ledger_root"],
        pair_id=boundary["pair_id"],
    )
    ledger.claim(
        "evolved_candidate",
        claim["authority"],
        allow_verified_replacement=True,
        expected_replacement=ledger.replacement_expectation("evolved_candidate"),
    )
    active_claim = json.loads(claim_path.read_text())
    replacement_run_id = active_claim["active_attempt_run_id"]
    payload = deepcopy(json.loads(boundary["failed_path"].read_text()))
    payload["task_id"] = replacement_run_id
    payload["session_id"] = "session-failed-g2-attempt-2"
    payload["metadata"]["run_id"] = replacement_run_id
    payload["metadata"]["evolution"]["context_id"] = "ctx-recovered-attempt-2"
    payload["trajectory"]["metadata"]["task_metadata"] = deepcopy(payload["metadata"])
    failed_path = _write_completion(boundary["completion_root"], payload)
    _, recovery_path, _ = _reconcile(
        boundary,
        task_item,
        failed_path=failed_path,
    )
    return failed_path, recovery_path


def _load_checkpoint(
    boundary,
    task_item,
    *,
    reflector_configs=None,
    core_completion_root="default",
):
    if core_completion_root == "default":
        core_completion_root = boundary["completion_root"]
    return load_evolved_candidate_boundary_checkpoint(
        config_path=boundary["config_path"],
        run_root=boundary["run_root"],
        ledger_root=boundary["ledger_root"],
        pair_id=boundary["pair_id"],
        task=task_item,
        expected_candidate_config_sha256=boundary["candidate_hash"],
        evaluator_config=next(iter(boundary["reflector_configs"].values())),
        expected_evaluator_id=boundary["evaluator_id"],
        feedback_mode=FeedbackMode.F2,
        reflector_configs=reflector_configs or boundary["reflector_configs"],
        separation_policy=boundary["separation"],
        evolution_db_path=boundary["db_path"],
        evolution_artifact_root=boundary["artifact_root"],
        core_completion_root=core_completion_root,
    )


def test_reconcile_evolved_setup_failure_preserves_five_scientific_calls(
    tmp_path, task_item, core_candidate_config
):
    boundary = _setup_recoverable_boundary(tmp_path, task_item, core_candidate_config)
    checkpoint_path, recovery_path, receipt = _reconcile(boundary, task_item)

    assert checkpoint_path.is_file()
    assert recovery_path.is_file()
    assert receipt["replacement_scope"] == ["evolved_candidate"]
    assert receipt["completed_scientific_calls_reused"] == 5
    assert receipt["prior_scientific_calls_redispatched"] is False
    assert receipt["model_calls_during_reconciliation"] == 0
    assert receipt["artifact_ids_by_type"] == boundary["bundle"].artifact_id_by_type()
    assert len(set(receipt["reflector_job_ids"])) == 3
    assert len(set(receipt["reflector_run_ids"])) == 3
    assert len(set(receipt["reflector_prompt_hashes"])) == 3
    claim = json.loads(
        (boundary["ledger_root"] / boundary["pair_id"] / "evolved_candidate.json").read_text()
    )
    assert claim["status"] == "replacement_ready"
    phase_receipt = claim["failed_attempts"][0]["receipt"]
    assert phase_receipt["evolved_candidate_model_call_proven_absent"] is True
    assert phase_receipt["infrastructure_canary_model_call_may_have_occurred"] is True
    assert set(phase_receipt["no_effect_predicates"].values()) == {True}

    loaded = _load_checkpoint(boundary, task_item)
    assert loaded is not None
    baseline, evaluation, bundle, replacement_run_id = loaded
    assert baseline == boundary["baseline"]
    assert evaluation == boundary["evaluation"]
    assert bundle.artifact_ids() == boundary["bundle"].artifact_ids()
    assert replacement_run_id == phase_receipt["replacement_run_id"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload["trajectory"].update({"traces": [{}]}), "zero_traces"),
        (
            lambda payload: payload["trajectory"]["metadata"].update({"record_count": 1}),
            "zero_records",
        ),
        (lambda payload: payload.update({"workspace_result": {}}), "workspace_result"),
        (
            lambda payload: payload["trajectory"]["metadata"]["task_metadata"]["evolution"].update(
                {"runtime_injection_receipt": {}}
            ),
            "runtime_injection_receipt",
        ),
        (
            lambda payload: payload["trajectory"]["metadata"]["task_metadata"]["evolution"][
                "context_artifact_ids"
            ].reverse(),
            "exact_three_context",
        ),
        (
            lambda payload: payload["metadata"]["openevo"].update(
                {"credential_isolation_receipt": {"status": "passed"}}
            ),
            "credential_readiness",
        ),
        (
            lambda payload: (
                payload["metadata"]["openevo"].update(
                    {"credential_isolation": {"arbitrary": "self-attestation"}}
                ),
                payload["trajectory"]["metadata"]["task_metadata"]["openevo"].update(
                    {"credential_isolation": {"arbitrary": "self-attestation"}}
                ),
            ),
            "credential_contract_present",
        ),
        (
            lambda payload: payload["trajectory"]["metadata"]["task_metadata"]["openevo"].update(
                {"credential_isolation_receipt": {"status": "passed"}}
            ),
            "credential_readiness",
        ),
        (
            lambda payload: payload["trajectory"]["metadata"]["task_metadata"].update(
                {"run_id": "different-evolved-run"}
            ),
            "core_route_bound|session_identity_present",
        ),
    ],
)
def test_reconcile_refuses_ambiguous_or_nonzero_g2_effect(
    tmp_path,
    task_item,
    core_candidate_config,
    mutation,
    message,
):
    boundary = _setup_recoverable_boundary(tmp_path, task_item, core_candidate_config)
    payload = json.loads(boundary["failed_path"].read_text())
    mutation(payload)
    boundary["failed_path"].write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        _reconcile(boundary, task_item)
    claim = json.loads(
        (boundary["ledger_root"] / boundary["pair_id"] / "evolved_candidate.json").read_text()
    )
    assert claim["status"] == "claimed"
    assert not (boundary["run_root"] / "recovery").exists()


def test_checkpoint_load_refuses_registered_artifact_drift(
    tmp_path, task_item, core_candidate_config
):
    boundary = _setup_recoverable_boundary(tmp_path, task_item, core_candidate_config)
    _reconcile(boundary, task_item)
    receipt = boundary["bundle"].artifacts[0]
    content_path = (
        boundary["artifact_root"]
        / "workers"
        / receipt.reflector_job_id
        / "text_memory_reflector"
        / "memory.md"
    )
    content_path.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(ValueError, match="artifact bytes differ"):
        _load_checkpoint(boundary, task_item)


def test_checkpoint_load_refuses_recovery_mapping_drift(
    tmp_path, task_item, core_candidate_config
):
    boundary = _setup_recoverable_boundary(tmp_path, task_item, core_candidate_config)
    _, recovery_path, _ = _reconcile(boundary, task_item)
    recovery = json.loads(recovery_path.read_text())
    recovery["reflector_job_ids"].reverse()
    recovery_path.write_text(json.dumps(recovery), encoding="utf-8")

    with pytest.raises(ValueError, match="semantic authority"):
        _load_checkpoint(boundary, task_item)


def test_checkpoint_load_refuses_core_job_row_drift(tmp_path, task_item, core_candidate_config):
    boundary = _setup_recoverable_boundary(tmp_path, task_item, core_candidate_config)
    _reconcile(boundary, task_item)
    job_id = boundary["bundle"].artifacts[0].reflector_job_id
    with sqlite3.connect(boundary["db_path"]) as connection:
        raw_config = connection.execute(
            "SELECT config_json FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()[0]
        config = json.loads(raw_config)
        config["unexpected_adapter_shaping"] = True
        connection.execute(
            "UPDATE jobs SET config_json = ? WHERE job_id = ?",
            (json.dumps(config), job_id),
        )

    with pytest.raises(ValueError, match="Core Reflector authority differs"):
        _load_checkpoint(boundary, task_item)


def test_reconcile_refuses_reusing_old_completion_for_active_replacement(
    tmp_path, task_item, core_candidate_config
):
    boundary = _setup_recoverable_boundary(tmp_path, task_item, core_candidate_config)
    _reconcile(boundary, task_item)
    claim_path = boundary["ledger_root"] / boundary["pair_id"] / "evolved_candidate.json"
    claim = json.loads(claim_path.read_text())
    ledger = VerifiedReplacementPhaseLedger(
        boundary["ledger_root"],
        pair_id=boundary["pair_id"],
    )
    ledger.claim(
        "evolved_candidate",
        claim["authority"],
        allow_verified_replacement=True,
        expected_replacement=ledger.replacement_expectation("evolved_candidate"),
    )

    with pytest.raises(ValueError, match="completion identity differs"):
        _reconcile(boundary, task_item)
    active = json.loads(claim_path.read_text())
    assert active["status"] == "claimed"
    assert (
        active["active_attempt_run_id"]
        == active["failed_attempts"][0]["receipt"]["replacement_run_id"]
    )


def test_checkpoint_load_requires_completion_root_authority(
    tmp_path, task_item, core_candidate_config
):
    boundary = _setup_recoverable_boundary(tmp_path, task_item, core_candidate_config)
    _reconcile(boundary, task_item)

    with pytest.raises(ValueError, match="--core-completions"):
        _load_checkpoint(boundary, task_item, core_completion_root=None)


def test_checkpoint_load_refuses_reflector_completion_drift(
    tmp_path, task_item, core_candidate_config
):
    boundary = _setup_recoverable_boundary(tmp_path, task_item, core_candidate_config)
    _reconcile(boundary, task_item)
    reflector_run_id = boundary["bundle"].artifacts[0].reflector_run_id
    completion_path = next(
        (boundary["completion_root"] / f"task_{reflector_run_id}").glob("*.json")
    )
    completion = json.loads(completion_path.read_text())
    completion["metadata"]["unbound_drift"] = True
    completion_path.write_text(json.dumps(completion), encoding="utf-8")

    with pytest.raises(ValueError, match="pre-G2 scientific authority drifted"):
        _load_checkpoint(boundary, task_item)


def test_checkpoint_load_refuses_reflector_event_drift(tmp_path, task_item, core_candidate_config):
    boundary = _setup_recoverable_boundary(tmp_path, task_item, core_candidate_config)
    _reconcile(boundary, task_item)
    event_path = min((boundary["artifact_root"] / "events").glob("*.json"))
    event = json.loads(event_path.read_text())
    event["unbound_drift"] = True
    event_path.write_text(json.dumps(event), encoding="utf-8")

    with pytest.raises(ValueError, match="pre-G2 scientific authority drifted"):
        _load_checkpoint(boundary, task_item)


def test_checkpoint_load_refuses_reflector_config_or_prompt_authority_drift(
    tmp_path, task_item, core_candidate_config
):
    boundary = _setup_recoverable_boundary(tmp_path, task_item, core_candidate_config)
    _reconcile(boundary, task_item)
    reflector_configs = deepcopy(boundary["reflector_configs"])
    reflector_configs[ArtifactKind.TEXT_MEMORY]["metadata"]["policy_version"] = "drifted"

    with pytest.raises(ValueError, match="Core Reflector authority differs"):
        _load_checkpoint(
            boundary,
            task_item,
            reflector_configs=reflector_configs,
        )


def test_checkpoint_load_refuses_same_task_id_with_changed_prompt(
    tmp_path, task_item, core_candidate_config
):
    boundary = _setup_recoverable_boundary(tmp_path, task_item, core_candidate_config)
    _reconcile(boundary, task_item)
    changed_task_payload = task_item.model_dump(mode="json")
    changed_task_payload["prompt"] = task_item.prompt + " Changed after recovery."
    changed_task_payload.pop("sanitized_item_sha256")
    changed_task = task_item.model_validate(
        {
            **changed_task_payload,
            "sanitized_item_sha256": canonical_sha256(changed_task_payload),
        }
    )

    with pytest.raises(ValueError, match="checkpoint authority differs"):
        _load_checkpoint(boundary, changed_task)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("broad_category", "drifted-category"),
        ("allowed_tool_metadata", {"registry": "drifted"}),
        ("safety_metadata", {"benchmark_only": False}),
        (
            "provenance",
            {
                "source_notebook": "tasks/drifted.ipynb",
                "source_sha256": "1" * 64,
                "extraction_method": "drifted",
            },
        ),
    ],
)
def test_checkpoint_load_recomputes_full_task_sanitized_hash(
    tmp_path,
    task_item,
    core_candidate_config,
    field,
    value,
):
    boundary = _setup_recoverable_boundary(tmp_path, task_item, core_candidate_config)
    _reconcile(boundary, task_item)
    changed_task = task_item.model_copy(update={field: value})

    with pytest.raises(ValueError, match="full canonical body"):
        _load_checkpoint(boundary, changed_task)


def test_reconcile_refuses_unclaimed_extra_evolved_completion(
    tmp_path, task_item, core_candidate_config
):
    boundary = _setup_recoverable_boundary(tmp_path, task_item, core_candidate_config)
    extra = deepcopy(json.loads(boundary["failed_path"].read_text()))
    extra["task_id"] = f"{boundary['pair_id']}-evolved-aaaaaaaaaa"
    extra["session_id"] = "session-unclaimed-extra-g2"
    _write_completion(boundary["completion_root"], extra)

    with pytest.raises(ValueError, match="completion inventory is not exhaustive"):
        _reconcile(boundary, task_item)
    claim = json.loads(
        (boundary["ledger_root"] / boundary["pair_id"] / "evolved_candidate.json").read_text()
    )
    assert claim["status"] == "claimed"
    assert not (boundary["run_root"] / "recovery").exists()


def test_checkpoint_load_refuses_preallocated_replacement_completion_appearing(
    tmp_path, task_item, core_candidate_config
):
    boundary = _setup_recoverable_boundary(tmp_path, task_item, core_candidate_config)
    _reconcile(boundary, task_item)
    claim = json.loads(
        (boundary["ledger_root"] / boundary["pair_id"] / "evolved_candidate.json").read_text()
    )
    replacement_run_id = claim["failed_attempts"][-1]["receipt"]["replacement_run_id"]
    appeared = deepcopy(json.loads(boundary["failed_path"].read_text()))
    appeared["task_id"] = replacement_run_id
    appeared["session_id"] = "session-preallocated-run-already-appeared"
    _write_completion(boundary["completion_root"], appeared)

    with pytest.raises(ValueError, match="completion inventory is not exhaustive"):
        _load_checkpoint(boundary, task_item)


def test_checkpoint_load_revalidates_every_historical_failed_completion(
    tmp_path, task_item, core_candidate_config
):
    boundary = _setup_recoverable_boundary(tmp_path, task_item, core_candidate_config)
    _reconcile(boundary, task_item)
    _record_second_no_effect_attempt(boundary, task_item)
    assert _load_checkpoint(boundary, task_item) is not None

    first_completion = json.loads(boundary["failed_path"].read_text())
    first_completion["metadata"]["historical_attempt_drift"] = True
    boundary["failed_path"].write_text(json.dumps(first_completion), encoding="utf-8")

    with pytest.raises(ValueError, match="no-effect proof failed|completion authority drifted"):
        _load_checkpoint(boundary, task_item)


def test_checkpoint_load_revalidates_every_historical_recovery_receipt(
    tmp_path, task_item, core_candidate_config
):
    boundary = _setup_recoverable_boundary(tmp_path, task_item, core_candidate_config)
    _, first_recovery_path, _ = _reconcile(boundary, task_item)
    _record_second_no_effect_attempt(boundary, task_item)
    assert _load_checkpoint(boundary, task_item) is not None

    first_recovery = json.loads(first_recovery_path.read_text())
    first_recovery["source_authority_sha256"]["evolved_candidate_recovery"] = "0" * 64
    first_recovery_path.write_text(json.dumps(first_recovery), encoding="utf-8")

    with pytest.raises(ValueError, match="attempt 1 authority differs"):
        _load_checkpoint(boundary, task_item)


def test_checkpoint_load_refuses_deleted_historical_failed_completion(
    tmp_path, task_item, core_candidate_config
):
    boundary = _setup_recoverable_boundary(tmp_path, task_item, core_candidate_config)
    _reconcile(boundary, task_item)
    _record_second_no_effect_attempt(boundary, task_item)
    boundary["failed_path"].unlink()

    with pytest.raises(ValueError, match="completion inventory"):
        _load_checkpoint(boundary, task_item)


def test_checkpoint_load_refuses_deleted_historical_recovery_receipt(
    tmp_path, task_item, core_candidate_config
):
    boundary = _setup_recoverable_boundary(tmp_path, task_item, core_candidate_config)
    _, first_recovery_path, _ = _reconcile(boundary, task_item)
    _record_second_no_effect_attempt(boundary, task_item)
    first_recovery_path.unlink()

    with pytest.raises(ValueError, match="recovery receipt inventory"):
        _load_checkpoint(boundary, task_item)


def test_checkpoint_load_refuses_symlinked_completion_evidence(
    tmp_path, task_item, core_candidate_config
):
    boundary = _setup_recoverable_boundary(tmp_path, task_item, core_candidate_config)
    _reconcile(boundary, task_item)
    outside = tmp_path / "outside-completion.json"
    outside.write_bytes(boundary["failed_path"].read_bytes())
    boundary["failed_path"].unlink()
    boundary["failed_path"].symlink_to(outside)

    with pytest.raises(ValueError, match="link-count-one regular file"):
        _load_checkpoint(boundary, task_item)


def test_reconcile_refuses_symlinked_evolution_database(
    tmp_path, task_item, core_candidate_config
):
    boundary = _setup_recoverable_boundary(tmp_path, task_item, core_candidate_config)
    real_database = tmp_path / "real-core.sqlite3"
    boundary["db_path"].rename(real_database)
    boundary["db_path"].symlink_to(real_database)

    with pytest.raises(ValueError, match="no-follow"):
        _reconcile(boundary, task_item)


def test_reconcile_refuses_intermediate_artifact_directory_symlink(
    tmp_path, task_item, core_candidate_config
):
    boundary = _setup_recoverable_boundary(tmp_path, task_item, core_candidate_config)
    workers = boundary["artifact_root"] / "workers"
    real_workers = boundary["artifact_root"] / "workers-real"
    workers.rename(real_workers)
    workers.symlink_to(real_workers, target_is_directory=True)

    with pytest.raises(ValueError, match="no-follow"):
        _reconcile(boundary, task_item)


def test_recovery_source_authority_covers_g2_dispatch_chain():
    repository_root = Path(__file__).resolve().parents[3]
    sources = _recovery_source_paths(repository_root)

    assert {
        "cli",
        "runtime",
        "three_artifact_protocol",
        "three_artifact_runtime",
        "replacement_ledger",
    } <= set(sources)
    assert all(path.is_file() for path in sources.values())
