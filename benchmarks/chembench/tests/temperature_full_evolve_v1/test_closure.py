from __future__ import annotations

import hashlib
import json
import shutil
import stat
from pathlib import Path

import pytest
from benchmarks.chembench.tests.temperature_full_evolve_v1.test_reporting import _payload

from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_private_file,
    write_public_file,
)
from openevo_chembench.supervised_transfer_v2.context_binding import (
    SupervisedSessionContextBindingV2,
    SupervisedTargetBindingV2,
    issue_supervised_context_binding_receipt_v2,
)
from openevo_chembench.temperature_full_evolve_v1.closure import (
    TemperatureClosureError,
    verify_formal_closure_v1,
    write_formal_closure_receipt_v1,
)
from openevo_chembench.temperature_full_evolve_v1.ledger import (
    TemperatureExperimentLedgerV1,
)
from openevo_chembench.temperature_full_evolve_v1.reporting import (
    AggregateReportInputV1,
    write_aggregate_report_input_v1,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _claim(
    *,
    logical: str,
    phase: str,
    ordinal: int,
) -> dict[str, object]:
    return {
        "logical_call_id": logical,
        "call_id": f"{logical}-a01",
        "task_id": f"chembench-{logical}-a01",
        "phase": phase,
        "logical_arm": (
            "batch_supervised_reflector" if phase == "train_reflector" else "candidate"
        ),
        "attempt_number": 1,
        "task_request_sha256": _sha(f"request-{logical}-{ordinal}"),
        "retry_semantics_sha256": _sha(f"retry-semantics-{logical}"),
        "service_identity_sha256": _sha("runtime-service"),
    }


def _accept(claim: dict[str, object]) -> dict[str, object]:
    response = "A"
    return {
        "logical_call_id": claim["logical_call_id"],
        "call_id": claim["call_id"],
        "response": response,
        "response_sha256": _sha(response),
        "task_result_sha256": _sha(f"result-{claim['logical_call_id']}"),
        "transcript_sha256": _sha(f"transcript-{claim['logical_call_id']}"),
        "completion_identity_sha256": _sha(f"completion-{claim['logical_call_id']}"),
        "tool_event_count": 0,
        "tool_policy_validated": True,
    }


def _evaluate(
    claim: dict[str, object],
    *,
    phase: str,
    batch: int | None,
    ordinal: int,
    correct: bool,
    context_binding_sha256: str | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "logical_call_id": claim["logical_call_id"],
        "phase": phase.replace("-", "_"),
        "batch_index": batch,
        "task_ordinal": ordinal,
        "correct": correct,
        "official_parse_status": "parsed",
        "strict_parse_status": "parsed",
    }
    if context_binding_sha256 is not None:
        body["context_binding_sha256"] = context_binding_sha256
    value = canonical_json_bytes(body).decode("utf-8")
    return {
        "logical_call_id": claim["logical_call_id"],
        "evaluation_json": value,
        "evaluation_sha256": hashlib.sha256(value.encode()).hexdigest(),
    }


def _candidate_phase(
    ledger: TemperatureExperimentLedgerV1,
    *,
    phase: str,
    batch: int | None,
    correctness: tuple[bool, ...],
    ordinal_start: int = 0,
    context_targets: tuple[SupervisedTargetBindingV2, ...] | None = None,
    tamper_context_binding: bool = False,
) -> None:
    for local_ordinal, correct in enumerate(correctness):
        ordinal = ordinal_start + local_ordinal
        token = f"-b{batch:02d}" if batch is not None else ""
        logical = f"closure-{phase}{token}-i{ordinal:03d}"
        claim = _claim(logical=logical, phase=phase, ordinal=ordinal)
        context_binding_sha256 = (
            None
            if context_targets is None
            else _context_binding_receipt_sha256(
                call_id=str(claim["call_id"]),
                targets=context_targets,
            )
        )
        if tamper_context_binding and local_ordinal == 0:
            context_binding_sha256 = _sha(f"tampered-{phase}")
        ledger.append("CALL_CLAIMED", claim)
        ledger.append("CALL_ACCEPTED", _accept(claim))
        ledger.append(
            "CALL_EVALUATED",
            _evaluate(
                claim,
                phase=phase,
                batch=batch,
                ordinal=ordinal,
                correct=correct,
                context_binding_sha256=context_binding_sha256,
            ),
        )


def _frozen_context_targets() -> tuple[SupervisedTargetBindingV2, ...]:
    resolution = _sha("frozen-c4-materialization")
    return tuple(
        SupervisedTargetBindingV2(
            target_id=target,  # type: ignore[arg-type]
            core_artifact_id=f"artifact-{target}",
            artifact_payload_sha256=_sha(f"payload-{target}"),
            resolved_content_sha256=_sha(f"resolved-{target}"),
            context_resolution_digest=resolution,
        )
        for target in ("text_memory", "skill_bundle", "agent_system")
    )


def _context_binding_receipt_sha256(
    *,
    call_id: str,
    targets: tuple[SupervisedTargetBindingV2, ...],
) -> str:
    binding = SupervisedSessionContextBindingV2(
        session_id="temp-" + _sha(call_id)[:40],
        targets=targets,
    )
    return issue_supervised_context_binding_receipt_v2(
        expected=binding,
        actual=binding,
    ).digest


def _reflector(
    ledger: TemperatureExperimentLedgerV1,
    *,
    batch: int,
    reject_first: bool = False,
) -> str:
    logical = f"closure-reflector-b{batch:02d}"
    claim = _claim(logical=logical, phase="train_reflector", ordinal=batch)
    ledger.append("CALL_CLAIMED", claim)
    if reject_first:
        ledger.append(
            "CALL_REJECTED_COMPLETION",
            {
                "logical_call_id": logical,
                "call_id": claim["call_id"],
                "rejection_code": "REFLECTOR_RESPONSE_PACKET_SEQUENCE_INVALID",
                "response_sha256": _sha(f"rejected-response-{batch}"),
                "task_result_sha256": _sha(f"rejected-result-{batch}"),
                "transcript_sha256": _sha(f"rejected-transcript-{batch}"),
                "completion_identity_sha256": _sha(
                    f"rejected-completion-{batch}"
                ),
                "durable_completion": True,
                "tool_event_count": 0,
                "tool_policy_validated": True,
            },
        )
        claim = {
            **claim,
            "call_id": f"{logical}-a02",
            "task_id": f"chembench-{logical}-a02",
            "attempt_number": 2,
            "task_request_sha256": _sha(f"request-{logical}-retry-{batch}"),
        }
        ledger.append("CALL_CLAIMED", claim)
    ledger.append("CALL_ACCEPTED", _accept(claim))
    return logical


def _write_preflight(root: Path, payload: dict[str, object]) -> str:
    root.mkdir(mode=0o755)
    identity = payload["identity"]
    preflight = payload["preflight"]
    assert isinstance(identity, dict) and isinstance(preflight, dict)
    regression = {
        "schema_version": "TemperatureRegressionReceiptV1",
        "focused_test_count": preflight["focused_test_count"],
        "focused_failure_count": 0,
        "integration_test_count": preflight["integration_test_count"],
        "integration_failure_count": 0,
        "command_sha256": _sha("test-command"),
        "output_sha256": _sha("test-output"),
        "model_calls": 0,
    }
    model = {
        "schema_version": "TemperatureModelIdentityReceiptV1",
        "status": "PASS_ZERO_MODEL_CALLS",
        "model": "gpt-5.5",
        "reasoning_effort": "medium",
        "candidate_codex_executable_sha256": identity["managed_codex_executable_sha256"],
        "reflector_codex_executable_sha256": identity["managed_codex_executable_sha256"],
        "candidate_image_id": identity["managed_candidate_image_id"],
        "candidate_baseline_identity_equal": True,
        "model_calls": 0,
    }
    runtime_body = {
        "schema_version": "TemperatureRuntimePreflightEvidenceV1",
        "candidate_codex_executable_sha256": identity["managed_codex_executable_sha256"],
        "reflector_codex_executable_sha256": identity["managed_codex_executable_sha256"],
        "candidate_image_id": identity["managed_candidate_image_id"],
        "model": "gpt-5.5",
        "reasoning_effort": "medium",
        "capture_mode": "transcript",
        "credential_metadata_passed": True,
        "credential_content_read": False,
        "managed_runtime_passed": True,
        "framework_registry_passed": True,
        "framework_lock_sha256": identity["framework_lock_sha256"],
        "formal_runtime_identity_sha256": identity[
            "formal_runtime_identity_sha256"
        ],
        "formal_runtime_receipt_sha256": identity[
            "formal_runtime_receipt_sha256"
        ],
        "formal_runtime_source_commit": identity["source_commit"],
        "formal_runtime_source_tree_sha256": identity["source_tree_sha256"],
        "core_wheel_sha256": identity["core_wheel_sha256"],
        "chembench_wheel_sha256": identity["chembench_wheel_sha256"],
        "core_editable": False,
        "chembench_editable": False,
        "formal_runtime_python_isolated": True,
        "runtime_services_ready": True,
        "runtime_services_identity_sha256": identity["runtime_services_identity_sha256"],
        "completion_persistence_shared": True,
        "completion_persistence_identity_sha256": _sha("completion-root"),
        "model_calls": 0,
    }
    runtime = {
        **runtime_body,
        "runtime_preflight_sha256": sha256_bytes(canonical_json_bytes(runtime_body)),
    }
    report = {
        "schema_version": "TemperatureFullEvolvePreflightV1",
        "status": "PASS_READY_FOR_FORMAL_EXECUTION",
        "protocol_id": payload["protocol_id"],
        "source_commit": identity["source_commit"],
        "config_sha256": identity["config_sha256"],
        "split_sha256": identity["split_sha256"],
        "regression_receipt_sha256": sha256_bytes(canonical_json_bytes(regression)),
        "preflight_model_calls": 0,
    }
    values = {
        "report": report,
        "split_manifest": {"dataset_sha256": identity["dataset_sha256"]},
        "historical_manifest": {"generation_zero_is_fresh": True},
        "grouping_manifest": {"whole_group_assignment": True},
        "config_manifest": {
            "source_tree_clean": True,
            "generation_zero_context_bytes": 0,
        },
        "model_identity_receipt": model,
        "runtime_identity_receipt": runtime,
        "regression_receipt": regression,
    }
    names = {
        "report": "preflight_report.json",
        "split_manifest": "split_manifest.json",
        "historical_manifest": "historical_exclusion_manifest.json",
        "grouping_manifest": "near_duplicate_group_manifest.json",
        "config_manifest": "config_manifest.json",
        "model_identity_receipt": "model_identity_receipt.json",
        "runtime_identity_receipt": "managed_runtime_receipt.json",
        "regression_receipt": "regression_receipt_v1.json",
    }
    for key, name in names.items():
        write_public_file(root / name, canonical_pretty_json_bytes(values[key]))
    protocol = b"# Frozen formal protocol\n"
    write_public_file(root / "EXPERIMENT_PROTOCOL.md", protocol)
    identity["model_identity_receipt_sha256"] = sha256_bytes(canonical_pretty_json_bytes(model))
    identity["managed_runtime_receipt_sha256"] = sha256_bytes(
        canonical_pretty_json_bytes(runtime)
    )
    digest = sha256_bytes(canonical_json_bytes(values))
    preflight["preflight_bundle_sha256"] = digest
    return digest


def _write_manifest(
    run_root: Path,
    *,
    report: AggregateReportInputV1,
    preflight_bundle_sha256: str,
) -> None:
    manifest = {
        "schema_version": "TemperatureFullEvolveFormalRunManifestV1",
        "protocol_id": report.protocol_id,
        "controller_run_id": report.run_ids.controller_run_id,
        "evolved_run_id": report.run_ids.evolved_run_id,
        "baseline_run_id": report.run_ids.baseline_run_id,
        "core_run_id": report.run_ids.core_run_id,
        "runtime_service_run_id": report.run_ids.runtime_service_run_id,
        "source_commit": report.identity.source_commit,
        "branch": report.identity.branch,
        "config_sha256": report.identity.config_sha256,
        "split_sha256": report.identity.split_sha256,
        "preflight_bundle_sha256": preflight_bundle_sha256,
        "runtime_services_identity_sha256": report.identity.runtime_services_identity_sha256,
        "generation_zero_context_empty": True,
        "old_artifact_imported": False,
        "old_completion_imported": False,
        "old_database_imported": False,
        "old_workspace_imported": False,
        "started_at_utc": report.started_at_utc,
    }
    write_private_file(
        run_root / "private/run_manifest_v1.json",
        canonical_pretty_json_bytes(manifest),
        replace=False,
    )


def _write_ledger(
    run_root: Path,
    *,
    report: AggregateReportInputV1,
    aggregate_sha256: str,
    incident_opened: bool = False,
    tampered_context_phase: str | None = None,
    rejected_reflector_batch: int | None = None,
) -> None:
    with TemperatureExperimentLedgerV1(
        path=(run_root / "private/events.jsonl").resolve(),
        run_id=report.run_ids.controller_run_id,
    ) as ledger:
        ledger.append(
            "RUN_CREATED",
            {
                "generation_zero": True,
                "old_artifact_imported": False,
                "old_completion_imported": False,
                "old_database_imported": False,
            },
        )
        ledger.append(
            "SPLIT_FROZEN",
            {
                "split_sha256": report.identity.split_sha256,
                "train_count": 100,
                "test_count": 100,
                "batch_size": 25,
                "test_sealed": True,
            },
        )
        for batch in range(1, 5):
            train_pre = (True,) * 17 + (False,) * 8
            train_post = (True,) * 15 + (False,) * 2 + (True,) * 4 + (False,) * 4
            ordinal_start = (batch - 1) * 25
            _candidate_phase(
                ledger,
                phase="train-pre",
                batch=batch,
                correctness=train_pre,
                ordinal_start=ordinal_start,
            )
            ledger.append(
                "BATCH_PRE_CLOSED",
                {"batch_index": batch, "accepted_count": 25, "evaluated_count": 25},
            )
            logical = _reflector(
                ledger,
                batch=batch,
                reject_first=batch == rejected_reflector_batch,
            )
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
                        "execution_receipt_sha256": _sha(f"execution-{batch}-{target}"),
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
            _candidate_phase(
                ledger,
                phase="train-post",
                batch=batch,
                correctness=train_post,
                ordinal_start=ordinal_start,
            )
            ledger.append(
                "BATCH_POST_CLOSED",
                {"batch_index": batch, "accepted_count": 25, "evaluated_count": 25},
            )
        frozen_targets = _frozen_context_targets()
        frozen_target_payload = [target.to_dict() for target in frozen_targets]
        ledger.append(
            "FINAL_STATE_FROZEN",
            {
                "batch_index": 4,
                "state_sha256": _sha("state-4"),
                "artifact_count": 3,
                "feedback_disabled": True,
                "test_sealed": True,
                "frozen_context_targets": frozen_target_payload,
                "frozen_context_targets_sha256": sha256_bytes(
                    canonical_json_bytes(frozen_target_payload)
                ),
            },
        )
        _candidate_phase(
            ledger,
            phase="evolved-test",
            batch=None,
            correctness=tuple(report.evolved_test.correctness),
            context_targets=frozen_targets,
            tamper_context_binding=tampered_context_phase == "evolved-test",
        )
        ledger.append(
            "EVOLVED_TEST_CLOSED",
            {
                "accepted_count": 100,
                "evaluated_count": 100,
                "feedback_disabled": True,
                "reflector_calls": 0,
                "core_jobs": 0,
                "artifact_updates": 0,
            },
        )
        _candidate_phase(
            ledger,
            phase="baseline-test",
            batch=None,
            correctness=tuple(report.baseline_test.correctness),
            context_targets=(),
            tamper_context_binding=tampered_context_phase == "baseline-test",
        )
        ledger.append(
            "BASELINE_TEST_CLOSED",
            {
                "accepted_count": 100,
                "evaluated_count": 100,
                "feedback_disabled": True,
                "reflector_calls": 0,
                "core_jobs": 0,
                "artifact_updates": 0,
                "context_empty": True,
            },
        )
        if incident_opened:
            ledger.append(
                "INCIDENT_OPENED",
                {"finding_code": "SYNTHETIC_CLOSURE_INCIDENT"},
            )
        ledger.append(
            "AUDIT_CLOSED",
            {
                "checksums_verified": True,
                "aggregate_report_input_sha256": aggregate_sha256,
            },
        )


@pytest.fixture(scope="module")
def closed_evidence(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    root = tmp_path_factory.mktemp("closure")
    run_root = root / "run"
    run_root.mkdir(mode=0o700)
    preflight_root = root / "preflight"
    payload = _payload()
    bundle_sha256 = _write_preflight(preflight_root, payload)
    report = AggregateReportInputV1.model_validate(payload)
    aggregate = run_root / "private/aggregate_report_input_v1.json"
    aggregate_sha256 = write_aggregate_report_input_v1(report=report, path=aggregate)
    _write_manifest(run_root, report=report, preflight_bundle_sha256=bundle_sha256)
    _write_ledger(run_root, report=report, aggregate_sha256=aggregate_sha256)
    return run_root, preflight_root, aggregate


def test_formal_closure_replays_private_evidence_and_emits_no_item_data(
    closed_evidence: tuple[Path, Path, Path],
) -> None:
    run_root, preflight_root, aggregate = closed_evidence
    closure = verify_formal_closure_v1(
        run_root=run_root.resolve(),
        preflight_root=preflight_root.resolve(),
        aggregate_input_path=aggregate.resolve(),
    )
    receipt = closure.receipt
    assert receipt.status == "COMPLETE"
    assert receipt.accepted_candidate_count == 400
    assert receipt.accepted_reflector_count == 4
    assert receipt.rejected_reflector_attempt_count == 0
    assert receipt.core_job_count == 12
    assert receipt.managed_runtime_session_attempt_count == 404
    assert receipt.readiness_passed_for_accepted_call_count == 404
    assert receipt.canary_provider_attempt_measurement_status == "NOT_MEASURED"
    assert receipt.canary_provider_attempt_lower_bound == 404
    assert receipt.ledger_head_sha256 == receipt.audit_closed_event_sha256
    rendered = json.dumps(receipt.model_dump(mode="json"), sort_keys=True)
    assert '"uid"' not in rendered
    assert '"question"' not in rendered
    assert '"target"' not in rendered
    assert '"completion"' not in rendered
    output = run_root / "private/formal_closure_receipt_v1.json"
    write_formal_closure_receipt_v1(closure, path=output.resolve())
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_formal_closure_binds_rejected_reflector_attempt_to_schema_failure(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "rejected-run"
    run_root.mkdir(mode=0o700)
    preflight_root = tmp_path / "rejected-preflight"
    payload = _payload()
    first_batch = payload["train_batches"][0]
    calls = payload["calls"]
    assert isinstance(first_batch, dict) and isinstance(calls, dict)
    failures = calls["failures"]
    assert isinstance(failures, dict)
    first_batch.update(
        {"failed_attempts": 1, "retries": 1, "rejected_attempts": 1}
    )
    calls.update(
        {
            "managed_runtime_session_attempts": 405,
            "failed_attempts": 1,
            "retries": 1,
            "rejected_attempts": 1,
        }
    )
    failures["artifact_or_schema"] = 1
    bundle_sha256 = _write_preflight(preflight_root, payload)
    report = AggregateReportInputV1.model_validate(payload)
    aggregate = run_root / "private/aggregate_report_input_v1.json"
    aggregate_sha256 = write_aggregate_report_input_v1(
        report=report,
        path=aggregate,
    )
    _write_manifest(
        run_root,
        report=report,
        preflight_bundle_sha256=bundle_sha256,
    )
    _write_ledger(
        run_root,
        report=report,
        aggregate_sha256=aggregate_sha256,
        rejected_reflector_batch=1,
    )

    closure = verify_formal_closure_v1(
        run_root=run_root.resolve(),
        preflight_root=preflight_root.resolve(),
        aggregate_input_path=aggregate.resolve(),
    )
    assert closure.receipt.rejected_reflector_attempt_count == 1
    assert closure.receipt.managed_runtime_session_attempt_count == 405


def test_formal_closure_rejects_staged_aggregate_tampering(
    closed_evidence: tuple[Path, Path, Path],
    tmp_path: Path,
) -> None:
    source_run, source_preflight, _source_aggregate = closed_evidence
    run = tmp_path / "run"
    preflight = tmp_path / "preflight"
    shutil.copytree(source_run, run)
    shutil.copytree(source_preflight, preflight)
    aggregate = run / "private/aggregate_report_input_v1.json"
    payload = json.loads(aggregate.read_text(encoding="utf-8"))
    payload["generated_at_utc"] = "2026-08-02T04:00:01Z"
    write_private_file(aggregate, canonical_pretty_json_bytes(payload))
    with pytest.raises(TemperatureClosureError, match="AUDIT_AGGREGATE_BINDING"):
        verify_formal_closure_v1(
            run_root=run.resolve(),
            preflight_root=preflight.resolve(),
            aggregate_input_path=aggregate.resolve(),
        )


def test_formal_closure_rejects_preflight_or_manifest_substitution(
    closed_evidence: tuple[Path, Path, Path],
    tmp_path: Path,
) -> None:
    source_run, source_preflight, _source_aggregate = closed_evidence
    run = tmp_path / "run"
    preflight = tmp_path / "preflight"
    shutil.copytree(source_run, run)
    shutil.copytree(source_preflight, preflight)
    config = preflight / "config_manifest.json"
    value = json.loads(config.read_text(encoding="utf-8"))
    value["source_tree_clean"] = False
    write_public_file(config, canonical_pretty_json_bytes(value))
    with pytest.raises(TemperatureClosureError, match="PREFLIGHT_BINDING"):
        verify_formal_closure_v1(
            run_root=run.resolve(),
            preflight_root=preflight.resolve(),
            aggregate_input_path=(run / "private/aggregate_report_input_v1.json").resolve(),
        )


def test_formal_closure_rejects_formal_runtime_identity_substitution(
    closed_evidence: tuple[Path, Path, Path],
    tmp_path: Path,
) -> None:
    source_run, source_preflight, _source_aggregate = closed_evidence
    run = tmp_path / "run"
    preflight = tmp_path / "preflight"
    shutil.copytree(source_run, run)
    shutil.copytree(source_preflight, preflight)
    runtime = preflight / "managed_runtime_receipt.json"
    value = json.loads(runtime.read_text(encoding="utf-8"))
    value["core_wheel_sha256"] = _sha("substituted-core-wheel")
    runtime_body = {
        key: item
        for key, item in value.items()
        if key != "runtime_preflight_sha256"
    }
    value["runtime_preflight_sha256"] = sha256_bytes(
        canonical_json_bytes(runtime_body)
    )
    write_public_file(runtime, canonical_pretty_json_bytes(value))

    with pytest.raises(TemperatureClosureError, match="PREFLIGHT_BINDING"):
        verify_formal_closure_v1(
            run_root=run.resolve(),
            preflight_root=preflight.resolve(),
            aggregate_input_path=(
                run / "private/aggregate_report_input_v1.json"
            ).resolve(),
        )


def test_formal_closure_rejects_incident_even_with_terminal_audit(
    tmp_path: Path,
) -> None:
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    preflight = tmp_path / "preflight"
    payload = _payload()
    bundle_sha256 = _write_preflight(preflight, payload)
    report = AggregateReportInputV1.model_validate(payload)
    aggregate = run / "private/aggregate_report_input_v1.json"
    aggregate_sha256 = write_aggregate_report_input_v1(
        report=report,
        path=aggregate,
    )
    _write_manifest(
        run,
        report=report,
        preflight_bundle_sha256=bundle_sha256,
    )
    _write_ledger(
        run,
        report=report,
        aggregate_sha256=aggregate_sha256,
        incident_opened=True,
    )

    with pytest.raises(TemperatureClosureError, match="LEDGER_COUNTS_INVALID"):
        verify_formal_closure_v1(
            run_root=run.resolve(),
            preflight_root=preflight.resolve(),
            aggregate_input_path=aggregate.resolve(),
        )


@pytest.mark.parametrize("tampered_phase", ("evolved-test", "baseline-test"))
def test_formal_closure_rejects_test_context_binding_substitution(
    tmp_path: Path,
    tampered_phase: str,
) -> None:
    run = tmp_path / tampered_phase
    run.mkdir(mode=0o700)
    preflight = tmp_path / f"preflight-{tampered_phase}"
    payload = _payload()
    bundle_sha256 = _write_preflight(preflight, payload)
    report = AggregateReportInputV1.model_validate(payload)
    aggregate = run / "private/aggregate_report_input_v1.json"
    aggregate_sha256 = write_aggregate_report_input_v1(
        report=report,
        path=aggregate,
    )
    _write_manifest(
        run,
        report=report,
        preflight_bundle_sha256=bundle_sha256,
    )
    _write_ledger(
        run,
        report=report,
        aggregate_sha256=aggregate_sha256,
        tampered_context_phase=tampered_phase,
    )

    with pytest.raises(
        TemperatureClosureError,
        match="TEST_EVALUATION_AGGREGATE_MISMATCH",
    ):
        verify_formal_closure_v1(
            run_root=run.resolve(),
            preflight_root=preflight.resolve(),
            aggregate_input_path=aggregate.resolve(),
        )
