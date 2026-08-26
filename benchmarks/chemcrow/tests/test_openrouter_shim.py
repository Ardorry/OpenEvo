from __future__ import annotations

import json

import httpx
import pytest

from openevo_chemcrow.hashing import canonical_sha256
from openevo_chemcrow.openrouter_shim import create_openrouter_shim_app


@pytest.mark.asyncio
async def test_openrouter_shim_pins_route_and_writes_value_free_receipt(tmp_path):
    observed = {}

    async def upstream(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers.get("authorization")
        observed["metadata_header"] = request.headers.get("x-openrouter-metadata")
        observed["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "generation-test",
                "model": "openai/gpt-4",
                "provider": "OpenAI",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": '{"student_a":{"grade":8,"strengths":[],"weaknesses":[],"justification":"ok","feedback":[]},"student_b":{"grade":7,"strengths":[],"weaknesses":[],"justification":"ok","feedback":[]}}',
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            },
        )

    receipt_root = tmp_path / "receipts"
    app = create_openrouter_shim_app(
        api_key="DO_NOT_PERSIST_THIS_KEY",
        base_url="http://mock.invalid/v1",
        receipt_root=receipt_root,
        transport=httpx.MockTransport(upstream),
        allowed_call_prompt_hashes={
            "paper-v5-chemcrow-01-baseline": canonical_sha256("PRIVATE PROMPT")
        },
        call_id_prefix="paper-v5-chemcrow-",
    )
    transport = httpx.ASGITransport(app=app)
    request = {
        "model": "openai/gpt-4",
        "temperature": 0.1,
        "max_tokens": 1200,
        "stream": False,
        "user": "paper-v5-chemcrow-01-baseline",
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "ChemCrow sealed-output paper evaluator. Return strict JSON only.",
            },
            {"role": "user", "content": "PRIVATE PROMPT"},
        ],
        "logprobs": True,
        "top_logprobs": 0,
        "return_token_ids": True,
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://shim") as client:
        response = await client.post("/v1/chat/completions", json=request)
        repeated = await client.post("/v1/chat/completions", json=request)

    assert response.status_code == 200
    assert repeated.status_code == 409
    assert observed["authorization"] == "Bearer DO_NOT_PERSIST_THIS_KEY"
    assert observed["metadata_header"] == "enabled"
    assert observed["payload"]["provider"] == {
        "only": ["openai"],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "allow",
    }
    assert "response_format" not in observed["payload"]
    assert "user" not in observed["payload"]
    assert "return_token_ids" not in observed["payload"]
    claim = json.loads(
        (receipt_root / "paper-v5-chemcrow-01-baseline.claim.json").read_text()
    )
    assert claim["request_sha256"] == canonical_sha256(request)
    receipt_text = (receipt_root / "paper-v5-chemcrow-01-baseline.receipt.json").read_text()
    assert "DO_NOT_PERSIST_THIS_KEY" not in receipt_text
    assert "PRIVATE PROMPT" not in receipt_text
    receipt = json.loads(receipt_text)
    assert receipt["provider"] == "OpenAI"
    assert receipt["list_price_cost_usd"] == 0.006
    assert receipt["internal_response_format_validated"] is True
    assert receipt["upstream_response_format_omitted"] is True
    assert receipt["internal_call_identity_validated"] is True
    assert receipt["upstream_user_omitted"] is True


@pytest.mark.asyncio
async def test_openrouter_shim_separates_calibration_call_namespace(tmp_path):
    async def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "generation-calibration-test",
                "model": "openai/gpt-4",
                "provider": "OpenAI",
                "choices": [{"message": {"role": "assistant", "content": "{}"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 1},
            },
        )

    call_id = "paper-chemcrow-cal-v1-chemcrow-01-current-v1"
    prompt = "CALIBRATION PROMPT"
    request = {
        "model": "openai/gpt-4",
        "temperature": 0.1,
        "max_tokens": 1200,
        "stream": False,
        "user": call_id,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": "ChemCrow sealed-output paper evaluator. Return strict JSON only.",
            },
            {"role": "user", "content": prompt},
        ],
    }
    production_app = create_openrouter_shim_app(
        api_key="SECRET",
        base_url="http://mock.invalid/v1",
        receipt_root=tmp_path / "production",
        transport=httpx.MockTransport(upstream),
        allowed_call_prompt_hashes={call_id: canonical_sha256(prompt)},
    )
    calibration_receipts = tmp_path / "calibration"
    calibration_app = create_openrouter_shim_app(
        api_key="SECRET",
        base_url="http://mock.invalid/v1",
        receipt_root=calibration_receipts,
        transport=httpx.MockTransport(upstream),
        allowed_call_prompt_hashes={call_id: canonical_sha256(prompt)},
        call_id_prefix="paper-chemcrow-cal-v1-",
        ledger_class="calibration",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=production_app), base_url="http://production"
    ) as client:
        rejected = await client.post("/v1/chat/completions", json=request)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=calibration_app), base_url="http://calibration"
    ) as client:
        accepted = await client.post("/v1/chat/completions", json=request)

    assert rejected.status_code == 400
    assert accepted.status_code == 200
    claim = json.loads((calibration_receipts / f"{call_id}.claim.json").read_text())
    receipt = json.loads((calibration_receipts / f"{call_id}.receipt.json").read_text())
    assert claim["ledger_class"] == receipt["ledger_class"] == "calibration"
    assert claim["production_ledger_included"] is False
    assert receipt["production_ledger_included"] is False


