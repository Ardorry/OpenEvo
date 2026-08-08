from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from openevo_researchclawbench.native_evolution_recovery import (
    NativeEvolutionRecoveryAdapter,
    NativeEvolutionRecoveryError,
    NativeEvolutionRecoverySettings,
    _successor_destination_run_id,
    _validate_source_receipts,
    native_evolution_recovery_dry_run,
)
from openevo_researchclawbench.training_state_store import canonical_sha256


def _seal(value: dict) -> dict:
    result = dict(value)
    result["content_sha256"] = canonical_sha256(result)
    return result


def _settings(tmp_path: Path) -> NativeEvolutionRecoverySettings:
    framework = tmp_path / "framework-lock.json"
    release = tmp_path / "daemon-manifest.json"
    framework.write_text("{}\n", encoding="utf-8")
    release.write_text("{}\n", encoding="utf-8")
    return NativeEvolutionRecoverySettings(
        task_id="Life_005",
        source_run_id="rcb_oe_v0_source",
        recovery_run_id="rcb_oe_v0_recovery",
        source_resolution_idempotency_key="rcb_oe_v0_source.resolve",
        source_resolution_prepared_path=None,
        source_runtime_generation_digest="1" * 64,
        source_daemon_release_identity="2" * 64,
        source_framework_lock=framework,
        source_release_bundle_manifest=release,
        source_failure_code="successor_transition_failed",
        source_transition_attempt_capacity=2,
        target_authorization_order=("agent_system", "skill_bundle", "text_memory"),
    )


def _source_receipts() -> dict[str, dict]:
    transition_id = "successor-source"
    candidate = _seal(
        {
            "operation_kind": "candidate",
            "status": "SUCCEEDED",
            "completed": True,
            "core_task_id": "task-source",
            "dataset_id": "dataset-source",
            "dataset_revision": "dataset-source.v1",
            "successor_transition_id": transition_id,
        }
    )
    validation = _seal(
        {
            "operation_kind": "validation",
            "status": "SUCCEEDED",
            "artifact_valid": True,
        }
    )
    attachment = _seal(
        {
            "operation_kind": "feedback_attachment",
            "status": "SUCCEEDED",
            "judge_calls": 0,
            "judge_feedback_included": False,
            "task_local_overlay_scope_id": "task-source",
            "successor_transition_id": transition_id,
            "attachment_id": "attachment-source",
            "attachment_sha256": "3" * 64,
            "resolved_dataset_artifact_id": "artifact-source",
            "resolved_view_sha256": "4" * 64,
        }
    )
    checkpoint = _seal(
        {
            "schema_version": (
                "openevo.researchclawbench.successor_recovery_checkpoint.v1"
            ),
            "successor_transition_id": transition_id,
            "transition_attempt_count": 2,
            "latest_transition_attempt_id": "successor-attempt-source",
            "terminal_error_code": "successor_transition_failed",
            "attachment_id": "attachment-source",
            "resolved_view_sha256": "4" * 64,
            "commit_absent": True,
            "successor_artifact_count": 0,
            "source_mutation_allowed": False,
            "candidate_reexecution_allowed": False,
            "judge_reexecution_allowed": False,
            "reflector_reexecution_allowed": False,
        }
    )
    evolution = _seal(
        {
            "operation_kind": "evolution",
            "status": "FAILED_TERMINAL",
            "failure_class": "AUTHORITY",
            "successor_recovery_checkpoint": checkpoint,
        }
    )
    return {
        "candidate": candidate,
        "validation": validation,
        "feedback_attachment": attachment,
        "evolution": evolution,
    }


