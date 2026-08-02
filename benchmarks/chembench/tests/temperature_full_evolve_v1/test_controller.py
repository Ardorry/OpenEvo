from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path

import pytest

from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes
from openevo_chembench.temperature_full_evolve_v1.controller import (
    STATUS_SCHEMA,
    ControllerActionKindV1,
    TemperatureControllerError,
    TemperatureExperimentControllerV1,
)
from openevo_chembench.temperature_full_evolve_v1.ledger import (
    TemperatureExperimentLedgerV1,
    TemperatureLedgerError,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _claim(index: int, *, phase: str = "train-pre", batch: int = 1) -> dict[str, object]:
    logical = f"temperature-controller-{phase}-b{batch:02d}-i{index:03d}"
    return {
        "logical_call_id": logical,
        "call_id": f"{logical}-a01",
        "task_id": f"chembench-{logical}-a01",
        "phase": phase,
        "logical_arm": "candidate",
        "attempt_number": 1,
        "task_request_sha256": _sha(f"request-{phase}-{batch}-{index}"),
        "service_identity_sha256": "1" * 64,
    }


def _accept(claim: dict[str, object]) -> dict[str, object]:
    response = "A"
    return {
        "logical_call_id": claim["logical_call_id"],
        "call_id": claim["call_id"],
        "response": response,
        "response_sha256": _sha(response),
        "task_result_sha256": "2" * 64,
        "transcript_sha256": "3" * 64,
        "completion_identity_sha256": "4" * 64,
        "tool_event_count": 0,
        "tool_policy_validated": True,
    }


def _evaluate(claim: dict[str, object]) -> dict[str, object]:
    evaluation_json = canonical_json_bytes(
        {"correct": True, "ordinal": claim["logical_call_id"]}
    ).decode()
    return {
        "logical_call_id": claim["logical_call_id"],
        "evaluation_json": evaluation_json,
        "evaluation_sha256": hashlib.sha256(evaluation_json.encode()).hexdigest(),
    }


def _close_candidate_phase(
    ledger: TemperatureExperimentLedgerV1,
    *,
    phase: str,
    batch: int,
    count: int = 25,
) -> None:
    for index in range(count):
        claim = _claim(index, phase=phase, batch=batch)
        ledger.append("CALL_CLAIMED", claim)
        ledger.append("CALL_ACCEPTED", _accept(claim))
        ledger.append("CALL_EVALUATED", _evaluate(claim))


def _run_created_payload() -> dict[str, object]:
    return {
        "generation_zero": True,
        "old_artifact_imported": False,
        "old_completion_imported": False,
        "old_database_imported": False,
    }


def _split_payload() -> dict[str, object]:
    return {
        "split_sha256": "a" * 64,
        "train_count": 100,
        "test_count": 100,
        "batch_size": 25,
        "test_sealed": True,
    }


def _frozen_context_payload() -> dict[str, object]:
    resolution = _sha("frozen-context-resolution")
    targets = [
        {
            "target_id": target,
            "core_artifact_id": f"artifact-{target}",
            "artifact_payload_sha256": _sha(f"payload-{target}"),
            "resolved_content_sha256": _sha(f"resolved-{target}"),
            "context_resolution_digest": resolution,
        }
        for target in ("text_memory", "skill_bundle", "agent_system")
    ]
    return {
        "frozen_context_targets": targets,
        "frozen_context_targets_sha256": hashlib.sha256(
            canonical_json_bytes(targets)
        ).hexdigest(),
    }


def _append_reflector_call(
    ledger: TemperatureExperimentLedgerV1,
    *,
    batch: int,
) -> str:
    logical = f"temperature-reflector-controller-b{batch:02d}"
    claim = {
        "logical_call_id": logical,
        "call_id": f"{logical}-a01",
        "task_id": f"chembench-{logical}-a01",
        "phase": "train_reflector",
        "logical_arm": "batch_supervised_reflector",
        "attempt_number": 1,
        "task_request_sha256": _sha(f"reflect-request-{batch}"),
        "service_identity_sha256": "1" * 64,
    }
    ledger.append("CALL_CLAIMED", claim)
    ledger.append("CALL_ACCEPTED", _accept(claim))
    return logical


def test_controller_requires_25_item_barrier_and_writes_closed_private_status(
    tmp_path: Path,
) -> None:
    run_id = "formal-run-3001"
    status_path = (tmp_path / "status" / "current.json").resolve()
    with TemperatureExperimentLedgerV1(
        path=(tmp_path / "ledger.jsonl").resolve(), run_id=run_id
    ) as ledger:
        controller = TemperatureExperimentControllerV1(
            run_id=run_id,
            ledger=ledger,
            status_path=status_path,
        )
        assert controller.action.kind is ControllerActionKindV1.CREATE_RUN
        controller.append_protocol_event("RUN_CREATED", _run_created_payload())
        controller.append_protocol_event("SPLIT_FROZEN", _split_payload())
        assert controller.action.kind is ControllerActionKindV1.RUN_TRAIN_PRE
        _close_candidate_phase(ledger, phase="train-pre", batch=1, count=24)
        assert controller.action.kind is ControllerActionKindV1.RUN_TRAIN_PRE
        claim = _claim(24, phase="train-pre", batch=1)
        ledger.append("CALL_CLAIMED", claim)
        ledger.append("CALL_ACCEPTED", _accept(claim))
        ledger.append("CALL_EVALUATED", _evaluate(claim))
        assert controller.action.kind is ControllerActionKindV1.CLOSE_TRAIN_PRE
        controller.append_protocol_event(
            "BATCH_PRE_CLOSED",
            {"batch_index": 1, "accepted_count": 25, "evaluated_count": 25},
        )
        assert controller.action.kind is ControllerActionKindV1.RUN_REFLECTOR
        status = controller.write_status()
        assert set(status) == {
            "schema_version",
            "run_id",
            "revision",
            "phase",
            "batch_index",
            "item_ordinal",
            "accepted_completions",
            "candidate_calls",
            "reflector_calls",
            "core_jobs",
            "active_lease",
            "staged_side_effects",
            "failed_side_effects",
            "last_progress_at_utc",
            "progress_counter",
            "runner_pid",
            "updated_at_utc",
        }
        assert status["schema_version"] == STATUS_SCHEMA
        assert status["candidate_calls"] == 25
        assert re.fullmatch(
            r"20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
            str(status["last_progress_at_utc"]),
        )
        assert stat.S_IMODE(status_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(status_path.parent.stat().st_mode) == 0o700
        assert json.loads(status_path.read_text()) == status


def test_unresolved_claim_preempts_new_work_and_formal_utc_run_id_is_allowed(
    tmp_path: Path,
) -> None:
    with TemperatureExperimentLedgerV1(
        path=(tmp_path / "ledger.jsonl").resolve(), run_id="formal-run-3002"
    ) as ledger:
        with pytest.raises(ValueError, match="invalid"):
            TemperatureExperimentControllerV1(
                run_id="formal run with spaces",
                ledger=ledger,
                status_path=(tmp_path / "status.json").resolve(),
            )
        controller = TemperatureExperimentControllerV1(
            run_id="formal-run-3002",
            ledger=ledger,
            status_path=(tmp_path / "status" / "current.json").resolve(),
        )
        controller.append_protocol_event("RUN_CREATED", _run_created_payload())
        controller.append_protocol_event("SPLIT_FROZEN", _split_payload())
        claim = _claim(0)
        ledger.append("CALL_CLAIMED", claim)
        action = controller.action
        assert action.kind is ControllerActionKindV1.RECOVER_UNRESOLVED_CALL
        assert action.unresolved_logical_call_ids == (claim["logical_call_id"],)
        with pytest.raises(TemperatureControllerError, match="BOUNDARY_NOT_READY"):
            controller.append_protocol_event("BATCH_PRE_CLOSED", {"batch_index": 1})

    utc_run = "stv3-temperature-full-evolve-v1-20260802T120000Z"
    with TemperatureExperimentLedgerV1(
        path=(tmp_path / "utc-ledger.jsonl").resolve(), run_id=utc_run
    ) as utc_ledger:
        controller = TemperatureExperimentControllerV1(
            run_id=utc_run,
            ledger=utc_ledger,
            status_path=(tmp_path / "utc-status" / "current.json").resolve(),
        )
        assert controller.action.kind is ControllerActionKindV1.CREATE_RUN


def test_test_freeze_rejects_late_reflector_or_core_semantics(tmp_path: Path) -> None:
    run_id = "formal-run-3003"
    with TemperatureExperimentLedgerV1(
        path=(tmp_path / "ledger.jsonl").resolve(), run_id=run_id
    ) as ledger:
        # Construct a content-free closed Train prefix directly.  Controller
        # startup validates the complete semantic ordering before Test work.
        ledger.append("RUN_CREATED", _run_created_payload())
        ledger.append("SPLIT_FROZEN", _split_payload())
        for batch in range(1, 5):
            _close_candidate_phase(ledger, phase="train-pre", batch=batch)
            ledger.append(
                "BATCH_PRE_CLOSED",
                {"batch_index": batch, "accepted_count": 25, "evaluated_count": 25},
            )
            logical = _append_reflector_call(ledger, batch=batch)
            ledger.append(
                "REFLECTOR_ACCEPTED",
                {
                    "batch_index": batch,
                    "logical_call_id": logical,
                    "accepted_synthesis_count_for_batch": 1,
                },
            )
            ledger.append(
                "EVIDENCE_VALIDATED",
                {"batch_index": batch, "evidence_sha256": _sha(f"evidence-{batch}")},
            )
            for target in ("text_memory", "skill_bundle", "agent_system"):
                ledger.append(
                    "TARGET_JOB_COMPLETED",
                    {
                        "batch_index": batch,
                        "target_id": target,
                        "job_id": f"core-job-{batch}-{target}",
                        "execution_receipt_sha256": _sha(f"core-{batch}"),
                    },
                )
            ledger.append(
                "BATCH_ARTIFACT_SET_COMMITTED",
                {
                    "batch_index": batch,
                    "state_sha256": _sha(f"state-{batch}"),
                    "artifact_count": 3,
                    "core_job_count": 3,
                },
            )
            _close_candidate_phase(ledger, phase="train-post", batch=batch)
            ledger.append(
                "BATCH_POST_CLOSED",
                {"batch_index": batch, "accepted_count": 25, "evaluated_count": 25},
            )
        ledger.append(
            "FINAL_STATE_FROZEN",
            {
                "batch_index": 4,
                "state_sha256": _sha("state-4"),
                "artifact_count": 3,
                "feedback_disabled": True,
                "test_sealed": True,
                **_frozen_context_payload(),
            },
        )
        controller = TemperatureExperimentControllerV1(
            run_id=run_id,
            ledger=ledger,
            status_path=(tmp_path / "status" / "current.json").resolve(),
        )
        assert controller.action.kind is ControllerActionKindV1.RUN_EVOLVED_TEST
        with pytest.raises(
            TemperatureLedgerError, match="LEDGER_PROTOCOL_EVENT_OUT_OF_ORDER"
        ):
            ledger.append(
                "TARGET_JOB_COMPLETED",
                {
                    "batch_index": 4,
                    "target_id": "text_memory",
                    "job_id": "late-core-job",
                    "execution_receipt_sha256": _sha("late"),
                },
            )
