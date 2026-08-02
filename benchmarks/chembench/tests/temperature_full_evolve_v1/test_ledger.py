from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes
from openevo_chembench.temperature_full_evolve_v1.ledger import (
    REFLECTOR_ATTEMPT_RETRY_EXHAUSTED,
    TemperatureExperimentLedgerV1,
    TemperatureLedgerError,
    read_validated_ledger_progress_v1,
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
        "retry_semantics_sha256": "9" * 64,
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


def _no_completion(
    *,
    call_id: str = "call-00000001",
    logical_call_id: str = "logical-00000001",
) -> dict[str, object]:
    return {
        "logical_call_id": logical_call_id,
        "call_id": call_id,
        "failure_class": "infrastructure_no_completion",
        "terminal_task_status": "failed",
        "durable_rollout_no_completion": True,
        "durable_gateway_absent": True,
        "no_completion_evidence_sha256": "f" * 64,
    }


def _reflector_claim(
    *,
    attempt: int = 1,
    call_id: str = "reflector-call-00000001",
    logical_call_id: str = "logical-00000001",
) -> dict[str, object]:
    return {
        **_claim(attempt=attempt, call_id=call_id),
        "logical_call_id": logical_call_id,
        "phase": "train_reflector",
        "logical_arm": "batch_supervised_reflector",
    }


def _rejected(
    *,
    call_id: str = "reflector-call-00000001",
    logical_call_id: str = "logical-00000001",
) -> dict[str, object]:
    return {
        "logical_call_id": logical_call_id,
        "call_id": call_id,
        "rejection_code": "REFLECTOR_RESPONSE_SCHEMA_OR_EVIDENCE_INVALID",
        "response_sha256": "1" * 64,
        "task_result_sha256": "2" * 64,
        "transcript_sha256": "3" * 64,
        "completion_identity_sha256": "4" * 64,
        "durable_completion": True,
        "tool_event_count": 0,
        "tool_policy_validated": True,
    }


def _reflector_exhausted_failure(*, rejected: int, no_completion: int) -> dict[str, object]:
    return {
        "failure_code": REFLECTOR_ATTEMPT_RETRY_EXHAUSTED,
        "phase": "train_reflector",
        "batch_index": 1,
        "attempt_count": 3,
        "rejected_completion_attempt_count": rejected,
        "no_completion_attempt_count": no_completion,
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
        changed_semantics = _claim(attempt=2, call_id="call-00000002")
        changed_semantics["retry_semantics_sha256"] = "8" * 64
        with pytest.raises(TemperatureLedgerError, match="LEDGER_CALL_RETRY_INVALID"):
            ledger.append("CALL_CLAIMED", changed_semantics)
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


def test_only_reflector_allowlisted_rejection_closes_attempt_and_allows_one_successor(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "rejected-ledger.jsonl").resolve()
    with TemperatureExperimentLedgerV1(path=path, run_id="formal-run-rejected-0001") as ledger:
        ledger.append("CALL_CLAIMED", _reflector_claim())
        ledger.append("CALL_REJECTED_COMPLETION", _rejected())
        assert ledger.completion_was_rejected("reflector-call-00000001")
        progress = read_validated_ledger_progress_v1(
            path=path,
            run_id="formal-run-rejected-0001",
        )
        assert progress["open_claim_count"] == 0
        assert progress["kind_counts"] == {
            "CALL_CLAIMED": 1,
            "CALL_REJECTED_COMPLETION": 1,
        }
        with pytest.raises(
            TemperatureLedgerError,
            match="LEDGER_REJECTED_COMPLETION_INVALID",
        ):
            ledger.append("CALL_REJECTED_COMPLETION", _rejected())
        ledger.append(
            "CALL_CLAIMED",
            _reflector_claim(attempt=2, call_id="reflector-call-00000002"),
        )
        ledger.append(
            "CALL_ACCEPTED",
            _accepted(call_id="reflector-call-00000002"),
        )

    with TemperatureExperimentLedgerV1(
        path=path,
        run_id="formal-run-rejected-0001",
    ) as recovered:
        assert recovered.completion_was_rejected("reflector-call-00000001")
        assert recovered.accepted_call("logical-00000001") is not None


def test_rejected_completion_event_rejects_candidate_and_unknown_codes(
    tmp_path: Path,
) -> None:
    candidate_path = (tmp_path / "candidate-ledger.jsonl").resolve()
    with TemperatureExperimentLedgerV1(
        path=candidate_path,
        run_id="formal-run-rejected-0002",
    ) as ledger:
        ledger.append("CALL_CLAIMED", _claim())
        payload = _rejected(call_id="call-00000001")
        with pytest.raises(
            TemperatureLedgerError,
            match="LEDGER_REJECTED_COMPLETION_INVALID",
        ):
            ledger.append("CALL_REJECTED_COMPLETION", payload)

    reflector_path = (tmp_path / "reflector-ledger.jsonl").resolve()
    with TemperatureExperimentLedgerV1(
        path=reflector_path,
        run_id="formal-run-rejected-0003",
    ) as ledger:
        ledger.append("CALL_CLAIMED", _reflector_claim())
        payload = _rejected()
        payload["rejection_code"] = "REFLECTOR_TRANSCRIPT_INVALID"
        with pytest.raises(
            TemperatureLedgerError,
            match="LEDGER_REJECTED_COMPLETION_INVALID",
        ):
            ledger.append("CALL_REJECTED_COMPLETION", payload)


def test_three_mixed_reflector_failures_seal_one_terminal_run_failure(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "reflector-exhausted-ledger.jsonl").resolve()
    run_id = "formal-run-reflector-exhausted-0001"
    logical = "temperature-reflector-exhausted-b01"
    with TemperatureExperimentLedgerV1(path=path, run_id=run_id) as ledger:
        for attempt in range(1, 4):
            call_id = f"{logical}-a{attempt:02d}"
            ledger.append(
                "CALL_CLAIMED",
                _reflector_claim(
                    attempt=attempt,
                    call_id=call_id,
                    logical_call_id=logical,
                ),
            )
            if attempt == 2:
                ledger.append(
                    "CALL_NO_COMPLETION_FAILURE",
                    _no_completion(call_id=call_id, logical_call_id=logical),
                )
            else:
                ledger.append(
                    "CALL_REJECTED_COMPLETION",
                    _rejected(call_id=call_id, logical_call_id=logical),
                )
        terminal = _reflector_exhausted_failure(rejected=2, no_completion=1)
        ledger.append("RUN_FAILED", terminal)
        with pytest.raises(TemperatureLedgerError, match="EVENT_AFTER_RUN_FAILED"):
            ledger.append("RUN_FAILED", terminal)

    with TemperatureExperimentLedgerV1(path=path, run_id=run_id) as recovered:
        assert recovered.events[-1]["kind"] == "RUN_FAILED"
        assert recovered.events[-1]["payload"] == terminal
        assert not any(
            event["kind"] == "CALL_CLAIMED"
            and event["payload"]["attempt_number"] == 4
            for event in recovered.events
        )


def test_reflector_terminal_failure_requires_three_closed_attempts(tmp_path: Path) -> None:
    logical = "temperature-reflector-not-exhausted-b01"
    with TemperatureExperimentLedgerV1(
        path=(tmp_path / "not-exhausted.jsonl").resolve(),
        run_id="formal-run-reflector-not-exhausted-0001",
    ) as ledger:
        claim = _reflector_claim(
            call_id=f"{logical}-a01",
            logical_call_id=logical,
        )
        ledger.append("CALL_CLAIMED", claim)
        ledger.append(
            "CALL_REJECTED_COMPLETION",
            _rejected(
                call_id=str(claim["call_id"]),
                logical_call_id=logical,
            ),
        )
        with pytest.raises(TemperatureLedgerError, match="RUN_FAILED_INVALID"):
            ledger.append(
                "RUN_FAILED",
                _reflector_exhausted_failure(rejected=1, no_completion=0),
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


def test_read_only_progress_validates_live_writer_without_projecting_payloads(
    tmp_path: Path,
) -> None:
    path = (tmp_path / "events.jsonl").resolve()
    run_id = "formal-run-aggregate-0001"
    with TemperatureExperimentLedgerV1(path=path, run_id=run_id) as ledger:
        ledger.append("CALL_CLAIMED", _claim())
        progress = read_validated_ledger_progress_v1(path=path, run_id=run_id)

    assert progress == {
        "schema_version": "TemperatureFullEvolveLedgerProgressV1",
        "sequence": 1,
        "head_sha256": progress["head_sha256"],
        "kind_counts": {"CALL_CLAIMED": 1},
        "open_claim_count": 1,
    }
    encoded = json.dumps(progress, sort_keys=True)
    assert "logical-00000001" not in encoded
    assert "call-00000001" not in encoded
    assert "task-00000001" not in encoded

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["event_sha256"] = "0" * 64
    path.write_bytes(canonical_json_bytes(payload))
    with pytest.raises(TemperatureLedgerError, match="LEDGER_CHAIN_INVALID"):
        read_validated_ledger_progress_v1(path=path, run_id=run_id)
