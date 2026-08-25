"""One-call direct OpenRouter diagnostic, isolated from Core and formal ledgers."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .hashing import canonical_sha256, file_sha256
from .openrouter_shim import _safe_upstream_failure_metadata
from .paper_evaluator import (
    PAPER_EVALUATOR_DATA_COLLECTION,
    PAPER_EVALUATOR_INPUT_USD_PER_TOKEN,
    PAPER_EVALUATOR_MAX_OUTPUT_TOKENS,
    PAPER_EVALUATOR_MODEL,
    PAPER_EVALUATOR_OUTPUT_USD_PER_TOKEN,
    PAPER_EVALUATOR_PROVIDER,
    PAPER_EVALUATOR_TEMPERATURE,
    paper_cost_ceiling,
)

DIRECT_DEBUG_AUTHORIZATION = "I_AUTHORIZE_ONE_DIRECT_PAPER_GPT4_DEBUG_V1_20260826"
DIRECT_DEBUG_CALL_ID = "paper-chemcrow-direct-debug-v1"


def run_direct_debug(
    *,
    api_key: str,
    base_url: str,
    receipt_root: Path,
    credential_probe_path: Path,
    output_path: Path,
    allow_paid: bool,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    if not allow_paid:
        raise PermissionError("direct paper debug requires --allow-paid")
    if os.environ.get("CHEMCROW_PAPER_DIRECT_DEBUG_AUTHORIZATION") != DIRECT_DEBUG_AUTHORIZATION:
        raise PermissionError("direct paper debug authorization literal is absent")
    if os.environ.get("CHEMCROW_PAPER_EVALUATOR_MODEL") != PAPER_EVALUATOR_MODEL:
        raise ValueError("paper evaluator model differs from frozen openai/gpt-4")
    if float(os.environ.get("CHEMCROW_PAPER_SMOKE_MAX_USD", "0")) < float(
        paper_cost_ceiling()["list_price_ceiling_usd_per_call"]
    ):
        raise PermissionError("direct paper debug budget is below the one-call ceiling")
    probe = json.loads(credential_probe_path.read_text(encoding="utf-8"))
    if (
        probe.get("status") != "VALID"
        or probe.get("auth_valid") is not True
        or probe.get("credit_probe_success") is not True
        or probe.get("model_calls") != 0
    ):
        raise PermissionError("zero-paid credential gate is not VALID")
    if not api_key:
        raise PermissionError("OPENROUTER_API_KEY is absent")
    if not base_url.startswith("https://") and transport is None:
        raise ValueError("OPENROUTER_BASE_URL must use https")

    prompt = 'Return exactly the JSON object {"ok":true}. Do not add Markdown or other keys.'
    request = {
        "model": PAPER_EVALUATOR_MODEL,
        "temperature": PAPER_EVALUATOR_TEMPERATURE,
        "max_tokens": PAPER_EVALUATOR_MAX_OUTPUT_TOKENS,
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": "ChemCrow paper route diagnostic. Return strict JSON only.",
            },
            {"role": "user", "content": prompt},
        ],
        "provider": {
            "only": [PAPER_EVALUATOR_PROVIDER],
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": PAPER_EVALUATOR_DATA_COLLECTION,
        },
    }
    receipt_root.mkdir(parents=True, exist_ok=True)
    claim_path = receipt_root / f"{DIRECT_DEBUG_CALL_ID}.claim.json"
    receipt_path = receipt_root / f"{DIRECT_DEBUG_CALL_ID}.receipt.json"
    if claim_path.exists() or receipt_path.exists() or output_path.exists():
        raise RuntimeError("direct paper debug call was already attempted")
    request_hash = canonical_sha256(request)
    _exclusive_json_write(
        claim_path,
        {
            "schema_version": "chemcrow_paper_direct_debug_claim_v1",
            "call_id": DIRECT_DEBUG_CALL_ID,
            "request_sha256": request_hash,
            "claimed_at": datetime.now(UTC).isoformat(),
            "prompt_or_response_included": False,
            "included_in_formal_42_call_ledger": False,
        },
    )
    try:
        with httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(330.0, connect=30.0),
            transport=transport,
            trust_env=False,
        ) as client:
            response = client.post(
                "/chat/completions",
                json=request,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "X-OpenRouter-Metadata": "enabled",
                },
            )
    except httpx.RequestError:
        result = _failure_result(
            request_hash=request_hash,
            failure="ambiguous_upstream_transport_error",
            credential_probe_path=credential_probe_path,
        )
    else:
        if response.is_success:
            try:
                result = _success_result(
                    response=response,
                    request_hash=request_hash,
                    credential_probe_path=credential_probe_path,
                )
            except RuntimeError:
                result = _failure_result(
                    request_hash=request_hash,
                    failure="invalid_success_response_envelope_or_content",
                    credential_probe_path=credential_probe_path,
                    upstream_http_status=response.status_code,
                    upstream_response_sha256=canonical_sha256(response.content),
                    upstream_response_body_included=False,
                )
        else:
            result = _failure_result(
                request_hash=request_hash,
                failure=f"upstream_http_{response.status_code}",
                credential_probe_path=credential_probe_path,
                **_safe_upstream_failure_metadata(response),
            )
    _exclusive_json_write(receipt_path, result)
    report = {
        **result,
        "claim_sha256": file_sha256(claim_path),
        "receipt_sha256": file_sha256(receipt_path),
        "prompt_or_response_body_in_report": False,
        "formal_paper_authorization_consumed": False,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _exclusive_json_write(output_path, report)
    return report


def _success_result(
    *, response: httpx.Response, request_hash: str, credential_probe_path: Path
) -> dict[str, Any]:
    try:
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        usage = payload["usage"]
        prompt_tokens = int(usage["prompt_tokens"])
        completion_tokens = int(usage["completion_tokens"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError("direct paper debug returned an invalid response envelope") from exc
    if parsed != {"ok": True}:
        raise RuntimeError("direct paper debug returned invalid strict JSON content")
    model = str(payload.get("model") or "")
    provider = str(payload.get("provider") or "")
    if model != PAPER_EVALUATOR_MODEL or provider.casefold() != "openai":
        raise RuntimeError("direct paper debug did not use the frozen model/provider")
    cost = (
        prompt_tokens * PAPER_EVALUATOR_INPUT_USD_PER_TOKEN
        + completion_tokens * PAPER_EVALUATOR_OUTPUT_USD_PER_TOKEN
    )
    return {
        "schema_version": "chemcrow_paper_direct_debug_v1",
        "status": "PASS",
        "call_id": DIRECT_DEBUG_CALL_ID,
        "request_sha256": request_hash,
        "response_sha256": canonical_sha256(payload),
        "model": model,
        "provider": provider,
        "temperature": PAPER_EVALUATOR_TEMPERATURE,
        "max_tokens": PAPER_EVALUATOR_MAX_OUTPUT_TOKENS,
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": PAPER_EVALUATOR_DATA_COLLECTION,
        "strict_json_valid": True,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "estimated_cost_usd": round(cost, 8),
        "provider_request_attempts": 1,
        "successful_model_completions": 1,
        "credential_probe_sha256": file_sha256(credential_probe_path),
        "prompt_or_response_included": False,
        "credential_included": False,
        "included_in_formal_42_call_ledger": False,
        "included_in_benchmark_metrics": False,
        "created_at": datetime.now(UTC).isoformat(),
    }


def _failure_result(
    *,
    request_hash: str,
    failure: str,
    credential_probe_path: Path,
    **metadata: Any,
) -> dict[str, Any]:
    return {
        "schema_version": "chemcrow_paper_direct_debug_v1",
        "status": "FAIL_CLOSED",
        "call_id": DIRECT_DEBUG_CALL_ID,
        "request_sha256": request_hash,
        "failure": failure,
        "provider_request_attempts": 1,
        "successful_model_completions": 0,
        "estimated_cost_usd": None,
        "credential_probe_sha256": file_sha256(credential_probe_path),
        "prompt_or_response_included": False,
        "credential_included": False,
        "included_in_formal_42_call_ledger": False,
        "included_in_benchmark_metrics": False,
        "created_at": datetime.now(UTC).isoformat(),
        **metadata,
    }


def _exclusive_json_write(path: Path, payload: dict[str, Any]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="chemcrow-paper-direct-debug")
    parser.add_argument("--receipt-root", type=Path, required=True)
    parser.add_argument("--credential-probe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-paid", action="store_true")
    args = parser.parse_args(argv)
    result = run_direct_debug(
        api_key=os.environ.get("OPENROUTER_API_KEY", ""),
        base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        receipt_root=args.receipt_root.resolve(),
        credential_probe_path=args.credential_probe.resolve(),
        output_path=args.output.resolve(),
        allow_paid=args.allow_paid,
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "model": result.get("model"),
                "provider": result.get("provider"),
                "strict_json_valid": result.get("strict_json_valid", False),
            },
            sort_keys=True,
        )
    )
    if result["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
