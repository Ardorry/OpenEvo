"""Dedicated OpenEvo Core port for sealed ChemCrow paper evaluation."""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any

import httpx

from .hashing import canonical_sha256, file_sha256
from .paper_evaluator import (
    FROZEN_PAPER_TASK_IDS,
    PAPER_EVALUATOR_AUTHORIZATION,
    PAPER_EVALUATOR_CALL_COUNT,
    PAPER_EVALUATOR_DATA_COLLECTION,
    PAPER_EVALUATOR_MAX_OUTPUT_TOKENS,
    PAPER_EVALUATOR_MODEL,
    PAPER_EVALUATOR_PROTOCOL,
    PAPER_EVALUATOR_PROVIDER,
    PAPER_EVALUATOR_TEMPERATURE,
    DualStudentAssessment,
    PaperEvaluationCall,
    PaperEvaluationResult,
    extract_historical_evaluator_grades,
    validate_assessment_text,
    validate_paper_evaluation_plan,
    validate_paper_plan_source_audit,
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
    source_run_root: Path,
    completed_run_audit: Path,
    allow_paid: bool,
    replacement_authority_path: Path | list[Path] | None = None,
    core_completion_root: Path | None = None,
) -> dict[str, Any]:
    """Run or safely resume a frozen 42-call plan.

    A claimed call without a sealed result is never redispatched.  This is a
    no-duplicate-call boundary, not a score-driven retry loop.
    """
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    calls = validate_paper_evaluation_plan(plan)
    validate_paper_plan_source_audit(
        plan=plan,
        run_root=source_run_root,
        completed_run_audit=completed_run_audit,
    )
    if not allow_paid:
        raise PermissionError("paper evaluation requires --allow-paid")
    if os.environ.get("CHEMCROW_PAPER_EVALUATOR_AUTHORIZATION") != PAPER_EVALUATOR_AUTHORIZATION:
        raise PermissionError("paper evaluation authorization literal is absent")
    if os.environ.get("CHEMCROW_PAPER_EVALUATOR_MODEL") != PAPER_EVALUATOR_MODEL:
        raise ValueError("CHEMCROW_PAPER_EVALUATOR_MODEL differs from the frozen model")
    replacement_authority_paths = (
        []
        if replacement_authority_path is None
        else [replacement_authority_path]
        if isinstance(replacement_authority_path, Path)
        else list(replacement_authority_path)
    )
    replacement_authorities = []
    if replacement_authority_paths:
        if core_completion_root is None:
            raise ValueError("paper replacement requires the durable Core completion root")
        from .paper_recovery import load_paper_replacement_authority

        replacement_authorities = [
            load_paper_replacement_authority(
                authority_path=authority_path,
                plan_path=plan_path,
                result_root=result_root,
                receipt_root=shim_receipt_root,
                source_run_root=source_run_root,
                completed_run_audit=completed_run_audit,
                core_completion_root=core_completion_root,
            )
            for authority_path in replacement_authority_paths
        ]
    authority_by_original = {
        authority.original_call_id: (authority, authority_path)
        for authority, authority_path in zip(
            replacement_authorities,
            replacement_authority_paths,
            strict=True,
        )
    }
    if len(authority_by_original) != len(replacement_authorities):
        raise ValueError("paper replacement authorities are not unique")
    ordered_authorities = sorted(
        replacement_authorities,
        key=lambda authority: authority.original_plan_ordinal,
    )
    if any(
        authority.prior_replacement_count != index
        for index, authority in enumerate(ordered_authorities)
    ):
        raise ValueError("paper replacement authority sequence differs")
    budget = float(os.environ.get("CHEMCROW_PAPER_EVALUATOR_MAX_USD", "0"))
    ceiling = float(plan["cost_ceiling"]["list_price_ceiling_usd_total"])
    ceiling += len(replacement_authorities) * float(
        plan["cost_ceiling"]["list_price_ceiling_usd_per_call"]
    )
    if budget < ceiling:
        raise PermissionError(
            f"paper evaluator budget ${budget:.6f} is below frozen ceiling ${ceiling:.6f}"
        )
    _validate_production_ledger_layout(
        plan_path=plan_path,
        result_root=result_root,
        shim_receipt_root=shim_receipt_root,
    )
    result_root.mkdir(parents=True, exist_ok=True)
    _validate_ledger_inventory(
        calls=calls,
        result_root=result_root,
        shim_receipt_root=shim_receipt_root,
        extra_call_ids={
            authority.replacement_call_id for authority in replacement_authorities
        },
    )
    _assert_dedicated_core_node(rollout_base_url)

    results: list[dict[str, Any]] = []
    for logical_call in calls:
        validate_paper_plan_source_audit(
            plan=plan,
            run_root=source_run_root,
            completed_run_audit=completed_run_audit,
        )
        call = logical_call
        authority_and_path = authority_by_original.get(logical_call.call_id)
        call_replacement_authority = None
        call_replacement_authority_path = None
        if authority_and_path is not None:
            from .paper_recovery import load_paper_replacement_authority

            call_replacement_authority_path = authority_and_path[1]
            call_replacement_authority = load_paper_replacement_authority(
                authority_path=call_replacement_authority_path,
                plan_path=plan_path,
                result_root=result_root,
                receipt_root=shim_receipt_root,
                source_run_root=source_run_root,
                completed_run_audit=completed_run_audit,
                core_completion_root=core_completion_root,
            )
            call = call_replacement_authority.replacement_call(logical_call)
        result_path = result_root / f"{call.call_id}.result.json"
        claim_path = shim_receipt_root / f"{call.call_id}.claim.json"
        receipt_path = shim_receipt_root / f"{call.call_id}.receipt.json"
        if result_path.exists():
            results.append(
                _validate_existing_result(
                    result_path,
                    call,
                    claim_path=claim_path,
                    receipt_path=receipt_path,
                    replacement_authority=call_replacement_authority,
                    replacement_authority_path=call_replacement_authority_path,
                )
            )
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
        _validate_claim_and_receipt(
            call=call,
            claim_path=claim_path,
            receipt_path=receipt_path,
            replacement_authority=call_replacement_authority,
            replacement_authority_path=call_replacement_authority_path,
        )
        result = PaperEvaluationResult.model_validate(
            {
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
        ).model_dump(mode="json")
        _exclusive_json_write(result_path, result)
        results.append(
            _validate_existing_result(
                result_path,
                call,
                claim_path=claim_path,
                receipt_path=receipt_path,
                replacement_authority=call_replacement_authority,
                replacement_authority_path=call_replacement_authority_path,
            )
        )
    aggregate = aggregate_paper_results(
        results,
        historical_grades=extract_historical_evaluator_grades(runs_root=historical_runs_root),
    )
    aggregate["plan_sha256"] = file_sha256(plan_path)
    aggregate["result_count"] = len(results)
    aggregate.update(
        _audit_production_ledger(
            calls=calls,
            result_root=result_root,
            shim_receipt_root=shim_receipt_root,
            replacement_authorities=replacement_authorities,
            replacement_authority_paths=replacement_authority_paths,
        )
    )
    aggregate_path = result_root / "aggregate.json"
    serialized_aggregate = json.dumps(aggregate, indent=2, sort_keys=True) + "\n"
    if aggregate_path.exists():
        if aggregate_path.read_text(encoding="utf-8") != serialized_aggregate:
            raise ValueError("existing paper aggregate differs from sealed authority")
    else:
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
    validated_results = [PaperEvaluationResult.model_validate(result) for result in results]
    if len({result.call_id for result in validated_results}) != PAPER_EVALUATOR_CALL_COUNT:
        raise ValueError("paper aggregate requires 42 unique result call IDs")
    for result in validated_results:
        assessment = result.assessment
        comparison = result.comparison
        grouped[comparison].append(assessment.student_a.grade)
        historical_gpt4[comparison].append(assessment.student_b.grade)
        task_grades.setdefault(result.task_id, {})[comparison] = assessment.student_a.grade
    if set(task_grades) != set(FROZEN_PAPER_TASK_IDS):
        raise ValueError("paper aggregate task inventory differs")
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

    def mean(values: list[float]) -> float:
        return sum(values) / len(values)

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
            "historical_chemcrow": current_control_chemcrow_mean - historical_chemcrow_mean,
            "historical_gpt4": current_control_gpt4_mean - historical_gpt4_mean,
        },
        "reflector_access": False,
        "final_evaluation_only": True,
    }


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


