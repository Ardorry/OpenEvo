"""Balanced, blinded, same-call GPT-4 comparison of sealed full-v5 G1 and G2.

The judge receives only the task and Student A/B answers.  A deterministic
pre-call shuffle assigns G1 to Student A for seven tasks and G2 to Student A
for seven tasks.  This project-added analysis has an isolated authorization,
call namespace, claim/receipt ledger, result root, and aggregate.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from .hashing import canonical_sha256, file_sha256
from .models import TaskItem
from .paper_core import (
    PAPER_CORE_ROUTE,
    _assert_dedicated_core_node,
    _assessment_from_core_status,
    _exclusive_json_write,
    _submit_once_and_poll,
    build_paper_task_request,
)
from .paper_direct_comparison import _bootstrap_mean_ci, _exact_sign_test
from .paper_evaluator import (
    FROZEN_PAPER_TASK_IDS,
    PAPER_EVALUATOR_CALIBRATION_RESULTS_SHA256,
    PAPER_EVALUATOR_CONTEXT_TOKENS,
    PAPER_EVALUATOR_DATA_COLLECTION,
    PAPER_EVALUATOR_INPUT_USD_PER_TOKEN,
    PAPER_EVALUATOR_MAX_OUTPUT_TOKENS,
    PAPER_EVALUATOR_MODEL,
    PAPER_EVALUATOR_OUTPUT_USD_PER_TOKEN,
    PAPER_EVALUATOR_PROMPT_CANDIDATE,
    PAPER_EVALUATOR_PROMPT_SHA256,
    PAPER_EVALUATOR_PROTOCOL,
    PAPER_EVALUATOR_PROVIDER,
    PAPER_EVALUATOR_TEMPERATURE,
    DualStudentAssessment,
    assert_sealed_run_ready,
    estimate_chat_input_tokens,
    render_compatible_prompt,
)
from .three_artifact_models import ThreeArtifactPairResult, three_artifact_protocol_label

BLIND_PLAN_SCHEMA = "chemcrow_paper_blind_g1_g2_plan_v1"
BLIND_RESULT_SCHEMA = "chemcrow_paper_blind_g1_g2_result_v1"
BLIND_AGGREGATE_SCHEMA = "chemcrow_paper_blind_g1_g2_aggregate_v1"
BLIND_PROTOCOL = "full_v5_g1_vs_g2_balanced_blind_calibrated_paper_judge_v1"
BLIND_COMPARISON = "blind_g1_vs_g2"
BLIND_CALL_COUNT = len(FROZEN_PAPER_TASK_IDS)
BLIND_CALL_ID_PREFIX = "paper-blind-v1"
BLIND_RANDOMIZATION_SEED = 20260827
BLIND_AUTHORIZATION = "I_AUTHORIZE_14_SEALED_BLIND_G1_G2_PAPER_COMPARISONS"
BLIND_MAX_AUTHORIZED_USD = 5.0
_IDENTITY_LABELS = (
    "openevo",
    "reflector",
    "agent_system",
    "skill_bundle",
    "student a is g1",
    "student a is g2",
    "student b is g1",
    "student b is g2",
)


class PaperBlindG1G2Call(BaseModel):
    """One frozen blind comparison with a privately recorded answer mapping."""

    model_config = ConfigDict(extra="forbid")

    call_id: str
    task_id: str
    comparison: Literal["blind_g1_vs_g2"]
    student_a_system: Literal["openevo_baseline", "openevo_evolved"]
    student_b_system: Literal["openevo_baseline", "openevo_evolved"]
    prompt: str
    prompt_sha256: str
    source_pair_id: str
    source_pair_result_sha256: str
    student_a_output_id: str
    student_a_answer_sha256: str
    student_b_output_id: str
    student_b_answer_sha256: str
    estimated_input_tokens: int
    metric_classification: Literal["project_added_blind_direct_comparison"]
    paper_comparable: Literal[False]
    identity_labels_inserted: Literal[False]

    @model_validator(mode="after")
    def _bind_mapping_and_sources(self) -> PaperBlindG1G2Call:
        if {self.student_a_system, self.student_b_system} != {
            "openevo_baseline",
            "openevo_evolved",
        }:
            raise ValueError("blind comparison must contain one G1 and one G2 answer")
        if self.prompt_sha256 != canonical_sha256(self.prompt):
            raise ValueError("blind-comparison prompt hash differs from prompt bytes")
        if not self.source_pair_id.endswith(f"--{self.task_id}"):
            raise ValueError("blind-comparison pair is not bound to the task")
        if self.estimated_input_tokens <= 0:
            raise ValueError("blind-comparison token estimate must be positive")
        return self


def blind_cost_ceiling() -> dict[str, Any]:
    max_input_tokens = PAPER_EVALUATOR_CONTEXT_TOKENS - PAPER_EVALUATOR_MAX_OUTPUT_TOKENS
    per_call = (
        max_input_tokens * PAPER_EVALUATOR_INPUT_USD_PER_TOKEN
        + PAPER_EVALUATOR_OUTPUT_USD_PER_TOKEN
        * PAPER_EVALUATOR_MAX_OUTPUT_TOKENS
    )
    return {
        "model_context_tokens": PAPER_EVALUATOR_CONTEXT_TOKENS,
        "max_output_tokens_per_call": PAPER_EVALUATOR_MAX_OUTPUT_TOKENS,
        "max_input_tokens_per_call": max_input_tokens,
        "call_count": BLIND_CALL_COUNT,
        "list_price_ceiling_usd_per_call": round(per_call, 8),
        "list_price_ceiling_usd_total": round(per_call * BLIND_CALL_COUNT, 8),
        "authorized_max_usd": BLIND_MAX_AUTHORIZED_USD,
    }


def balanced_answer_order() -> dict[str, str]:
    """Return the frozen 7/7 Student-A assignment without using any scores."""
    task_ids = list(FROZEN_PAPER_TASK_IDS)
    randomizer = random.Random(BLIND_RANDOMIZATION_SEED)
    randomizer.shuffle(task_ids)
    g1_as_a = set(task_ids[: BLIND_CALL_COUNT // 2])
    assignment = {
        task_id: "openevo_baseline" if task_id in g1_as_a else "openevo_evolved"
        for task_id in FROZEN_PAPER_TASK_IDS
    }
    if list(assignment.values()).count("openevo_baseline") != BLIND_CALL_COUNT // 2:
        raise AssertionError("blind assignment is not exactly balanced")
    return assignment


def build_blind_g1_g2_plan(
    *,
    tasks: list[TaskItem],
    pairs: dict[str, ThreeArtifactPairResult],
    expected_source_pair_protocol: str,
    immutable_reference_hashes: dict[str, tuple[Path, str]],
) -> dict[str, Any]:
    """Freeze balanced answer order and all prompt/source hashes before calls."""
    if [task.task_id for task in tasks] != list(FROZEN_PAPER_TASK_IDS):
        raise ValueError("blind G1/G2 comparison requires the frozen 14-task order")
    for label, (path, expected_sha256) in immutable_reference_hashes.items():
        if file_sha256(path) != expected_sha256:
            raise ValueError(f"immutable reference differs before blind comparison: {label}")
    assignment = balanced_answer_order()
    calls: list[PaperBlindG1G2Call] = []
    for task in tasks:
        pair = pairs[task.task_id]
        if three_artifact_protocol_label(pair.artifact_bundle.protocol) != (
            expected_source_pair_protocol
        ):
            raise ValueError(f"blind-comparison source protocol differs: {task.task_id}")
        a_is_g1 = assignment[task.task_id] == "openevo_baseline"
        student_a = pair.baseline if a_is_g1 else pair.evolved
        student_b = pair.evolved if a_is_g1 else pair.baseline
        student_a_system = "openevo_baseline" if a_is_g1 else "openevo_evolved"
        student_b_system = "openevo_evolved" if a_is_g1 else "openevo_baseline"
        prompt = render_compatible_prompt(
            task_prompt=task.prompt,
            student_a=student_a.answer,
            student_b=student_b.answer,
        )
        prompt_framing = prompt.casefold()
        if any(label in prompt_framing for label in _IDENTITY_LABELS):
            raise ValueError(f"blind prompt exposes an experiment identity: {task.task_id}")
        estimated = estimate_chat_input_tokens(prompt)
        call_id = f"{BLIND_CALL_ID_PREFIX}-{task.task_id}-blind"
        if estimated > blind_cost_ceiling()["max_input_tokens_per_call"]:
            raise ValueError(f"blind-comparison prompt exceeds input budget: {call_id}")
        calls.append(
            PaperBlindG1G2Call(
                call_id=call_id,
                task_id=task.task_id,
                comparison=BLIND_COMPARISON,
                student_a_system=student_a_system,
                student_b_system=student_b_system,
                prompt=prompt,
                prompt_sha256=canonical_sha256(prompt),
                source_pair_id=pair.pair_id,
                source_pair_result_sha256=canonical_sha256(pair.model_dump(mode="json")),
                student_a_output_id=student_a.run_id,
                student_a_answer_sha256=canonical_sha256(student_a.answer),
                student_b_output_id=student_b.run_id,
                student_b_answer_sha256=canonical_sha256(student_b.answer),
                estimated_input_tokens=estimated,
                metric_classification="project_added_blind_direct_comparison",
                paper_comparable=False,
                identity_labels_inserted=False,
            )
        )
    if len(calls) != BLIND_CALL_COUNT or len({call.call_id for call in calls}) != len(calls):
        raise AssertionError("blind comparison requires 14 unique calls")
    order_manifest = [
        {"task_id": call.task_id, "student_a_system": call.student_a_system}
        for call in calls
    ]
    return {
        "schema_version": BLIND_PLAN_SCHEMA,
        "protocol": BLIND_PROTOCOL,
        "judge_protocol": PAPER_EVALUATOR_PROTOCOL,
        "prompt_candidate_id": PAPER_EVALUATOR_PROMPT_CANDIDATE,
        "prompt_candidate_sha256": PAPER_EVALUATOR_PROMPT_SHA256,
        "calibration_results_sha256": PAPER_EVALUATOR_CALIBRATION_RESULTS_SHA256,
        "model": PAPER_EVALUATOR_MODEL,
        "temperature": PAPER_EVALUATOR_TEMPERATURE,
        "provider_only": [PAPER_EVALUATOR_PROVIDER],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": PAPER_EVALUATOR_DATA_COLLECTION,
        "blind_to_g1_g2_identity": True,
        "answer_order_method": "deterministic_seeded_shuffle_then_balanced_7_7",
        "answer_order_seed": BLIND_RANDOMIZATION_SEED,
        "answer_order_manifest": order_manifest,
        "answer_order_manifest_sha256": canonical_sha256(order_manifest),
        "g1_as_student_a_count": sum(
            call.student_a_system == "openevo_baseline" for call in calls
        ),
        "g2_as_student_a_count": sum(
            call.student_a_system == "openevo_evolved" for call in calls
        ),
        "reflector_access": False,
        "evolution_feedback_access": False,
        "sealed_output_only": True,
        "paper_comparable": False,
        "project_added_metric": True,
        "source_pair_protocol": expected_source_pair_protocol,
        "call_id_prefix": BLIND_CALL_ID_PREFIX,
        "call_count": len(calls),
        "cost_ceiling": blind_cost_ceiling(),
        "immutable_reference_sha256": {
            label: expected for label, (_, expected) in immutable_reference_hashes.items()
        },
        "calls": [call.model_dump(mode="json") for call in calls],
    }


def validate_blind_g1_g2_plan(plan: dict[str, Any]) -> list[PaperBlindG1G2Call]:
    if (
        plan.get("schema_version") != BLIND_PLAN_SCHEMA
        or plan.get("protocol") != BLIND_PROTOCOL
        or plan.get("judge_protocol") != PAPER_EVALUATOR_PROTOCOL
        or plan.get("prompt_candidate_sha256") != PAPER_EVALUATOR_PROMPT_SHA256
        or plan.get("model") != PAPER_EVALUATOR_MODEL
        or plan.get("temperature") != PAPER_EVALUATOR_TEMPERATURE
        or plan.get("provider_only") != [PAPER_EVALUATOR_PROVIDER]
        or plan.get("allow_fallbacks") is not False
        or plan.get("call_count") != BLIND_CALL_COUNT
        or plan.get("call_id_prefix") != BLIND_CALL_ID_PREFIX
        or plan.get("blind_to_g1_g2_identity") is not True
        or plan.get("answer_order_seed") != BLIND_RANDOMIZATION_SEED
        or plan.get("g1_as_student_a_count") != 7
        or plan.get("g2_as_student_a_count") != 7
        or plan.get("paper_comparable") is not False
        or plan.get("project_added_metric") is not True
    ):
        raise ValueError("blind G1/G2 plan authority is invalid")
    calls = [PaperBlindG1G2Call.model_validate(raw) for raw in plan.get("calls", [])]
    order_manifest = [
        {"task_id": call.task_id, "student_a_system": call.student_a_system}
        for call in calls
    ]
    if (
        len(calls) != BLIND_CALL_COUNT
        or tuple(call.task_id for call in calls) != FROZEN_PAPER_TASK_IDS
        or len({call.call_id for call in calls}) != BLIND_CALL_COUNT
        or any(
            call.call_id != f"{BLIND_CALL_ID_PREFIX}-{call.task_id}-blind"
            for call in calls
        )
        or plan.get("answer_order_manifest") != order_manifest
        or plan.get("answer_order_manifest_sha256") != canonical_sha256(order_manifest)
        or order_manifest
        != [
            {"task_id": task_id, "student_a_system": student_a_system}
            for task_id, student_a_system in balanced_answer_order().items()
        ]
    ):
        raise ValueError("blind G1/G2 call/order inventory is invalid")
    return calls


def run_blind_g1_g2_plan(
    *,
    plan_path: Path,
    rollout_base_url: str,
    runtime: dict[str, Any],
    result_root: Path,
    shim_receipt_root: Path,
    immutable_reference_paths: dict[str, Path],
    allow_paid: bool,
) -> dict[str, Any]:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    calls = validate_blind_g1_g2_plan(plan)
    if not allow_paid:
        raise PermissionError("blind G1/G2 comparison requires --allow-paid")
    if os.environ.get("CHEMCROW_PAPER_BLIND_G1_G2_AUTHORIZATION") != BLIND_AUTHORIZATION:
        raise PermissionError("blind G1/G2 authorization literal is absent")
    if os.environ.get("CHEMCROW_PAPER_EVALUATOR_MODEL") != PAPER_EVALUATOR_MODEL:
        raise ValueError("blind G1/G2 model differs from frozen openai/gpt-4")
    budget = float(os.environ.get("CHEMCROW_PAPER_BLIND_G1_G2_MAX_USD", "0"))
    ceiling = float(plan["cost_ceiling"]["list_price_ceiling_usd_total"])
    if budget < ceiling or budget > BLIND_MAX_AUTHORIZED_USD:
        raise PermissionError("blind G1/G2 budget is outside the authorized range")
    for label, expected in plan["immutable_reference_sha256"].items():
        path = immutable_reference_paths.get(label)
        if path is None or file_sha256(path) != expected:
            raise ValueError(f"immutable reference changed before blind execution: {label}")
    result_root.mkdir(parents=True, exist_ok=True)
    _assert_dedicated_core_node(rollout_base_url)
    results: list[dict[str, Any]] = []
    for call in calls:
        result_path = result_root / f"{call.call_id}.result.json"
        claim_path = shim_receipt_root / f"{call.call_id}.claim.json"
        receipt_path = shim_receipt_root / f"{call.call_id}.receipt.json"
        if result_path.exists():
            results.append(_validate_existing_result(result_path, call))
            continue
        if claim_path.exists() or receipt_path.exists():
            raise RuntimeError(f"ambiguous blind call will not be retried: {call.call_id}")
        status = _submit_once_and_poll(
            rollout_base_url,
            build_paper_task_request(call=call, runtime=runtime),  # type: ignore[arg-type]
        )
        assessment = _assessment_from_core_status(status)
        if not receipt_path.is_file():
            raise RuntimeError(f"blind G1/G2 receipt is missing: {call.call_id}")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        _validate_receipt(receipt, call.call_id)
        result = {
            "schema_version": BLIND_RESULT_SCHEMA,
            "call_id": call.call_id,
            "task_id": call.task_id,
            "comparison": call.comparison,
            "student_a_system": call.student_a_system,
            "student_b_system": call.student_b_system,
            "prompt_sha256": call.prompt_sha256,
            "assessment": assessment.model_dump(mode="json"),
            "assessment_sha256": canonical_sha256(assessment.model_dump(mode="json")),
            "openrouter_receipt_sha256": file_sha256(receipt_path),
            "core_terminal_payload_sha256": canonical_sha256(status),
            "execution_route": PAPER_CORE_ROUTE,
            "judge_identity_labels_received": False,
            "reflector_access": False,
            "evolution_feedback_access": False,
            "project_added_metric": True,
        }
        _exclusive_json_write(result_path, result)
        results.append(result)
    aggregate = aggregate_blind_g1_g2_results(
        results=results,
        calls=calls,
        receipt_root=shim_receipt_root,
        plan_sha256=file_sha256(plan_path),
        immutable_reference_paths=immutable_reference_paths,
        expected_reference_hashes=plan["immutable_reference_sha256"],
    )
    aggregate_path = result_root / "aggregate.json"
    serialized = json.dumps(aggregate, indent=2, sort_keys=True) + "\n"
    if aggregate_path.exists():
        if aggregate_path.read_text(encoding="utf-8") != serialized:
            raise ValueError("existing blind aggregate differs from sealed results")
    else:
        _exclusive_json_write(aggregate_path, aggregate)
    return aggregate


def aggregate_blind_g1_g2_results(
    *,
    results: list[dict[str, Any]],
    calls: list[PaperBlindG1G2Call],
    receipt_root: Path,
    plan_sha256: str,
    immutable_reference_paths: dict[str, Path],
    expected_reference_hashes: dict[str, str],
) -> dict[str, Any]:
    if len(results) != BLIND_CALL_COUNT or len(calls) != BLIND_CALL_COUNT:
        raise ValueError("blind G1/G2 aggregate requires exactly 14 calls")
    call_by_id = {call.call_id: call for call in calls}
    per_task: list[dict[str, Any]] = []
    costs: list[float] = []
    prompt_tokens = completion_tokens = 0
    for result in sorted(results, key=lambda item: str(item["task_id"])):
        call = call_by_id[str(result["call_id"])]
        assessment = DualStudentAssessment.model_validate(result["assessment"])
        if call.student_a_system == "openevo_baseline":
            g1_grade, g2_grade = assessment.student_a.grade, assessment.student_b.grade
            g1_position, g2_position = "A", "B"
        else:
            g1_grade, g2_grade = assessment.student_b.grade, assessment.student_a.grade
            g1_position, g2_position = "B", "A"
        delta = g2_grade - g1_grade
        receipt_path = receipt_root / f"{call.call_id}.receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        _validate_receipt(receipt, call.call_id)
        cost = receipt.get("openrouter_reported_cost_usd")
        if not isinstance(cost, int | float):
            raise TypeError(f"blind receipt has no proven cost: {call.call_id}")
        costs.append(float(cost))
        prompt_tokens += int(receipt["prompt_tokens"])
        completion_tokens += int(receipt["completion_tokens"])
        per_task.append(
            {
                "task_id": call.task_id,
                "call_id": call.call_id,
                "g1_position": g1_position,
                "g2_position": g2_position,
                "g1_grade": g1_grade,
                "g2_grade": g2_grade,
                "g2_minus_g1": delta,
                "winner": "g2" if delta > 0 else "g1" if delta < 0 else "tie",
                "assessment_sha256": result["assessment_sha256"],
                "receipt_sha256": file_sha256(receipt_path),
            }
        )
    deltas = [float(row["g2_minus_g1"]) for row in per_task]
    g1_scores = [float(row["g1_grade"]) for row in per_task]
    g2_scores = [float(row["g2_grade"]) for row in per_task]
    wins = sum(delta > 0 for delta in deltas)
    losses = sum(delta < 0 for delta in deltas)
    references_after = {
        label: file_sha256(path) for label, path in immutable_reference_paths.items()
    }
    return {
        "schema_version": BLIND_AGGREGATE_SCHEMA,
        "status": "PROVISIONAL_LLM_JUDGED_BLIND_G1_G2_COMPLETE",
        "protocol": BLIND_PROTOCOL,
        "judge_protocol": PAPER_EVALUATOR_PROTOCOL,
        "model": PAPER_EVALUATOR_MODEL,
        "temperature": PAPER_EVALUATOR_TEMPERATURE,
        "provider": "OpenAI",
        "allow_fallbacks": False,
        "paper_comparable": False,
        "project_added_metric": True,
        "blind_to_g1_g2_identity": True,
        "answer_order_seed": BLIND_RANDOMIZATION_SEED,
        "g1_as_student_a_count": sum(row["g1_position"] == "A" for row in per_task),
        "g2_as_student_a_count": sum(row["g2_position"] == "A" for row in per_task),
        "call_count": BLIND_CALL_COUNT,
        "mean_g1_grade": statistics.mean(g1_scores),
        "mean_g2_grade": statistics.mean(g2_scores),
        "mean_g2_minus_g1": statistics.mean(deltas),
        "median_g2_minus_g1": statistics.median(deltas),
        "g2_wins": wins,
        "ties": sum(delta == 0 for delta in deltas),
        "g1_wins": losses,
        "paired_bootstrap_95_ci_mean_delta": _bootstrap_mean_ci(deltas),
        "exact_sign_test_two_sided_p": _exact_sign_test(wins=wins, losses=losses),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "actual_openrouter_reported_cost_usd": round(sum(costs), 8),
        "plan_sha256": plan_sha256,
        "immutable_reference_sha256_before": expected_reference_hashes,
        "immutable_reference_sha256_after": references_after,
        "immutable_references_unchanged": references_after == expected_reference_hashes,
        "per_task": per_task,
        "limitations": (
            "One call per task and one answer order per task. Order is randomized and exactly "
            "balanced across tasks, but no within-task A/B reversal replicate was run; N=14 and "
            "the GPT-4 judge is stochastic with discrete grades."
        ),
    }


def preflight_blind_g1_g2(*, config_path: Path, output: Path) -> dict[str, Any]:
    config = _load_config(config_path)
    tasks = _read_tasks(_path(config_path, config["task_manifest"]))
    pairs = assert_sealed_run_ready(
        run_root=_path(config_path, config["run_root"]),
        experiment_id=str(config["experiment_id"]),
        task_ids=[task.task_id for task in tasks],
        completed_run_audit=_path(config_path, config["completed_run_audit"]),
    )
    references = _reference_bindings(config_path, config)
    plan = build_blind_g1_g2_plan(
        tasks=tasks,
        pairs=pairs,
        expected_source_pair_protocol=str(config["source_pair_protocol"]),
        immutable_reference_hashes=references,
    )
    plan_path = _path(config_path, config["plan_path"])
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(plan, indent=2, sort_keys=True) + "\n"
    if plan_path.exists():
        if plan_path.read_text(encoding="utf-8") != serialized:
            raise ValueError("existing blind G1/G2 plan differs from frozen inputs")
    else:
        descriptor = os.open(plan_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
    report = {
        "schema_version": "chemcrow_paper_blind_g1_g2_preflight_v1",
        "status": "READY_FOR_EXPLICIT_PAID_BLIND_G1_G2_COMPARISON",
        "model_calls": 0,
        "paid_operations": 0,
        "call_count": plan["call_count"],
        "task_ids": [task.task_id for task in tasks],
        "plan_path": str(plan_path),
        "plan_sha256": file_sha256(plan_path),
        "prompt_candidate_sha256": PAPER_EVALUATOR_PROMPT_SHA256,
        "answer_order_seed": BLIND_RANDOMIZATION_SEED,
        "answer_order_manifest_sha256": plan["answer_order_manifest_sha256"],
        "g1_as_student_a_count": plan["g1_as_student_a_count"],
        "g2_as_student_a_count": plan["g2_as_student_a_count"],
        "identity_labels_inserted_count": 0,
        "cost_ceiling": plan["cost_ceiling"],
        "immutable_references_unchanged": True,
        "production_42_call_ledger_included": False,
        "historical_direct_ledger_included": False,
        "secret_values_included": False,
    }
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def audit_blind_g1_g2(
    *, config_path: Path, output_json: Path, output_markdown: Path
) -> dict[str, Any]:
    config = _load_config(config_path)
    plan_path = _path(config_path, config["plan_path"])
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    calls = validate_blind_g1_g2_plan(plan)
    result_root = _path(config_path, config["result_root"])
    receipt_root = _path(config_path, config["shim_receipt_root"])
    results = [
        _validate_existing_result(result_root / f"{call.call_id}.result.json", call)
        for call in calls
    ]
    reference_paths = {
        label: path for label, (path, _) in _reference_bindings(config_path, config).items()
    }
    aggregate = aggregate_blind_g1_g2_results(
        results=results,
        calls=calls,
        receipt_root=receipt_root,
        plan_sha256=file_sha256(plan_path),
        immutable_reference_paths=reference_paths,
        expected_reference_hashes=plan["immutable_reference_sha256"],
    )
    claims = sorted(receipt_root.glob("*.claim.json"))
    receipts = sorted(receipt_root.glob("*.receipt.json"))
    expected_call_ids = {call.call_id for call in calls}
    if len(claims) != BLIND_CALL_COUNT or len(receipts) != BLIND_CALL_COUNT:
        raise ValueError("blind G1/G2 claim/receipt inventory is incomplete")
    observed_claim_ids: set[str] = set()
    for path in claims:
        claim = json.loads(path.read_text(encoding="utf-8"))
        if (
            claim.get("call_id") not in expected_call_ids
            or claim.get("ledger_class") != "blind_g1_g2"
            or claim.get("production_ledger_included") is not False
            or claim.get("prompt_or_response_included") is not False
        ):
            raise ValueError(f"invalid blind G1/G2 claim: {path.name}")
        observed_claim_ids.add(str(claim["call_id"]))
    if observed_claim_ids != expected_call_ids:
        raise ValueError("blind G1/G2 claims differ from plan")
    audit = {
        **aggregate,
        "status": "PASS",
        "claim_count": len(claims),
        "receipt_count": len(receipts),
        "result_count": len(results),
        "http_200_count": len(results),
        "json_valid_count": len(results),
        "pydantic_valid_count": len(results),
        "openai_provider_count": len(results),
        "no_fallback_count": len(results),
        "identity_labels_inserted_count": 0,
        "credential_persisted_count": 0,
        "prompt_or_response_persisted_in_receipt_count": 0,
    }
    output_json.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output_markdown.write_text(_render_markdown(audit), encoding="utf-8")
    return audit


def _validate_existing_result(path: Path, call: PaperBlindG1G2Call) -> dict[str, Any]:
    result = json.loads(path.read_text(encoding="utf-8"))
    if (
        result.get("schema_version") != BLIND_RESULT_SCHEMA
        or result.get("call_id") != call.call_id
        or result.get("task_id") != call.task_id
        or result.get("prompt_sha256") != call.prompt_sha256
        or result.get("execution_route") != PAPER_CORE_ROUTE
        or result.get("student_a_system") != call.student_a_system
        or result.get("student_b_system") != call.student_b_system
        or result.get("judge_identity_labels_received") is not False
    ):
        raise ValueError(f"existing blind result authority mismatch: {call.call_id}")
    DualStudentAssessment.model_validate(result.get("assessment"))
    return result


def _validate_receipt(receipt: dict[str, Any], call_id: str) -> None:
    if (
        receipt.get("status") != "terminal_success"
        or receipt.get("call_id") != call_id
        or receipt.get("model") != PAPER_EVALUATOR_MODEL
        or str(receipt.get("provider", "")).lower() != "openai"
        or receipt.get("upstream_http_status") != 200
        or receipt.get("temperature") != PAPER_EVALUATOR_TEMPERATURE
        or receipt.get("allow_fallbacks") is not False
        or receipt.get("provider_only") != [PAPER_EVALUATOR_PROVIDER]
        or receipt.get("require_parameters") is not True
        or receipt.get("data_collection") != PAPER_EVALUATOR_DATA_COLLECTION
        or receipt.get("internal_response_format_validated") is not True
        or receipt.get("upstream_response_format_omitted") is not True
        or receipt.get("ledger_class") != "blind_g1_g2"
        or receipt.get("production_ledger_included") is not False
        or receipt.get("prompt_or_response_included") is not False
        or receipt.get("credential_included") is not False
    ):
        raise ValueError(f"invalid blind G1/G2 receipt: {call_id}")


def _render_markdown(audit: dict[str, Any]) -> str:
    rows = [
        "# Balanced Blind GPT-4 Comparison: full-v5 G1 vs G2",
        "",
        (
            "This project-added comparison uses the frozen calibrated ChemCrow-compatible "
            "judge. The judge received only the task and Student A/B answers."
        ),
        "",
        f"- status: `{audit['status']}`",
        f"- calls: `{audit['call_count']}/14`",
        (
            f"- G1 as A / G2 as A: `{audit['g1_as_student_a_count']}/"
            f"{audit['g2_as_student_a_count']}`"
        ),
        f"- randomization seed: `{audit['answer_order_seed']}`",
        f"- mean G1: `{audit['mean_g1_grade']:.3f}`",
        f"- mean G2: `{audit['mean_g2_grade']:.3f}`",
        f"- mean G2 - G1: `{audit['mean_g2_minus_g1']:+.3f}`",
        f"- G2 wins/ties/G1 wins: `{audit['g2_wins']}/{audit['ties']}/{audit['g1_wins']}`",
        f"- bootstrap 95% CI: `{audit['paired_bootstrap_95_ci_mean_delta']}`",
        f"- exact sign-test p: `{audit['exact_sign_test_two_sided_p']:.6f}`",
        f"- proven cost: `${audit['actual_openrouter_reported_cost_usd']:.5f}`",
        f"- immutable prior ledgers unchanged: `{audit['immutable_references_unchanged']}`",
        "",
        "| task | G1 pos | G1 | G2 pos | G2 | G2 - G1 | winner |",
        "|---|---|---:|---|---:|---:|---|",
    ]
    for row in audit["per_task"]:
        rows.append(
            f"| {row['task_id']} | {row['g1_position']} | {row['g1_grade']:.1f} | "
            f"{row['g2_position']} | {row['g2_grade']:.1f} | "
            f"{row['g2_minus_g1']:+.1f} | {row['winner']} |"
        )
    rows.extend(["", f"Limitation: {audit['limitations']}", ""])
    return "\n".join(rows)


def _reference_bindings(
    config_path: Path, config: dict[str, Any]
) -> dict[str, tuple[Path, str]]:
    return {
        "production_42_call_aggregate": (
            _path(config_path, config["reference_42_call_aggregate"]),
            str(config["reference_42_call_aggregate_sha256"]),
        ),
        "historical_vs_g2_direct_aggregate": (
            _path(config_path, config["reference_historical_direct_aggregate"]),
            str(config["reference_historical_direct_aggregate_sha256"]),
        ),
    }


def _load_config(path: Path) -> dict[str, Any]:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise TypeError("blind G1/G2 config must be a YAML object")
    return config


def _path(config_path: Path, value: Any) -> Path:
    path = Path(str(value))
    if not path.is_absolute():
        path = config_path.parent / path
    return path.resolve()


def _read_tasks(path: Path) -> list[TaskItem]:
    return [
        TaskItem.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _resolve_env(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        name = value[2:-1]
        if not name or not os.environ.get(name):
            raise ValueError(f"required environment variable is absent: {name}")
        return os.environ[name]
    if isinstance(value, list):
        return [_resolve_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_env(item) for key, item in value.items()}
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chemcrow-paper-blind-g1-g2")
    commands = parser.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--config", type=Path, required=True)
    preflight.add_argument("--output", type=Path, required=True)
    preflight.add_argument("--no-model-calls", action="store_true", required=True)
    run = commands.add_parser("run")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--allow-paid", action="store_true")
    audit = commands.add_parser("audit")
    audit.add_argument("--config", type=Path, required=True)
    audit.add_argument("--output-json", type=Path, required=True)
    audit.add_argument("--output-markdown", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    config_path = args.config.resolve()
    if args.command == "preflight":
        report = preflight_blind_g1_g2(
            config_path=config_path, output=args.output.resolve()
        )
        print(json.dumps({"status": report["status"], "model_calls": 0}, sort_keys=True))
        return
    if args.command == "run":
        config = _resolve_env(_load_config(config_path))
        aggregate = run_blind_g1_g2_plan(
            plan_path=_path(config_path, config["plan_path"]),
            rollout_base_url=str(config["rollout_base_url"]),
            runtime=dict(config["runtime"]),
            result_root=_path(config_path, config["result_root"]),
            shim_receipt_root=_path(config_path, config["shim_receipt_root"]),
            immutable_reference_paths={
                label: path
                for label, (path, _) in _reference_bindings(config_path, config).items()
            },
            allow_paid=args.allow_paid,
        )
        print(
            json.dumps(
                {"status": aggregate["status"], "result_count": aggregate["call_count"]},
                sort_keys=True,
            )
        )
        return
    audit = audit_blind_g1_g2(
        config_path=config_path,
        output_json=args.output_json.resolve(),
        output_markdown=args.output_markdown.resolve(),
    )
    print(json.dumps({"status": audit["status"], "result_count": audit["result_count"]}))


if __name__ == "__main__":
    main()
