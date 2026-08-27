from __future__ import annotations

import json
from pathlib import Path

import pytest
from openevo.harness.models import AgentSpec

from openevo_chemcrow.hashing import canonical_sha256, file_sha256
from openevo_chemcrow.models import TaskItem, Trajectory
from openevo_chemcrow.paper_direct_comparison import (
    DIRECT_AUTHORIZATION,
    DIRECT_CALL_COUNT,
    DIRECT_CALL_ID_PREFIX,
    aggregate_direct_comparison_results,
    build_direct_comparison_plan,
    direct_cost_ceiling,
    run_direct_comparison_plan,
    validate_direct_comparison_plan,
)
from openevo_chemcrow.paper_evaluator import (
    FROZEN_PAPER_TASK_IDS,
    PAPER_EVALUATOR_MODEL,
    PAPER_EVALUATOR_PROMPT_SHA256,
    extract_historical_answers,
)
from openevo_chemcrow.paper_harness import PaperEvaluatorHarness
from openevo_chemcrow.three_artifact_models import (
    CORE_NATIVE_THREE_ARTIFACT_BUNDLE_PROTOCOL,
    CORE_NATIVE_THREE_ARTIFACT_PROTOCOL_LABEL,
    ThreeArtifactBundleReceipt,
    ThreeArtifactPairResult,
)


def _tasks() -> list[TaskItem]:
    path = Path(__file__).resolve().parents[1] / "tasks.jsonl"
    return [TaskItem.model_validate_json(line) for line in path.read_text().splitlines()]


def _pairs(tasks: list[TaskItem]) -> dict[str, ThreeArtifactPairResult]:
    return {
        task.task_id: ThreeArtifactPairResult.model_construct(
            task_id=task.task_id,
            pair_id=f"test-full-v5--{task.task_id}",
            artifact_bundle=ThreeArtifactBundleReceipt.model_construct(
                protocol=CORE_NATIVE_THREE_ARTIFACT_BUNDLE_PROTOCOL
            ),
            baseline=Trajectory(
                run_id=f"baseline-{task.task_id}",
                task_id=task.task_id,
                role="baseline",
                status="COMPLETED",
                answer=f"sealed baseline {task.task_id}",
                candidate_config_sha256="s0",
            ),
            evolved=Trajectory(
                run_id=f"evolved-{task.task_id}",
                task_id=task.task_id,
                role="evolved",
                status="COMPLETED",
                answer=f"sealed evolved {task.task_id}",
                candidate_config_sha256="s0",
                artifact_ids=["memory", "skill", "agent-system"],
            ),
        )
        for task in tasks
    }


def _plan(tmp_path: Path, runs_root: Path) -> tuple[dict, Path]:
    reference = tmp_path / "reference-42-aggregate.json"
    reference.write_text('{"sealed":true}\n', encoding="utf-8")
    tasks = _tasks()
    plan = build_direct_comparison_plan(
        tasks=tasks,
        pairs=_pairs(tasks),
        historical=extract_historical_answers(runs_root=runs_root),
        expected_source_pair_protocol=CORE_NATIVE_THREE_ARTIFACT_PROTOCOL_LABEL,
        reference_42_call_aggregate=reference,
        expected_reference_42_call_aggregate_sha256=file_sha256(reference),
    )
    return plan, reference


def test_direct_plan_is_exactly_14_frozen_same_call_comparisons(tmp_path, runs_root):
    plan, _ = _plan(tmp_path, runs_root)
    calls = validate_direct_comparison_plan(plan)

    assert plan["call_count"] == DIRECT_CALL_COUNT == 14
    assert plan["prompt_candidate_sha256"] == PAPER_EVALUATOR_PROMPT_SHA256
    assert plan["student_a_system"] == "historical_chemcrow"
    assert plan["student_b_system"] == "openevo_evolved"
    assert plan["paper_comparable"] is False
    assert plan["project_added_metric"] is True
    assert tuple(call.task_id for call in calls) == FROZEN_PAPER_TASK_IDS
    assert all(call.student_a_system == "historical_chemcrow" for call in calls)
    assert all(call.student_b_system == "openevo_evolved" for call in calls)
    assert all(call.call_id.startswith(f"{DIRECT_CALL_ID_PREFIX}-chemcrow-") for call in calls)
    assert len({call.prompt_sha256 for call in calls}) == 14


