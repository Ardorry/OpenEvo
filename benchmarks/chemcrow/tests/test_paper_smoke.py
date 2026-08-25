from __future__ import annotations

import json

import httpx
import pytest

from openevo_chemcrow.paper_direct_debug import (
    DIRECT_DEBUG_AUTHORIZATION,
    run_direct_debug,
)
from openevo_chemcrow.paper_evaluator import DualStudentAssessment
from openevo_chemcrow.paper_smoke import (
    PAPER_SMOKE_AUTHORIZATION,
    PAPER_SMOKE_CALL_ID,
    build_smoke_call,
    main,
    run_paid_smoke,
    seal_unreached_smoke_infrastructure_failure,
)


def _assessment() -> DualStudentAssessment:
    return DualStudentAssessment.model_validate(
        {
            "student_a": {
                "grade": 10,
                "strengths": ["correct"],
                "weaknesses": [],
                "justification": "correct",
                "feedback": [],
            },
            "student_b": {
                "grade": 10,
                "strengths": ["correct"],
                "weaknesses": [],
                "justification": "correct",
                "feedback": [],
            },
        }
    )


def test_smoke_call_is_separate_from_formal_42_call_inventory():
    call = build_smoke_call()
    assert call.call_id == PAPER_SMOKE_CALL_ID
    assert call.task_id == "chemcrow-smoke-not-a-benchmark-task"
    assert call.call_id not in {
        f"paper-chemcrow-{number:02d}-{comparison}"
        for number in range(1, 16)
        for comparison in ("historical_control", "baseline", "evolved")
    }


@pytest.mark.parametrize(
    "version", ("v1", "v2", "v3", "v4", "v5", "v6", "v7", "v8")
)
def test_historical_smoke_config_cannot_launch_v9_call(tmp_path, version):
    config = tmp_path / f"{version}.json"
    config.write_text(
        json.dumps({"schema_version": f"chemcrow_paper_evaluator_smoke_config_{version}"}),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="not the frozen v9 schema"):
        main(
            [
                "run",
                "--config",
                str(config),
                "--credential-probe",
                str(tmp_path / "absent-probe.json"),
                "--output",
                str(tmp_path / "absent-output.json"),
            ]
        )


def test_paid_smoke_seals_hash_only_report_and_removes_raw_completion(monkeypatch, tmp_path):
    receipt_root = tmp_path / "receipts"
    receipt_root.mkdir()
    completion_root = tmp_path / "completions"
    completion = completion_root / f"task_{PAPER_SMOKE_CALL_ID}" / "result.json"
    completion.parent.mkdir(parents=True)
    completion.write_text('{"private":"CORE BODY MUST NOT REMAIN"}\n', encoding="utf-8")
    probe = tmp_path / "probe.json"
    probe.write_text(
        json.dumps(
            {
                "status": "VALID",
                "auth_valid": True,
                "credit_probe_success": True,
                "sufficient_remaining_for_frozen_ceiling": True,
                "model_calls": 0,
            }
        ),
        encoding="utf-8",
    )
    receipt = {
        "status": "terminal_success",
        "call_id": PAPER_SMOKE_CALL_ID,
        "model": "openai/gpt-4",
        "provider": "OpenAI",
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "allow",
        "temperature": 0.1,
        "upstream_http_status": 200,
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "list_price_cost_usd": 0.006,
        "openrouter_reported_cost_usd": 0.006,
        "completed_at": "2026-08-25T00:00:00+00:00",
        "internal_response_format_validated": True,
        "upstream_response_format_omitted": True,
        "internal_call_identity_validated": True,
        "upstream_user_omitted": True,
    }

    monkeypatch.setenv("CHEMCROW_PAPER_SMOKE_AUTHORIZATION", PAPER_SMOKE_AUTHORIZATION)
    monkeypatch.setenv("CHEMCROW_PAPER_SMOKE_MAX_USD", "1")
    monkeypatch.setenv("CHEMCROW_PAPER_EVALUATOR_MODEL", "openai/gpt-4")
    monkeypatch.setattr("openevo_chemcrow.paper_smoke._assert_dedicated_core_node", lambda _: None)

    def submit_once(*args, **kwargs):
        (receipt_root / f"{PAPER_SMOKE_CALL_ID}.claim.json").write_text("{}\n", encoding="utf-8")
        (receipt_root / f"{PAPER_SMOKE_CALL_ID}.receipt.json").write_text(
            json.dumps(receipt), encoding="utf-8"
        )
        return {"terminal": True}

    monkeypatch.setattr("openevo_chemcrow.paper_smoke._submit_once_and_poll", submit_once)
    monkeypatch.setattr(
        "openevo_chemcrow.paper_smoke._assessment_from_core_status",
        lambda _: _assessment(),
    )
    output = tmp_path / "smoke.json"
    result = run_paid_smoke(
        rollout_base_url="http://127.0.0.1:8180",
        runtime={"backend": "docker"},
        receipt_root=receipt_root,
        core_completion_root=completion_root,
        credential_probe_path=probe,
        output_path=output,
        allow_paid=True,
    )
    assert result["status"] == "PASS"
    assert result["paid_model_calls"] == 1
    assert result["included_in_formal_42_call_ledger"] is False
    assert result["raw_core_completion_retained"] is False
    assert result["json_parse_valid"] is True
    assert result["pydantic_assessment_valid"] is True
    assert result["internal_response_format_validated"] is True
    assert result["upstream_response_format_omitted"] is True
    assert result["internal_call_identity_validated"] is True
    assert result["upstream_user_omitted"] is True
    assert not completion.exists()
    text = output.read_text(encoding="utf-8")
    assert "CORE BODY MUST NOT REMAIN" not in text
    assert "Water has molecular formula" not in text
    with pytest.raises(RuntimeError, match="already attempted"):
        run_paid_smoke(
            rollout_base_url="http://127.0.0.1:8180",
            runtime={"backend": "docker"},
            receipt_root=receipt_root,
            core_completion_root=completion_root,
            credential_probe_path=probe,
            output_path=output,
            allow_paid=True,
        )


