"""One-shot direct comparison of historical ChemCrow answers against full-v5 G1/G2.

This is a project-added analysis.  It deliberately uses its own plan schema,
authorization literal, call namespace, receipts, results, and aggregate.  It
does not alter or extend the frozen 42-call paper-evaluator production ledger.
"""

from __future__ import annotations

import argparse
import json
import math
import os
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
    HistoricalAnswers,
    assert_sealed_run_ready,
    estimate_chat_input_tokens,
    extract_historical_answers,
    render_compatible_prompt,
)
from .three_artifact_models import ThreeArtifactPairResult, three_artifact_protocol_label

DIRECT_COMPARISON_SCHEMA = "chemcrow_paper_direct_comparison_plan_v1"
DIRECT_COMPARISON_RESULT_SCHEMA = "chemcrow_paper_direct_comparison_result_v1"
DIRECT_COMPARISON_AGGREGATE_SCHEMA = "chemcrow_paper_direct_comparison_aggregate_v1"
DIRECT_COMPARISON_PROTOCOL = (
    "historical_chemcrow_vs_full_v5_g2_direct_calibrated_paper_judge_v1"
)
DIRECT_COMPARISON = "g2_vs_historical_chemcrow"
DIRECT_G1_COMPARISON_SCHEMA = "chemcrow_paper_direct_g1_comparison_plan_v1"
DIRECT_G1_COMPARISON_RESULT_SCHEMA = (
    "chemcrow_paper_direct_g1_comparison_result_v1"
)
DIRECT_G1_COMPARISON_AGGREGATE_SCHEMA = (
    "chemcrow_paper_direct_g1_comparison_aggregate_v1"
)
DIRECT_G1_COMPARISON_PROTOCOL = (
    "historical_chemcrow_vs_full_v5_g1_direct_calibrated_paper_judge_v1"
)
DIRECT_G1_COMPARISON = "g1_vs_historical_chemcrow"
DIRECT_CALL_COUNT = len(FROZEN_PAPER_TASK_IDS)
DIRECT_CALL_ID_PREFIX = "paper-direct-v1"
DIRECT_G1_CALL_ID_PREFIX = "paper-direct-g1-v1"
DIRECT_AUTHORIZATION = "I_AUTHORIZE_14_SEALED_DIRECT_PAPER_COMPARISONS"
DIRECT_G1_AUTHORIZATION = (
    "I_AUTHORIZE_14_SEALED_G1_VS_CHEMCROW_DIRECT_PAPER_COMPARISONS"
)
DIRECT_MAX_AUTHORIZED_USD = 5.0
DIRECT_BOOTSTRAP_SEED = 20260827
DIRECT_BOOTSTRAP_REPLICATES = 20_000
DIRECT_TARGET_G1 = "openevo_baseline"
DIRECT_TARGET_G2 = "openevo_evolved"
_DIRECT_G2_LEDGER_ROOT = "paper-evaluator-direct-v5-g2-vs-historical-chemcrow-v1"
_DIRECT_G1_LEDGER_ROOT = "paper-evaluator-direct-v5-g1-vs-historical-chemcrow-v1"


class PaperDirectComparisonCall(BaseModel):
    """One frozen historical-ChemCrow versus v5-G1/G2 judge request."""

    model_config = ConfigDict(extra="forbid")

    call_id: str
    task_id: str
    comparison: Literal[
        "g2_vs_historical_chemcrow", "g1_vs_historical_chemcrow"
    ]
    student_a_system: Literal["historical_chemcrow"]
    student_b_system: Literal["openevo_evolved", "openevo_baseline"]
    prompt: str
    prompt_sha256: str
    source_pair_id: str
    source_pair_result_sha256: str
    historical_source_sha256: str
    historical_answer_sha256: str
    evolved_output_id: str | None = None
    evolved_answer_sha256: str | None = None
    baseline_output_id: str | None = None
    baseline_answer_sha256: str | None = None
    estimated_input_tokens: int
    metric_classification: Literal["project_added_direct_comparison"]
    paper_comparable: Literal[False]

    @model_validator(mode="after")
    def _bind_prompt_and_sources(self) -> PaperDirectComparisonCall:
        if self.prompt_sha256 != canonical_sha256(self.prompt):
            raise ValueError("direct-comparison prompt hash differs from prompt bytes")
        if not self.source_pair_id.endswith(f"--{self.task_id}"):
            raise ValueError("direct-comparison pair is not bound to the task")
        if self.estimated_input_tokens <= 0:
            raise ValueError("direct-comparison token estimate must be positive")
        expected = (
            (
                self.comparison == DIRECT_COMPARISON
                and self.evolved_output_id is not None
                and self.evolved_answer_sha256 is not None
                and self.baseline_output_id is None
                and self.baseline_answer_sha256 is None
            )
            if self.student_b_system == DIRECT_TARGET_G2
            else (
                self.comparison == DIRECT_G1_COMPARISON
                and self.baseline_output_id is not None
                and self.baseline_answer_sha256 is not None
                and self.evolved_output_id is None
                and self.evolved_answer_sha256 is None
            )
        )
        if not expected:
            raise ValueError("direct-comparison target fields are inconsistent")
        return self


