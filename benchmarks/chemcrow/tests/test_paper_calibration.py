from __future__ import annotations

import json
from pathlib import Path

import pytest

from openevo_chemcrow.hashing import canonical_sha256
from openevo_chemcrow.paper_calibration import (
    BOOTSTRAP_SEED,
    CALIBRATION_AUTHORIZATION,
    CALIBRATION_MAX_CALLS,
    CALIBRATION_MAX_USD,
    CalibrationCall,
    CandidateOutput,
    SelectedPrompt,
    _assessment_from_status,
    assert_calibration_ledger_separation,
    bootstrap_confidence_intervals,
    build_calibration_plan,
    build_calibration_task_request,
    build_prompt_candidate_manifest,
    calibration_cost_ceiling,
    compute_candidate_metrics,
    extract_historical_calibration_dataset,
    leave_one_task_out_selection,
    parse_historical_assessment,
    select_prompt_candidate,
    validate_prompt_candidate_manifest,
)
from openevo_chemcrow.paper_evaluator import (
    PAPER_EVALUATOR_AUTHORIZATION,
)


def _assessment(a: float, b: float) -> dict[str, object]:
    student = lambda grade: {
        "grade": grade,
        "strengths": ["strength"],
        "weaknesses": ["weakness"],
        "justification": "justification",
        "feedback": ["feedback"],
    }
    return {"student_a": student(a), "student_b": student(b)}


def _dataset_record(task_id: str, a: float, b: float) -> dict[str, object]:
    return {
        "repo_task_id": task_id,
        "historical_evaluator_label_available": True,
        "historical_evaluator": {
            "student_a": {"grade": a},
            "student_b": {"grade": b},
        },
    }


def test_historical_notebook_extraction_is_complete_and_binds_overwrites(runs_root):
    dataset = extract_historical_calibration_dataset(runs_root=runs_root)

    assert dataset["exact_prompt_recovered"] is False
    assert dataset["task_count"] == dataset["labeled_task_count"] == 14
    assert dataset["missing_label_task_ids"] == []
    assert [item["repo_task_id"] for item in dataset["tasks"]] == [
        "chemcrow-01",
        "chemcrow-02",
        "chemcrow-03",
        "chemcrow-04",
        "chemcrow-05",
        "chemcrow-06",
        "chemcrow-07",
        "chemcrow-08",
        "chemcrow-09",
        "chemcrow-10",
        "chemcrow-12",
        "chemcrow-13",
        "chemcrow-14",
        "chemcrow-15",
    ]
    task12 = next(item for item in dataset["tasks"] if item["repo_task_id"] == "chemcrow-12")
    assert task12["student_b_source"]["resolution"] == "literal_assignment"
    assert task12["student_b_source"]["cell_execution_count"] == 6
    assert "cyclosarin" in task12["historical_no_tools_gpt4_final_answer"]
    task13 = next(item for item in dataset["tasks"] if item["repo_task_id"] == "chemcrow-13")
    assert task13["task_source"]["variable_name"] == "prompt"
    assert all(item["supplement_discrepancy"] is False for item in dataset["tasks"])


def test_historical_extraction_is_byte_deterministic(runs_root):
    first = extract_historical_calibration_dataset(runs_root=runs_root)
    second = extract_historical_calibration_dataset(runs_root=runs_root)
    assert canonical_sha256(first) == canonical_sha256(second)


def test_historical_grade_parser_and_missing_label_handling():
    parsed = parse_historical_assessment(
        """Student 1's Grade: 8.5
Strengths: Good chemistry.
Weaknesses: Missing one condition.
Grade Justification: Mostly correct.

Student 2's Grade: 7
Strengths: Clear.
Weaknesses: Incomplete.
Grade Justification: It does not finish the task.
"""
    )
    assert parsed is not None
    assert parsed["student_a"]["grade"] == 8.5
    assert parsed["student_b"]["weaknesses"] == "Incomplete."
    assert parse_historical_assessment("") is None


def test_calibration_parses_rollout_terminal_result_envelope():
    assessment = _assessment(8, 7)
    status = {
        "task_id": "paper-chemcrow-cal-v1-test",
        "status": "completed",
        "results": [
            {
                "status": "COMPLETED",
                "trajectory": {
                    "traces": [
                        {
                            "response_messages": [
                                {
                                    "role": "assistant",
                                    "content": json.dumps(assessment),
                                }
                            ]
                        }
                    ]
                },
            }
        ],
    }

    parsed = _assessment_from_status(status)
    assert parsed.student_a.grade == 8
    assert parsed.student_b.grade == 7