@pytest.mark.asyncio
async def test_openrouter_shim_seals_sanitized_upstream_failure_metadata(tmp_path):
    sensitive_message = "No endpoints available under data policy; PRIVATE ROUTING DETAIL"

    async def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"X-Generation-Id": "gen-safe-diagnostic"},
            json={
                "error": {
                    "code": 403,
                    "message": sensitive_message,
                    "error_type": "not_found",
                    "http_status": 403,
                    "metadata": {"patterns": ["PRIVATE ROUTING DETAIL"]},
                    "availability": {
                        "code": "privacy_restricted",
                        "retryable": False,
                        "requested_models": ["openai/gpt-4"],
                        "affected_providers": ["openai", "azure"],
                        "excluded_by": ["data_policy/zdr"],
                    },
                },
                "openrouter_metadata": {
                    "requested": "openai/gpt-4",
                    "strategy": "direct",
                    "attempt": 1,
                    "endpoints": {
                        "total": 1,
                        "available": [
                            {
                                "provider": "OpenAI",
                                "model": "openai/gpt-4",
                                "selected": False,
                            }
                        ],
                    },
                    "pipeline": [
                        {
                            "type": "guardrail",
                            "name": "content-filter",
                            "guardrail_scope": "api-key",
                            "summary": "PRIVATE GUARDRAIL NAME",
                            "data": {
                                "action": "blocked",
                                "detected": True,
                                "patterns": ["PRIVATE PROMPT"],
                            },
                        }
                    ],
                },
            },
        )

    receipt_root = tmp_path / "receipts"
    app = create_openrouter_shim_app(
        api_key="DO_NOT_PERSIST_THIS_KEY",
        base_url="http://mock.invalid/v1",
        receipt_root=receipt_root,
        transport=httpx.MockTransport(upstream),
        allowed_call_prompt_hashes={
            "paper-chemcrow-01-baseline": canonical_sha256("PRIVATE PROMPT")
        },
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://shim") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4",
                "temperature": 0.1,
                "max_tokens": 1200,
                "stream": False,
                "user": "paper-chemcrow-01-baseline",
                "response_format": {"type": "json_object"},
                "messages": [
                    {
                        "role": "system",
                        "content": "ChemCrow sealed-output paper evaluator. Return strict JSON only.",
                    },
                    {"role": "user", "content": "PRIVATE PROMPT"},
                ],
            },
        )

    assert response.status_code == 502
    receipt_text = (receipt_root / "paper-chemcrow-01-baseline.receipt.json").read_text()
    assert sensitive_message not in receipt_text
    assert "PRIVATE ROUTING DETAIL" not in receipt_text
    assert "PRIVATE GUARDRAIL NAME" not in receipt_text
    assert "PRIVATE PROMPT" not in receipt_text
    assert "DO_NOT_PERSIST_THIS_KEY" not in receipt_text
    receipt = json.loads(receipt_text)
    assert receipt["upstream_http_status"] == 403
    assert receipt["upstream_error_code"] == 403
    assert receipt["upstream_error_category"] == "provider_data_policy"
    assert receipt["upstream_error_type"] == "not_found"
    assert receipt["upstream_declared_http_status"] == 403
    assert receipt["upstream_availability_code"] == "privacy_restricted"
    assert receipt["upstream_availability_retryable"] is False
    assert receipt["upstream_availability_affected_providers"] == ["openai", "azure"]
    assert receipt["upstream_availability_excluded_by"] == ["data_policy/zdr"]
    assert receipt["upstream_error_message_sha256"] == canonical_sha256(sensitive_message)
    assert receipt["upstream_response_body_included"] is False
    assert receipt["openrouter_metadata_body_included"] is False
    assert receipt["upstream_generation_id"] == "gen-safe-diagnostic"
    assert receipt["openrouter_attempt"] == 1
    assert receipt["openrouter_endpoint_total"] == 1
    assert receipt["openrouter_endpoints"] == [
        {"provider": "OpenAI", "model": "openai/gpt-4", "selected": False}
    ]
    assert receipt["openrouter_pipeline"][0]["type"] == "guardrail"
    assert receipt["openrouter_pipeline"][0]["name"] == "content-filter"
    assert receipt["openrouter_pipeline"][0]["action"] == "blocked"
    assert receipt["openrouter_pipeline"][0]["detected"] is True
    assert receipt["internal_response_format_validated"] is True
    assert receipt["upstream_response_format_omitted"] is True
    assert receipt["internal_call_identity_validated"] is True
    assert receipt["upstream_user_omitted"] is True


@pytest.mark.asyncio
async def test_openrouter_shim_rejects_model_fallbacks_before_upstream(tmp_path):
    called = False

    async def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    app = create_openrouter_shim_app(
        api_key="test",
        base_url="http://mock.invalid/v1",
        receipt_root=tmp_path,
        transport=httpx.MockTransport(upstream),
        mock_mode=True,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://shim") as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "openai/gpt-4",
                "models": ["openai/gpt-4", "openai/gpt-4o"],
                "temperature": 0.1,
                "max_tokens": 1200,
                "stream": False,
                "user": "paper-chemcrow-01-baseline",
                "response_format": {"type": "json_object"},
                "messages": [{}, {}],
            },
        )

    assert response.status_code == 400
    assert called is False


def test_real_openrouter_shim_requires_frozen_plan_allowlist(tmp_path):
    with pytest.raises(ValueError, match="plan allowlist"):
        create_openrouter_shim_app(
            api_key="test",
            base_url="https://openrouter.ai/api/v1",
            receipt_root=tmp_path,
        )