def _validate_existing_result(
    path: Path,
    call: PaperEvaluationCall,
    *,
    claim_path: Path,
    receipt_path: Path,
    replacement_authority: Any | None = None,
    replacement_authority_path: Path | None = None,
) -> dict[str, Any]:
    _validate_claim_and_receipt(
        call=call,
        claim_path=claim_path,
        receipt_path=receipt_path,
        replacement_authority=replacement_authority,
        replacement_authority_path=replacement_authority_path,
    )
    result = PaperEvaluationResult.model_validate_json(path.read_text(encoding="utf-8"))
    if (
        result.call_id != call.call_id
        or result.task_id != call.task_id
        or result.comparison != call.comparison
        or result.student_a_system != call.student_a_system
        or result.student_b_system != call.student_b_system
        or result.prompt_sha256 != call.prompt_sha256
        or result.openrouter_receipt_sha256 != file_sha256(receipt_path)
        or not _is_sha256(result.core_terminal_payload_sha256)
        or result.execution_route != PAPER_CORE_ROUTE
    ):
        raise ValueError(f"existing paper result authority mismatch: {call.call_id}")
    return result.model_dump(mode="json")


def _validate_production_ledger_layout(
    *, plan_path: Path, result_root: Path, shim_receipt_root: Path
) -> None:
    resolved_plan = plan_path.resolve()
    resolved_results = result_root.resolve()
    resolved_receipts = shim_receipt_root.resolve()
    ledger_root = resolved_plan.parent.parent
    if (
        resolved_plan.parent.name != "private"
        or resolved_plan.name != "plan.json"
        or resolved_results.parent != ledger_root
        or resolved_results.name != "results"
        or resolved_receipts.parent != ledger_root
        or resolved_receipts.name != "openrouter-receipts"
        or resolved_results == resolved_receipts
    ):
        raise ValueError("paper production plan/results/receipts ledger layout differs")