def test_paid_smoke_seals_terminal_upstream_failure_without_assessment(monkeypatch, tmp_path):
    receipt_root = tmp_path / "receipts"
    receipt_root.mkdir()
    completion_root = tmp_path / "completions"
    completion = completion_root / f"task_{PAPER_SMOKE_CALL_ID}" / "result.json"
    completion.parent.mkdir(parents=True)
    completion.write_text(
        json.dumps(
            {
                "status": "ERROR",
                "error": "post-run failed: subscription transcript could not be read safely",
                "trajectory": "PRIVATE CORE FAILURE BODY",
            }
        ),
        encoding="utf-8",
    )
    probe = tmp_path / "probe.json"
    probe.write_text(
        json.dumps(
            {
                "status": "VALID",
                "auth_valid": True,
                "credit_probe_success": True,
                "sufficient_remaining_for_frozen_ceiling": True,
                "model_calls": 0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("CHEMCROW_PAPER_SMOKE_AUTHORIZATION", PAPER_SMOKE_AUTHORIZATION)
    monkeypatch.setenv("CHEMCROW_PAPER_SMOKE_MAX_USD", "1")
    monkeypatch.setenv("CHEMCROW_PAPER_EVALUATOR_MODEL", "openai/gpt-4")
    monkeypatch.setattr("openevo_chemcrow.paper_smoke._assert_dedicated_core_node", lambda _: None)

    def submit_once(*args, **kwargs):
        (receipt_root / f"{PAPER_SMOKE_CALL_ID}.claim.json").write_text(
            json.dumps(
                {
                    "call_id": PAPER_SMOKE_CALL_ID,
                    "request_sha256": "a" * 64,
                }
            ),
            encoding="utf-8",
        )
        (receipt_root / f"{PAPER_SMOKE_CALL_ID}.receipt.json").write_text(
            json.dumps(
                {
                    "status": "terminal_failure_or_ambiguous",
                    "call_id": PAPER_SMOKE_CALL_ID,
                    "request_sha256": "a" * 64,
                    "upstream_http_status": 403,
                    "upstream_error_code": 403,
                    "upstream_error_category": "other_upstream_error",
                    "upstream_error_message_sha256": "b" * 64,
                    "upstream_response_sha256": "c" * 64,
                    "internal_response_format_validated": True,
                    "upstream_response_format_omitted": True,
                    "internal_call_identity_validated": True,
                    "upstream_user_omitted": True,
                }
            ),
            encoding="utf-8",
        )
        return {"terminal": True}

    monkeypatch.setattr("openevo_chemcrow.paper_smoke._submit_once_and_poll", submit_once)
    monkeypatch.setattr(
        "openevo_chemcrow.paper_smoke._assessment_from_core_status",
        lambda _: pytest.fail("assessment must not run after terminal upstream failure"),
    )
    output = tmp_path / "failed-smoke.json"
    result = run_paid_smoke(
        rollout_base_url="http://127.0.0.1:8180",
        runtime={"backend": "docker"},
        receipt_root=receipt_root,
        core_completion_root=completion_root,
        credential_probe_path=probe,
        output_path=output,
        allow_paid=True,
    )

    assert result["status"] == "FAIL_CLOSED"
    assert result["paid_model_calls_during_sealing"] == 0
    assert result["provider_request_attempts"] == 1
    assert result["successful_model_completions"] == 0
    assert result["raw_core_completion_retained"] is False
    assert not completion.exists()
    report_text = output.read_text(encoding="utf-8")
    assert "PRIVATE CORE FAILURE BODY" not in report_text


def test_paid_smoke_requires_separate_test_authorization(monkeypatch, tmp_path):
    monkeypatch.delenv("CHEMCROW_PAPER_SMOKE_AUTHORIZATION", raising=False)
    monkeypatch.setenv(
        "CHEMCROW_PAPER_EVALUATOR_AUTHORIZATION",
        "I_AUTHORIZE_42_SEALED_PAPER_EVALUATIONS",
    )
    with pytest.raises(PermissionError, match="test-only"):
        run_paid_smoke(
            rollout_base_url="http://127.0.0.1:8180",
            runtime={},
            receipt_root=tmp_path / "receipts",
            core_completion_root=tmp_path / "completions",
            credential_probe_path=tmp_path / "probe.json",
            output_path=tmp_path / "output.json",
            allow_paid=True,
        )


def test_unreached_infrastructure_failure_proves_zero_provider_attempts(tmp_path):
    receipt_root = tmp_path / "receipts"
    receipt_root.mkdir()
    completion_root = tmp_path / "completions"
    completion = completion_root / f"task_{PAPER_SMOKE_CALL_ID}" / "result.json"
    completion.parent.mkdir(parents=True)
    completion.write_text(
        json.dumps({"status": "ERROR", "error": "agent import failed"}),
        encoding="utf-8",
    )
    probe = tmp_path / "probe.json"
    probe.write_text('{"status":"VALID"}\n', encoding="utf-8")
    output = tmp_path / "infrastructure-failure.json"
    result = seal_unreached_smoke_infrastructure_failure(
        receipt_root=receipt_root,
        core_completion_root=completion_root,
        credential_probe_path=probe,
        output_path=output,
    )
    assert result["status"] == "FAIL_CLOSED_INFRASTRUCTURE_BEFORE_PROVIDER"
    assert result["provider_request_attempts"] == result["paid_model_calls"] == 0
    assert result["shim_claim_exists"] is result["shim_receipt_exists"] is False
    assert completion.exists()
    assert "agent import failed" not in output.read_text(encoding="utf-8")


def test_direct_debug_is_single_claimed_hash_only_nonformal_call(tmp_path, monkeypatch):
    observed = {}

    def upstream(request: httpx.Request) -> httpx.Response:
        observed["request"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "gen-direct-debug",
                "model": "openai/gpt-4",
                "provider": "OpenAI",
                "choices": [
                    {"message": {"role": "assistant", "content": '{"ok":true}'}}
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 5},
            },
        )

    probe = tmp_path / "probe.json"
    probe.write_text(
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
    monkeypatch.setenv(
        "CHEMCROW_PAPER_DIRECT_DEBUG_AUTHORIZATION", DIRECT_DEBUG_AUTHORIZATION
    )
    monkeypatch.setenv("CHEMCROW_PAPER_EVALUATOR_MODEL", "openai/gpt-4")
    monkeypatch.setenv("CHEMCROW_PAPER_SMOKE_MAX_USD", "1")
    output = tmp_path / "direct-report.json"
    result = run_direct_debug(
        api_key="DO_NOT_PERSIST",
        base_url="http://mock.invalid/v1",
        receipt_root=tmp_path / "receipts",
        credential_probe_path=probe,
        output_path=output,
        allow_paid=True,
        transport=httpx.MockTransport(upstream),
    )
    assert result["status"] == "PASS"
    assert result["included_in_formal_42_call_ledger"] is False
    assert observed["request"]["provider"]["only"] == ["openai"]
    assert observed["request"]["provider"]["require_parameters"] is True
    assert "response_format" not in observed["request"]
    assert "user" not in observed["request"]
    text = output.read_text(encoding="utf-8")
    assert "DO_NOT_PERSIST" not in text
    assert "Return exactly" not in text