def _direct_variant(student_b_system: str) -> dict[str, str]:
    if student_b_system == DIRECT_TARGET_G2:
        return {
            "schema_version": DIRECT_COMPARISON_SCHEMA,
            "result_schema": DIRECT_COMPARISON_RESULT_SCHEMA,
            "aggregate_schema": DIRECT_COMPARISON_AGGREGATE_SCHEMA,
            "protocol": DIRECT_COMPARISON_PROTOCOL,
            "comparison": DIRECT_COMPARISON,
            "call_id_prefix": DIRECT_CALL_ID_PREFIX,
            "authorization_env": "CHEMCROW_PAPER_DIRECT_COMPARISON_AUTHORIZATION",
            "authorization": DIRECT_AUTHORIZATION,
            "budget_env": "CHEMCROW_PAPER_DIRECT_COMPARISON_MAX_USD",
            "ledger_root": _DIRECT_G2_LEDGER_ROOT,
            "target_label": "v5_g2",
            "display_label": "full-v5 G2",
        }
    if student_b_system == DIRECT_TARGET_G1:
        return {
            "schema_version": DIRECT_G1_COMPARISON_SCHEMA,
            "result_schema": DIRECT_G1_COMPARISON_RESULT_SCHEMA,
            "aggregate_schema": DIRECT_G1_COMPARISON_AGGREGATE_SCHEMA,
            "protocol": DIRECT_G1_COMPARISON_PROTOCOL,
            "comparison": DIRECT_G1_COMPARISON,
            "call_id_prefix": DIRECT_G1_CALL_ID_PREFIX,
            "authorization_env": "CHEMCROW_PAPER_DIRECT_G1_COMPARISON_AUTHORIZATION",
            "authorization": DIRECT_G1_AUTHORIZATION,
            "budget_env": "CHEMCROW_PAPER_DIRECT_G1_COMPARISON_MAX_USD",
            "ledger_root": _DIRECT_G1_LEDGER_ROOT,
            "target_label": "v5_g1",
            "display_label": "full-v5 G1",
        }
    raise ValueError(f"unsupported direct-comparison target: {student_b_system}")


def direct_plan_target_system(plan: dict[str, Any]) -> str:
    """Resolve the frozen G1/G2 target from a direct-comparison plan."""
    for target in (DIRECT_TARGET_G2, DIRECT_TARGET_G1):
        variant = _direct_variant(target)
        if (
            plan.get("schema_version") == variant["schema_version"]
            and plan.get("protocol") == variant["protocol"]
            and plan.get("student_b_system") == target
        ):
            return target
    raise ValueError("direct-comparison plan variant is invalid")


def direct_plan_runtime_authority(plan: dict[str, Any]) -> dict[str, str]:
    """Return the closed authorization and ledger namespace for a valid plan."""
    return _direct_variant(direct_plan_target_system(plan))


def direct_cost_ceiling() -> dict[str, Any]:
    """Return the frozen worst-case list-price ceiling for exactly 14 calls."""
    max_input_tokens = PAPER_EVALUATOR_CONTEXT_TOKENS - PAPER_EVALUATOR_MAX_OUTPUT_TOKENS
    per_call = (
        max_input_tokens * PAPER_EVALUATOR_INPUT_USD_PER_TOKEN
        + PAPER_EVALUATOR_MAX_OUTPUT_TOKENS * PAPER_EVALUATOR_OUTPUT_USD_PER_TOKEN
    )
    return {
        "model_context_tokens": PAPER_EVALUATOR_CONTEXT_TOKENS,
        "max_output_tokens_per_call": PAPER_EVALUATOR_MAX_OUTPUT_TOKENS,
        "max_input_tokens_per_call": max_input_tokens,
        "call_count": DIRECT_CALL_COUNT,
        "list_price_ceiling_usd_per_call": round(per_call, 8),
        "list_price_ceiling_usd_total": round(per_call * DIRECT_CALL_COUNT, 8),
        "authorized_max_usd": DIRECT_MAX_AUTHORIZED_USD,
    }


