from __future__ import annotations

import json
from pathlib import Path

import pytest
from openevo.harness.models import AgentSpec

from openevo_chemcrow.hashing import canonical_sha256, file_sha256
from openevo_chemcrow.models import TaskItem, Trajectory
from openevo_chemcrow.paper_blind_g1_g2 import (
    BLIND_AUTHORIZATION,
    BLIND_CALL_COUNT,
    BLIND_ORDER_REVERSED,
    BLIND_RANDOMIZATION_SEED,
    BLIND_REVERSED_AUTHORIZATION,
    BLIND_REVERSED_CALL_ID_PREFIX,
    _position_diagnostics,
    aggregate_blind_g1_g2_results,
    balanced_answer_order,
    blind_cost_ceiling,
    blind_plan_runtime_authority,
    build_blind_g1_g2_plan,
    reversed_answer_order,
    run_blind_g1_g2_plan,
    validate_blind_g1_g2_plan,
)
from openevo_chemcrow.paper_evaluator import (
    FROZEN_PAPER_TASK_IDS,
    PAPER_EVALUATOR_MODEL,
    PAPER_EVALUATOR_PROMPT_SHA256,
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
                answer=f"sealed first answer {task.task_id}",
                candidate_config_sha256="s0",
            ),
            evolved=Trajectory(
                run_id=f"evolved-{task.task_id}",
                task_id=task.task_id,
                role="evolved",
                status="COMPLETED",
                answer=f"sealed second answer {task.task_id}",
                candidate_config_sha256="s0",
                artifact_ids=["memory", "skill", "agent-system"],
            ),
        )
        for task in tasks
    }


def _references(
    tmp_path: Path, *, include_original_round: bool = False
) -> dict[str, tuple[Path, str]]:
    refs = {}
    for label in ("production_42_call_aggregate", "historical_vs_g2_direct_aggregate"):
        path = tmp_path / f"{label}.json"
        path.write_text(json.dumps({"label": label}) + "\n", encoding="utf-8")
        refs[label] = (path, file_sha256(path))
    if include_original_round:
        for label in ("original_blind_plan", "original_blind_aggregate"):
            path = tmp_path / f"{label}.json"
            path.write_text(json.dumps({"label": label}) + "\n", encoding="utf-8")
            refs[label] = (path, file_sha256(path))
    return refs


def _plan(tmp_path: Path) -> tuple[dict, dict[str, tuple[Path, str]]]:
    tasks = _tasks()
    refs = _references(tmp_path)
    return (
        build_blind_g1_g2_plan(
            tasks=tasks,
            pairs=_pairs(tasks),
            expected_source_pair_protocol=CORE_NATIVE_THREE_ARTIFACT_PROTOCOL_LABEL,
            immutable_reference_hashes=refs,
        ),
        refs,
    )


def test_balanced_order_is_seeded_deterministic_and_exactly_seven_seven():
    first = balanced_answer_order()
    second = balanced_answer_order()

    assert first == second
    assert tuple(first) == FROZEN_PAPER_TASK_IDS
    assert list(first.values()).count("openevo_baseline") == 7
    assert list(first.values()).count("openevo_evolved") == 7
    assert BLIND_RANDOMIZATION_SEED == 20260827


def test_reversed_order_is_the_exact_per_task_inverse():
    original = balanced_answer_order()
    reversed_order = reversed_answer_order()

    assert tuple(reversed_order) == FROZEN_PAPER_TASK_IDS
    assert all(original[task_id] != reversed_order[task_id] for task_id in original)
    assert list(reversed_order.values()).count("openevo_baseline") == 7
    assert list(reversed_order.values()).count("openevo_evolved") == 7


