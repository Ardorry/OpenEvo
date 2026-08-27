"""Credential-isolating OpenRouter shim for the dedicated paper judge Gateway.

This process is the only benchmark component allowed to read
``OPENROUTER_API_KEY``.  It persists hashes and usage/cost receipts, never
prompt text, response text, or the credential.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .hashing import canonical_sha256
from .openrouter_request_audit import sanitized_request_descriptor
from .paper_evaluator import (
    PAPER_EVALUATOR_AUTHORIZATION,
    PAPER_EVALUATOR_CALL_COUNT,
    PAPER_EVALUATOR_DATA_COLLECTION,
    PAPER_EVALUATOR_INPUT_USD_PER_TOKEN,
    PAPER_EVALUATOR_MAX_OUTPUT_TOKENS,
    PAPER_EVALUATOR_MODEL,
    PAPER_EVALUATOR_OUTPUT_USD_PER_TOKEN,
    PAPER_EVALUATOR_PROVIDER,
    PAPER_EVALUATOR_TEMPERATURE,
)

_CORE_INJECTED_FIELDS = {"logprobs", "top_logprobs", "return_token_ids"}


def create_openrouter_shim_app(
    *,
    api_key: str,
    base_url: str,
    receipt_root: Path,
    transport: httpx.AsyncBaseTransport | None = None,
    allowed_call_prompt_hashes: dict[str, str] | None = None,
    call_id_prefix: str = "paper-chemcrow-",
    ledger_class: str = "production",
    mock_mode: bool = False,
    use_environment_proxy: bool = False,
) -> FastAPI:
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY is required")
    if not base_url.startswith("https://") and transport is None:
        raise ValueError("OPENROUTER_BASE_URL must use https")
    if allowed_call_prompt_hashes is None and not mock_mode:
        raise ValueError("a frozen paper evaluator plan allowlist is required")
    if ledger_class not in {
        "production",
        "calibration",
        "direct_comparison",
        "blind_g1_g2",
    }:
        raise ValueError("paper evaluator ledger class is invalid")
    if ledger_class == "calibration":
        prefix_valid = call_id_prefix == "paper-chemcrow-cal-v1-"
    else:
        prefix_valid = re.fullmatch(
            r"paper(?:-[a-z0-9]+)*-chemcrow-", call_id_prefix
        ) is not None
    if not prefix_valid:
        raise ValueError("paper evaluator call prefix is invalid")
    allowed_call_prompt_hashes = dict(allowed_call_prompt_hashes or {})
    receipt_root.mkdir(parents=True, exist_ok=True)
    proxy_descriptor = _environment_proxy_descriptor(use_environment_proxy)
    client = httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        timeout=httpx.Timeout(330.0, connect=30.0),
        transport=transport,
        trust_env=use_environment_proxy,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(
        title="ChemCrow paper evaluator OpenRouter auth shim",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "model": PAPER_EVALUATOR_MODEL,
            "provider_only": [PAPER_EVALUATOR_PROVIDER],
            "data_collection": PAPER_EVALUATOR_DATA_COLLECTION,
            "credential_present": True,
            "credential_value_included": False,
            "environment_proxy": proxy_descriptor,
        }

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        return {"object": "list", "data": [{"id": PAPER_EVALUATOR_MODEL, "object": "model"}]}

    @app.post("/v1/chat/completions")
    async def completion(request: Request) -> JSONResponse:
        try:
            incoming = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="invalid JSON request") from exc
        if not isinstance(incoming, dict):
            raise HTTPException(status_code=400, detail="request must be a JSON object")
        call_id = _validate_and_prepare(
            incoming,
            allowed_call_prompt_hashes=allowed_call_prompt_hashes,
            call_id_prefix=call_id_prefix,
            mock_mode=mock_mode,
        )
        request_hash = canonical_sha256(incoming)
        claim_path = receipt_root / f"{call_id}.claim.json"
        receipt_path = receipt_root / f"{call_id}.receipt.json"
        claim = {
            "schema_version": "chemcrow_paper_openrouter_claim_v1",
            "call_id": call_id,
            "request_sha256": request_hash,
            "claimed_at": datetime.now(UTC).isoformat(),
            "prompt_or_response_included": False,
            "ledger_class": ledger_class,
            "production_ledger_included": ledger_class == "production",
        }
        try:
            descriptor = os.open(
                claim_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError as exc:
            raise HTTPException(
                status_code=409,
                detail="paper evaluator call ID already claimed; automatic retry is forbidden",
            ) from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(claim, handle, sort_keys=True)
            handle.write("\n")

        upstream_payload = dict(incoming)
        for field in _CORE_INJECTED_FIELDS:
            upstream_payload.pop(field, None)
        # Legacy first-party OpenAI GPT-4 does not accept the Chat Completions
        # ``json_object`` transport parameter.  The internal contract above
        # still requires and hashes that exact field; only the already-claimed
        # transport copy is adapted for the pinned upstream model.  Strict JSON
        # parsing and DualStudentAssessment validation remain downstream and
        # fail closed without a retry.
        upstream_payload.pop("response_format", None)
        # ``user`` is the internal, already-claimed call identity.  It has no
        # remaining transport purpose after the exclusive claim is written, so
        # omit it to keep the legacy GPT-4 upstream payload minimal.  The live
        # v7/direct A/B proved that this omission alone does not clear the
        # current pre-provider 403; do not describe it as that failure's cause.
        upstream_payload.pop("user", None)
        upstream_payload["provider"] = {
            "only": [PAPER_EVALUATOR_PROVIDER],
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": PAPER_EVALUATOR_DATA_COLLECTION,
        }
        outgoing = client.build_request(
            "POST",
            "/chat/completions",
            json=upstream_payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-OpenRouter-Metadata": "enabled",
            },
        )
        request_descriptor = sanitized_request_descriptor(outgoing)
        transport_metadata = {
            "upstream_request_descriptor": request_descriptor,
            "environment_proxy": proxy_descriptor,
        }
        try:
            response = await client.send(outgoing)
        except httpx.RequestError as exc:
            _write_failure_receipt(
                receipt_path,
                call_id=call_id,
                request_hash=request_hash,
                failure="ambiguous_upstream_transport_error",
                upstream_metadata=transport_metadata,
                ledger_class=ledger_class,
            )
            raise HTTPException(
                status_code=502,
                detail="ambiguous OpenRouter transport error; automatic retry is forbidden",
            ) from exc
        if not response.is_success:
            _write_failure_receipt(
                receipt_path,
                call_id=call_id,
                request_hash=request_hash,
                failure=f"upstream_http_{response.status_code}",
                upstream_metadata={
                    **transport_metadata,
                    **_safe_upstream_failure_metadata(response),
                },
                ledger_class=ledger_class,
            )
            raise HTTPException(
                status_code=502,
                detail=f"OpenRouter returned HTTP {response.status_code}; retry is forbidden",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            _write_failure_receipt(
                receipt_path,
                call_id=call_id,
                request_hash=request_hash,
                failure="invalid_upstream_json",
                upstream_metadata=transport_metadata,
                ledger_class=ledger_class,
            )
            raise HTTPException(
                status_code=502, detail="OpenRouter returned invalid JSON"
            ) from exc
        try:
            receipt = _success_receipt(
                call_id,
                request_hash,
                payload,
                upstream_http_status=response.status_code,
                upstream_request_descriptor=request_descriptor,
                environment_proxy=proxy_descriptor,
                ledger_class=ledger_class,
            )
        except HTTPException:
            _write_failure_receipt(
                receipt_path,
                call_id=call_id,
                request_hash=request_hash,
                failure="invalid_or_unpinned_upstream_receipt",
                upstream_metadata=transport_metadata,
                ledger_class=ledger_class,
            )
            raise
        _exclusive_json_write(receipt_path, receipt)
        return JSONResponse(payload)

    return app


def _validate_and_prepare(
    request: dict[str, Any],
    *,
    allowed_call_prompt_hashes: dict[str, str],
    call_id_prefix: str,
    mock_mode: bool,
) -> str:
    if request.get("model") != PAPER_EVALUATOR_MODEL:
        raise HTTPException(status_code=400, detail="model must be openai/gpt-4")
    if float(request.get("temperature", -1)) != PAPER_EVALUATOR_TEMPERATURE:
        raise HTTPException(status_code=400, detail="temperature must be exactly 0.1")
    if int(request.get("max_tokens", -1)) != PAPER_EVALUATOR_MAX_OUTPUT_TOKENS:
        raise HTTPException(status_code=400, detail="max_tokens differs from frozen protocol")
    if request.get("stream") is not False:
        raise HTTPException(status_code=400, detail="streaming is forbidden")
    if "models" in request:
        raise HTTPException(status_code=400, detail="model fallback lists are forbidden")
    if "provider" in request:
        raise HTTPException(
            status_code=400, detail="caller-supplied provider routing is forbidden"
        )
    if any(field in request for field in ("tools", "tool_choice", "plugins")):
        raise HTTPException(status_code=400, detail="tools and plugins are forbidden")
    if request.get("response_format") != {"type": "json_object"}:
        raise HTTPException(status_code=400, detail="strict JSON response format is required")
    messages = request.get("messages")
    if not isinstance(messages, list) or len(messages) != 2:
        raise HTTPException(status_code=400, detail="exactly two evaluator messages are required")
    if messages[0] != {
        "role": "system",
        "content": "ChemCrow sealed-output paper evaluator. Return strict JSON only.",
    }:
        raise HTTPException(status_code=400, detail="paper evaluator system message differs")
    call_id = str(request.get("user") or "")
    if not call_id.startswith(call_id_prefix) or not all(
        character.isalnum() or character in "-_" for character in call_id
    ):
        raise HTTPException(status_code=400, detail="invalid paper evaluator call ID")
    if call_id_prefix == "paper-chemcrow-" and call_id.startswith(
        "paper-chemcrow-cal-v1-"
    ):
        raise HTTPException(status_code=400, detail="calibration call ID is forbidden")
    user_message = messages[1]
    if not isinstance(user_message, dict) or user_message.get("role") != "user":
        raise HTTPException(status_code=400, detail="paper evaluator user message differs")
    prompt = user_message.get("content")
    if not isinstance(prompt, str):
        raise HTTPException(status_code=400, detail="paper evaluator prompt must be text")
    if not mock_mode:
        expected_prompt_hash = allowed_call_prompt_hashes.get(call_id)
        if expected_prompt_hash is None:
            raise HTTPException(status_code=403, detail="call ID is absent from frozen plan")
        if canonical_sha256(prompt) != expected_prompt_hash:
            raise HTTPException(status_code=403, detail="prompt differs from frozen plan")
    return call_id


def _success_receipt(
    call_id: str,
    request_hash: str,
    payload: dict[str, Any],
    *,
    upstream_http_status: int,
    upstream_request_descriptor: dict[str, Any],
    environment_proxy: dict[str, Any],
    ledger_class: str,
) -> dict[str, Any]:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        raise HTTPException(status_code=502, detail="OpenRouter response has no usage receipt")
    prompt_tokens = _nonnegative_int(usage.get("prompt_tokens"), "prompt_tokens")
    completion_tokens = _nonnegative_int(usage.get("completion_tokens"), "completion_tokens")
    provider = str(payload.get("provider") or "")
    if provider.lower() != "openai":
        raise HTTPException(
            status_code=502, detail="OpenRouter did not use the pinned OpenAI provider"
        )
    model = str(payload.get("model") or "")
    if model != PAPER_EVALUATOR_MODEL:
        raise HTTPException(
            status_code=502, detail="OpenRouter response model differs from frozen model"
        )
    list_cost = (
        prompt_tokens * PAPER_EVALUATOR_INPUT_USD_PER_TOKEN
        + completion_tokens * PAPER_EVALUATOR_OUTPUT_USD_PER_TOKEN
    )
    return {
        "schema_version": "chemcrow_paper_openrouter_receipt_v1",
        "status": "terminal_success",
        "call_id": call_id,
        "request_sha256": request_hash,
        "response_sha256": canonical_sha256(payload),
        "response_id": payload.get("id"),
        "model": model,
        "provider": provider,
        "upstream_http_status": upstream_http_status,
        "temperature": PAPER_EVALUATOR_TEMPERATURE,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "list_price_cost_usd": round(list_cost, 8),
        "openrouter_reported_cost_usd": usage.get("cost"),
        "completed_at": datetime.now(UTC).isoformat(),
        "prompt_or_response_included": False,
        "credential_included": False,
        "ledger_class": ledger_class,
        "production_ledger_included": ledger_class == "production",
        "allow_fallbacks": False,
        "provider_only": [PAPER_EVALUATOR_PROVIDER],
        "require_parameters": True,
        "data_collection": PAPER_EVALUATOR_DATA_COLLECTION,
        "internal_response_format_validated": True,
        "upstream_response_format_omitted": True,
        "internal_call_identity_validated": True,
        "upstream_user_omitted": True,
        "upstream_request_descriptor": upstream_request_descriptor,
        "environment_proxy": environment_proxy,
    }


def _environment_proxy_descriptor(enabled: bool) -> dict[str, Any]:
    if not enabled:
        return {"mode": "disabled"}
    value = (
        os.environ.get("https_proxy")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("all_proxy")
        or os.environ.get("ALL_PROXY")
    )
    if not value:
        raise ValueError("environment proxy mode requires https_proxy/HTTPS_PROXY")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname:
        raise ValueError("environment proxy URL is invalid")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("credential-bearing environment proxy URLs are forbidden")
    return {
        "mode": "environment_proxy",
        "scheme": parsed.scheme,
        "hostname": parsed.hostname,
        "port": parsed.port,
        "url_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        "credentials_present": False,
    }


def _write_failure_receipt(
    path: Path,
    *,
    call_id: str,
    request_hash: str,
    failure: str,
    upstream_metadata: dict[str, Any] | None = None,
    ledger_class: str = "production",
) -> None:
    safe_metadata = dict(upstream_metadata or {})
    _exclusive_json_write(
        path,
        {
            "schema_version": "chemcrow_paper_openrouter_receipt_v1",
            "status": "terminal_failure_or_ambiguous",
            "call_id": call_id,
            "request_sha256": request_hash,
            "failure": failure,
            "completed_at": datetime.now(UTC).isoformat(),
            "prompt_or_response_included": False,
            "credential_included": False,
            "ledger_class": ledger_class,
            "production_ledger_included": ledger_class == "production",
            "internal_response_format_validated": True,
            "upstream_response_format_omitted": True,
            "internal_call_identity_validated": True,
            "upstream_user_omitted": True,
            **safe_metadata,
        },
    )


def _safe_upstream_failure_metadata(response: httpx.Response) -> dict[str, Any]:
    """Return diagnostic hashes/categories without persisting the upstream body."""

    result: dict[str, Any] = {
        "upstream_http_status": response.status_code,
        "upstream_response_sha256": hashlib.sha256(response.content).hexdigest(),
        "upstream_response_body_included": False,
    }
    generation_id = response.headers.get("x-generation-id")
    if generation_id and len(generation_id) <= 128 and all(
        character.isalnum() or character in "._-" for character in generation_id
    ):
        result["upstream_generation_id"] = generation_id
    try:
        payload = response.json()
    except ValueError:
        result["upstream_error_category"] = "non_json_upstream_error"
        return result
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        result["upstream_error_category"] = "unstructured_upstream_error"
        return result
    code = error.get("code")
    if isinstance(code, int) or (
        isinstance(code, str)
        and 0 < len(code) <= 64
        and all(character.isalnum() or character in "._-" for character in code)
    ):
        result["upstream_error_code"] = code
    error_type = _safe_label(error.get("error_type"))
    if error_type is not None:
        result["upstream_error_type"] = error_type
    declared_http_status = error.get("http_status")
    if isinstance(declared_http_status, int) and 100 <= declared_http_status <= 599:
        result["upstream_declared_http_status"] = declared_http_status
    message = error.get("message")
    if isinstance(message, str):
        result["upstream_error_message_sha256"] = canonical_sha256(message)
        result["upstream_error_category"] = _classify_upstream_error(message)
    else:
        result["upstream_error_category"] = "missing_upstream_error_message"
    error_metadata = error.get("metadata")
    if isinstance(error_metadata, dict):
        result["upstream_error_metadata_sha256"] = canonical_sha256(error_metadata)
        result["upstream_error_metadata_body_included"] = False
        metadata_error_type = _safe_label(error_metadata.get("error_type"))
        if metadata_error_type is not None:
            result["upstream_metadata_error_type"] = metadata_error_type
        provider_code = _safe_label(error_metadata.get("provider_code"))
        if provider_code is not None:
            result["upstream_provider_error_code"] = provider_code
    availability = error.get("availability")
    if isinstance(availability, dict):
        result["upstream_availability_sha256"] = canonical_sha256(availability)
        result["upstream_availability_body_included"] = False
        availability_code = _safe_label(availability.get("code"))
        if availability_code is not None:
            result["upstream_availability_code"] = availability_code
        if isinstance(availability.get("retryable"), bool):
            result["upstream_availability_retryable"] = availability["retryable"]
        for key in ("requested_models", "affected_providers", "excluded_by"):
            values = availability.get(key)
            if isinstance(values, list):
                result[f"upstream_availability_{key}"] = [
                    label
                    for label in (_safe_label(value) for value in values[:32])
                    if label is not None
                ]
    # OpenRouter returns router metadata beside ``error`` rather than nested
    # inside it.  Retain only categorical routing evidence and hashes; free-form
    # summaries, patterns, prompt fragments, and provider bodies remain absent.
    router_metadata = payload.get("openrouter_metadata")
    if isinstance(router_metadata, dict):
        result.update(_safe_router_metadata(router_metadata))
    return result


def _safe_router_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "openrouter_metadata_sha256": canonical_sha256(metadata),
        "openrouter_metadata_body_included": False,
    }
    for key in ("requested", "strategy", "region"):
        value = metadata.get(key)
        if isinstance(value, str) and len(value) <= 128 and all(
            character.isalnum() or character in "._-/" for character in value
        ):
            result[f"openrouter_{key}"] = value
    attempt = metadata.get("attempt")
    if isinstance(attempt, int) and 0 <= attempt <= 64:
        result["openrouter_attempt"] = attempt
    if isinstance(metadata.get("is_byok"), bool):
        result["openrouter_is_byok"] = metadata["is_byok"]
    summary = metadata.get("summary")
    if isinstance(summary, str):
        result["openrouter_summary_sha256"] = canonical_sha256(summary)

    endpoints = metadata.get("endpoints")
    if isinstance(endpoints, dict):
        total = endpoints.get("total")
        if isinstance(total, int) and 0 <= total <= 1024:
            result["openrouter_endpoint_total"] = total
        available = endpoints.get("available")
        if isinstance(available, list):
            result["openrouter_endpoints"] = [
                {
                    "provider": _safe_label(item.get("provider")),
                    "model": _safe_label(item.get("model")),
                    "selected": item.get("selected") if isinstance(item.get("selected"), bool) else None,
                }
                for item in available[:16]
                if isinstance(item, dict)
            ]

    attempts = metadata.get("attempts")
    if isinstance(attempts, list):
        result["openrouter_provider_attempts"] = [
            {
                "provider": _safe_label(item.get("provider")),
                "model": _safe_label(item.get("model")),
                "status": item.get("status") if isinstance(item.get("status"), int) else None,
            }
            for item in attempts[:16]
            if isinstance(item, dict)
        ]

    pipeline = metadata.get("pipeline")
    if isinstance(pipeline, list):
        safe_pipeline: list[dict[str, Any]] = []
        for item in pipeline[:32]:
            if not isinstance(item, dict):
                continue
            entry: dict[str, Any] = {
                "type": _safe_label(item.get("type")),
                "name": _safe_label(item.get("name")),
                "scope": _safe_label(item.get("guardrail_scope")),
            }
            summary = item.get("summary")
            if isinstance(summary, str):
                entry["summary_sha256"] = canonical_sha256(summary)
            data = item.get("data")
            if isinstance(data, dict):
                entry["data_sha256"] = canonical_sha256(data)
                for key in ("action", "decision", "confidence_level"):
                    entry[key] = _safe_label(data.get(key))
                if isinstance(data.get("detected"), bool):
                    entry["detected"] = data["detected"]
            safe_pipeline.append(entry)
        result["openrouter_pipeline"] = safe_pipeline
    return result


def _safe_label(value: Any) -> str | None:
    if not isinstance(value, str) or not (0 < len(value) <= 128):
        return None
    if not all(character.isalnum() or character in " ._-/" for character in value):
        return None
    return value


def _classify_upstream_error(message: str) -> str:
    lowered = message.casefold()
    if "data policy" in lowered or "data collection" in lowered or "privacy" in lowered:
        return "provider_data_policy"
    if "no endpoint" in lowered or ("provider" in lowered and "available" in lowered):
        return "provider_endpoint_routing"
    if "guardrail" in lowered or "content filter" in lowered:
        return "account_or_content_guardrail"
    if "budget" in lowered or "spending limit" in lowered or "credit" in lowered:
        return "budget_or_credit_guardrail"
    if "permission" in lowered or "forbidden" in lowered or "allowlist" in lowered:
        return "permission_or_allowlist"
    return "other_upstream_error"


def _exclusive_json_write(path: Path, payload: dict[str, Any]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _nonnegative_int(value: Any, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"invalid OpenRouter {label}") from exc
    if parsed < 0:
        raise HTTPException(status_code=502, detail=f"invalid OpenRouter {label}")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chemcrow-paper-openrouter-shim")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8400)
    parser.add_argument("--receipt-root", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--calibration", action="store_true")
    mode.add_argument("--direct-comparison", action="store_true")
    mode.add_argument("--blind-g1-g2", action="store_true")
    parser.add_argument("--use-environment-proxy", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    plan = json.loads(args.plan.resolve().read_text(encoding="utf-8"))
    if args.calibration:
        from .paper_calibration import (
            CALIBRATION_AUTHORIZATION,
            CALIBRATION_CALL_PREFIX,
            CALIBRATION_MAX_USD,
            require_calibration_authorization,
            validate_calibration_plan,
        )

        require_calibration_authorization()
        validated_calls = validate_calibration_plan(plan)
        allowed = {call.call_id: call.prompt_sha256 for call in validated_calls}
        configured_budget = float(os.environ.get("CHEMCROW_PAPER_CALIBRATION_MAX_USD", "0"))
        if configured_budget > CALIBRATION_MAX_USD:
            raise SystemExit("calibration budget exceeds the authorized maximum")
        if (
            os.environ.get("CHEMCROW_PAPER_CALIBRATION_AUTHORIZATION")
            != CALIBRATION_AUTHORIZATION
        ):
            raise SystemExit("paper calibration paid authorization literal is absent")
        call_id_prefix = CALIBRATION_CALL_PREFIX
        ledger_class = "calibration"
        resolved_receipts = args.receipt_root.resolve()
        if "paper-evaluator-calibration-v1" not in resolved_receipts.parts:
            raise SystemExit("calibration receipts are outside the dedicated ledger")
    elif args.direct_comparison:
        from .paper_direct_comparison import (
            DIRECT_CALL_COUNT,
            DIRECT_MAX_AUTHORIZED_USD,
            direct_plan_runtime_authority,
            validate_direct_comparison_plan,
        )

        runtime_authority = direct_plan_runtime_authority(plan)
        if os.environ.get("CHEMCROW_PAPER_EVALUATOR_MODEL") != PAPER_EVALUATOR_MODEL:
            raise SystemExit("CHEMCROW_PAPER_EVALUATOR_MODEL differs from frozen model")
        if (
            os.environ.get(runtime_authority["authorization_env"])
            != runtime_authority["authorization"]
        ):
            raise SystemExit("direct-comparison paid authorization literal is absent")
        validated_calls = validate_direct_comparison_plan(plan)
        allowed = {call.call_id: call.prompt_sha256 for call in validated_calls}
        if len(allowed) != DIRECT_CALL_COUNT:
            raise SystemExit("direct-comparison plan allowlist is incomplete")
        configured_budget = float(
            os.environ.get(runtime_authority["budget_env"], "0")
        )
        if configured_budget > DIRECT_MAX_AUTHORIZED_USD:
            raise SystemExit("direct-comparison budget exceeds the authorized maximum")
        call_id_prefix = f"{runtime_authority['call_id_prefix']}-chemcrow-"
        ledger_class = "direct_comparison"
        resolved_receipts = args.receipt_root.resolve()
        if runtime_authority["ledger_root"] not in resolved_receipts.parts:
            raise SystemExit("direct-comparison receipts are outside the dedicated ledger")
    elif args.blind_g1_g2:
        from .paper_blind_g1_g2 import (
            BLIND_CALL_COUNT,
            BLIND_MAX_AUTHORIZED_USD,
            blind_plan_runtime_authority,
            validate_blind_g1_g2_plan,
        )

        runtime_authority = blind_plan_runtime_authority(plan)
        if os.environ.get("CHEMCROW_PAPER_EVALUATOR_MODEL") != PAPER_EVALUATOR_MODEL:
            raise SystemExit("CHEMCROW_PAPER_EVALUATOR_MODEL differs from frozen model")
        if (
            os.environ.get(runtime_authority["authorization_env"])
            != runtime_authority["authorization"]
        ):
            raise SystemExit("blind G1/G2 paid authorization literal is absent")
        validated_calls = validate_blind_g1_g2_plan(plan)
        allowed = {call.call_id: call.prompt_sha256 for call in validated_calls}
        if len(allowed) != BLIND_CALL_COUNT:
            raise SystemExit("blind G1/G2 plan allowlist is incomplete")
        configured_budget = float(
            os.environ.get(runtime_authority["budget_env"], "0")
        )
        if configured_budget > BLIND_MAX_AUTHORIZED_USD:
            raise SystemExit("blind G1/G2 budget exceeds the authorized maximum")
        call_id_prefix = f"{runtime_authority['call_id_prefix']}-chemcrow-"
        ledger_class = "blind_g1_g2"
        resolved_receipts = args.receipt_root.resolve()
        if runtime_authority["ledger_root"] not in resolved_receipts.parts:
            raise SystemExit("blind G1/G2 receipts are outside the dedicated ledger")
    else:
        if os.environ.get("CHEMCROW_PAPER_EVALUATOR_MODEL") != PAPER_EVALUATOR_MODEL:
            raise SystemExit("CHEMCROW_PAPER_EVALUATOR_MODEL differs from frozen model")
        if (
            os.environ.get("CHEMCROW_PAPER_EVALUATOR_AUTHORIZATION")
            != PAPER_EVALUATOR_AUTHORIZATION
        ):
            raise SystemExit("paper evaluator paid authorization literal is absent")
        if (
            plan.get("schema_version") != "chemcrow_paper_evaluator_plan_v1"
            or plan.get("call_count") != PAPER_EVALUATOR_CALL_COUNT
        ):
            raise SystemExit("paper evaluator plan authority is invalid")
        from .paper_evaluator import PaperEvaluationCall

        validated_calls = [
            PaperEvaluationCall.model_validate(call) for call in plan.get("calls", [])
        ]
        allowed = {call.call_id: call.prompt_sha256 for call in validated_calls}
        if len(allowed) != PAPER_EVALUATOR_CALL_COUNT:
            raise SystemExit("paper evaluator plan allowlist is incomplete")
        configured_budget = float(os.environ.get("CHEMCROW_PAPER_EVALUATOR_MAX_USD", "0"))
        plan_call_id_prefix = plan.get("call_id_prefix")
        if not isinstance(plan_call_id_prefix, str):
            raise SystemExit("paper evaluator call ID prefix is absent")
        call_id_prefix = f"{plan_call_id_prefix}-chemcrow-"
        ledger_class = "production"
    required_budget = float(plan["cost_ceiling"]["list_price_ceiling_usd_total"])
    if configured_budget < required_budget:
        raise SystemExit("paper evaluator budget is below the frozen plan ceiling")
    app = create_openrouter_shim_app(
        api_key=api_key,
        base_url=base_url,
        receipt_root=args.receipt_root.resolve(),
        allowed_call_prompt_hashes=allowed,
        call_id_prefix=call_id_prefix,
        ledger_class=ledger_class,
        use_environment_proxy=args.use_environment_proxy,
    )
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