def build_direct_comparison_plan(
    *,
    tasks: list[TaskItem],
    pairs: dict[str, ThreeArtifactPairResult],
    historical: dict[str, HistoricalAnswers],
    expected_source_pair_protocol: str,
    reference_42_call_aggregate: Path,
    expected_reference_42_call_aggregate_sha256: str,
    student_b_system: Literal["openevo_evolved", "openevo_baseline"] = (
        DIRECT_TARGET_G2
    ),
) -> dict[str, Any]:
    """Freeze 14 direct comparisons before any paid request is dispatched."""
    if [task.task_id for task in tasks] != list(FROZEN_PAPER_TASK_IDS):
        raise ValueError("direct comparison requires the frozen 14-task order")
    if file_sha256(reference_42_call_aggregate) != expected_reference_42_call_aggregate_sha256:
        raise ValueError("reference 42-call aggregate differs before direct comparison")
    variant = _direct_variant(student_b_system)
    calls: list[PaperDirectComparisonCall] = []
    for task in tasks:
        pair = pairs[task.task_id]
        source_protocol = three_artifact_protocol_label(pair.artifact_bundle.protocol)
        if source_protocol != expected_source_pair_protocol:
            raise ValueError(f"direct-comparison source protocol differs: {task.task_id}")
        old = historical[task.task_id]
        target = pair.evolved if student_b_system == DIRECT_TARGET_G2 else pair.baseline
        prompt = render_compatible_prompt(
            task_prompt=task.prompt,
            student_a=old.chemcrow_answer,
            student_b=target.answer,
        )
        call_id = f"{variant['call_id_prefix']}-{task.task_id}-direct"
        estimated = estimate_chat_input_tokens(prompt)
        if estimated > direct_cost_ceiling()["max_input_tokens_per_call"]:
            raise ValueError(f"direct-comparison prompt exceeds input budget: {call_id}")
        target_fields = (
            {
                "evolved_output_id": target.run_id,
                "evolved_answer_sha256": canonical_sha256(target.answer),
            }
            if student_b_system == DIRECT_TARGET_G2
            else {
                "baseline_output_id": target.run_id,
                "baseline_answer_sha256": canonical_sha256(target.answer),
            }
        )
        calls.append(
            PaperDirectComparisonCall(
                call_id=call_id,
                task_id=task.task_id,
                comparison=variant["comparison"],
                student_a_system="historical_chemcrow",
                student_b_system=student_b_system,
                prompt=prompt,
                prompt_sha256=canonical_sha256(prompt),
                source_pair_id=pair.pair_id,
                source_pair_result_sha256=canonical_sha256(pair.model_dump(mode="json")),
                historical_source_sha256=old.notebook_sha256,
                historical_answer_sha256=canonical_sha256(old.chemcrow_answer),
                estimated_input_tokens=estimated,
                metric_classification="project_added_direct_comparison",
                paper_comparable=False,
                **target_fields,
            )
        )
    if len(calls) != DIRECT_CALL_COUNT or len({call.call_id for call in calls}) != len(calls):
        raise AssertionError("direct comparison requires 14 unique calls")
    return {
        "schema_version": variant["schema_version"],
        "protocol": variant["protocol"],
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
        "student_a_system": "historical_chemcrow",
        "student_b_system": student_b_system,
        "answer_order_frozen": True,
        "reflector_access": False,
        "evolution_feedback_access": False,
        "sealed_output_only": True,
        "paper_comparable": False,
        "project_added_metric": True,
        "source_pair_protocol": expected_source_pair_protocol,
        "call_id_prefix": variant["call_id_prefix"],
        "call_count": len(calls),
        "cost_ceiling": direct_cost_ceiling(),
        "reference_42_call_aggregate_sha256": expected_reference_42_call_aggregate_sha256,
        "calls": [call.model_dump(mode="json", exclude_none=True) for call in calls],
    }