def test_reversed_plan_has_fresh_namespace_and_binds_original_round(tmp_path):
    tasks = _tasks()
    refs = _references(tmp_path, include_original_round=True)
    plan = build_blind_g1_g2_plan(
        tasks=tasks,
        pairs=_pairs(tasks),
        expected_source_pair_protocol=CORE_NATIVE_THREE_ARTIFACT_PROTOCOL_LABEL,
        immutable_reference_hashes=refs,
        answer_order_variant=BLIND_ORDER_REVERSED,
    )
    calls = validate_blind_g1_g2_plan(plan)
    authority = blind_plan_runtime_authority(plan)

    assert plan["answer_order_variant"] == BLIND_ORDER_REVERSED
    assert plan["reversed_from_plan_sha256"] == refs["original_blind_plan"][1]
    assert plan["reversed_from_aggregate_sha256"] == refs["original_blind_aggregate"][1]
    assert authority["authorization"] == BLIND_REVERSED_AUTHORIZATION
    assert authority["authorization"] != BLIND_AUTHORIZATION
    assert authority["call_id_prefix"] == BLIND_REVERSED_CALL_ID_PREFIX
    assert all(call.call_id.startswith(f"{BLIND_REVERSED_CALL_ID_PREFIX}-") for call in calls)
    assert all(
        call.student_a_system == reversed_answer_order()[call.task_id] for call in calls
    )


def test_reversed_plan_requires_original_round_hash_bindings(tmp_path):
    tasks = _tasks()
    with pytest.raises(ValueError, match="not bound to the original round"):
        build_blind_g1_g2_plan(
            tasks=tasks,
            pairs=_pairs(tasks),
            expected_source_pair_protocol=CORE_NATIVE_THREE_ARTIFACT_PROTOCOL_LABEL,
            immutable_reference_hashes=_references(tmp_path),
            answer_order_variant=BLIND_ORDER_REVERSED,
        )


def test_blind_plan_freezes_mapping_without_identity_labels(tmp_path):
    plan, _ = _plan(tmp_path)
    calls = validate_blind_g1_g2_plan(plan)

    assert plan["call_count"] == BLIND_CALL_COUNT == 14
    assert plan["prompt_candidate_sha256"] == PAPER_EVALUATOR_PROMPT_SHA256
    assert plan["blind_to_g1_g2_identity"] is True
    assert plan["g1_as_student_a_count"] == plan["g2_as_student_a_count"] == 7
    assert len({call.prompt_sha256 for call in calls}) == 14
    assert all(call.identity_labels_inserted is False for call in calls)
    assert all("STUDENT A ANSWER:" in call.prompt for call in calls)
    assert all("STUDENT B ANSWER:" in call.prompt for call in calls)
    assert all(call.call_id.endswith("-blind") for call in calls)


def test_blind_cost_and_authorization_are_isolated(tmp_path, monkeypatch):
    plan, refs = _plan(tmp_path)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")

    assert blind_cost_ceiling()["list_price_ceiling_usd_total"] == 3.94422
    monkeypatch.setenv("CHEMCROW_PAPER_EVALUATOR_MODEL", PAPER_EVALUATOR_MODEL)
    monkeypatch.setenv("CHEMCROW_PAPER_BLIND_G1_G2_MAX_USD", "3.94422")
    monkeypatch.setenv(
        "CHEMCROW_PAPER_EVALUATOR_AUTHORIZATION",
        "I_AUTHORIZE_42_SEALED_PAPER_EVALUATIONS",
    )
    monkeypatch.delenv("CHEMCROW_PAPER_BLIND_G1_G2_AUTHORIZATION", raising=False)
    with pytest.raises(PermissionError, match="authorization"):
        run_blind_g1_g2_plan(
            plan_path=plan_path,
            rollout_base_url="http://127.0.0.1:8180",
            runtime={},
            result_root=tmp_path / "results",
            shim_receipt_root=tmp_path / "receipts",
            immutable_reference_paths={label: path for label, (path, _) in refs.items()},
            allow_paid=True,
        )
    assert BLIND_AUTHORIZATION != "I_AUTHORIZE_42_SEALED_PAPER_EVALUATIONS"