def test_direct_comparison_cost_ceiling_and_authorization_are_isolated(
    tmp_path, runs_root, monkeypatch
):
    plan, reference = _plan(tmp_path, runs_root)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    ceiling = direct_cost_ceiling()

    assert ceiling["list_price_ceiling_usd_total"] == 3.94422
    assert ceiling["authorized_max_usd"] == 5.0
    monkeypatch.setenv(
        "CHEMCROW_PAPER_EVALUATOR_AUTHORIZATION",
        "I_AUTHORIZE_42_SEALED_PAPER_EVALUATIONS",
    )
    monkeypatch.setenv("CHEMCROW_PAPER_EVALUATOR_MODEL", PAPER_EVALUATOR_MODEL)
    monkeypatch.setenv("CHEMCROW_PAPER_DIRECT_COMPARISON_MAX_USD", "3.94422")
    monkeypatch.delenv("CHEMCROW_PAPER_DIRECT_COMPARISON_AUTHORIZATION", raising=False)
    with pytest.raises(PermissionError, match="authorization"):
        run_direct_comparison_plan(
            plan_path=plan_path,
            rollout_base_url="http://127.0.0.1:8180",
            runtime={},
            result_root=tmp_path / "results",
            shim_receipt_root=tmp_path / "receipts",
            reference_42_call_aggregate=reference,
            allow_paid=True,
        )
    assert DIRECT_AUTHORIZATION != "I_AUTHORIZE_42_SEALED_PAPER_EVALUATIONS"


def test_direct_harness_accepts_only_the_fresh_direct_call_namespace():
    spec = AgentSpec(
        import_path="openevo_chemcrow.paper_harness:PaperEvaluatorHarness",
        model_name=PAPER_EVALUATOR_MODEL,
        settings={
            "capture_mode": "transcript",
            "temperature": 0.1,
            "max_tokens": 1200,
            "runtime_gateway_base_url": "http://host.docker.internal:8110/v1",
        },
        env={"PAPER_EVALUATOR_CALL_ID": "paper-direct-v1-chemcrow-01-direct"},
    )

    step = PaperEvaluatorHarness(spec).run_steps("sealed direct prompt")[0]

    assert step.env is not None
    assert step.env["PAPER_EVALUATOR_CALL_ID"] == "paper-direct-v1-chemcrow-01-direct"
    assert step.env["PAPER_EVALUATOR_PROMPT"] == "sealed direct prompt"


def test_direct_aggregate_uses_same_call_student_a_and_b_grades(tmp_path):
    receipt_root = tmp_path / "receipts"
    receipt_root.mkdir()
    results = []
    for index, task_id in enumerate(FROZEN_PAPER_TASK_IDS, start=1):
        call_id = f"{DIRECT_CALL_ID_PREFIX}-{task_id}-direct"
        assessment = {
            "student_a": {
                "grade": 7,
                "strengths": ["historical"],
                "weaknesses": [],
                "justification": "valid",
                "feedback": [],
            },
            "student_b": {
                "grade": 8,
                "strengths": ["evolved"],
                "weaknesses": [],
                "justification": "valid",
                "feedback": [],
            },
        }
        receipt = {
            "status": "terminal_success",
            "call_id": call_id,
            "model": "openai/gpt-4",
            "provider": "OpenAI",
            "allow_fallbacks": False,
            "ledger_class": "direct_comparison",
            "prompt_or_response_included": False,
            "credential_included": False,
            "openrouter_reported_cost_usd": 0.01,
            "prompt_tokens": 100 + index,
            "completion_tokens": 20,
        }
        receipt_path = receipt_root / f"{call_id}.receipt.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        results.append(
            {
                "call_id": call_id,
                "task_id": task_id,
                "assessment": assessment,
                "assessment_sha256": canonical_sha256(assessment),
            }
        )
    reference = tmp_path / "reference.json"
    reference.write_text('{"sealed":true}\n', encoding="utf-8")
    reference_hash = file_sha256(reference)

    aggregate = aggregate_direct_comparison_results(
        results=results,
        receipt_root=receipt_root,
        plan_sha256="a" * 64,
        reference_42_call_aggregate=reference,
        expected_reference_sha256=reference_hash,
    )

    assert aggregate["mean_historical_chemcrow_grade"] == 7
    assert aggregate["mean_v5_g2_grade"] == 8
    assert aggregate["mean_v5_g2_minus_historical_chemcrow"] == 1
    assert aggregate["v5_g2_wins"] == 14
    assert aggregate["ties"] == aggregate["historical_chemcrow_wins"] == 0
    assert aggregate["actual_openrouter_reported_cost_usd"] == 0.14
    assert aggregate["reference_42_call_ledger_unchanged"] is True
