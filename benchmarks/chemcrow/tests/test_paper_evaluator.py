from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from openevo.harness.models import AgentSpec

from openevo_chemcrow.models import TaskItem
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
        task.task_id: SimpleNamespace(
            baseline=SimpleNamespace(answer=f"sealed baseline {task.task_id}"),
            evolved=SimpleNamespace(answer=f"sealed evolved {task.task_id}"),
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
    assert plan["provider_only"] == ["openai"]
    assert {call["comparison"] for call in plan["calls"]} == {
        "historical_control",
        "baseline",
        "evolved",
    }
    assert max(call["estimated_input_tokens"] for call in plan["calls"]) <= 6991


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
        prompt_sha256="a" * 64,
        historical_source_sha256="b" * 64,
        target_answer_sha256="c" * 64,
        historical_gpt4_answer_sha256="d" * 64,
        estimated_input_tokens=100,
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
