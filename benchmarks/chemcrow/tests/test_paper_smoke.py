from __future__ import annotations

import json

import pytest

from openevo_chemcrow.paper_evaluator import DualStudentAssessment
from openevo_chemcrow.paper_smoke import (
    PAPER_SMOKE_AUTHORIZATION,
    PAPER_SMOKE_CALL_ID,
    build_smoke_call,
    run_paid_smoke,
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
        "data_collection": "deny",
        "temperature": 0.1,
        "upstream_http_status": 200,
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "list_price_cost_usd": 0.006,
        "openrouter_reported_cost_usd": 0.006,
        "completed_at": "2026-08-25T00:00:00+00:00",
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
