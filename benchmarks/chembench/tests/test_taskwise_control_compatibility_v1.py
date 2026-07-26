from __future__ import annotations

import json
from pathlib import Path
import stat

import pytest

from openevo_chembench.taskwise_control_compatibility_v1 import (
    ControlRunCompatibilityReceiptV1,
    write_control_compatibility_receipt_v1,
)


def _payload(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "status": "COMPATIBLE",
        "historical_source_commit": "1" * 40,
        "current_source_commit": "2" * 40,
        "historical_generation_id_sha256": "3" * 64,
        "historical_attempt_id_sha256": "4" * 64,
        "control_task_manifest_identical": True,
        "stream_order_identical": True,
        "model_reasoning_codex_identical": True,
        "prompt_parser_evaluator_identical": True,
        "three_sessions_per_task_identical": True,
        "executor_success_semantics_proven_identical": True,
        "control_validator_absent": True,
        "control_evolution_absent": True,
        "predictions_replay_identical": True,
        "historical_run_complete": True,
        "result_digest_chain_closed": True,
        "source_diff_allowlisted": True,
        "completed_streams": 10,
        "completed_tasks": 500,
        "completed_sessions": 1500,
        "success_receipt_count": 1500,
        "infrastructure_failures": 0,
        "security_violations": 0,
        "context_binding_violations": 0,
        "contract_violations": 0,
        "changed_file_count": 3,
        "disallowed_changed_file_count": 0,
        "public_chain_set_sha256": "5" * 64,
        "private_chain_set_sha256": "6" * 64,
        "result_evidence_sha256": "7" * 64,
        "source_diff_sha256": "8" * 64,
        "allowlist_sha256": "9" * 64,
        "finding_codes": (),
    }
    value.update(overrides)
    return value


def test_compatibility_requires_all_twelve_direct_predicates() -> None:
    receipt = ControlRunCompatibilityReceiptV1.model_validate(_payload())
    assert receipt.status == "COMPATIBLE"
    assert len(receipt.digest) == 64

    incompatible = ControlRunCompatibilityReceiptV1.model_validate(
        _payload(
            status="INCOMPATIBLE",
            executor_success_semantics_proven_identical=False,
            finding_codes=("CONTROL_EXECUTOR_SEMANTICS_UNPROVEN",),
        )
    )
    assert incompatible.status == "INCOMPATIBLE"

    with pytest.raises(ValueError, match="status disagrees"):
        ControlRunCompatibilityReceiptV1.model_validate(
            _payload(executor_success_semantics_proven_identical=False)
        )


def test_compatibility_receipt_is_content_free_and_exclusive(tmp_path: Path) -> None:
    receipt = ControlRunCompatibilityReceiptV1.model_validate(_payload())
    target = tmp_path / "private" / "control_compatibility_v1.json"
    write_control_compatibility_receipt_v1(target, receipt)

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["status"] == "COMPATIBLE"
    assert not any(
        key in json.dumps(payload, sort_keys=True).casefold()
        for key in ("raw_completion", "target", "question", "option", "feedback")
    )
    with pytest.raises(FileExistsError):
        write_control_compatibility_receipt_v1(target, receipt)
