from __future__ import annotations

import json

import httpx

from openevo_chemcrow.credentials import (
    CONTROL_ENVIRONMENT_NAMES,
    PAPER_EVALUATOR_ENVIRONMENT_NAMES,
    credential_report,
    probe_openrouter_key,
)


def test_credential_report_is_value_free_and_ready_for_local_profile(
    monkeypatch, tmp_path
):
    sentinel = "DO_NOT_LEAK_THIS_SECRET_VALUE"
    for name in CONTROL_ENVIRONMENT_NAMES:
        monkeypatch.setenv(name, "configured-control")
    monkeypatch.setenv("SERP_API_KEY", sentinel)
    monkeypatch.setenv("RXN4CHEM_API_KEY", sentinel)
    monkeypatch.setenv("RXN4CHEM_PROJECT_ID", "configured-project")
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}", encoding="utf-8")

    report = credential_report(codex_auth_file=auth_file)

    assert report["status"] == "READY_REDUCED_PROFILE"
    assert report["secret_values_included"] is False
    assert report["provider_modes"]["local_rxn_takes_precedence"] is True
    assert report["core_model_authentication"]["openai_api_key_required"] is False
    assert sentinel not in json.dumps(report)


def test_credential_report_marks_excluded_keys_and_missing_controls(monkeypatch, tmp_path):
    for name in CONTROL_ENVIRONMENT_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "legacy-secret")

    report = credential_report(codex_auth_file=tmp_path / "missing-auth.json")

    assert report["status"] == "BLOCKED"
    assert set(report["missing_required_control_names"]) == set(
        CONTROL_ENVIRONMENT_NAMES
    )
    assert report["unexpected_legacy_or_excluded_key_names"] == ["OPENAI_API_KEY"]


def test_core_model_readiness_does_not_depend_on_rxn_controls(monkeypatch, tmp_path):
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}", encoding="utf-8")
    for name in CONTROL_ENVIRONMENT_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name in CONTROL_ENVIRONMENT_NAMES[:5]:
        monkeypatch.setenv(name, "configured")

    report = credential_report(codex_auth_file=auth_file)

    assert report["capability_status"]["core_candidate_reflector_evaluators"] == "ready"
    assert report["status"] == "BLOCKED"


def test_paper_evaluator_presence_never_reports_values(monkeypatch, tmp_path):
    for name in PAPER_EVALUATOR_ENVIRONMENT_NAMES:
        monkeypatch.setenv(name, f"secret-{name}")

    report = credential_report(codex_auth_file=tmp_path / "absent")

    assert report["capability_status"]["paper_evaluator_openrouter"] == (
        "configured_not_authorized_or_tested"
    )
    assert report["missing_paper_evaluator_names"] == []
    assert "secret-OPENROUTER_API_KEY" not in json.dumps(report)


def test_openrouter_key_probe_is_zero_model_and_value_free():
    sentinel = "DO_NOT_LEAK_KEY_OR_LABEL"

    def upstream(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {sentinel}"
        return httpx.Response(
            200,
            json={
                "data": {
                    "label": sentinel,
                    "limit": 20,
                    "limit_remaining": 15,
                    "usage": 5,
                    "expires_at": None,
                    "is_free_tier": False,
                    "is_management_key": False,
                }
            },
        )

    report = probe_openrouter_key(
        api_key=sentinel,
        base_url="http://mock.invalid/v1",
        transport=httpx.MockTransport(upstream),
    )

    assert report["status"] == "VALID"
    assert report["model_calls"] == report["paid_operations"] == 0
    assert report["sufficient_remaining_for_frozen_ceiling"] is True
    assert sentinel not in json.dumps(report)


def test_openrouter_key_probe_rejects_unauthorized_key_without_body_leak():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(401, json={"error": "DO_NOT_LEAK_BODY"})
    )
    report = probe_openrouter_key(
        api_key="invalid",
        base_url="http://mock.invalid/v1",
        transport=transport,
    )

    assert report["status"] == "INVALID_OR_UNAUTHORIZED"
    assert "DO_NOT_LEAK_BODY" not in json.dumps(report)
