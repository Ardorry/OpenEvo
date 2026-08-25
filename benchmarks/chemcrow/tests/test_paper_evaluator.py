from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from openevo.harness.models import AgentSpec

from openevo_chemcrow.composite import (
    validate_duplicate_authorization_receipt,
    validate_repair_execution_parity,
)
from openevo_chemcrow.hashing import canonical_sha256, file_sha256
from openevo_chemcrow.models import TaskItem, Trajectory
from openevo_chemcrow.paper_core import PAPER_CORE_ROUTE, build_paper_task_request
from openevo_chemcrow.paper_evaluator import (
    FROZEN_PAPER_TASK_IDS,
    PAPER_EVALUATOR_CALL_COUNT,
    PAPER_EVALUATOR_MODEL,
    PAPER_EVALUATOR_PROTOCOL,
    PaperEvaluationCall,
    assert_sealed_run_ready,
    build_paper_evaluation_plan,
    extract_historical_answers,
    extract_historical_evaluator_grades,
    paper_cost_ceiling,
    validate_assessment_text,
)
from openevo_chemcrow.paper_harness import PaperEvaluatorHarness
from openevo_chemcrow.three_artifact_models import ThreeArtifactPairResult


def _tasks() -> list[TaskItem]:
    path = Path(__file__).resolve().parents[1] / "tasks.jsonl"
    return [TaskItem.model_validate_json(line) for line in path.read_text().splitlines()]


def test_historical_extractor_reads_student_cells_but_not_teacher(runs_root):
    historical = extract_historical_answers(runs_root=runs_root)

    assert tuple(historical) == FROZEN_PAPER_TASK_IDS
    assert all(item.chemcrow_answer and item.gpt4_answer for item in historical.values())
    assert all("Student 1's Grade" not in item.chemcrow_answer for item in historical.values())
    assert all("Student 1's Grade" not in item.gpt4_answer for item in historical.values())


def test_historical_grades_are_separate_post_judge_drift_metadata(runs_root):
    grades = extract_historical_evaluator_grades(runs_root=runs_root)

    assert tuple(grades) == FROZEN_PAPER_TASK_IDS
    assert sum(item.chemcrow_grade for item in grades.values()) / 14 == pytest.approx(
        7.357142857142857
    )
    assert sum(item.gpt4_grade for item in grades.values()) / 14 == pytest.approx(8.75)


