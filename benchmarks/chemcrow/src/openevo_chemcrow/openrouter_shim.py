"""Credential-isolating OpenRouter shim for the dedicated paper judge Gateway.

This process is the only benchmark component allowed to read
``OPENROUTER_API_KEY``.  It persists hashes and usage/cost receipts, never
prompt text, response text, or the credential.
"""

from __future__ import annotations

import argparse
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .hashing import canonical_sha256
from .paper_evaluator import (
    PAPER_EVALUATOR_AUTHORIZATION,
    PAPER_EVALUATOR_CALL_COUNT,
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
    mock_mode: bool = False,
) -> FastAPI:
    if not api_key:
        raise ValueError("OPENROUTER_API_KEY is required")
    if not base_url.startswith("https://") and transport is None:
        raise ValueError("OPENROUTER_BASE_URL must use https")
    if allowed_call_prompt_hashes is None and not mock_mode:
        raise ValueError("a frozen paper evaluator plan allowlist is required")
    allowed_call_prompt_hashes = dict(allowed_call_prompt_hashes or {})
    receipt_root.mkdir(parents=True, exist_ok=True)
    client = httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        timeout=httpx.Timeout(330.0, connect=30.0),
        transport=transport,
        trust_env=False,
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
            "credential_present": True,
            "credential_value_included": False,
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
        upstream_payload["provider"] = {
            "only": [PAPER_EVALUATOR_PROVIDER],
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "deny",
        }
        try:
            response = await client.post(
                "/chat/completions",
                json=upstream_payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.RequestError as exc:
            _write_failure_receipt(
                receipt_path,
                call_id=call_id,
                request_hash=request_hash,
                failure="ambiguous_upstream_transport_error",
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
            )
        except HTTPException:
            _write_failure_receipt(
                receipt_path,
                call_id=call_id,
                request_hash=request_hash,
                failure="invalid_or_unpinned_upstream_receipt",
            )
            raise
        _exclusive_json_write(receipt_path, receipt)
        return JSONResponse(payload)

    return app


def _validate_and_prepare(
    request: dict[str, Any],
    *,
    allowed_call_prompt_hashes: dict[str, str],
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
    if not call_id.startswith("paper-chemcrow-") or not all(
        character.isalnum() or character in "-_" for character in call_id
    ):
        raise HTTPException(status_code=400, detail="invalid paper evaluator call ID")
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
        "allow_fallbacks": False,
        "provider_only": [PAPER_EVALUATOR_PROVIDER],
        "require_parameters": True,
        "data_collection": "deny",
    }


def _write_failure_receipt(path: Path, *, call_id: str, request_hash: str, failure: str) -> None:
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
        },
    )


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
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    if os.environ.get("CHEMCROW_PAPER_EVALUATOR_MODEL") != PAPER_EVALUATOR_MODEL:
        raise SystemExit("CHEMCROW_PAPER_EVALUATOR_MODEL differs from frozen model")
    if os.environ.get("CHEMCROW_PAPER_EVALUATOR_AUTHORIZATION") != PAPER_EVALUATOR_AUTHORIZATION:
        raise SystemExit("paper evaluator paid authorization literal is absent")
    plan = json.loads(args.plan.resolve().read_text(encoding="utf-8"))
    if (
        plan.get("schema_version") != "chemcrow_paper_evaluator_plan_v1"
        or plan.get("call_count") != PAPER_EVALUATOR_CALL_COUNT
    ):
        raise SystemExit("paper evaluator plan authority is invalid")
    allowed = {
        str(call["call_id"]): str(call["prompt_sha256"])
        for call in plan.get("calls", [])
        if isinstance(call, dict)
    }
    if len(allowed) != PAPER_EVALUATOR_CALL_COUNT:
        raise SystemExit("paper evaluator plan allowlist is incomplete")
    configured_budget = float(os.environ.get("CHEMCROW_PAPER_EVALUATOR_MAX_USD", "0"))
    required_budget = float(plan["cost_ceiling"]["list_price_ceiling_usd_total"])
    if configured_budget < required_budget:
        raise SystemExit("paper evaluator budget is below the frozen plan ceiling")
    app = create_openrouter_shim_app(
        api_key=api_key,
        base_url=base_url,
        receipt_root=args.receipt_root.resolve(),
        allowed_call_prompt_hashes=allowed,
    )
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
