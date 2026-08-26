"""Dedicated OpenEvo Core port for sealed ChemCrow paper evaluation."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

from .hashing import canonical_sha256, file_sha256
from .paper_evaluator import (
    PAPER_EVALUATOR_AUTHORIZATION,
    PAPER_EVALUATOR_CALL_COUNT,
    PAPER_EVALUATOR_MAX_OUTPUT_TOKENS,
    PAPER_EVALUATOR_MODEL,
    PAPER_EVALUATOR_PROTOCOL,
    PAPER_EVALUATOR_TEMPERATURE,
    SUPPORTED_PAPER_SOURCE_PAIR_PROTOCOLS,
    DualStudentAssessment,
    PaperEvaluationCall,
    extract_historical_evaluator_grades,
    validate_assessment_text,
)
from .paper_harness import PAPER_RUNTIME_GATEWAY_BASE_URL
from .runtime import OpenEvoRolloutError, _message_content, _poll_rollout_until_terminal

PAPER_CORE_ROUTE = "dedicated_openevo_rollout_gateway_openrouter_shim_v1"


def run_paper_evaluation_plan(
    *,
    plan_path: Path,
    rollout_base_url: str,
    runtime: dict[str, Any],
    result_root: Path,
    shim_receipt_root: Path,
    historical_runs_root: Path,
    allow_paid: bool,
) -> dict[str, Any]:
    """Run or safely resume a frozen 42-call plan.

    A claimed call without a sealed result is never redispatched.  This is a
    no-duplicate-call boundary, not a score-driven retry loop.
    """
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    _validate_plan_authority(plan)
    if not allow_paid:
        raise PermissionError("paper evaluation requires --allow-paid")
    if os.environ.get("CHEMCROW_PAPER_EVALUATOR_AUTHORIZATION") != PAPER_EVALUATOR_AUTHORIZATION:
        raise PermissionError("paper evaluation authorization literal is absent")
    if os.environ.get("CHEMCROW_PAPER_EVALUATOR_MODEL") != PAPER_EVALUATOR_MODEL:
        raise ValueError("CHEMCROW_PAPER_EVALUATOR_MODEL differs from the frozen model")
    budget = float(os.environ.get("CHEMCROW_PAPER_EVALUATOR_MAX_USD", "0"))
    ceiling = float(plan["cost_ceiling"]["list_price_ceiling_usd_total"])
    if budget < ceiling:
        raise PermissionError(
            f"paper evaluator budget ${budget:.6f} is below frozen ceiling ${ceiling:.6f}"
        )
    result_root.mkdir(parents=True, exist_ok=True)
    _assert_dedicated_core_node(rollout_base_url)

    results: list[dict[str, Any]] = []
    for raw_call in plan["calls"]:
        call = PaperEvaluationCall.model_validate(raw_call)
        result_path = result_root / f"{call.call_id}.result.json"
        claim_path = shim_receipt_root / f"{call.call_id}.claim.json"
        receipt_path = shim_receipt_root / f"{call.call_id}.receipt.json"
        if result_path.exists():
            results.append(_validate_existing_result(result_path, call))
            continue
        if claim_path.exists() or receipt_path.exists():
            raise RuntimeError(
                f"unsealed or ambiguous paper call exists and will not be retried: {call.call_id}"
            )
        core_payload = build_paper_task_request(call=call, runtime=runtime)
        status = _submit_once_and_poll(rollout_base_url, core_payload)
        assessment = _assessment_from_core_status(status)
        if not receipt_path.is_file():
            raise RuntimeError(f"OpenRouter usage receipt is missing: {call.call_id}")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (
            receipt.get("status") != "terminal_success"
            or receipt.get("call_id") != call.call_id
        ):
            raise RuntimeError(f"OpenRouter call did not seal successfully: {call.call_id}")
        result = {
            "schema_version": "chemcrow_paper_evaluator_result_v1",
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
            "reflector_access": False,
            "evolution_feedback_access": False,
            "provisional_llm_judged": True,
        }
        _exclusive_json_write(result_path, result)
        results.append(result)
    aggregate = aggregate_paper_results(
        results,
        historical_grades=extract_historical_evaluator_grades(
            runs_root=historical_runs_root
        ),
    )
    aggregate["plan_sha256"] = file_sha256(plan_path)
    aggregate["result_count"] = len(results)
    aggregate_path = result_root / "aggregate.json"
    if not aggregate_path.exists():
        _exclusive_json_write(aggregate_path, aggregate)
    return aggregate


def build_paper_task_request(
    *, call: PaperEvaluationCall, runtime: dict[str, Any]
) -> dict[str, Any]:
    return {
        "task_id": call.call_id,
        "instruction": call.prompt,
        "num_samples": 1,
        "timeout_seconds": 420.0,
        "runtime": runtime,
        "agent": {
            "harness": None,
            "import_path": "openevo_chemcrow.paper_harness:PaperEvaluatorHarness",
            "model_name": PAPER_EVALUATOR_MODEL,
            "settings": {
                "capture_mode": "transcript",
                "temperature": PAPER_EVALUATOR_TEMPERATURE,
                "max_tokens": PAPER_EVALUATOR_MAX_OUTPUT_TOKENS,
                "runtime_gateway_base_url": PAPER_RUNTIME_GATEWAY_BASE_URL,
            },
            "env": {"PAPER_EVALUATOR_CALL_ID": call.call_id},
            "mcp_servers": [],
            "skills_path": None,
            "custom_shell": None,
        },
        "builder": {"strategy": "agent_transcript", "config": {}},
        "evaluator": None,
        "callback_url": None,
        "metadata": {
            "execution_route": PAPER_CORE_ROUTE,
            "paper_evaluator_protocol": PAPER_EVALUATOR_PROTOCOL,
            "sealed_output_only": True,
            "reflector_access": False,
            "evolution_context_forbidden": True,
            "prompt_sha256": call.prompt_sha256,
        },
        "workspace_handoff": None,
        "runtime_context_binding": None,
    }


def aggregate_paper_results(
    results: list[dict[str, Any]], *, historical_grades: dict[str, Any]
) -> dict[str, Any]:
    if len(results) != PAPER_EVALUATOR_CALL_COUNT:
        raise ValueError("paper aggregate requires exactly 42 sealed results")
    grouped: dict[str, list[float]] = {
        "historical_control": [],
        "baseline": [],
        "evolved": [],
    }
    historical_gpt4: dict[str, list[float]] = {key: [] for key in grouped}
    task_grades: dict[str, dict[str, float]] = {}
    for result in results:
        assessment = DualStudentAssessment.model_validate(result["assessment"])
        comparison = str(result["comparison"])
        grouped[comparison].append(assessment.student_a.grade)
        historical_gpt4[comparison].append(assessment.student_b.grade)
        task_grades.setdefault(str(result["task_id"]), {})[comparison] = (
            assessment.student_a.grade
        )
    wins = losses = ties = 0
    deltas: list[float] = []
    for task_id, grades in task_grades.items():
        if set(grades) != set(grouped):
            raise ValueError(f"paper comparison inventory is incomplete: {task_id}")
        delta = grades["evolved"] - grades["baseline"]
        deltas.append(delta)
        wins += int(delta > 0)
        losses += int(delta < 0)
        ties += int(delta == 0)
    mean = lambda values: sum(values) / len(values)
    historical_chemcrow_mean = mean(
        [historical_grades[task_id].chemcrow_grade for task_id in sorted(task_grades)]
    )
    historical_gpt4_mean = mean(
        [historical_grades[task_id].gpt4_grade for task_id in sorted(task_grades)]
    )
    current_control_chemcrow_mean = mean(grouped["historical_control"])
    current_control_gpt4_mean = mean(historical_gpt4["historical_control"])
    return {
        "schema_version": "chemcrow_paper_evaluator_aggregate_v1",
        "status": "PROVISIONAL_LLM_JUDGED_RESULT",
        "protocol": PAPER_EVALUATOR_PROTOCOL,
        "model": PAPER_EVALUATOR_MODEL,
        "task_count": len(task_grades),
        "mean_historical_chemcrow_grade": current_control_chemcrow_mean,
        "mean_openevo_baseline_grade": mean(grouped["baseline"]),
        "mean_openevo_evolved_grade": mean(grouped["evolved"]),
        "mean_evolved_minus_baseline": mean(deltas),
        "mean_evolved_minus_historical_chemcrow": (
            mean(grouped["evolved"]) - mean(grouped["historical_control"])
        ),
        "evolved_vs_baseline_wins": wins,
        "evolved_vs_baseline_losses": losses,
        "evolved_vs_baseline_ties": ties,
        "historical_gpt4_position_control_means": {
            key: mean(values) for key, values in historical_gpt4.items()
        },
        "paper_notebook_historical_grade_means": {
            "historical_chemcrow": historical_chemcrow_mean,
            "historical_gpt4": historical_gpt4_mean,
        },
        "current_judge_minus_notebook_grade_drift": {
            "historical_chemcrow": current_control_chemcrow_mean
            - historical_chemcrow_mean,
            "historical_gpt4": current_control_gpt4_mean - historical_gpt4_mean,
        },
        "reflector_access": False,
        "final_evaluation_only": True,
    }


def _validate_plan_authority(plan: dict[str, Any]) -> None:
    if (
        plan.get("schema_version") != "chemcrow_paper_evaluator_plan_v1"
        or plan.get("protocol") != PAPER_EVALUATOR_PROTOCOL
        or plan.get("model") != PAPER_EVALUATOR_MODEL
        or plan.get("temperature") != PAPER_EVALUATOR_TEMPERATURE
        or plan.get("call_count") != PAPER_EVALUATOR_CALL_COUNT
        or plan.get("reflector_access") is not False
        or plan.get("sealed_output_only") is not True
        or plan.get("source_pair_protocol") not in SUPPORTED_PAPER_SOURCE_PAIR_PROTOCOLS
        or not isinstance(plan.get("call_id_prefix"), str)
        or any(
            not str(call.get("call_id", "")).startswith(
                f"{plan['call_id_prefix']}-chemcrow-"
            )
            for call in plan.get("calls", [])
        )
    ):
        raise ValueError("paper evaluator plan authority is invalid")


def _assert_dedicated_core_node(base_url: str) -> None:
    with httpx.Client(timeout=10.0, trust_env=False) as client:
        response = client.get(base_url.rstrip("/") + "/nodes")
        response.raise_for_status()
        nodes = response.json()
    if not isinstance(nodes, list) or len(nodes) != 1:
        raise RuntimeError("paper evaluator requires exactly one dedicated Core Gateway node")
    node = nodes[0]
    if (
        not isinstance(node, dict)
        or node.get("node_id") != "chemcrow-paper-gateway-01"
        or node.get("healthy") is not True
    ):
        raise RuntimeError("dedicated paper evaluator Gateway is not healthy")


def _submit_once_and_poll(base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    task_id = str(payload["task_id"])
    with httpx.Client(timeout=30.0, trust_env=False) as client:
        response = client.post(base_url.rstrip("/") + "/rollout/task/submit", json=payload)
        response.raise_for_status()
        status, _ = _poll_rollout_until_terminal(
            client=client,
            url=base_url.rstrip("/") + f"/rollout/task/{task_id}",
            started=started,
            admitted_timeout_seconds=float(payload["timeout_seconds"]),
            poll_seconds=1.0,
        )
    return status


def _assessment_from_core_status(status: dict[str, Any]) -> DualStudentAssessment:
    results = status.get("results")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise OpenEvoRolloutError("paper Core task did not return exactly one result")
    result = results[0]
    if str(result.get("status", "")).upper() != "COMPLETED":
        raise OpenEvoRolloutError("paper Core task was not completed")
    trajectory = result.get("trajectory")
    traces = trajectory.get("traces") if isinstance(trajectory, dict) else None
    if not isinstance(traces, list) or len(traces) != 1 or not isinstance(traces[0], dict):
        raise OpenEvoRolloutError("paper Core transcript trajectory is invalid")
    answers = [
        _message_content(message)
        for message in traces[0].get("response_messages", [])
        if isinstance(message, dict) and message.get("role") == "assistant"
    ]
    if len(answers) != 1:
        raise OpenEvoRolloutError("paper Core transcript does not contain one answer")
    return validate_assessment_text(answers[0])


def _validate_existing_result(path: Path, call: PaperEvaluationCall) -> dict[str, Any]:
    result = json.loads(path.read_text(encoding="utf-8"))
    if (
        result.get("call_id") != call.call_id
        or result.get("prompt_sha256") != call.prompt_sha256
        or result.get("execution_route") != PAPER_CORE_ROUTE
    ):
        raise ValueError(f"existing paper result authority mismatch: {call.call_id}")
    DualStudentAssessment.model_validate(result.get("assessment"))
    return result


def _exclusive_json_write(path: Path, payload: dict[str, Any]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