def test_prompt_candidates_are_frozen_before_results(runs_root):
    dataset = extract_historical_calibration_dataset(runs_root=runs_root)
    manifest = build_prompt_candidate_manifest(dataset_sha256=canonical_sha256(dataset))

    assert manifest["exact_prompt_recovered"] is False
    assert [item["candidate_id"] for item in manifest["candidates"]] == [
        "CURRENT_V1",
        "PAPER_MINIMAL",
        "PAPER_OUTPUT_FAITHFUL",
    ]
    assert manifest["selection_rule"] == [
        "highest_pairwise_preference_agreement",
        "lowest_delta_mae",
        "lowest_absolute_score_mae",
        "highest_spearman_correlation",
        "lowest_parse_schema_failure_rate",
        "simplest_closest_to_published_paper_semantics",
    ]
    assert manifest["bootstrap_seed"] == BOOTSTRAP_SEED
    assert validate_prompt_candidate_manifest(manifest) == manifest
    assert {
        item["candidate_id"]: item["prompt_candidate_sha256"]
        for item in manifest["candidates"]
    } == {
        "CURRENT_V1": "7b51dcfb3cb45dc1d3f2702e65ada7d260e5c76427dd06ef6743d44f4c63674a",
        "PAPER_MINIMAL": "aa32cbc3190a53512bb121b167bcd68fb10cd92a5f07d30f68edcdda27ea0767",
        "PAPER_OUTPUT_FAITHFUL": (
            "927c9defcdb07bf68a7b8aeaf1f89830d6e620c1844404b1396b73e00f51b566"
        ),
    }
    current = manifest["candidates"][0]
    assert current["rendered_example_sha256"] == canonical_sha256(
        current["rendered_example"]
    )

    mutated = json.loads(json.dumps(manifest))
    mutated["candidates"][1]["instruction_template"] += " Tune this score."
    with pytest.raises(ValueError, match="candidate"):
        validate_prompt_candidate_manifest(mutated)


def test_calibration_plan_has_unique_calls_and_no_grade_leakage(runs_root):
    dataset = extract_historical_calibration_dataset(runs_root=runs_root)
    manifest = build_prompt_candidate_manifest(dataset_sha256=canonical_sha256(dataset))
    plan = build_calibration_plan(dataset=dataset, candidate_manifest=manifest)

    assert plan["call_count"] == 42
    assert len({call["call_id"] for call in plan["calls"]}) == 42
    assert set(plan["prompt_candidate_ids"]) == {
        "CURRENT_V1",
        "PAPER_MINIMAL",
        "PAPER_OUTPUT_FAITHFUL",
    }
    assert plan["production_ledger_included"] is False
    for call in plan["calls"]:
        assert "official_student_a_grade" not in call
        assert "official_student_b_grade" not in call
        assert call["prompt_sha256"] == canonical_sha256(call["prompt"])


def test_calibration_cost_and_authorization_are_isolated(tmp_path, monkeypatch):
    ceiling = calibration_cost_ceiling()
    assert ceiling["max_total_calls"] == CALIBRATION_MAX_CALLS == 48
    assert ceiling["list_price_ceiling_usd_total"] <= CALIBRATION_MAX_USD == 15.0
    assert CALIBRATION_AUTHORIZATION != PAPER_EVALUATOR_AUTHORIZATION
    calibration = tmp_path / "paper-evaluator-calibration-v1"
    production = tmp_path / "paper-evaluator-full-v4-three-pipeline"
    assert_calibration_ledger_separation(calibration, production)
    with pytest.raises(ValueError, match="separate"):
        assert_calibration_ledger_separation(production / "child", production)
    monkeypatch.setenv(
        "CHEMCROW_PAPER_EVALUATOR_AUTHORIZATION", PAPER_EVALUATOR_AUTHORIZATION
    )
    monkeypatch.delenv("CHEMCROW_PAPER_CALIBRATION_AUTHORIZATION", raising=False)
    manifest = build_prompt_candidate_manifest(dataset_sha256="d" * 64)
    call = CalibrationCall(
        call_id="paper-chemcrow-cal-v1-chemcrow-01-current-v1",
        task_id="chemcrow-01",
        prompt_candidate_id="CURRENT_V1",
        prompt_candidate_sha256=manifest["candidates"][0]["prompt_candidate_sha256"],
        prompt="prompt",
        prompt_sha256=canonical_sha256("prompt"),
        input_sha256="b" * 64,
        estimated_input_tokens=100,
        phase="initial",
        repetition=0,
    )
    with pytest.raises(PermissionError, match="calibration"):
        build_calibration_task_request(call=call, runtime={})