def validate_direct_comparison_plan(plan: dict[str, Any]) -> list[PaperDirectComparisonCall]:
    """Validate the complete direct plan and return its closed call inventory."""
    student_b_system = direct_plan_target_system(plan)
    variant = _direct_variant(student_b_system)
    if (
        plan.get("schema_version") != variant["schema_version"]
        or plan.get("protocol") != variant["protocol"]
        or plan.get("judge_protocol") != PAPER_EVALUATOR_PROTOCOL
        or plan.get("prompt_candidate_sha256") != PAPER_EVALUATOR_PROMPT_SHA256
        or plan.get("model") != PAPER_EVALUATOR_MODEL
        or plan.get("temperature") != PAPER_EVALUATOR_TEMPERATURE
        or plan.get("provider_only") != [PAPER_EVALUATOR_PROVIDER]
        or plan.get("allow_fallbacks") is not False
        or plan.get("call_count") != DIRECT_CALL_COUNT
        or plan.get("call_id_prefix") != variant["call_id_prefix"]
        or plan.get("student_a_system") != "historical_chemcrow"
        or plan.get("student_b_system") != student_b_system
        or plan.get("answer_order_frozen") is not True
        or plan.get("paper_comparable") is not False
        or plan.get("project_added_metric") is not True
    ):
        raise ValueError("direct-comparison plan authority is invalid")
    calls = [PaperDirectComparisonCall.model_validate(raw) for raw in plan.get("calls", [])]
    if (
        len(calls) != DIRECT_CALL_COUNT
        or tuple(call.task_id for call in calls) != FROZEN_PAPER_TASK_IDS
        or len({call.call_id for call in calls}) != DIRECT_CALL_COUNT
        or any(
            call.call_id != f"{variant['call_id_prefix']}-{call.task_id}-direct"
            for call in calls
        )
        or any(call.student_b_system != student_b_system for call in calls)
        or any(call.comparison != variant["comparison"] for call in calls)
    ):
        raise ValueError("direct-comparison call inventory is invalid")
    return calls


