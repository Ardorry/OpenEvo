"""Claimed one-shot OpenRouter request-shape diagnostics.

The module persists only request structure, hashes, sanitized router metadata,
and usage. Prompt, completion, response body, and credentials are never written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from .hashing import canonical_sha256, file_sha256
from .openrouter_request_audit import sanitized_request_descriptor
from .openrouter_shim import _safe_router_metadata, _safe_upstream_failure_metadata
from .paper_evaluator import (
    PAPER_EVALUATOR_DATA_COLLECTION,
    PAPER_EVALUATOR_INPUT_USD_PER_TOKEN,
    PAPER_EVALUATOR_MODEL,
    PAPER_EVALUATOR_OUTPUT_USD_PER_TOKEN,
    PAPER_EVALUATOR_PROVIDER,
    PAPER_EVALUATOR_TEMPERATURE,
    render_compatible_prompt,
    validate_assessment_text,
)

DELTA_DEBUG_AUTHORIZATION = "I_AUTHORIZE_12_REQUEST_DELTA_DEBUG_CALLS_20260826"
DELTA_DEBUG_SCHEMA = "chemcrow_openrouter_delta_debug_v1"
DELTA_DEBUG_MAX_ATTEMPTS = 12
DELTA_DEBUG_MAX_USD = 3.0


@dataclass(frozen=True)
class DeltaVariant:
    variant: str
    call_id: str
    max_tokens: int
    messages: tuple[tuple[str, str], ...]
    use_environment_proxy: bool = False
    response_contract: str = "completion_present"


def _variant(name: str) -> DeltaVariant:
    neutral = "Reply with exactly OK."
    evaluator_prompt = render_compatible_prompt(
        task_prompt=(
            "Test-only evaluator contract check: assess whether each answer correctly states "
            "the molecular formula of water. This is not a ChemCrow benchmark item."
        ),
        student_a="Water has molecular formula H2O.",
        student_b="The molecular formula of water is H2O.",
    )
    variants = {
        "known_good_a": DeltaVariant(
            variant="known_good_a",
            call_id="paper-chemcrow-request-delta-known-good-a",
            max_tokens=4,
            messages=(("user", neutral),),
        ),
        "known_good_a_proxy": DeltaVariant(
            variant="known_good_a_proxy",
            call_id="paper-chemcrow-request-delta-known-good-a-proxy",
            max_tokens=4,
            messages=(("user", neutral),),
            use_environment_proxy=True,
        ),
        "a1_max_tokens_1200": DeltaVariant(
            variant="a1_max_tokens_1200",
            call_id="paper-chemcrow-request-delta-a1-max-tokens-1200",
            max_tokens=1200,
            messages=(("user", neutral),),
        ),
        "a2_two_messages_neutral": DeltaVariant(
            variant="a2_two_messages_neutral",
            call_id="paper-chemcrow-request-delta-a2-two-messages-neutral",
            max_tokens=1200,
            messages=(
                ("system", "ChemCrow paper route diagnostic. Return strict JSON only."),
                ("user", neutral),
            ),
        ),
        "a3_previous_direct_shape": DeltaVariant(
            variant="a3_previous_direct_shape",
            call_id="paper-chemcrow-request-delta-a3-previous-direct-shape",
            max_tokens=1200,
            messages=(
                ("system", "ChemCrow paper route diagnostic. Return strict JSON only."),
                (
                    "user",
                    'Return exactly the JSON object {"ok":true}. Do not add Markdown or other keys.',
                ),
            ),
        ),
        "evaluator_smoke_actual_proxy": DeltaVariant(
            variant="evaluator_smoke_actual_proxy",
            call_id="paper-chemcrow-request-delta-evaluator-smoke-actual-proxy",
            max_tokens=1200,
            messages=(
                (
                    "system",
                    "ChemCrow sealed-output paper evaluator. Return strict JSON only.",
                ),
                ("user", evaluator_prompt),
            ),
            use_environment_proxy=True,
            response_contract="dual_student_assessment",
        ),
    }
    try:
        return variants[name]
    except KeyError as exc:
        raise ValueError(f"unknown frozen request-delta variant: {name}") from exc


def build_delta_payload(name: str) -> tuple[DeltaVariant, dict[str, Any]]:
    variant = _variant(name)
    return variant, {
        "model": PAPER_EVALUATOR_MODEL,
        "temperature": PAPER_EVALUATOR_TEMPERATURE,
        "max_tokens": variant.max_tokens,
        "stream": False,
        "messages": [
            {"role": role, "content": content} for role, content in variant.messages
        ],
        "provider": {
            "only": [PAPER_EVALUATOR_PROVIDER],
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": PAPER_EVALUATOR_DATA_COLLECTION,
        },
    }


def run_delta_debug(
    *,
    variant_name: str,
    api_key: str,
    base_url: str,
    receipt_root: Path,
    credential_probe_path: Path,
    allow_paid: bool,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    if not allow_paid:
        raise PermissionError("request-delta debug requires --allow-paid")
    if os.environ.get("CHEMCROW_OPENROUTER_DELTA_AUTHORIZATION") != DELTA_DEBUG_AUTHORIZATION:
        raise PermissionError("request-delta debug authorization literal is absent")
    if float(os.environ.get("CHEMCROW_OPENROUTER_DELTA_MAX_USD", "0")) < DELTA_DEBUG_MAX_USD:
        raise PermissionError("request-delta debug budget is below the authorized ceiling")
    if os.environ.get("CHEMCROW_PAPER_EVALUATOR_MODEL") != PAPER_EVALUATOR_MODEL:
        raise ValueError("paper evaluator model differs from frozen openai/gpt-4")
    if not api_key:
        raise PermissionError("OPENROUTER_API_KEY is absent")
    if not base_url.startswith("https://") and transport is None:
        raise ValueError("OPENROUTER_BASE_URL must use https")
    probe = json.loads(credential_probe_path.read_text(encoding="utf-8"))
    if (
        probe.get("status") != "VALID"
        or probe.get("auth_valid") is not True
        or probe.get("credit_probe_success") is not True
        or probe.get("model_calls") != 0
    ):
        raise PermissionError("zero-paid credential gate is not VALID")

    variant, payload = build_delta_payload(variant_name)
    receipt_root.mkdir(parents=True, exist_ok=True)
    existing_claims = list(receipt_root.glob("*.claim.json"))
    if len(existing_claims) >= DELTA_DEBUG_MAX_ATTEMPTS:
        raise PermissionError("request-delta debug attempt limit is exhausted")
    claim_path = receipt_root / f"{variant.call_id}.claim.json"
    receipt_path = receipt_root / f"{variant.call_id}.receipt.json"
    if claim_path.exists() or receipt_path.exists():
        raise RuntimeError("request-delta variant was already attempted")

    with httpx.Client(
        base_url=base_url.rstrip("/"),
        timeout=httpx.Timeout(330.0, connect=30.0),
        transport=transport,
        trust_env=variant.use_environment_proxy,
    ) as client:
        outgoing = client.build_request(
            "POST",
            "/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-OpenRouter-Metadata": "enabled",
            },
        )
        descriptor = sanitized_request_descriptor(outgoing)
        claim = {
            "schema_version": f"{DELTA_DEBUG_SCHEMA}_claim",
            "variant": variant.variant,
            "call_id": variant.call_id,
            "request_descriptor": descriptor,
            "method": outgoing.method,
            "url_origin_and_path": (
                f"{outgoing.url.scheme}://{outgoing.url.host}{outgoing.url.path}"
            ),
            "credential_sha256_prefix": hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12],
            "environment_proxy_inherited": variant.use_environment_proxy,
            "claimed_at": datetime.now(UTC).isoformat(),
            "prompt_or_response_included": False,
            "credential_included": False,
            "included_in_formal_42_call_ledger": False,
        }
        _exclusive_json_write(claim_path, claim)
        try:
            response = client.send(outgoing)
        except httpx.RequestError:
            result = _failure_result(
                variant=variant,
                descriptor=descriptor,
                credential_probe_path=credential_probe_path,
                failure="ambiguous_upstream_transport_error",
            )
        else:
            if response.is_success:
                result = _success_result(
                    variant=variant,
                    descriptor=descriptor,
                    credential_probe_path=credential_probe_path,
                    response=response,
                )
            else:
                result = _failure_result(
                    variant=variant,
                    descriptor=descriptor,
                    credential_probe_path=credential_probe_path,
                    failure=f"upstream_http_{response.status_code}",
                    **_safe_upstream_failure_metadata(response),
                )
    _exclusive_json_write(receipt_path, result)
    return {
        **result,
        "claim_sha256": file_sha256(claim_path),
        "receipt_sha256": file_sha256(receipt_path),
    }


def _success_result(
    *,
    variant: DeltaVariant,
    descriptor: dict[str, Any],
    credential_probe_path: Path,
    response: httpx.Response,
) -> dict[str, Any]:
    try:
        payload = response.json()
        content = str(payload["choices"][0]["message"]["content"])
        usage = payload["usage"]
        prompt_tokens = int(usage["prompt_tokens"])
        completion_tokens = int(usage["completion_tokens"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError("request-delta success response envelope is invalid") from exc
    model = str(payload.get("model") or "")
    provider = str(payload.get("provider") or "")
    if model != PAPER_EVALUATOR_MODEL or provider.casefold() != "openai" or not content:
        raise RuntimeError("request-delta success did not bind model/provider/completion")
    json_parse_valid = False
    pydantic_assessment_valid = False
    if variant.response_contract == "dual_student_assessment":
        try:
            validate_assessment_text(content)
        except ValueError as exc:
            raise RuntimeError("evaluator-shaped direct response failed strict validation") from exc
        json_parse_valid = True
        pydantic_assessment_valid = True
    metadata: dict[str, Any] = {}
    if isinstance(payload.get("openrouter_metadata"), dict):
        metadata = _safe_router_metadata(payload["openrouter_metadata"])
    reported_cost = usage.get("cost")
    if not isinstance(reported_cost, int | float):
        reported_cost = None
    list_price_cost = (
        prompt_tokens * PAPER_EVALUATOR_INPUT_USD_PER_TOKEN
        + completion_tokens * PAPER_EVALUATOR_OUTPUT_USD_PER_TOKEN
    )
    return {
        "schema_version": DELTA_DEBUG_SCHEMA,
        "status": "PASS",
        "variant": variant.variant,
        "call_id": variant.call_id,
        "request_descriptor": descriptor,
        "upstream_http_status": response.status_code,
        "model": model,
        "provider": provider,
        "completion_present": True,
        "completion_sha256": canonical_sha256(content),
        "json_parse_valid": json_parse_valid,
        "pydantic_assessment_valid": pydantic_assessment_valid,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "openrouter_reported_cost_usd": reported_cost,
        "list_price_cost_usd": round(list_price_cost, 8),
        "successful_model_completions": 1,
        "provider_request_attempts": 1,
        "credential_probe_sha256": file_sha256(credential_probe_path),
        "prompt_or_response_included": False,
        "credential_included": False,
        "included_in_formal_42_call_ledger": False,
        "created_at": datetime.now(UTC).isoformat(),
        **metadata,
    }


def _failure_result(
    *,
    variant: DeltaVariant,
    descriptor: dict[str, Any],
    credential_probe_path: Path,
    failure: str,
    **metadata: Any,
) -> dict[str, Any]:
    return {
        "schema_version": DELTA_DEBUG_SCHEMA,
        "status": "FAIL_CLOSED",
        "variant": variant.variant,
        "call_id": variant.call_id,
        "request_descriptor": descriptor,
        "failure": failure,
        "successful_model_completions": 0,
        "provider_request_attempts": 1,
        "openrouter_reported_cost_usd": None,
        "credential_probe_sha256": file_sha256(credential_probe_path),
        "prompt_or_response_included": False,
        "credential_included": False,
        "included_in_formal_42_call_ledger": False,
        "created_at": datetime.now(UTC).isoformat(),
        **metadata,
    }


def _exclusive_json_write(path: Path, payload: dict[str, Any]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="chemcrow-openrouter-delta-debug")
    parser.add_argument("--variant", required=True)
    parser.add_argument("--receipt-root", type=Path, required=True)
    parser.add_argument("--credential-probe", type=Path, required=True)
    parser.add_argument("--allow-paid", action="store_true")
    args = parser.parse_args(argv)
    result = run_delta_debug(
        variant_name=args.variant,
        api_key=os.environ.get("OPENROUTER_API_KEY", ""),
        base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        receipt_root=args.receipt_root.resolve(),
        credential_probe_path=args.credential_probe.resolve(),
        allow_paid=args.allow_paid,
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "variant": result["variant"],
                "upstream_http_status": result.get("upstream_http_status"),
                "model": result.get("model"),
                "provider": result.get("provider"),
                "openrouter_attempt": result.get("openrouter_attempt"),
                "selected_endpoints": [
                    endpoint
                    for endpoint in result.get("openrouter_endpoints", [])
                    if endpoint.get("selected") is True
                ],
            },
            sort_keys=True,
        )
    )
    if result["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
