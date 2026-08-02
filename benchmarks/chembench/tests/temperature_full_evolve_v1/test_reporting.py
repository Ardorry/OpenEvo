from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    canonical_pretty_json_bytes,
)
from openevo_chembench.temperature_full_evolve_v1 import closure as closure_module
from openevo_chembench.temperature_full_evolve_v1.closure import (
    FormalClosureReceiptV1,
    VerifiedFormalClosureV1,
)
from openevo_chembench.temperature_full_evolve_v1.config import (
    EXPECTED_CANDIDATE_IMAGE_ID,
    EXPECTED_CODEX_SHA256,
)
from openevo_chembench.temperature_full_evolve_v1.reporting import (
    AggregateReportInputV1,
    TemperatureReportError,
    load_aggregate_report_input_v1,
    write_aggregate_report_input_v1,
    write_final_report_package_v1,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _payload() -> dict[str, object]:
    batches: list[dict[str, object]] = []
    for batch_index in range(1, 5):
        memory_bytes = 700 * batch_index
        skill_bytes = 200 * batch_index
        agent_bytes = 100 * batch_index
        combined_bytes = memory_bytes + skill_bytes + agent_bytes + 50
        batches.append(
            {
                "batch_index": batch_index,
                "task_count": 25,
                "pre_correct": 17,
                "post_correct": 19,
                "both_correct": 15,
                "pre_only_correct": 2,
                "post_only_correct": 4,
                "both_wrong": 4,
                "pre_parser_success": 25,
                "post_parser_success": 25,
                "pre_candidate_calls": 25,
                "post_candidate_calls": 25,
                "reflector_calls": 1,
                "core_jobs": 3,
                "failed_attempts": 0,
                "retries": 0,
                "rejected_attempts": 0,
                "artifacts": {
                    "text_memory_sha256": _digest(f"memory-{batch_index}"),
                    "skill_bundle_sha256": _digest(f"skill-{batch_index}"),
                    "agent_system_sha256": _digest(f"agent-{batch_index}"),
                    "combined_context_sha256": _digest(f"combined-{batch_index}"),
                    "text_memory_bytes": memory_bytes,
                    "skill_bundle_bytes": skill_bytes,
                    "agent_system_bytes": agent_bytes,
                    "combined_context_bytes": combined_bytes,
                    "validation_passed": True,
                },
                "rules": {
                    "active_rules": 2 * batch_index,
                    "supported_rules": 2 * batch_index,
                    "conflicted_rules": batch_index - 1,
                    "retired_rules": batch_index - 1,
                    "support_count_total": 5 * batch_index,
                    "contradiction_count_total": batch_index - 1,
                    "evidence_sha256": _digest(f"evidence-{batch_index}"),
                },
            }
        )
    final = batches[-1]["artifacts"]
    assert isinstance(final, dict)
    baseline = (True,) * 80 + (True,) * 10 + (False,) * 5 + (False,) * 5
    evolved = (True,) * 80 + (False,) * 10 + (True,) * 5 + (False,) * 5
    run_ids = (
        "stv3-temperature-full-evolve-v1-20260802T000000Z",
        "stv3-temperature-full-evolve-v1-evolved-test",
        "stv3-temperature-full-evolve-v1-baseline-test",
        "stv3-temperature-full-evolve-v1-core",
        "stv3-temperature-services-20260802T000000Z-1234abcd",
    )
    return {
        "schema_version": "TemperatureFullEvolveAggregateReportInputV1",
        "protocol_id": "chembench_temperature_full_evolve_v1",
        "status": "READY_FOR_AUDIT",
        "generated_at_utc": "2026-08-02T04:00:00Z",
        "started_at_utc": "2026-08-02T00:00:00Z",
        "completed_at_utc": "2026-08-02T03:59:00Z",
        "train_post_is_same_item_supervised_diagnostic": True,
        "run_ids": {
            "controller_run_id": run_ids[0],
            "evolved_run_id": run_ids[1],
            "baseline_run_id": run_ids[2],
            "core_run_id": run_ids[3],
            "runtime_service_run_id": run_ids[4],
            "all_run_ids": run_ids,
        },
        "identity": {
            "source_commit": "1" * 40,
            "branch": "chembench-temperature-full-evolve-v1",
            "config_sha256": _digest("config"),
            "split_sha256": _digest("split"),
            "dataset_sha256": _digest("dataset"),
            "group_manifest_sha256": _digest("groups"),
            "group_assignment_sha256": _digest("assignments"),
            "framework_lock_sha256": _digest("framework"),
            "formal_runtime_identity_sha256": _digest("formal-runtime"),
            "formal_runtime_receipt_sha256": _digest("formal-runtime-receipt"),
            "source_tree_sha256": _digest("formal-source-tree"),
            "core_wheel_sha256": _digest("core-wheel"),
            "chembench_wheel_sha256": _digest("chembench-wheel"),
            "core_editable": False,
            "chembench_editable": False,
            "runtime_services_identity_sha256": _digest("services"),
            "model_identity_receipt_sha256": _digest("model-receipt"),
            "managed_runtime_receipt_sha256": _digest("runtime-receipt"),
            "model": "gpt-5.5",
            "reasoning_effort": "medium",
            "managed_codex_executable_sha256": EXPECTED_CODEX_SHA256,
            "managed_candidate_image_id": EXPECTED_CANDIDATE_IMAGE_ID,
            "capture_mode": "transcript",
            "parser_id": "first_uppercase_opencompass_compatible",
            "strict_parser_id": "strict_single_letter_parser",
            "evaluator_id": "ChemBench4KPrivateEvaluator",
            "candidate_baseline_stack_equal": True,
            "evolved_then_baseline_order": True,
            "test_feedback_enabled": False,
            "test_reflector_calls": 0,
            "test_core_jobs": 0,
            "test_artifact_updates": 0,
        },
        "historical_reuse": {
            "policy": "fixed_official_temperature_pool_fresh_c0",
            "protocol_fixed_at_local_date": "2026-08-02",
            "historical_exclusion_applied": False,
            "selected_items_may_have_historical_exposure": True,
            "historical_exposure_used_for_item_selection": False,
            "prior_artifacts_imported": 0,
            "prior_completions_imported": 0,
            "prior_databases_imported": 0,
            "prior_workspaces_imported": 0,
            "generation_zero_empty_context": True,
        },
        "split": {
            "train_count": 100,
            "test_count": 100,
            "reserve_count": 2,
            "batch_size": 25,
            "batch_count": 4,
            "eligible_pool_count": 202,
            "group_count": 202,
            "singleton_group_count": 202,
            "maximum_group_size": 1,
            "uid_overlap_count": 0,
            "normalized_text_overlap_count": 0,
            "normalized_option_overlap_count": 0,
            "source_group_fields_available": False,
            "source_group_overlap_count": None,
            "test_sealed_until_train_complete": True,
            "split_frozen_before_model_calls": True,
        },
        "preflight": {
            "preflight_id": "temperature-preflight-20260802",
            "preflight_bundle_sha256": _digest("preflight-bundle"),
            "model_calls": 0,
            "focused_test_count": 60,
            "focused_failure_count": 0,
            "integration_test_count": 10,
            "integration_failure_count": 0,
            "generation_zero_context_bytes": 0,
            "source_tree_frozen": True,
            "exactly_once_recovery_passed": True,
            "test_feedback_barrier_passed": True,
        },
        "train_batches": tuple(batches),
        "evolved_test": {
            "arm": "evolved",
            "correctness": evolved,
            "accepted_completion_count": 100,
            "official_parser_success_count": 100,
            "strict_parser_success_count": 100,
            "logical_candidate_calls": 100,
            "model_attempts": 100,
            "failed_attempts": 0,
            "retries": 0,
            "rejected_attempts": 0,
            "context_bytes": final["combined_context_bytes"],
            "context_sha256": final["combined_context_sha256"],
            "feedback_enabled": False,
            "reflector_calls": 0,
            "core_jobs": 0,
            "artifact_updates": 0,
        },
        "baseline_test": {
            "arm": "baseline",
            "correctness": baseline,
            "accepted_completion_count": 100,
            "official_parser_success_count": 100,
            "strict_parser_success_count": 100,
            "logical_candidate_calls": 100,
            "model_attempts": 100,
            "failed_attempts": 0,
            "retries": 0,
            "rejected_attempts": 0,
            "context_bytes": 0,
            "context_sha256": hashlib.sha256(b"").hexdigest(),
            "feedback_enabled": False,
            "reflector_calls": 0,
            "core_jobs": 0,
            "artifact_updates": 0,
        },
        "calls": {
            "candidate_logical_calls": 400,
            "reflector_logical_calls": 4,
            "core_jobs": 12,
            "experimental_model_logical_calls": 404,
            "managed_runtime_session_attempts": 404,
            "readiness_passed_for_accepted_calls": 404,
            "canary_provider_attempts": {
                "status": "NOT_MEASURED",
                "lower_bound": 404,
            },
            "failed_attempts": 0,
            "retries": 0,
            "rejected_attempts": 0,
            "recoveries": 0,
            "invalidated_formal_runs": 0,
            "failures": {
                "infrastructure_no_completion": 0,
                "parser_or_evaluator": 0,
                "orchestration": 0,
                "runtime_or_credential": 0,
                "duplicate_or_lease": 0,
                "artifact_or_schema": 0,
                "data_integrity": 0,
            },
        },
        "final_artifacts": {
            **final,
            "final_state_id": "C4",
            "final_manifest_sha256": _digest("final-manifest"),
            "lineage_sha256": _digest("lineage"),
            "frozen_before_test": True,
        },
        "regression": {
            "focused_passed": 60,
            "focused_failed": 0,
            "integration_passed": 10,
            "integration_failed": 0,
            "test_command_sha256": _digest("test-command"),
            "test_output_sha256": _digest("test-output"),
            "model_calls_during_tests": 0,
        },
    }


def _verified_closure(
    report: AggregateReportInputV1,
    *,
    protocol_bytes: bytes,
) -> VerifiedFormalClosureV1:
    body: dict[str, object] = {
        "schema_version": "TemperatureFormalClosureReceiptV1",
        "status": "COMPLETE",
        "protocol_id": report.protocol_id,
        "issued_at_utc": "2026-08-02T04:01:00Z",
        "controller_run_id": report.run_ids.controller_run_id,
        "evolved_run_id": report.run_ids.evolved_run_id,
        "baseline_run_id": report.run_ids.baseline_run_id,
        "core_run_id": report.run_ids.core_run_id,
        "runtime_service_run_id": report.run_ids.runtime_service_run_id,
        "all_run_ids": report.run_ids.all_run_ids,
        "source_commit": report.identity.source_commit,
        "config_sha256": report.identity.config_sha256,
        "split_sha256": report.identity.split_sha256,
        "dataset_sha256": report.identity.dataset_sha256,
        "formal_runtime_identity_sha256": (
            report.identity.formal_runtime_identity_sha256
        ),
        "formal_runtime_receipt_sha256": (
            report.identity.formal_runtime_receipt_sha256
        ),
        "source_tree_sha256": report.identity.source_tree_sha256,
        "core_wheel_sha256": report.identity.core_wheel_sha256,
        "chembench_wheel_sha256": report.identity.chembench_wheel_sha256,
        "core_editable": False,
        "chembench_editable": False,
        "preflight_bundle_sha256": report.preflight.preflight_bundle_sha256,
        "run_manifest_sha256": _digest("run-manifest"),
        "runtime_services_identity_sha256": report.identity.runtime_services_identity_sha256,
        "experiment_protocol_sha256": hashlib.sha256(protocol_bytes).hexdigest(),
        "aggregate_input_sha256": hashlib.sha256(
            canonical_pretty_json_bytes(report.model_dump(mode="json"))
        ).hexdigest(),
        "aggregate_input_utf8_bytes": len(
            canonical_pretty_json_bytes(report.model_dump(mode="json"))
        ),
        "ledger_event_count": 1234,
        "ledger_head_sha256": _digest("audit-event"),
        "audit_closed_sequence": 1234,
        "audit_closed_event_sha256": _digest("audit-event"),
        "audit_closed_at_utc": "2026-08-02T04:00:00Z",
        "unresolved_call_count": 0,
        "run_failed_event_count": 0,
        "incident_opened_event_count": 0,
        "claimed_logical_call_count": 404,
        "managed_runtime_session_attempt_count": 404,
        "readiness_passed_for_accepted_call_count": 404,
        "canary_provider_attempt_measurement_status": "NOT_MEASURED",
        "canary_provider_attempt_lower_bound": 404,
        "accepted_candidate_count": 400,
        "evaluated_candidate_count": 400,
        "accepted_reflector_count": 4,
        "rejected_reflector_attempt_count": report.calls.rejected_attempts,
        "core_job_count": 12,
        "preflight_model_call_count": 0,
        "contains_item_identities": False,
        "contains_benchmark_content": False,
        "contains_answers_or_predictions": False,
        "contains_completions_or_transcripts": False,
    }
    receipt = FormalClosureReceiptV1.model_validate(
        {
            **body,
            "receipt_sha256": hashlib.sha256(canonical_json_bytes(body)).hexdigest(),
        },
        strict=True,
    )
    return VerifiedFormalClosureV1(receipt=receipt, _token=closure_module._CLOSURE_TOKEN)


def test_final_report_package_is_closed_aggregate_and_checksum_complete(tmp_path: Path) -> None:
    report = AggregateReportInputV1.model_validate(_payload())
    sealed_input = (tmp_path / "private" / "aggregate_report_input_v1.json").resolve()
    input_digest = write_aggregate_report_input_v1(report=report, path=sealed_input)
    assert hashlib.sha256(sealed_input.read_bytes()).hexdigest() == input_digest
    assert sealed_input.stat().st_mode & 0o777 == 0o600
    report = load_aggregate_report_input_v1(sealed_input)
    protocol = tmp_path / "protocol.md"
    protocol.write_text("# Frozen protocol\n", encoding="utf-8")
    destination = tmp_path / "public-report"
    closure = _verified_closure(report, protocol_bytes=protocol.read_bytes())

    result = write_final_report_package_v1(
        report=report,
        closure=closure,
        destination=destination,
        protocol_source=protocol,
    )

    assert result["status"] == "COMPLETE"
    checksums = (destination / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
    assert len(checksums) == result["file_count"] - 1
    for line in checksums:
        digest, name = line.split("  ", maxsplit=1)
        assert hashlib.sha256((destination / name).read_bytes()).hexdigest() == digest
    charts = sorted(destination.glob("*.png"))
    assert len(charts) == 8
    assert all(path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n") for path in charts)
    paired = json.loads((destination / "paired_test_comparison.json").read_text())
    assert paired["baseline_correct"] == 90
    assert paired["evolved_correct"] == 85
    assert paired["paired_delta_percentage_points"] == -5.0
    assert paired["baseline_only_correct"] == 10
    assert paired["evolved_only_correct"] == 5
    assert "correctness" not in paired
    historical = json.loads((destination / "historical_exclusion_manifest.json").read_text())
    assert historical["historical_exclusion_applied"] is False
    assert historical["selected_items_may_have_historical_exposure"] is True
    package = json.loads((destination / "package_manifest.json").read_text())
    assert package["contains_item_identities"] is False
    assert package["contains_answers_or_predictions"] is False
    formal_closure = json.loads((destination / "formal_closure_receipt.json").read_text())
    assert formal_closure["status"] == "COMPLETE"
    assert formal_closure["contains_benchmark_content"] is False
    runtime = json.loads((destination / "managed_runtime_receipt.json").read_text())
    assert runtime["formal_runtime_identity_sha256"] == _digest("formal-runtime")
    assert runtime["source_tree_sha256"] == _digest("formal-source-tree")
    assert runtime["core_wheel_sha256"] == _digest("core-wheel")
    assert runtime["chembench_wheel_sha256"] == _digest("chembench-wheel")
    assert runtime["core_editable"] is False
    assert runtime["chembench_editable"] is False
    calls = json.loads((destination / "call_and_failure_summary.json").read_text())
    assert calls["experimental_model_logical_calls"] == 404
    assert calls["managed_runtime_session_attempts"] == 404
    assert calls["readiness_passed_for_accepted_calls"] == 404
    assert calls["canary_provider_attempts"] == {
        "status": "NOT_MEASURED",
        "lower_bound": 404,
    }
    assert "managed_model_attempts" not in calls
    assert calls["rejected_attempts"] == 0
    assert "rejected attempts" in (destination / "FINAL_REPORT.md").read_text(
        encoding="utf-8"
    )
    assert "rejected/recoveries" in (destination / "HANDOFF.md").read_text(
        encoding="utf-8"
    )


def test_report_boundary_rejects_private_item_fields_and_nonempty_baseline_context() -> None:
    payload = _payload()
    payload["uid"] = "private-item"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AggregateReportInputV1.model_validate(payload)

    payload = _payload()
    baseline = payload["baseline_test"]
    assert isinstance(baseline, dict)
    baseline["context_bytes"] = 1
    with pytest.raises(ValidationError, match="baseline must have empty context"):
        AggregateReportInputV1.model_validate(payload)


def test_publication_rejects_protocol_substitution_after_closure(tmp_path: Path) -> None:
    report = AggregateReportInputV1.model_validate(_payload())
    frozen = b"# Frozen protocol\n"
    closure = _verified_closure(report, protocol_bytes=frozen)
    substituted = tmp_path / "substituted.md"
    substituted.write_bytes(b"# Different protocol\n")
    with pytest.raises(TemperatureReportError, match="FORMAL_CLOSURE_BINDING_INVALID"):
        write_final_report_package_v1(
            report=report,
            closure=closure,
            destination=tmp_path / "public-report",
            protocol_source=substituted,
        )


def test_split_never_claims_unavailable_source_group_overlap() -> None:
    payload = _payload()
    split = payload["split"]
    assert isinstance(split, dict)
    split["source_group_overlap_count"] = 0
    with pytest.raises(ValidationError, match="split aggregate counts are inconsistent"):
        AggregateReportInputV1.model_validate(payload)


def test_report_boundary_rejects_inconsistent_transitions_or_stale_c4() -> None:
    payload = _payload()
    batches = payload["train_batches"]
    assert isinstance(batches, tuple)
    first = batches[0]
    assert isinstance(first, dict)
    first["both_wrong"] = 5
    with pytest.raises(ValidationError, match="transition counts"):
        AggregateReportInputV1.model_validate(payload)

    payload = _payload()
    evolved = payload["evolved_test"]
    assert isinstance(evolved, dict)
    evolved["context_sha256"] = _digest("stale-context")
    with pytest.raises(ValidationError, match="frozen C4"):
        AggregateReportInputV1.model_validate(payload)


def test_crash_recovery_need_not_be_counted_as_a_failed_model_attempt() -> None:
    payload = _payload()
    calls = payload["calls"]
    assert isinstance(calls, dict)
    calls["recoveries"] = 2
    report = AggregateReportInputV1.model_validate(payload)
    assert report.calls.recoveries == 2
    assert report.calls.failed_attempts == 0
