"""One-call, test-only OpenEvo Core smoke for the paper evaluator route.

This module has a separate authorization literal, call ID, receipt root, and
result schema. It cannot create or consume the production 42-call ledger.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .hashing import canonical_sha256, file_sha256
from .openrouter_shim import create_openrouter_shim_app
from .paper_core import (
    PAPER_CORE_ROUTE,
    _assert_dedicated_core_node,
    _assessment_from_core_status,
    _submit_once_and_poll,
    build_paper_task_request,
)
from .paper_evaluator import (
    PAPER_EVALUATOR_DATA_COLLECTION,
    PAPER_EVALUATOR_MAX_OUTPUT_TOKENS,
    PAPER_EVALUATOR_MODEL,
    PAPER_EVALUATOR_PROVIDER,
    PAPER_EVALUATOR_TEMPERATURE,
    PaperEvaluationCall,
    estimate_chat_input_tokens,
    paper_cost_ceiling,
    render_compatible_prompt,
)

PAPER_SMOKE_AUTHORIZATION = "I_AUTHORIZE_ONE_CORE_PAPER_GPT4_SMOKE_V10_20260826"
PAPER_SMOKE_CALL_ID = "paper-chemcrow-smoke-core-v10"
PAPER_SMOKE_SCHEMA = "chemcrow_paper_evaluator_paid_smoke_v10"
PAPER_SMOKE_CONFIG_SCHEMA = "chemcrow_paper_evaluator_smoke_config_v10"


def build_smoke_call() -> PaperEvaluationCall:
    prompt = render_compatible_prompt(
        task_prompt=(
            "Test-only evaluator contract check: assess whether each answer correctly states "
            "the molecular formula of water. This is not a ChemCrow benchmark item."
        ),
        student_a="Water has molecular formula H2O.",
        student_b="The molecular formula of water is H2O.",
    )
    return PaperEvaluationCall(
        call_id=PAPER_SMOKE_CALL_ID,
        task_id="chemcrow-smoke-not-a-benchmark-task",
        comparison="historical_control",
        student_a_system="historical_chemcrow",
        prompt=prompt,
        prompt_sha256=canonical_sha256(prompt),
        source_pair_id="test-only:paper-smoke-v10",
        source_pair_result_sha256="0" * 64,
        source_output_id="test-only:student-a",
        source_output_sha256=canonical_sha256("Water has molecular formula H2O."),
        historical_source_sha256="0" * 64,
        target_answer_sha256=canonical_sha256("Water has molecular formula H2O."),
        historical_gpt4_answer_sha256=canonical_sha256("The molecular formula of water is H2O."),
        estimated_input_tokens=estimate_chat_input_tokens(prompt),
        metric_classification="paper_control_reconstruction",
        paper_comparable=True,
    )


def create_smoke_shim_app(
    *,
    api_key: str,
    base_url: str,
    receipt_root: Path,
    use_environment_proxy: bool = False,
):
    call = build_smoke_call()
    return create_openrouter_shim_app(
        api_key=api_key,
        base_url=base_url,
        receipt_root=receipt_root,
        allowed_call_prompt_hashes={call.call_id: call.prompt_sha256},
        use_environment_proxy=use_environment_proxy,
    )


def run_paid_smoke(
    *,
    rollout_base_url: str,
    runtime: dict[str, Any],
    receipt_root: Path,
    core_completion_root: Path,
    credential_probe_path: Path,
    output_path: Path,
    allow_paid: bool,
) -> dict[str, Any]:
    if not allow_paid:
        raise PermissionError("paper smoke requires --allow-paid")
    if os.environ.get("CHEMCROW_PAPER_SMOKE_AUTHORIZATION") != PAPER_SMOKE_AUTHORIZATION:
        raise PermissionError("test-only paper smoke authorization literal is absent")
    if os.environ.get("CHEMCROW_PAPER_EVALUATOR_MODEL") != PAPER_EVALUATOR_MODEL:
        raise ValueError("paper evaluator model differs from frozen openai/gpt-4")
    budget = float(os.environ.get("CHEMCROW_PAPER_SMOKE_MAX_USD", "0"))
    per_call_ceiling = float(paper_cost_ceiling()["list_price_ceiling_usd_per_call"])
    if budget < per_call_ceiling:
        raise PermissionError("test-only paper smoke budget is below one-call frozen ceiling")
    probe = json.loads(credential_probe_path.read_text(encoding="utf-8"))
    if (
        probe.get("status") != "VALID"
        or probe.get("auth_valid") is not True
        or probe.get("credit_probe_success") is not True
        or probe.get("sufficient_remaining_for_frozen_ceiling") is not True
        or probe.get("model_calls") != 0
    ):
        raise PermissionError("zero-paid credential/credit gate is not VALID")
    call = build_smoke_call()
    claim_path = receipt_root / f"{call.call_id}.claim.json"
    receipt_path = receipt_root / f"{call.call_id}.receipt.json"
    if output_path.exists() or claim_path.exists() or receipt_path.exists():
        raise RuntimeError("paper smoke was already attempted; retry is forbidden")
    _assert_dedicated_core_node(rollout_base_url)
    core_payload = build_paper_task_request(call=call, runtime=runtime)
    status = _submit_once_and_poll(rollout_base_url, core_payload)
    if not receipt_path.is_file():
        raise RuntimeError("OpenRouter smoke usage receipt is missing")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("status") == "terminal_failure_or_ambiguous":
        return seal_failed_smoke_attempt(
            receipt_root=receipt_root,
            core_completion_root=core_completion_root,
            credential_probe_path=credential_probe_path,
            output_path=output_path,
        )
    assessment = _assessment_from_core_status(status)
    if (
        receipt.get("status") != "terminal_success"
        or receipt.get("call_id") != call.call_id
        or receipt.get("model") != PAPER_EVALUATOR_MODEL
        or str(receipt.get("provider", "")).casefold() != "openai"
        or receipt.get("allow_fallbacks") is not False
        or receipt.get("require_parameters") is not True
        or receipt.get("data_collection") != PAPER_EVALUATOR_DATA_COLLECTION
        or receipt.get("temperature") != PAPER_EVALUATOR_TEMPERATURE
        or receipt.get("internal_response_format_validated") is not True
        or receipt.get("upstream_response_format_omitted") is not True
        or receipt.get("internal_call_identity_validated") is not True
        or receipt.get("upstream_user_omitted") is not True
    ):
        raise RuntimeError("OpenRouter smoke receipt differs from frozen route")

    completion_matches = list(core_completion_root.glob(f"task_{call.call_id}/*.json"))
    if len(completion_matches) != 1:
        raise RuntimeError("paper smoke Core completion authority is not unique")
    completion_path = completion_matches[0]
    completion_hash = file_sha256(completion_path)
    result = {
        "schema_version": PAPER_SMOKE_SCHEMA,
        "status": "PASS",
        "request_claim_id": call.call_id,
        "execution_route": (
            "PaperEvaluatorHarness -> dedicated OpenEvo Rollout -> dedicated OpenEvo "
            "Gateway -> test-only auth shim -> OpenRouter"
        ),
        "core_route": PAPER_CORE_ROUTE,
        "model_requested": PAPER_EVALUATOR_MODEL,
        "model_reported": receipt["model"],
        "provider_requested": PAPER_EVALUATOR_PROVIDER,
        "provider_reported": receipt["provider"],
        "temperature": PAPER_EVALUATOR_TEMPERATURE,
        "max_output_tokens": PAPER_EVALUATOR_MAX_OUTPUT_TOKENS,
        "fallback_disabled": receipt["allow_fallbacks"] is False,
        "require_parameters": receipt["require_parameters"],
        "data_collection": receipt["data_collection"],
        "upstream_http_status": receipt["upstream_http_status"],
        "schema_valid": True,
        "json_parse_valid": True,
        "pydantic_assessment_valid": True,
        "internal_response_format_validated": receipt[
            "internal_response_format_validated"
        ],
        "upstream_response_format_omitted": receipt[
            "upstream_response_format_omitted"
        ],
        "internal_call_identity_validated": receipt[
            "internal_call_identity_validated"
        ],
        "upstream_user_omitted": receipt["upstream_user_omitted"],
        "request_prompt_sha256": call.prompt_sha256,
        "response_assessment_sha256": canonical_sha256(assessment.model_dump(mode="json")),
        "usage": {
            "prompt_tokens": receipt["prompt_tokens"],
            "completion_tokens": receipt["completion_tokens"],
            "total_tokens": receipt["total_tokens"],
        },
        "estimated_cost_usd": receipt["list_price_cost_usd"],
        "openrouter_reported_cost_present": receipt.get("openrouter_reported_cost_usd")
        is not None,
        "timestamp": receipt["completed_at"],
        "credential_probe_sha256": file_sha256(credential_probe_path),
        "shim_receipt_sha256": file_sha256(receipt_path),
        "core_completion_sha256": completion_hash,
        "paid_model_calls": 1,
        "included_in_formal_42_call_ledger": False,
        "included_in_benchmark_metrics": False,
        "prompt_or_response_body_in_report": False,
        "formal_paper_authorization_consumed": False,
        "test_authorization_literal": PAPER_SMOKE_AUTHORIZATION,
        "test_authorization_source": "explicit_user_message_2026-08-25",
        "raw_core_completion_retained": False,
        "created_at": datetime.now(UTC).isoformat(),
    }
    # The immutable hashes and shim usage receipt are sufficient for this
    # test-only claim. Remove the temporary Core transcript so prompt/response
    # bodies do not enter the durable regular completion corpus.
    completion_path.unlink()
    try:
        completion_path.parent.rmdir()
    except OSError:
        pass
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _exclusive_json_write(output_path, result)
    return result


def seal_failed_smoke_attempt(
    *,
    receipt_root: Path,
    core_completion_root: Path,
    credential_probe_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Seal an already-terminal failed claim without issuing any upstream request."""

    call = build_smoke_call()
    claim_path = receipt_root / f"{call.call_id}.claim.json"
    receipt_path = receipt_root / f"{call.call_id}.receipt.json"
    if output_path.exists():
        raise RuntimeError("paper smoke failure report already exists")
    if not claim_path.is_file() or not receipt_path.is_file():
        raise RuntimeError("paper smoke claim and terminal failure receipt are required")
    claim = json.loads(claim_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if (
        claim.get("call_id") != call.call_id
        or receipt.get("call_id") != call.call_id
        or receipt.get("status") != "terminal_failure_or_ambiguous"
        or claim.get("request_sha256") != receipt.get("request_sha256")
    ):
        raise RuntimeError("paper smoke failure lineage is invalid")
    completion_matches = list(core_completion_root.glob(f"task_{call.call_id}/*.json"))
    if len(completion_matches) != 1:
        raise RuntimeError("failed paper smoke Core completion authority is not unique")
    completion_path = completion_matches[0]
    completion_hash = file_sha256(completion_path)
    completion = json.loads(completion_path.read_text(encoding="utf-8"))
    core_status = str(completion.get("status") or "")
    core_error = str(completion.get("error") or "")
    if core_status != "ERROR":
        raise RuntimeError("failed paper smoke Core completion is not ERROR")
    probe_hash = file_sha256(credential_probe_path)
    result = {
        "schema_version": PAPER_SMOKE_SCHEMA,
        "status": "FAIL_CLOSED",
        "request_claim_id": call.call_id,
        "execution_route": (
            "PaperEvaluatorHarness -> dedicated OpenEvo Rollout -> dedicated OpenEvo "
            "Gateway -> test-only auth shim -> OpenRouter"
        ),
        "core_route": PAPER_CORE_ROUTE,
        "model_requested": PAPER_EVALUATOR_MODEL,
        "model_reported": None,
        "provider_requested": PAPER_EVALUATOR_PROVIDER,
        "provider_reported": None,
        "temperature": PAPER_EVALUATOR_TEMPERATURE,
        "max_output_tokens": PAPER_EVALUATOR_MAX_OUTPUT_TOKENS,
        "fallback_disabled": True,
        "require_parameters": True,
        "data_collection": PAPER_EVALUATOR_DATA_COLLECTION,
        "upstream_http_status": receipt.get("upstream_http_status"),
        "upstream_error_code": receipt.get("upstream_error_code"),
        "upstream_error_category": receipt.get("upstream_error_category"),
        "upstream_error_message_sha256": receipt.get("upstream_error_message_sha256"),
        "upstream_response_sha256": receipt.get("upstream_response_sha256"),
        "upstream_response_body_included": False,
        "schema_valid": False,
        "json_parse_valid": False,
        "pydantic_assessment_valid": False,
        "internal_response_format_validated": receipt.get(
            "internal_response_format_validated"
        ),
        "upstream_response_format_omitted": receipt.get(
            "upstream_response_format_omitted"
        ),
        "internal_call_identity_validated": receipt.get(
            "internal_call_identity_validated"
        ),
        "upstream_user_omitted": receipt.get("upstream_user_omitted"),
        "usage_metadata_present": False,
        "estimated_cost_usd": None,
        "billing_status": "NO_USAGE_RECEIPT_UPSTREAM_FAILURE; billing not proven",
        "provider_request_attempts": 1,
        "successful_model_completions": 0,
        "automatic_retry_allowed": False,
        "core_terminal_status": core_status,
        "core_error_sha256": canonical_sha256(core_error),
        "core_completion_sha256": completion_hash,
        "credential_probe_sha256": probe_hash,
        "shim_claim_sha256": file_sha256(claim_path),
        "shim_receipt_sha256": file_sha256(receipt_path),
        "included_in_formal_42_call_ledger": False,
        "included_in_benchmark_metrics": False,
        "prompt_or_response_body_in_report": False,
        "formal_paper_authorization_consumed": False,
        "test_authorization_literal": PAPER_SMOKE_AUTHORIZATION,
        "test_authorization_source": "explicit_user_message_2026-08-25",
        "raw_core_completion_retained": False,
        "sealed_from_existing_terminal_evidence_only": True,
        "paid_model_calls_during_sealing": 0,
        "created_at": datetime.now(UTC).isoformat(),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _exclusive_json_write(output_path, result)
    completion_path.unlink()
    try:
        completion_path.parent.rmdir()
    except OSError:
        pass
    return result


def seal_unreached_smoke_infrastructure_failure(
    *,
    receipt_root: Path,
    core_completion_root: Path,
    credential_probe_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Seal a Core failure that provably happened before the auth shim claim."""

    call = build_smoke_call()
    claim_path = receipt_root / f"{call.call_id}.claim.json"
    receipt_path = receipt_root / f"{call.call_id}.receipt.json"
    if output_path.exists():
        raise RuntimeError("paper smoke infrastructure report already exists")
    if claim_path.exists() or receipt_path.exists():
        raise RuntimeError("provider claim/receipt exists; infrastructure-only seal is forbidden")
    completion_matches = list(core_completion_root.glob(f"task_{call.call_id}/*.json"))
    if len(completion_matches) != 1:
        raise RuntimeError("infrastructure-failed Core completion authority is not unique")
    completion_path = completion_matches[0]
    completion = json.loads(completion_path.read_text(encoding="utf-8"))
    core_status = str(completion.get("status") or "")
    core_error = str(completion.get("error") or "")
    if core_status != "ERROR" or not core_error:
        raise RuntimeError("infrastructure-failed Core completion is not terminal ERROR")
    result = {
        "schema_version": PAPER_SMOKE_SCHEMA,
        "status": "FAIL_CLOSED_INFRASTRUCTURE_BEFORE_PROVIDER",
        "request_claim_id": call.call_id,
        "core_route": PAPER_CORE_ROUTE,
        "model_requested": PAPER_EVALUATOR_MODEL,
        "provider_requested": PAPER_EVALUATOR_PROVIDER,
        "temperature": PAPER_EVALUATOR_TEMPERATURE,
        "provider_request_attempts": 0,
        "paid_model_calls": 0,
        "estimated_cost_usd": 0.0,
        "shim_claim_exists": False,
        "shim_receipt_exists": False,
        "core_terminal_status": core_status,
        "core_error_sha256": canonical_sha256(core_error),
        "core_completion_sha256": file_sha256(completion_path),
        "credential_probe_sha256": file_sha256(credential_probe_path),
        "included_in_formal_42_call_ledger": False,
        "included_in_benchmark_metrics": False,
        "formal_paper_authorization_consumed": False,
        "prompt_or_response_body_in_report": False,
        "credential_included": False,
        "raw_core_completion_retained": True,
        "created_at": datetime.now(UTC).isoformat(),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _exclusive_json_write(output_path, result)
    return result


def _exclusive_json_write(path: Path, payload: dict[str, Any]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chemcrow-paper-smoke")
    commands = parser.add_subparsers(dest="command", required=True)
    shim = commands.add_parser("shim")
    shim.add_argument("--host", default="127.0.0.1")
    shim.add_argument("--port", type=int, default=8400)
    shim.add_argument("--receipt-root", type=Path, required=True)
    shim.add_argument("--use-environment-proxy", action="store_true")
    run = commands.add_parser("run")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--credential-probe", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--allow-paid", action="store_true")
    seal_failure = commands.add_parser("seal-failure")
    seal_failure.add_argument("--config", type=Path, required=True)
    seal_failure.add_argument("--credential-probe", type=Path, required=True)
    seal_failure.add_argument("--output", type=Path, required=True)
    seal_infrastructure = commands.add_parser("seal-infrastructure-failure")
    seal_infrastructure.add_argument("--config", type=Path, required=True)
    seal_infrastructure.add_argument("--credential-probe", type=Path, required=True)
    seal_infrastructure.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command == "shim":
        if os.environ.get("CHEMCROW_PAPER_SMOKE_AUTHORIZATION") != PAPER_SMOKE_AUTHORIZATION:
            raise SystemExit("test-only paper smoke authorization literal is absent")
        if os.environ.get("CHEMCROW_PAPER_EVALUATOR_MODEL") != PAPER_EVALUATOR_MODEL:
            raise SystemExit("paper evaluator model differs")
        app = create_smoke_shim_app(
            api_key=os.environ.get("OPENROUTER_API_KEY", ""),
            base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            receipt_root=args.receipt_root.resolve(),
            use_environment_proxy=args.use_environment_proxy,
        )
        import uvicorn

        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            access_log=False,
            log_level="warning",
        )
        return
    config = json.loads(args.config.resolve().read_text(encoding="utf-8"))
    if config.get("schema_version") != PAPER_SMOKE_CONFIG_SCHEMA:
        raise SystemExit("paper smoke config is not the frozen v10 schema")
    if args.command == "seal-infrastructure-failure":
        result = seal_unreached_smoke_infrastructure_failure(
            receipt_root=Path(str(config["receipt_root"])).resolve(),
            core_completion_root=Path(str(config["core_completion_root"])).resolve(),
            credential_probe_path=args.credential_probe.resolve(),
            output_path=args.output.resolve(),
        )
        print(
            json.dumps(
                {
                    "status": result["status"],
                    "provider_request_attempts": 0,
                    "paid_model_calls": 0,
                    "request_claim_id": result["request_claim_id"],
                },
                sort_keys=True,
            )
        )
        return
    if args.command == "seal-failure":
        result = seal_failed_smoke_attempt(
            receipt_root=Path(str(config["receipt_root"])).resolve(),
            core_completion_root=Path(str(config["core_completion_root"])).resolve(),
            credential_probe_path=args.credential_probe.resolve(),
            output_path=args.output.resolve(),
        )
        print(
            json.dumps(
                {
                    "status": result["status"],
                    "paid_model_calls_during_sealing": 0,
                    "request_claim_id": result["request_claim_id"],
                },
                sort_keys=True,
            )
        )
        return
    result = run_paid_smoke(
        rollout_base_url=str(config["rollout_base_url"]),
        runtime=dict(config["runtime"]),
        receipt_root=Path(str(config["receipt_root"])).resolve(),
        core_completion_root=Path(str(config["core_completion_root"])).resolve(),
        credential_probe_path=args.credential_probe.resolve(),
        output_path=args.output.resolve(),
        allow_paid=args.allow_paid,
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "paid_model_calls": result.get(
                    "paid_model_calls", result.get("successful_model_completions", 0)
                ),
                "model": result.get("model_reported"),
                "provider": result.get("provider_reported"),
                "schema_valid": result["schema_valid"],
            },
            sort_keys=True,
        )
    )
    if result["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
