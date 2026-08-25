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
    PAPER_EVALUATOR_MAX_OUTPUT_TOKENS,
    PAPER_EVALUATOR_MODEL,
    PAPER_EVALUATOR_PROVIDER,
    PAPER_EVALUATOR_TEMPERATURE,
    PaperEvaluationCall,
    estimate_chat_input_tokens,
    paper_cost_ceiling,
    render_compatible_prompt,
)

PAPER_SMOKE_AUTHORIZATION = "I_AUTHORIZE_ONE_CORE_PAPER_GPT4_SMOKE_20260825"
PAPER_SMOKE_CALL_ID = "paper-chemcrow-smoke-core-v1"
PAPER_SMOKE_SCHEMA = "chemcrow_paper_evaluator_paid_smoke_v1"


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
        historical_source_sha256="0" * 64,
        target_answer_sha256=canonical_sha256("Water has molecular formula H2O."),
        historical_gpt4_answer_sha256=canonical_sha256("The molecular formula of water is H2O."),
        estimated_input_tokens=estimate_chat_input_tokens(prompt),
    )


def create_smoke_shim_app(
    *,
    api_key: str,
    base_url: str,
    receipt_root: Path,
):
    call = build_smoke_call()
    return create_openrouter_shim_app(
        api_key=api_key,
        base_url=base_url,
        receipt_root=receipt_root,
        allowed_call_prompt_hashes={call.call_id: call.prompt_sha256},
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
    assessment = _assessment_from_core_status(status)
    if not receipt_path.is_file():
        raise RuntimeError("OpenRouter smoke usage receipt is missing")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if (
        receipt.get("status") != "terminal_success"
        or receipt.get("call_id") != call.call_id
        or receipt.get("model") != PAPER_EVALUATOR_MODEL
        or str(receipt.get("provider", "")).casefold() != "openai"
        or receipt.get("allow_fallbacks") is not False
        or receipt.get("require_parameters") is not True
        or receipt.get("data_collection") != "deny"
        or receipt.get("temperature") != PAPER_EVALUATOR_TEMPERATURE
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
    run = commands.add_parser("run")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--credential-probe", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--allow-paid", action="store_true")
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
                "paid_model_calls": result["paid_model_calls"],
                "model": result["model_reported"],
                "provider": result["provider_reported"],
                "schema_valid": result["schema_valid"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
