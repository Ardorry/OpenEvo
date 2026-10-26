from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from openevo_chemcrow.openrouter_delta_debug import (
    DELTA_DEBUG_AUTHORIZATION,
    build_delta_payload,
    run_delta_debug,
)


def _probe(path):
    path.write_text(
        json.dumps(
            {
                "status": "VALID",
                "auth_valid": True,
                "credit_probe_success": True,
                "model_calls": 0,
            }
        ),
        encoding="utf-8",
    )


def test_known_good_shape_is_exact_and_minimal():
    variant, payload = build_delta_payload("known_good_a")
    assert variant.max_tokens == 4
    assert payload == {
        "model": "openai/gpt-4",
        "temperature": 0.1,
        "max_tokens": 4,
        "stream": False,
        "messages": [{"role": "user", "content": "Reply with exactly OK."}],
        "provider": {
            "only": ["openai"],
            "allow_fallbacks": False,
            "require_parameters": True,
            "data_collection": "allow",
        },
    }


def test_proxy_variant_changes_only_transport_policy():
    direct_variant, direct_payload = build_delta_payload("known_good_a")
    proxy_variant, proxy_payload = build_delta_payload("known_good_a_proxy")
    assert direct_payload == proxy_payload
    assert direct_variant.use_environment_proxy is False
    assert proxy_variant.use_environment_proxy is True
    assert direct_variant.call_id != proxy_variant.call_id


def test_evaluator_variant_matches_shim_upstream_contract():
    variant, payload = build_delta_payload("evaluator_smoke_actual_proxy")
    assert variant.use_environment_proxy is True
    assert variant.response_contract == "dual_student_assessment"
    assert payload["max_tokens"] == 1200
    assert [message["role"] for message in payload["messages"]] == ["system", "user"]
    assert "user" not in payload
    assert "response_format" not in payload


def test_delta_debug_captures_actual_bytes_without_secret_or_text(monkeypatch, tmp_path):
    observed = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        observed["body"] = bytes(request.content)
        return httpx.Response(
            200,
            json={
                "id": "gen-test",
                "model": "openai/gpt-4",
                "provider": "OpenAI",
                "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 2, "cost": 0.00048},
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
                                "selected": True,
                            }
                        ],
                    },
                },
            },
        )

    probe = tmp_path / "probe.json"
    _probe(probe)
    monkeypatch.setenv("CHEMCROW_OPENROUTER_DELTA_AUTHORIZATION", DELTA_DEBUG_AUTHORIZATION)
    monkeypatch.setenv("CHEMCROW_OPENROUTER_DELTA_MAX_USD", "3")
    monkeypatch.setenv("CHEMCROW_PAPER_EVALUATOR_MODEL", "openai/gpt-4")
    result = run_delta_debug(
        variant_name="known_good_a",
        api_key="DO_NOT_PERSIST",
        base_url="http://mock.invalid/v1",
        receipt_root=tmp_path / "receipts",
        credential_probe_path=probe,
        allow_paid=True,
        transport=httpx.MockTransport(upstream),
    )
    descriptor = result["request_descriptor"]
    assert result["status"] == "PASS"
    assert result["openrouter_attempt"] == 1
    assert result["openrouter_endpoints"][0]["selected"] is True
    assert descriptor["request_body_sha256"] == hashlib.sha256(observed["body"]).hexdigest()
    assert descriptor["message_roles"] == ["user"]
    assert descriptor["response_format_present"] is False
    combined = "\n".join(path.read_text() for path in (tmp_path / "receipts").iterdir())
    assert "DO_NOT_PERSIST" not in combined
    assert "Reply with exactly OK" not in combined


def test_delta_debug_never_retries_claimed_variant(monkeypatch, tmp_path):
    calls = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, json={"error": {"code": 403, "message": "blocked"}})

    probe = tmp_path / "probe.json"
    _probe(probe)
    monkeypatch.setenv("CHEMCROW_OPENROUTER_DELTA_AUTHORIZATION", DELTA_DEBUG_AUTHORIZATION)
    monkeypatch.setenv("CHEMCROW_OPENROUTER_DELTA_MAX_USD", "3")
    monkeypatch.setenv("CHEMCROW_PAPER_EVALUATOR_MODEL", "openai/gpt-4")
    kwargs = {
        "variant_name": "known_good_a",
        "api_key": "secret",
        "base_url": "http://mock.invalid/v1",
        "receipt_root": tmp_path / "receipts",
        "credential_probe_path": probe,
        "allow_paid": True,
        "transport": httpx.MockTransport(upstream),
    }
    first = run_delta_debug(**kwargs)
    assert first["status"] == "FAIL_CLOSED"
    with pytest.raises(RuntimeError, match="already attempted"):
        run_delta_debug(**kwargs)
    assert calls == 1
