from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes
from openevo_chembench.temperature_full_evolve_v1.ledger import (
    TemperatureExperimentLedgerV1,
    TemperatureLedgerError,
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _claim(*, attempt: int = 1, call_id: str = "call-00000001") -> dict[str, object]:
    return {
        "logical_call_id": "logical-00000001",
        "call_id": call_id,
        "task_id": f"task-{attempt:08d}",
        "phase": "train-pre",
        "logical_arm": "candidate",
        "attempt_number": attempt,
        "task_request_sha256": "a" * 64,
        "service_identity_sha256": "b" * 64,
    }


def _accepted(*, call_id: str = "call-00000001", response: str = "A") -> dict[str, object]:
    return {
        "logical_call_id": "logical-00000001",
        "call_id": call_id,
        "response": response,
        "response_sha256": _digest(response),
        "task_result_sha256": "c" * 64,
        "transcript_sha256": "d" * 64,
        "completion_identity_sha256": "e" * 64,
        "tool_event_count": 0,
        "tool_policy_validated": True,
    }


def _no_completion(*, call_id: str = "call-00000001") -> dict[str, object]:
    return {
        "logical_call_id": "logical-00000001",
        "call_id": call_id,
        "failure_class": "infrastructure_no_completion",
        "terminal_task_status": "failed",
        "durable_rollout_no_completion": True,
        "durable_gateway_absent": True,
        "no_completion_evidence_sha256": "f" * 64,
    }


def test_ledger_persists_one_accepted_completion_and_evaluation(tmp_path: Path) -> None:
    path = (tmp_path / "private" / "ledger.jsonl").resolve()
    evaluation = canonical_json_bytes(
        {"correct": True, "parsed_prediction": "A", "parser_success": True}
    ).decode("utf-8")
    with TemperatureExperimentLedgerV1(path=path, run_id="formal-run-0001") as ledger:
        ledger.append("CALL_CLAIMED", _claim())
        ledger.append("CALL_ACCEPTED", _accepted())
        ledger.append(
            "CALL_EVALUATED",
            {
                "logical_call_id": "logical-00000001",
                "evaluation_json": evaluation,
                "evaluation_sha256": _digest(evaluation),
            },
        )
        assert ledger.accepted_call("logical-00000001") == _accepted()

    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    with TemperatureExperimentLedgerV1(path=path, run_id="formal-run-0001") as recovered:
        assert len(recovered.events) == 3
        assert recovered.accepted_call("logical-00000001") == _accepted()


def test_retry_requires_dual_durable_no_completion_evidence(tmp_path: Path) -> None:
    path = (tmp_path / "ledger.jsonl").resolve()
    with TemperatureExperimentLedgerV1(path=path, run_id="formal-run-0002") as ledger:
        ledger.append("CALL_CLAIMED", _claim())
        with pytest.raises(TemperatureLedgerError, match="LEDGER_CALL_RETRY_INVALID"):
            ledger.append("CALL_CLAIMED", _claim(attempt=2, call_id="call-00000002"))
        bad = _no_completion()
        bad["durable_gateway_absent"] = False
        with pytest.raises(
            TemperatureLedgerError,
            match="LEDGER_NO_COMPLETION_FAILURE_INVALID",
        ):
            ledger.append("CALL_NO_COMPLETION_FAILURE", bad)
        ledger.append("CALL_NO_COMPLETION_FAILURE", _no_completion())
        ledger.append("CALL_CLAIMED", _claim(attempt=2, call_id="call-00000002"))
        ledger.append("CALL_ACCEPTED", _accepted(call_id="call-00000002"))
        with pytest.raises(TemperatureLedgerError, match="LEDGER_DUPLICATE_CALL_CLAIM"):
            ledger.append("CALL_CLAIMED", _claim(attempt=3, call_id="call-00000003"))


def test_tool_event_or_noncanonical_evaluation_is_rejected(tmp_path: Path) -> None:
    path = (tmp_path / "ledger.jsonl").resolve()
    with TemperatureExperimentLedgerV1(path=path, run_id="formal-run-0003") as ledger:
        ledger.append("CALL_CLAIMED", _claim())
        accepted = _accepted()
        accepted["tool_event_count"] = 1
        with pytest.raises(TemperatureLedgerError, match="LEDGER_ACCEPTED_CALL_INVALID"):
            ledger.append("CALL_ACCEPTED", accepted)
        ledger.append("CALL_ACCEPTED", _accepted())
        evaluation = json.dumps({"parser_success": True})
        with pytest.raises(TemperatureLedgerError, match="LEDGER_CALL_EVALUATION_INVALID"):
            ledger.append(
                "CALL_EVALUATED",
                {
                    "logical_call_id": "logical-00000001",
                    "evaluation_json": evaluation,
                    "evaluation_sha256": _digest(evaluation),
                },
            )


def test_second_writer_and_partial_or_tampered_recovery_fail_closed(tmp_path: Path) -> None:
    path = (tmp_path / "ledger.jsonl").resolve()
    ledger = TemperatureExperimentLedgerV1(path=path, run_id="formal-run-0004")
    try:
        with pytest.raises(TemperatureLedgerError, match="LEDGER_WRITER_ALREADY_ACTIVE"):
            TemperatureExperimentLedgerV1(path=path, run_id="formal-run-0004")
        ledger.append("CALL_CLAIMED", _claim())
    finally:
        ledger.close()

    with path.open("ab") as stream:
        stream.write(b"partial")
        stream.flush()
        os.fsync(stream.fileno())
    with pytest.raises(TemperatureLedgerError, match="LEDGER_PARTIAL_RECORD"):
        TemperatureExperimentLedgerV1(path=path, run_id="formal-run-0004")


def test_duplicate_json_keys_in_recovered_ledger_fail_closed(tmp_path: Path) -> None:
    path = (tmp_path / "ledger.jsonl").resolve()
    path.write_text('{"a":1,"a":2}\n', encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(TemperatureLedgerError, match="LEDGER_RECORD_MALFORMED"):
        TemperatureExperimentLedgerV1(path=path, run_id="formal-run-0005")


def test_ledger_accepts_protocol_utc_run_id(tmp_path: Path) -> None:
    run_id = "stv3-temperature-full-evolve-v1-20260802T120000Z"
    with TemperatureExperimentLedgerV1(
        path=(tmp_path / "events.jsonl").resolve(),
        run_id=run_id,
    ):
        pass