def test_paper_harness_accepts_fresh_blind_namespace():
    spec = AgentSpec(
        import_path="openevo_chemcrow.paper_harness:PaperEvaluatorHarness",
        model_name=PAPER_EVALUATOR_MODEL,
        settings={
            "capture_mode": "transcript",
            "temperature": 0.1,
            "max_tokens": 1200,
            "runtime_gateway_base_url": "http://host.docker.internal:8110/v1",
        },
        env={"PAPER_EVALUATOR_CALL_ID": "paper-blind-v1-chemcrow-01-blind"},
    )

    step = PaperEvaluatorHarness(spec).run_steps("blind student answers")[0]

    assert step.env is not None
    assert step.env["PAPER_EVALUATOR_CALL_ID"] == "paper-blind-v1-chemcrow-01-blind"


def test_blind_aggregate_remaps_a_b_back_to_g1_g2(tmp_path):
    plan, refs = _plan(tmp_path)
    calls = validate_blind_g1_g2_plan(plan)
    receipt_root = tmp_path / "receipts"
    receipt_root.mkdir()
    results = []
    for call in calls:
        if call.student_a_system == "openevo_baseline":
            grade_a, grade_b = 7, 8
        else:
            grade_a, grade_b = 8, 7
        assessment = {
            "student_a": {
                "grade": grade_a,
                "strengths": [],
                "weaknesses": [],
                "justification": "valid",
                "feedback": [],
            },
            "student_b": {
                "grade": grade_b,
                "strengths": [],
                "weaknesses": [],
                "justification": "valid",
                "feedback": [],
            },
        }
        receipt = {
            "status": "terminal_success",
            "call_id": call.call_id,
            "model": "openai/gpt-4",
            "provider": "OpenAI",
            "upstream_http_status": 200,
            "temperature": 0.1,
            "allow_fallbacks": False,
            "provider_only": ["openai"],
            "require_parameters": True,
            "data_collection": "allow",
            "internal_response_format_validated": True,
            "upstream_response_format_omitted": True,
            "ledger_class": "blind_g1_g2",
            "production_ledger_included": False,
            "prompt_or_response_included": False,
            "credential_included": False,
            "openrouter_reported_cost_usd": 0.01,
            "prompt_tokens": 100,
            "completion_tokens": 20,
        }
        (receipt_root / f"{call.call_id}.receipt.json").write_text(
            json.dumps(receipt), encoding="utf-8"
        )
        results.append(
            {
                "call_id": call.call_id,
                "task_id": call.task_id,
                "assessment": assessment,
                "assessment_sha256": canonical_sha256(assessment),
            }
        )
    aggregate = aggregate_blind_g1_g2_results(
        results=results,
        calls=calls,
        receipt_root=receipt_root,
        plan_sha256="a" * 64,
        immutable_reference_paths={label: path for label, (path, _) in refs.items()},
        expected_reference_hashes={label: digest for label, (_, digest) in refs.items()},
    )

    assert aggregate["g1_as_student_a_count"] == 7
    assert aggregate["g2_as_student_a_count"] == 7
    assert aggregate["mean_g1_grade"] == 7
    assert aggregate["mean_g2_grade"] == 8
    assert aggregate["mean_g2_minus_g1"] == 1
    assert aggregate["g2_wins"] == 14
    assert aggregate["immutable_references_unchanged"] is True


def test_position_diagnostic_separates_system_mapping_from_a_b_order():
    diagnostic = _position_diagnostics(
        [
            {"g2_position": "A", "g2_minus_g1": 1.0},
            {"g2_position": "B", "g2_minus_g1": -1.0},
            {"g2_position": "B", "g2_minus_g1": 1.0},
            {"g2_position": "A", "g2_minus_g1": 0.0},
        ]
    )

    assert diagnostic["student_a_wins"] == 2
    assert diagnostic["student_b_wins"] == 1
    assert diagnostic["ties"] == 1
    assert diagnostic["g2_when_student_a"]["g2_wins"] == 1
    assert diagnostic["g2_when_student_b"]["g1_wins"] == 1