def test_metric_pairwise_delta_and_selection_rule():
    dataset = {
        "tasks": [
            _dataset_record("chemcrow-01", 8, 6),
            _dataset_record("chemcrow-02", 5, 7),
            _dataset_record("chemcrow-03", 9, 9),
        ]
    }
    outputs = [
        CandidateOutput("chemcrow-01", "X", _assessment(7, 6), True, True),
        CandidateOutput("chemcrow-02", "X", _assessment(6, 8), True, True),
        CandidateOutput("chemcrow-03", "X", _assessment(9, 8), True, True),
    ]
    metrics = compute_candidate_metrics(dataset=dataset, outputs=outputs)
    assert metrics["score_mae"] == pytest.approx(4 / 6)
    assert metrics["pairwise_preference_agreement"] == pytest.approx(2 / 3)
    assert metrics["delta_mae"] == pytest.approx(2 / 3)
    assert metrics["preference_confusion"]["tie"]["a_wins"] == 1

    candidates = {
        "CURRENT_V1": {**metrics, "pairwise_preference_agreement": 0.8, "delta_mae": 1.2},
        "PAPER_MINIMAL": {**metrics, "pairwise_preference_agreement": 0.8, "delta_mae": 1.0},
        "PAPER_OUTPUT_FAITHFUL": {
            **metrics,
            "pairwise_preference_agreement": 0.7,
            "delta_mae": 0.1,
        },
    }
    assert select_prompt_candidate(candidates)["selected_candidate_id"] == "PAPER_MINIMAL"


def test_bootstrap_and_leave_one_out_are_deterministic():
    dataset = {
        "tasks": [
            _dataset_record("chemcrow-01", 8, 6),
            _dataset_record("chemcrow-02", 5, 7),
            _dataset_record("chemcrow-03", 9, 9),
            _dataset_record("chemcrow-04", 4, 8),
        ]
    }
    by_candidate = {
        candidate: [
            CandidateOutput("chemcrow-01", candidate, _assessment(8, 6), True, True),
            CandidateOutput("chemcrow-02", candidate, _assessment(5, 7), True, True),
            CandidateOutput("chemcrow-03", candidate, _assessment(9, 9), True, True),
            CandidateOutput("chemcrow-04", candidate, _assessment(4, 8), True, True),
        ]
        for candidate in ("CURRENT_V1", "PAPER_MINIMAL", "PAPER_OUTPUT_FAITHFUL")
    }
    outputs = by_candidate["CURRENT_V1"]
    assert bootstrap_confidence_intervals(dataset=dataset, outputs=outputs, samples=200) == (
        bootstrap_confidence_intervals(dataset=dataset, outputs=outputs, samples=200)
    )
    first = leave_one_task_out_selection(dataset=dataset, outputs_by_candidate=by_candidate)
    second = leave_one_task_out_selection(dataset=dataset, outputs_by_candidate=by_candidate)
    assert first == second
    assert first["fold_count"] == 4


def test_selected_prompt_hash_binding():
    manifest = build_prompt_candidate_manifest(dataset_sha256="d" * 64)
    template = manifest["candidates"][1]["instruction_template"]
    selected = SelectedPrompt(
        protocol="CHEMCROW_EVALUATORGPT_PROMPT_CALIBRATED_V2",
        candidate_id="PAPER_MINIMAL",
        instruction_template=template,
        prompt_sha256=canonical_sha256(template),
        exact_prompt_recovered=False,
    )
    assert selected.prompt_sha256 == canonical_sha256(selected.instruction_template)
    with pytest.raises(ValueError, match="hash"):
        SelectedPrompt(
            protocol="CHEMCROW_EVALUATORGPT_PROMPT_CALIBRATED_V2",
            candidate_id="PAPER_MINIMAL",
            instruction_template="mutated",
            prompt_sha256="0" * 64,
            exact_prompt_recovered=False,
        )


def test_completed_v5_production_matrix_binds_selected_prompt_and_sealed_outputs():
    report = (
        Path(__file__).resolve().parents[1]
        / "reports"
        / "PAPER_COMPARISON_MATRIX.json"
    )
    matrix = json.loads(report.read_text(encoding="utf-8"))

    assert matrix["protocol"] == "CHEMCROW_EVALUATORGPT_PROMPT_CALIBRATED_V2"
    assert matrix["prompt_candidate_id"] == "PAPER_MINIMAL"
    assert matrix["prompt_candidate_sha256"] == (
        "aa32cbc3190a53512bb121b167bcd68fb10cd92a5f07d30f68edcdda27ea0767"
    )
    assert matrix["status"] == "COMPLETE_FULL_V5_CORE_NATIVE"
    assert matrix["source_pair_protocol"] == (
        "chemcrow-three-isolated-core-native-artifacts-v2"
    )
    assert matrix["call_count"] == len(matrix["calls"]) == 42
    assert len({call["call_id"] for call in matrix["calls"]}) == 42
    assert all(call["prompt_sha256"] for call in matrix["calls"])
    assert all(call["source_output_sha256"] for call in matrix["calls"])
    assert all(call["sealed_result_present"] is True for call in matrix["calls"])
    assert matrix["all_prompt_hashes_frozen"] is True
    assert matrix["production_ledger_complete"] is True
