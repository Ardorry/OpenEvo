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
            "paper-chemcrow-01-baseline": canonical_sha256("PRIVATE PROMPT")
        },
    )
    transport = httpx.ASGITransport(app=app)
    request = {
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
    assert "return_token_ids" not in observed["payload"]
    receipt_text = (receipt_root / "paper-chemcrow-01-baseline.receipt.json").read_text()
    assert "DO_NOT_PERSIST_THIS_KEY" not in receipt_text
    assert "PRIVATE PROMPT" not in receipt_text
    receipt = json.loads(receipt_text)
    assert receipt["provider"] == "OpenAI"
    assert receipt["list_price_cost_usd"] == 0.006


@pytest.mark.asyncio
async def test_openrouter_shim_seals_sanitized_upstream_failure_metadata(tmp_path):
    sensitive_message = "No endpoints available under data policy; PRIVATE ROUTING DETAIL"

    async def upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": 403,
                    "message": sensitive_message,
                    "metadata": {
                        "openrouter_metadata": {
                            "pipeline": [{"stage": "PRIVATE GUARDRAIL NAME"}]
                        }
                    },
                }
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
    assert receipt["upstream_error_message_sha256"] == canonical_sha256(sensitive_message)
    assert receipt["upstream_response_body_included"] is False
    assert receipt["openrouter_metadata_body_included"] is False


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