def _validate_ledger_inventory(
    *,
    calls: list[PaperEvaluationCall],
    result_root: Path,
    shim_receipt_root: Path,
    extra_call_ids: set[str] | None = None,
) -> None:
    expected = {call.call_id for call in calls} | set(extra_call_ids or ())
    inventories = (
        (
            result_root,
            "*.result.json",
            lambda path: path.name.removesuffix(".result.json"),
        ),
        (
            shim_receipt_root,
            "*.claim.json",
            lambda path: path.name.removesuffix(".claim.json"),
        ),
        (
            shim_receipt_root,
            "*.receipt.json",
            lambda path: path.name.removesuffix(".receipt.json"),
        ),
    )
    for root, pattern, identifier in inventories:
        observed = {identifier(path) for path in root.glob(pattern)} if root.is_dir() else set()
        if not observed <= expected:
            raise ValueError("paper production ledger contains calls outside the frozen plan")


def _validate_claim_and_receipt(
    *,
    call: PaperEvaluationCall,
    claim_path: Path,
    receipt_path: Path,
    replacement_authority: Any | None = None,
    replacement_authority_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not claim_path.is_file() or not receipt_path.is_file():
        raise ValueError(f"paper claim/receipt is incomplete: {call.call_id}")
    claim = json.loads(claim_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    request_sha256 = claim.get("request_sha256")
    descriptor = receipt.get("upstream_request_descriptor")
    proxy = receipt.get("environment_proxy")
    provider_route = {
        "only": [PAPER_EVALUATOR_PROVIDER],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": PAPER_EVALUATOR_DATA_COLLECTION,
    }
    if (
        claim.get("schema_version") != "chemcrow_paper_openrouter_claim_v1"
        or claim.get("call_id") != call.call_id
        or not _is_sha256(request_sha256)
        or claim.get("prompt_or_response_included") is not False
        or claim.get("ledger_class") != "production"
        or claim.get("production_ledger_included") is not True
        or receipt.get("schema_version") != "chemcrow_paper_openrouter_receipt_v1"
        or receipt.get("status") != "terminal_success"
        or receipt.get("call_id") != call.call_id
        or receipt.get("request_sha256") != request_sha256
        or not _is_sha256(receipt.get("response_sha256"))
        or not isinstance(receipt.get("response_id"), str)
        or not receipt["response_id"]
        or receipt.get("model") != PAPER_EVALUATOR_MODEL
        or str(receipt.get("provider", "")).lower() != PAPER_EVALUATOR_PROVIDER
        or receipt.get("upstream_http_status") != 200
        or receipt.get("temperature") != PAPER_EVALUATOR_TEMPERATURE
        or receipt.get("allow_fallbacks") is not False
        or receipt.get("provider_only") != [PAPER_EVALUATOR_PROVIDER]
        or receipt.get("require_parameters") is not True
        or receipt.get("data_collection") != PAPER_EVALUATOR_DATA_COLLECTION
        or receipt.get("internal_response_format_validated") is not True
        or receipt.get("upstream_response_format_omitted") is not True
        or receipt.get("internal_call_identity_validated") is not True
        or receipt.get("upstream_user_omitted") is not True
        or receipt.get("prompt_or_response_included") is not False
        or receipt.get("credential_included") is not False
        or receipt.get("ledger_class") != "production"
        or receipt.get("production_ledger_included") is not True
        or not isinstance(descriptor, dict)
        or descriptor.get("model") != PAPER_EVALUATOR_MODEL
        or descriptor.get("temperature") != PAPER_EVALUATOR_TEMPERATURE
        or descriptor.get("max_tokens") != PAPER_EVALUATOR_MAX_OUTPUT_TOKENS
        or descriptor.get("stream") is not False
        or descriptor.get("provider") != provider_route
        or descriptor.get("user_present") is not False
        or descriptor.get("response_format_present") is not False
        or descriptor.get("message_count") != 2
        or descriptor.get("message_roles") != ["system", "user"]
        or not isinstance(proxy, dict)
        or proxy.get("mode") != "environment_proxy"
        or proxy.get("credentials_present") is not False
    ):
        raise ValueError(f"paper claim/receipt authority differs: {call.call_id}")
    prompt_tokens = _nonnegative_receipt_number(receipt, "prompt_tokens", integer=True)
    completion_tokens = _nonnegative_receipt_number(receipt, "completion_tokens", integer=True)
    total_tokens = _nonnegative_receipt_number(receipt, "total_tokens", integer=True)
    if total_tokens != prompt_tokens + completion_tokens:
        raise ValueError(f"paper receipt token total differs: {call.call_id}")
    _nonnegative_receipt_number(receipt, "list_price_cost_usd")
    _nonnegative_receipt_number(receipt, "openrouter_reported_cost_usd")
    if replacement_authority is not None:
        if replacement_authority_path is None:
            raise ValueError("paper replacement authority path is absent")
        from .paper_recovery import validate_replacement_claim_metadata

        validate_replacement_claim_metadata(
            payload=claim,
            authority=replacement_authority,
            authority_path=replacement_authority_path,
        )
        validate_replacement_claim_metadata(
            payload=receipt,
            authority=replacement_authority,
            authority_path=replacement_authority_path,
        )
    return claim, receipt


def _audit_production_ledger(
    *,
    calls: list[PaperEvaluationCall],
    result_root: Path,
    shim_receipt_root: Path,
    replacement_authority: Any | None = None,
    replacement_authority_path: Path | None = None,
    replacement_authorities: list[Any] | None = None,
    replacement_authority_paths: list[Path] | None = None,
) -> dict[str, Any]:
    authorities = list(replacement_authorities or [])
    authority_paths = list(replacement_authority_paths or [])
    if replacement_authority is not None:
        authorities.append(replacement_authority)
        if replacement_authority_path is None:
            raise ValueError("paper replacement authority path is absent")
        authority_paths.append(replacement_authority_path)
    if len(authorities) != len(authority_paths):
        raise ValueError("paper replacement authority path inventory differs")
    authority_by_original = {
        authority.original_call_id: (authority, path)
        for authority, path in zip(authorities, authority_paths, strict=True)
    }
    if len(authority_by_original) != len(authorities):
        raise ValueError("paper replacement authorities are not unique")
    extra_call_ids = {authority.replacement_call_id for authority in authorities}
    _validate_ledger_inventory(
        calls=calls,
        result_root=result_root,
        shim_receipt_root=shim_receipt_root,
        extra_call_ids=extra_call_ids,
    )
    reported_costs: list[float] = []
    list_costs: list[float] = []
    response_ids: set[str] = set()
    valid_call_ids: set[str] = set()
    for logical_call in calls:
        call = logical_call
        authority_and_path = authority_by_original.get(logical_call.call_id)
        call_replacement_authority = None
        call_replacement_authority_path = None
        if authority_and_path is not None:
            call_replacement_authority, call_replacement_authority_path = authority_and_path
            call = call_replacement_authority.replacement_call(logical_call)
        valid_call_ids.add(call.call_id)
        result_path = result_root / f"{call.call_id}.result.json"
        claim_path = shim_receipt_root / f"{call.call_id}.claim.json"
        receipt_path = shim_receipt_root / f"{call.call_id}.receipt.json"
        _validate_existing_result(
            result_path,
            call,
            claim_path=claim_path,
            receipt_path=receipt_path,
            replacement_authority=call_replacement_authority,
            replacement_authority_path=call_replacement_authority_path,
        )
        _, receipt = _validate_claim_and_receipt(
            call=call,
            claim_path=claim_path,
            receipt_path=receipt_path,
            replacement_authority=call_replacement_authority,
            replacement_authority_path=call_replacement_authority_path,
        )
        response_ids.add(str(receipt["response_id"]))
        reported_costs.append(float(receipt["openrouter_reported_cost_usd"]))
        list_costs.append(float(receipt["list_price_cost_usd"]))
    excluded_infrastructure_attempt_count = 0
    excluded_invalid_assessment_count = 0
    excluded_pre_response_count = 0
    excluded_response_count = 0
    from .paper_recovery import (
        _read_regular_json_no_follow,
        _validate_pre_response_402_claim_and_receipt,
    )

    for authority in authorities:
        original = next(call for call in calls if call.call_id == authority.original_call_id)
        claim_path = shim_receipt_root / f"{original.call_id}.claim.json"
        receipt_path = shim_receipt_root / f"{original.call_id}.receipt.json"
        if authority.failure_class == "upstream_http_402_no_response":
            claim, _ = _read_regular_json_no_follow(
                claim_path,
                label="paper excluded HTTP 402 claim",
            )
            excluded_receipt, _ = _read_regular_json_no_follow(
                receipt_path,
                label="paper excluded HTTP 402 receipt",
            )
            _validate_pre_response_402_claim_and_receipt(
                original=original,
                claim=claim,
                receipt=excluded_receipt,
            )
            excluded_infrastructure_attempt_count += 1
            excluded_pre_response_count += 1
            continue
        _, excluded_receipt = _validate_claim_and_receipt(
            call=original,
            claim_path=claim_path,
            receipt_path=receipt_path,
        )
        response_ids.add(str(excluded_receipt["response_id"]))
        reported_costs.append(float(excluded_receipt["openrouter_reported_cost_usd"]))
        list_costs.append(float(excluded_receipt["list_price_cost_usd"]))
        excluded_response_count += 1
        if authority.failure_class == "invalid_structured_assessment":
            excluded_invalid_assessment_count += 1
        else:
            excluded_infrastructure_attempt_count += 1
    excluded_attempt_count = len(authorities)
    actual_attempt_count = PAPER_EVALUATOR_CALL_COUNT + excluded_attempt_count
    expected_response_count = PAPER_EVALUATOR_CALL_COUNT + excluded_response_count
    if len(response_ids) != expected_response_count:
        raise ValueError("paper provider response IDs are not unique")
    return {
        "production_ledger_class": "production",
        "claim_count": actual_attempt_count,
        "receipt_count": actual_attempt_count,
        "actual_upstream_call_count": actual_attempt_count,
        "valid_target_result_count": PAPER_EVALUATOR_CALL_COUNT,
        "excluded_attempt_count": excluded_attempt_count,
        "explicit_replacement_count": excluded_attempt_count,
        "unique_claim_call_id_count": actual_attempt_count,
        "unique_receipt_call_id_count": actual_attempt_count,
        "unique_result_call_id_count": len(valid_call_ids),
        "unique_provider_response_id_count": len(response_ids),
        "provider_response_count": expected_response_count,
        "json_valid_count": len(calls),
        "pydantic_valid_count": len(calls),
        "openai_provider_count": expected_response_count,
        "valid_result_openai_provider_count": PAPER_EVALUATOR_CALL_COUNT,
        "fallback_false_count": actual_attempt_count,
        "environment_proxy_count": actual_attempt_count,
        "excluded_invalid_assessment_count": excluded_invalid_assessment_count,
        "excluded_pre_response_attempt_count": excluded_pre_response_count,
        "excluded_infrastructure_attempt_count": excluded_infrastructure_attempt_count,
        "actual_openrouter_reported_cost_usd": round(sum(reported_costs), 8),
        "list_price_cost_usd": round(sum(list_costs), 8),
        "automatic_provider_retries": False,
        "failed_call_id_reuse": False,
        "score_driven_retry": False,
    }


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _nonnegative_receipt_number(
    receipt: dict[str, Any], key: str, *, integer: bool = False
) -> float | int:
    value = receipt.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"paper receipt {key} is not numeric")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0 or (integer and not parsed.is_integer()):
        raise ValueError(f"paper receipt {key} is invalid")
    return int(parsed) if integer else parsed


def _exclusive_json_write(path: Path, payload: dict[str, Any]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