def test_plan_has_exactly_three_fixed_comparisons_per_task(runs_root):
    tasks = _tasks()
    pairs = {
        task.task_id: ThreeArtifactPairResult.model_construct(
            task_id=task.task_id,
            pair_id=f"test-full-v4--{task.task_id}",
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
    plan = build_paper_evaluation_plan(
        tasks=tasks,
        pairs=pairs,
        historical=extract_historical_answers(runs_root=runs_root),
    )

    assert plan["protocol"] == PAPER_EVALUATOR_PROTOCOL
    assert plan["call_count"] == PAPER_EVALUATOR_CALL_COUNT == 42
    assert plan["reflector_access"] is False
    assert plan["sealed_output_only"] is True
    assert plan["source_pair_protocol"] == "chemcrow-three-isolated-artifacts-v1"
    assert plan["provider_only"] == ["openai"]
    assert plan["data_collection"] == "allow"
    assert {call["comparison"] for call in plan["calls"]} == {
        "historical_control",
        "baseline",
        "evolved",
    }
    assert max(call["estimated_input_tokens"] for call in plan["calls"]) <= 6991
    assert all(call["prompt_sha256"] for call in plan["calls"])
    assert all(call["source_output_id"] for call in plan["calls"])
    assert all(call["source_output_sha256"] for call in plan["calls"])


def test_paper_plan_rejects_legacy_single_artifact_pairs(runs_root):
    tasks = _tasks()
    legacy_pairs = {task.task_id: object() for task in tasks}
    with pytest.raises(TypeError, match="legacy single-artifact"):
        build_paper_evaluation_plan(
            tasks=tasks,
            pairs=legacy_pairs,  # type: ignore[arg-type]
            historical=extract_historical_answers(runs_root=runs_root),
        )


def test_exact_context_list_price_ceiling_is_frozen():
    ceiling = paper_cost_ceiling()

    assert ceiling["model_context_tokens"] == 8191
    assert ceiling["max_input_tokens_per_call"] == 6991
    assert ceiling["list_price_ceiling_usd_total"] == 11.83266


def test_stopped_full_v3_is_not_paper_evaluation_ready(tmp_path):
    (tmp_path / "STOPPED_BY_USER.json").write_text("{}", encoding="utf-8")
    audit = tmp_path / "audit.json"
    audit.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="STOPPED_BY_USER"):
        assert_sealed_run_ready(
            run_root=tmp_path,
            experiment_id="full-v3",
            task_ids=list(FROZEN_PAPER_TASK_IDS),
            completed_run_audit=audit,
        )


def test_strict_paper_assessment_schema():
    assessment = validate_assessment_text(
        json.dumps(
            {
                "student_a": {
                    "grade": 8.5,
                    "strengths": ["correct"],
                    "weaknesses": ["brief"],
                    "justification": "Mostly correct.",
                    "feedback": ["Add conditions."],
                },
                "student_b": {
                    "grade": 7,
                    "strengths": ["clear"],
                    "weaknesses": ["omission"],
                    "justification": "Incomplete.",
                    "feedback": ["Complete the task."],
                },
            }
        )
    )
    assert assessment.student_a.grade == 8.5
    with pytest.raises(ValueError, match="Markdown fence"):
        validate_assessment_text("```json\n{}\n```")
    with pytest.raises(ValueError):
        validate_assessment_text("not JSON")
    with pytest.raises(ValueError):
        validate_assessment_text('{"student_a":{},"student_b":{}}')
    invalid_grade = assessment.model_dump(mode="json")
    invalid_grade["student_a"]["grade"] = 10.1
    with pytest.raises(ValueError):
        validate_assessment_text(json.dumps(invalid_grade))


def test_paper_harness_routes_only_through_core_gateway():
    spec = AgentSpec(
        import_path="openevo_chemcrow.paper_harness:PaperEvaluatorHarness",
        model_name=PAPER_EVALUATOR_MODEL,
        settings={
            "capture_mode": "transcript",
            "temperature": 0.1,
            "max_tokens": 1200,
            "runtime_gateway_base_url": "http://host.docker.internal:8110/v1",
        },
        env={"PAPER_EVALUATOR_CALL_ID": "paper-chemcrow-01-baseline"},
    )
    step = PaperEvaluatorHarness(spec).run_steps("private sealed prompt")[0]

    assert "openrouter.ai" not in step.command
    assert "OPENROUTER_API_KEY" not in step.command
    assert "OPENAI_BASE_URL" in step.command
    assert step.env is not None
    assert step.env["OPENAI_BASE_URL"] == "http://host.docker.internal:8110/v1"
    assert step.env["PAPER_EVALUATOR_PROMPT"] == "private sealed prompt"


def test_paper_core_request_has_no_reflector_or_evolution_context():
    call = PaperEvaluationCall(
        call_id="paper-chemcrow-01-baseline",
        task_id="chemcrow-01",
        comparison="baseline",
        student_a_system="openevo_baseline",
        prompt="sealed prompt",
        prompt_sha256=canonical_sha256("sealed prompt"),
        source_pair_id="pair-1",
        source_pair_result_sha256="b" * 64,
        source_output_id="baseline-1",
        source_output_sha256="c" * 64,
        historical_source_sha256="b" * 64,
        target_answer_sha256="c" * 64,
        historical_gpt4_answer_sha256="d" * 64,
        estimated_input_tokens=100,
        metric_classification="project_added_openevo_metric",
        paper_comparable=False,
    )
    request = build_paper_task_request(
        call=call,
        runtime={"backend": "docker", "image": "sha256:" + "1" * 64},
    )

    assert request["metadata"]["execution_route"] == PAPER_CORE_ROUTE
    assert request["metadata"]["reflector_access"] is False
    assert request["runtime_context_binding"] is None
    assert request["evaluator"] is None
    assert request["agent"]["import_path"].endswith(":PaperEvaluatorHarness")


def test_two_task_repair_freezes_same_s0_and_all_model_roles():
    config_root = Path(__file__).resolve().parents[1] / "configs"
    full = yaml.safe_load((config_root / "full.v3.yaml").read_text())
    repair = yaml.safe_load((config_root / "paper_repair.v1.yaml").read_text())

    assert repair["task_ids"] == ["chemcrow-14", "chemcrow-15"]
    for role in ("candidate", "reflector", "evolution_evaluator", "final_evaluator"):
        assert repair[role] == full[role]
    assert repair["duplicate_authorization_receipt"].endswith(
        "CHEMCROW_14_DUPLICATE_AUTHORIZATION.json"
    )
    parity = validate_repair_execution_parity(
        config_root.parent / "reports" / "PAPER_REPAIR_EXECUTION_PARITY.json"
    )
    assert parity["scientific_execution_module_count"] == 9


def test_duplicate_authorization_is_bound_to_interrupted_claim(tmp_path):
    experiment_id = "chemcrow-task-local-full-v3"
    claim = (
        tmp_path
        / "claims"
        / f"{experiment_id}--chemcrow-14"
        / "evolved_candidate.json"
    )
    claim.parent.mkdir(parents=True)
    claim.write_text('{"status":"claimed"}\n', encoding="utf-8")
    receipt = tmp_path / "authorization.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": "chemcrow_duplicate_task_authorization_v1",
                "status": "AUTHORIZED",
                "task_id": "chemcrow-14",
                "prior_experiment_id": experiment_id,
                "prior_phase": "evolved_candidate",
                "prior_claim_sha256": file_sha256(claim),
                "authorization_literal": (
                    "I_AUTHORIZE_FRESH_CHEMCROW_14_PAIR_AFTER_USER_STOP"
                ),
                "reason": (
                    "prior pair interrupted before sealing; no prior pair result is eligible"
                ),
                "authorized_at": "2026-08-25T00:00:00Z",
                "authorized_by": "user",
            }
        ),
        encoding="utf-8",
    )

    safe = validate_duplicate_authorization_receipt(
        path=receipt,
        primary_run_root=tmp_path,
        primary_experiment_id=experiment_id,
    )

    assert safe["task_id"] == "chemcrow-14"
    assert "authorization_literal" not in safe