def test_pre_job_source_receipts_preserve_gt_and_judge_isolation(
    tmp_path: Path,
) -> None:
    source = _validate_source_receipts(_source_receipts(), _settings(tmp_path))

    assert source["core_cross_check"] == {
        "successor_transition_id": "successor-source",
        "failed_pre_job_operation_id": "successor-attempt-source",
        "dataset_id": "dataset-source",
        "dataset_revision": "dataset-source.v1",
        "attachment_id": "attachment-source",
        "attachment_sha256": "3" * 64,
        "resolved_dataset_artifact_id": "artifact-source",
        "resolved_view_sha256": "4" * 64,
    }


def test_pre_job_source_receipts_reject_judge_feedback(tmp_path: Path) -> None:
    receipts = _source_receipts()
    body = dict(receipts["feedback_attachment"])
    body.pop("content_sha256")
    body["judge_feedback_included"] = True
    receipts["feedback_attachment"] = _seal(body)

    with pytest.raises(
        NativeEvolutionRecoveryError,
        match="baseline, GT attachment, or terminal authority changed",
    ):
        _validate_source_receipts(receipts, _settings(tmp_path))


def test_native_recovery_dry_run_has_no_provider_or_candidate_calls(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    config = SimpleNamespace(
        experiment_root=tmp_path,
        raw={
            "native_engineering_recovery": {
                "task": settings.task_id,
                "source_run_id": settings.source_run_id,
                "recovery_run_id": settings.recovery_run_id,
                "source_runtime_generation_digest": (
                    settings.source_runtime_generation_digest
                ),
                "source_resolution_idempotency_key": (
                    settings.source_resolution_idempotency_key
                ),
                "source_resolution_prepared_path": None,
                "source_daemon_release_identity": (
                    settings.source_daemon_release_identity
                ),
                "source_framework_lock": str(settings.source_framework_lock),
                "source_release_bundle_manifest": str(
                    settings.source_release_bundle_manifest
                ),
                "source_failure_code": settings.source_failure_code,
                "source_transition_attempt_capacity": 2,
                "target_authorization_order": list(
                    settings.target_authorization_order
                ),
            }
        },
    )

    receipt = native_evolution_recovery_dry_run(
        config,
        source_run_id=settings.source_run_id,
        recovery_run_id=settings.recovery_run_id,
    )

    assert receipt["provider_calls"] == 0
    assert receipt["model_started"] is False
    assert receipt["candidate_executed"] is False
    assert receipt["judge_executed"] is False
    assert receipt["target_authorization_order"] == [
        "agent_system",
        "skill_bundle",
        "text_memory",
    ]


def test_new_recovery_reuses_sealed_source_resolution_and_restores_core_task_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    cross_check = {
        "successor_transition_id": "successor-source",
        "failed_pre_job_operation_id": "successor-attempt-source",
        "dataset_id": "dataset-source",
        "dataset_revision": "dataset-source.v1",
        "attachment_id": "attachment-source",
        "attachment_sha256": "3" * 64,
        "resolved_dataset_artifact_id": "artifact-source",
        "resolved_view_sha256": "4" * 64,
    }
    state = {
        "state_sha256": "5" * 64,
        "database_sha256_before": "6" * 64,
    }
    tree_sha256 = "7" * 64
    sealed_body = {
        "schema_version": (
            "openevo.researchclawbench.native_evolution_recovery_prepared.v1"
        ),
        "source_run_id": settings.source_run_id,
        "recovery_run_id": "prior-recovery",
        "task_id": settings.task_id,
        "supervisor_state_sha256": state["state_sha256"],
        "supervisor_database_sha256": state["database_sha256_before"],
        "supervisor_tree_sha256": tree_sha256,
        "source_core_cross_check": cross_check,
        "historical_attestation": {"attestation_id": "sealed-attestation"},
        "provider_calls": 0,
        "model_started": False,
    }
    sealed = _seal(sealed_body)
    sealed_path = tmp_path / "sealed-prepared-source.json"
    sealed_path.write_text(json.dumps(sealed), encoding="utf-8")
    settings = replace(
        settings,
        source_resolution_prepared_path=sealed_path,
    )

    class _Dump:
        def __init__(self, value: dict) -> None:
            self.value = value

        def model_dump(self, *, mode: str) -> dict:
            assert mode == "json"
            return self.value

    class _Client:
        def __init__(self, authority: object) -> None:
            assert authority is core_authority

        def close(self) -> None:
            return None

    core_authority = object()
    readiness = SimpleNamespace(
        execution_identity=_Dump({"core_generation": "8" * 64}),
        managed_reflector_credential_mount=_Dump({"authority_id": "mount"}),
        managed_reflector_runtime=_Dump({"runtime_identity": "managed"}),
    )
    monkeypatch.setattr(
        "openevo_researchclawbench.native_evolution_recovery.CoreControlV2Client",
        _Client,
    )
    adapter = object.__new__(NativeEvolutionRecoveryAdapter)
    adapter.settings = settings
    adapter.core_authority = core_authority
    adapter.recovery_root = tmp_path / "recovery"
    adapter._readiness = lambda client: readiness

    prepared = adapter._prepare_from_sealed_resolution(
        source={
            "core_cross_check": cross_check,
            "source_core_task_id": "task-source",
        },
        state=state,
        tree_sha256=tree_sha256,
    )

    assert prepared["source_core_task_id"] == "task-source"
    assert prepared["historical_attestation"] == sealed["historical_attestation"]
    assert prepared["sealed_source_resolution_prepared_sha256"] == sealed[
        "content_sha256"
    ]
    assert prepared["provider_calls"] == 0
    assert prepared["model_started"] is False
    assert sealed_path.read_text(encoding="utf-8") == json.dumps(sealed)


def test_completed_recovery_target_is_read_without_reposting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_id = "agent_system"
    result = SimpleNamespace(
        target_id=target_id,
        output=SimpleNamespace(artifact_type="agent_system"),
        reflector_runtime_receipt=SimpleNamespace(
            model_name="gpt-5.5",
            runtime_profile="managed_science",
            auth_mode="subscription",
            capture_mode="transcript",
        ),
        inference_budget_receipt=SimpleNamespace(
            actual_reflector_model_calls=1,
        ),
    )
    operation = SimpleNamespace(
        state="succeeded",
        result=result,
        model_dump=lambda *, mode: {
            "state": "succeeded",
            "target_id": target_id,
        },
    )
    snapshot = SimpleNamespace(
        record=SimpleNamespace(recovery_id="recovery-source")
    )

    class _Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def json(self, method: str, path: str, **_kwargs):
            self.calls.append((method, path))
            assert method == "GET"
            if "/targets/" in path:
                return operation
            return snapshot

    monkeypatch.setattr(
        "openevo_researchclawbench.native_evolution_recovery._strict_wire",
        lambda _model, payload: payload,
    )
    adapter = object.__new__(NativeEvolutionRecoveryAdapter)
    adapter.settings = _settings(tmp_path)
    adapter.config = SimpleNamespace(require=lambda _key: 1)
    adapter.recovery_root = tmp_path / "recovery"
    client = _Client()

    observed_snapshot, observed_operation = adapter._run_target(
        client,
        snapshot,
        target_id,
    )

    assert observed_snapshot is snapshot
    assert observed_operation is operation
    assert all(method == "GET" for method, _path in client.calls)


def test_successor_destination_run_id_obeys_existing_workspace_contract() -> None:
    first = _successor_destination_run_id(
        task_id="Life_005",
        recovery_run_id="rcb_oe_v0_native_life005_recovery",
    )
    repeated = _successor_destination_run_id(
        task_id="Life_005",
        recovery_run_id="rcb_oe_v0_native_life005_recovery",
    )
    other = _successor_destination_run_id(
        task_id="Life_005",
        recovery_run_id="rcb_oe_v0_native_life005_recovery_2",
    )

    assert first.startswith("Life_005_a1_recovery_")
    assert all(character.isalnum() or character in "_-" for character in first)
    assert first == repeated
    assert first != other