def run_direct_comparison_plan(
    *,
    plan_path: Path,
    rollout_base_url: str,
    runtime: dict[str, Any],
    result_root: Path,
    shim_receipt_root: Path,
    reference_42_call_aggregate: Path,
    allow_paid: bool,
) -> dict[str, Any]:
    """Execute each frozen direct comparison at most once through OpenEvo Core."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    calls = validate_direct_comparison_plan(plan)
    runtime_authority = direct_plan_runtime_authority(plan)
    if not allow_paid:
        raise PermissionError("direct paper comparison requires --allow-paid")
    if (
        os.environ.get(runtime_authority["authorization_env"])
        != runtime_authority["authorization"]
    ):
        raise PermissionError("direct-comparison authorization literal is absent")
    if os.environ.get("CHEMCROW_PAPER_EVALUATOR_MODEL") != PAPER_EVALUATOR_MODEL:
        raise ValueError("direct-comparison model differs from frozen openai/gpt-4")
    budget = float(os.environ.get(runtime_authority["budget_env"], "0"))
    ceiling = float(plan["cost_ceiling"]["list_price_ceiling_usd_total"])
    if budget < ceiling or budget > DIRECT_MAX_AUTHORIZED_USD:
        raise PermissionError("direct-comparison budget is outside the frozen authorized range")
    if file_sha256(reference_42_call_aggregate) != plan[
        "reference_42_call_aggregate_sha256"
    ]:
        raise ValueError("reference 42-call aggregate changed before direct execution")
    result_root.mkdir(parents=True, exist_ok=True)
    _assert_dedicated_core_node(rollout_base_url)
    results: list[dict[str, Any]] = []
    for call in calls:
        result_path = result_root / f"{call.call_id}.result.json"
        claim_path = shim_receipt_root / f"{call.call_id}.claim.json"
        receipt_path = shim_receipt_root / f"{call.call_id}.receipt.json"
        if result_path.exists():
            results.append(_validate_existing_direct_result(result_path, call))
            continue
        if claim_path.exists() or receipt_path.exists():
            raise RuntimeError(
                f"unsealed or ambiguous direct call will not be retried: {call.call_id}"
            )
        status = _submit_once_and_poll(
            rollout_base_url,
            build_paper_task_request(call=call, runtime=runtime),  # type: ignore[arg-type]
        )
        assessment = _assessment_from_core_status(status)
        if not receipt_path.is_file():
            raise RuntimeError(f"direct-comparison usage receipt is missing: {call.call_id}")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (
            receipt.get("status") != "terminal_success"
            or receipt.get("call_id") != call.call_id
            or receipt.get("ledger_class") != "direct_comparison"
            or receipt.get("production_ledger_included") is not False
        ):
            raise RuntimeError(f"direct-comparison receipt is invalid: {call.call_id}")
        result = {
            "schema_version": runtime_authority["result_schema"],
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
            "project_added_metric": True,
        }
        _exclusive_json_write(result_path, result)
        results.append(result)
    aggregate = aggregate_direct_comparison_results(
        results=results,
        receipt_root=shim_receipt_root,
        plan_sha256=file_sha256(plan_path),
        reference_42_call_aggregate=reference_42_call_aggregate,
        expected_reference_sha256=plan["reference_42_call_aggregate_sha256"],
        student_b_system=direct_plan_target_system(plan),
        protocol=str(plan["protocol"]),
        aggregate_schema=runtime_authority["aggregate_schema"],
    )
    aggregate_path = result_root / "aggregate.json"
    serialized = json.dumps(aggregate, indent=2, sort_keys=True) + "\n"
    if aggregate_path.exists():
        if aggregate_path.read_text(encoding="utf-8") != serialized:
            raise ValueError("existing direct aggregate differs from sealed results")
    else:
        _exclusive_json_write(aggregate_path, aggregate)
    return aggregate


def aggregate_direct_comparison_results(
    *,
    results: list[dict[str, Any]],
    receipt_root: Path,
    plan_sha256: str,
    reference_42_call_aggregate: Path,
    expected_reference_sha256: str,
    student_b_system: Literal["openevo_evolved", "openevo_baseline"] = (
        DIRECT_TARGET_G2
    ),
    protocol: str = DIRECT_COMPARISON_PROTOCOL,
    aggregate_schema: str = DIRECT_COMPARISON_AGGREGATE_SCHEMA,
) -> dict[str, Any]:
    """Aggregate same-call historical-ChemCrow versus v5-G1/G2 grades."""
    if len(results) != DIRECT_CALL_COUNT:
        raise ValueError("direct comparison requires exactly 14 sealed results")
    variant = _direct_variant(student_b_system)
    if protocol != variant["protocol"] or aggregate_schema != variant["aggregate_schema"]:
        raise ValueError("direct-comparison aggregate variant is inconsistent")
    target_label = variant["target_label"]
    grade_key = f"{target_label}_grade"
    delta_key = f"{target_label}_minus_historical_chemcrow"
    wins_key = f"{target_label}_wins"
    per_task: list[dict[str, Any]] = []
    costs: list[float] = []
    prompt_tokens = completion_tokens = 0
    for result in sorted(results, key=lambda item: str(item["task_id"])):
        assessment = DualStudentAssessment.model_validate(result["assessment"])
        historical_grade = assessment.student_a.grade
        target_grade = assessment.student_b.grade
        delta = target_grade - historical_grade
        call_id = str(result["call_id"])
        receipt_path = receipt_root / f"{call_id}.receipt.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (
            receipt.get("status") != "terminal_success"
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
            or receipt.get("ledger_class") != "direct_comparison"
            or receipt.get("production_ledger_included") is not False
            or receipt.get("prompt_or_response_included") is not False
            or receipt.get("credential_included") is not False
        ):
            raise ValueError(f"invalid direct-comparison receipt: {call_id}")
        cost = receipt.get("openrouter_reported_cost_usd")
        if not isinstance(cost, int | float):
            raise TypeError(f"direct-comparison receipt has no proven cost: {call_id}")
        costs.append(float(cost))
        prompt_tokens += int(receipt["prompt_tokens"])
        completion_tokens += int(receipt["completion_tokens"])
        per_task.append(
            {
                "task_id": result["task_id"],
                "call_id": call_id,
                "historical_chemcrow_grade": historical_grade,
                grade_key: target_grade,
                delta_key: delta,
                "winner": (
                    target_label
                    if delta > 0
                    else "historical_chemcrow"
                    if delta < 0
                    else "tie"
                ),
                "assessment_sha256": result["assessment_sha256"],
                "receipt_sha256": file_sha256(receipt_path),
            }
        )
    deltas = [float(row[delta_key]) for row in per_task]
    historical = [float(row["historical_chemcrow_grade"]) for row in per_task]
    target_grades = [float(row[grade_key]) for row in per_task]
    ci = _bootstrap_mean_ci(deltas)
    return {
        "schema_version": aggregate_schema,
        "status": "PROVISIONAL_LLM_JUDGED_DIRECT_COMPARISON_COMPLETE",
        "protocol": protocol,
        "judge_protocol": PAPER_EVALUATOR_PROTOCOL,
        "model": PAPER_EVALUATOR_MODEL,
        "temperature": PAPER_EVALUATOR_TEMPERATURE,
        "provider": "OpenAI",
        "allow_fallbacks": False,
        "paper_comparable": False,
        "project_added_metric": True,
        "student_a_system": "historical_chemcrow",
        "student_b_system": student_b_system,
        "call_count": DIRECT_CALL_COUNT,
        "mean_historical_chemcrow_grade": statistics.mean(historical),
        f"mean_{grade_key}": statistics.mean(target_grades),
        f"mean_{delta_key}": statistics.mean(deltas),
        f"median_{delta_key}": statistics.median(deltas),
        wins_key: sum(delta > 0 for delta in deltas),
        "ties": sum(delta == 0 for delta in deltas),
        "historical_chemcrow_wins": sum(delta < 0 for delta in deltas),
        "paired_bootstrap_95_ci_mean_delta": ci,
        "bootstrap_seed": DIRECT_BOOTSTRAP_SEED,
        "bootstrap_replicates": DIRECT_BOOTSTRAP_REPLICATES,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "actual_openrouter_reported_cost_usd": round(sum(costs), 8),
        "plan_sha256": plan_sha256,
        "reference_42_call_aggregate_sha256_before": expected_reference_sha256,
        "reference_42_call_aggregate_sha256_after": file_sha256(
            reference_42_call_aggregate
        ),
        "reference_42_call_ledger_unchanged": (
            file_sha256(reference_42_call_aggregate) == expected_reference_sha256
        ),
        "per_task": per_task,
        "limitations": (
            "One GPT-4 call per task with fixed answer order (historical ChemCrow as "
            f"Student A, {variant['display_label']} as Student B); N=14, discrete "
            "grades, stochastic judge, "
            "and no answer-order reversal replicate."
        ),
    }


def audit_direct_comparison(
    *, config_path: Path, output_json: Path, output_markdown: Path
) -> dict[str, Any]:
    """Validate sealed calls/claims/receipts/results and publish a concise report."""
    config = _load_config(config_path)
    plan_path = _path(config_path, config["plan_path"])
    result_root = _path(config_path, config["result_root"])
    receipt_root = _path(config_path, config["shim_receipt_root"])
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    calls = validate_direct_comparison_plan(plan)
    student_b_system = direct_plan_target_system(plan)
    variant = _direct_variant(student_b_system)
    results = [
        _validate_existing_direct_result(
            result_root / f"{call.call_id}.result.json", call
        )
        for call in calls
    ]
    aggregate = aggregate_direct_comparison_results(
        results=results,
        receipt_root=receipt_root,
        plan_sha256=file_sha256(plan_path),
        reference_42_call_aggregate=_path(
            config_path, config["reference_42_call_aggregate"]
        ),
        expected_reference_sha256=str(config["reference_42_call_aggregate_sha256"]),
        student_b_system=student_b_system,
        protocol=str(plan["protocol"]),
        aggregate_schema=variant["aggregate_schema"],
    )
    claims = sorted(receipt_root.glob("*.claim.json"))
    receipts = sorted(receipt_root.glob("*.receipt.json"))
    if len(claims) != DIRECT_CALL_COUNT or len(receipts) != DIRECT_CALL_COUNT:
        raise ValueError("direct-comparison claim/receipt inventory is incomplete")
    expected_call_ids = {call.call_id for call in calls}
    observed_claim_ids: set[str] = set()
    for path in claims:
        claim = json.loads(path.read_text(encoding="utf-8"))
        call_id = claim.get("call_id")
        if (
            call_id not in expected_call_ids
            or claim.get("ledger_class") != "direct_comparison"
            or claim.get("production_ledger_included") is not False
            or claim.get("prompt_or_response_included") is not False
        ):
            raise ValueError(f"invalid direct-comparison claim: {path.name}")
        observed_claim_ids.add(str(call_id))
    if observed_claim_ids != expected_call_ids:
        raise ValueError("direct-comparison claim IDs differ from the frozen plan")
    wins = int(aggregate[f"{variant['target_label']}_wins"])
    losses = int(aggregate["historical_chemcrow_wins"])
    audit = {
        **aggregate,
        "status": "PASS",
        "claim_count": len(claims),
        "receipt_count": len(receipts),
        "result_count": len(results),
        "json_valid_count": len(results),
        "pydantic_valid_count": len(results),
        "http_200_count": len(results),
        "openai_provider_count": len(results),
        "no_fallback_count": len(results),
        "exact_sign_test_two_sided_p": _exact_sign_test(wins=wins, losses=losses),
        "credential_persisted_count": 0,
        "prompt_or_response_persisted_in_receipt_count": 0,
    }
    output_json.write_text(json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output_markdown.write_text(_render_markdown(audit), encoding="utf-8")
    return audit


def preflight_direct_comparison(*, config_path: Path, output: Path) -> dict[str, Any]:
    """Build and seal the zero-paid direct-comparison plan."""
    config = _load_config(config_path)
    student_b_system = str(config.get("student_b_system", DIRECT_TARGET_G2))
    if student_b_system not in {DIRECT_TARGET_G1, DIRECT_TARGET_G2}:
        raise ValueError("direct-comparison config has an invalid student B system")
    variant = _direct_variant(student_b_system)
    tasks = _read_tasks(_path(config_path, config["task_manifest"]))
    task_ids = [task.task_id for task in tasks]
    pairs = assert_sealed_run_ready(
        run_root=_path(config_path, config["run_root"]),
        experiment_id=str(config["experiment_id"]),
        task_ids=task_ids,
        completed_run_audit=_path(config_path, config["completed_run_audit"]),
    )
    historical = extract_historical_answers(
        runs_root=_path(config_path, config["historical_runs_root"])
    )
    plan = build_direct_comparison_plan(
        tasks=tasks,
        pairs=pairs,
        historical=historical,
        expected_source_pair_protocol=str(config["source_pair_protocol"]),
        reference_42_call_aggregate=_path(
            config_path, config["reference_42_call_aggregate"]
        ),
        expected_reference_42_call_aggregate_sha256=str(
            config["reference_42_call_aggregate_sha256"]
        ),
        student_b_system=student_b_system,  # type: ignore[arg-type]
    )
    plan_path = _path(config_path, config["plan_path"])
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(plan, indent=2, sort_keys=True) + "\n"
    if plan_path.exists():
        if plan_path.read_text(encoding="utf-8") != serialized:
            raise ValueError("existing direct-comparison plan differs from frozen inputs")
    else:
        descriptor = os.open(plan_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
    report = {
        "schema_version": f"{variant['schema_version']}_preflight",
        "status": "READY_FOR_EXPLICIT_PAID_DIRECT_COMPARISON",
        "model_calls": 0,
        "paid_operations": 0,
        "task_count": len(tasks),
        "task_ids": task_ids,
        "call_count": plan["call_count"],
        "plan_path": str(plan_path),
        "plan_sha256": file_sha256(plan_path),
        "cost_ceiling": plan["cost_ceiling"],
        "judge_protocol": PAPER_EVALUATOR_PROTOCOL,
        "prompt_candidate_sha256": PAPER_EVALUATOR_PROMPT_SHA256,
        "student_a_system": "historical_chemcrow",
        "student_b_system": student_b_system,
        "reference_42_call_ledger_unchanged": True,
        "production_42_call_ledger_included": False,
        "secret_values_included": False,
    }
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def _validate_existing_direct_result(
    path: Path, call: PaperDirectComparisonCall
) -> dict[str, Any]:
    result = json.loads(path.read_text(encoding="utf-8"))
    variant = _direct_variant(call.student_b_system)
    if (
        result.get("schema_version") != variant["result_schema"]
        or result.get("call_id") != call.call_id
        or result.get("task_id") != call.task_id
        or result.get("prompt_sha256") != call.prompt_sha256
        or result.get("execution_route") != PAPER_CORE_ROUTE
        or result.get("comparison") != call.comparison
        or result.get("student_a_system") != call.student_a_system
        or result.get("student_b_system") != call.student_b_system
    ):
        raise ValueError(f"existing direct result authority mismatch: {call.call_id}")
    DualStudentAssessment.model_validate(result.get("assessment"))
    return result


def _bootstrap_mean_ci(values: list[float]) -> list[float]:
    import random

    randomizer = random.Random(DIRECT_BOOTSTRAP_SEED)
    count = len(values)
    means = sorted(
        statistics.mean(randomizer.choice(values) for _ in range(count))
        for _ in range(DIRECT_BOOTSTRAP_REPLICATES)
    )
    lower = means[int(0.025 * DIRECT_BOOTSTRAP_REPLICATES)]
    upper = means[int(0.975 * DIRECT_BOOTSTRAP_REPLICATES) - 1]
    return [lower, upper]


def _exact_sign_test(*, wins: int, losses: int) -> float:
    non_ties = wins + losses
    if non_ties == 0:
        return 1.0
    smaller = min(wins, losses)
    tail = sum(math.comb(non_ties, index) for index in range(smaller + 1))
    return min(1.0, 2.0 * tail / (2**non_ties))


def _render_markdown(audit: dict[str, Any]) -> str:
    variant = _direct_variant(str(audit["student_b_system"]))
    target_label = variant["target_label"]
    display_label = variant["display_label"]
    grade_key = f"{target_label}_grade"
    delta_key = f"{target_label}_minus_historical_chemcrow"
    wins_key = f"{target_label}_wins"
    short_label = "G1" if target_label == "v5_g1" else "G2"
    rows = [
        f"# Direct GPT-4 Comparison: Historical ChemCrow vs {display_label}",
        "",
        (
            "This is a project-added, same-call comparison using the frozen calibrated "
            "ChemCrow-compatible judge. It is not an original-paper metric."
        ),
        "",
        f"- status: `{audit['status']}`",
        f"- calls: `{audit['call_count']}/14`",
        f"- historical ChemCrow mean: `{audit['mean_historical_chemcrow_grade']:.3f}`",
        f"- {display_label} mean: `{audit[f'mean_{grade_key}']:.3f}`",
        (
            f"- {short_label} minus historical ChemCrow: "
            f"`{audit[f'mean_{delta_key}']:+.3f}`"
        ),
        (
            f"- {short_label} wins/ties/historical wins: "
            f"`{audit[wins_key]}/{audit['ties']}/"
            f"{audit['historical_chemcrow_wins']}`"
        ),
        f"- paired bootstrap 95% CI: `{audit['paired_bootstrap_95_ci_mean_delta']}`",
        f"- exact sign-test p: `{audit['exact_sign_test_two_sided_p']:.6f}`",
        f"- proven cost: `${audit['actual_openrouter_reported_cost_usd']:.5f}`",
        f"- answer order: historical ChemCrow = Student A; {display_label} = Student B",
        (
            "- original 42-call ledger unchanged: "
            f"`{audit['reference_42_call_ledger_unchanged']}`"
        ),
        "",
        (
            f"| task | historical ChemCrow | {display_label} | "
            f"{short_label} - historical | winner |"
        ),
        "|---|---:|---:|---:|---|",
    ]
    for row in audit["per_task"]:
        rows.append(
            f"| {row['task_id']} | {row['historical_chemcrow_grade']:.1f} | "
            f"{row[grade_key]:.1f} | "
            f"{row[delta_key]:+.1f} | {row['winner']} |"
        )
    rows.extend(["", f"Limitation: {audit['limitations']}", ""])
    return "\n".join(rows)


def _load_config(path: Path) -> dict[str, Any]:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise TypeError("direct-comparison config must be a YAML object")
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
    parser = argparse.ArgumentParser(prog="chemcrow-paper-direct-comparison")
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
        report = preflight_direct_comparison(
            config_path=config_path, output=args.output.resolve()
        )
        print(json.dumps({"status": report["status"], "model_calls": 0}, sort_keys=True))
        return
    if args.command == "run":
        config = _resolve_env(_load_config(config_path))
        aggregate = run_direct_comparison_plan(
            plan_path=_path(config_path, config["plan_path"]),
            rollout_base_url=str(config["rollout_base_url"]),
            runtime=dict(config["runtime"]),
            result_root=_path(config_path, config["result_root"]),
            shim_receipt_root=_path(config_path, config["shim_receipt_root"]),
            reference_42_call_aggregate=_path(
                config_path, config["reference_42_call_aggregate"]
            ),
            allow_paid=args.allow_paid,
        )
        print(
            json.dumps(
                {"status": aggregate["status"], "result_count": aggregate["call_count"]},
                sort_keys=True,
            )
        )
        return
    audit = audit_direct_comparison(
        config_path=config_path,
        output_json=args.output_json.resolve(),
        output_markdown=args.output_markdown.resolve(),
    )
    print(json.dumps({"status": audit["status"], "result_count": audit["result_count"]}))


if __name__ == "__main__":
    main()
