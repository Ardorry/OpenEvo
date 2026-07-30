from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from openevo.backend.contracts.v2.app import create_core_control_v2_contract_app
from openevo.backend.contracts.v2.models import (
    EffectiveExecutionSnapshotRefV2,
    EvolutionRevisionRefV2,
    ProjectHeadRefV2,
    RuntimeContextSnapshotRefV2,
    WorkspaceSnapshotRefV2,
)
from openevo.backend.run_control import CoreTaskControlError
from openevo.backend.science_run_owner import (
    _successor_recovery_seed_authority_from_wire,
)
from openevo.backend.science_successor import (
    ScienceMethodOutputV2,
    ScienceSuccessorMethodPlanV2,
    ScienceSuccessorPlanV2,
    SealedTranscriptDatasetV2,
    TrainingFeedbackBindingV2,
    science_successor_plan_sha256,
)
from openevo.backend.science_successor_preparer_v2 import (
    ProductionScienceSuccessorPreparerV2,
    ScienceSuccessorPreparationV2Error,
    _recovery_seed_contributions,
    _recovery_seed_result_is_authorized,
    _recovery_seed_target_inventory_matches,
    _successor_context_task_tag,
)
from openevo.backend.science_successor_recovery_control_v1 import (
    install_core_managed_reflector_readiness_endpoint,
    install_core_science_successor_recovery_endpoint,
)
from openevo.backend.science_successor_recovery_executor_v1 import (
    ProductionManagedReflectorReadinessProviderV1,
    ProductionScienceSuccessorRecoveryExecutorV1,
    ProductionScienceSuccessorRecoveryNativeRunnerV1,
    _close_recovery_seed_resources,
    _completed_operation_is_bound_to_prior_record,
    _feedback_binding_from_authorities,
    _recovery_project_seed_failure_detail,
    _recovery_source_export_failure_code,
    _recovery_source_readback_failure_code,
    _validate_sealed_recovery_source_provenance,
    _validate_strict_wire_model,
)
from openevo.backend.science_successor_recovery_v1 import (
    AgentSystemAuditReceiptV1,
    ExhaustedSuccessorRecoveryAuthorityV1,
    FailedEvolutionJobAuthorityV1,
    FailedEvolutionPreJobOperationAuthorityV1,
    HistoricalEvolutionEvidenceDigestV1,
    HistoricalEvolutionSourceAttestationV1,
    HistoricalEvolutionSourceProvenanceV1,
    RecoveryExecutionIdentityV1,
    RecoveryTargetTerminalFailureV1,
    ScienceSuccessorRecoveryAdoptedJobAuthorityV1,
    ScienceSuccessorRecoveryAuthorityReaderV1,
    ScienceSuccessorRecoveryCarriedTargetAuthorityV1,
    ScienceSuccessorRecoveryCoordinatorV1,
    ScienceSuccessorRecoveryCreateRequestV1,
    ScienceSuccessorRecoveryProjectSeedRequestV1,
    ScienceSuccessorRecoveryReadinessErrorV1,
    ScienceSuccessorRecoverySourceResolveRequestV1,
    ScienceSuccessorRecoverySourceResponseV1,
    ScienceSuccessorRecoveryStoreV1,
    ScienceSuccessorRecoverySupersessionAuthorityV1,
    ScienceSuccessorRecoveryTargetExecutorV1,
    ScienceSuccessorRecoveryTargetFailureV1,
    ScienceSuccessorRecoveryTargetIntentV1,
    ScienceSuccessorRecoveryTargetReadinessV1,
    ScienceSuccessorRecoveryTargetResultV1,
    ScienceSuccessorRecoveryV1Error,
    science_successor_recovery_admission_report_sha256,
    science_successor_recovery_adoptable_job_result_sha256,
    science_successor_recovery_source_sha256,
)
from openevo.backend.service_supervisor import (
    ServiceExecutionMode,
    ServiceRunBinding,
    SupervisorStateError,
)
from openevo.evolution.admission import (
    ArtifactAdmissionEvidence,
    ArtifactContentAdmissionBasis,
    ArtifactProposalDecisionReceipt,
    ProposalAction,
    artifact_content_admission_receipt,
)
from openevo.evolution.framework import (
    EvolutionExecutionProfile,
    EvolutionTargetSelection,
    canonical_digest,
)
from openevo.evolution.framework.execution import (
    ReflectorInferenceBudgetReceipt,
    ReflectorRuntimeReceipt,
)
from openevo.evolution.managed_reflector import default_managed_reflector_runtime
from openevo.evolution.models import (
    ArtifactResponse,
    ArtifactState,
    ArtifactType,
    ReflectorInferenceReservationReceipt,
    SuccessorTransitionJobInventoryResponse,
)
from openevo.evolution.revisions import (
    AtomicSuccessorCommitV2,
    AtomicSuccessorRecoverySeedManifestV1,
    SuccessorArtifactContributionV2,
    atomic_successor_manifest_sha256,
)
from openevo.evolution.training_feedback import (
    CompletedDatasetAuthority,
    TrainingFeedbackAttachment,
)
from openevo.experiments.clients import EvolutionHttpStatusError
from openevo.experiments.compiler import CompiledEvolutionMethodSpec
from openevo.internal_auth import InternalServiceIdentity
from openevo.projects.science.compiler import MANAGED_RUNTIME_IMAGES
from openevo.runtime.managed import MANAGED_RUNTIME_RELEASES
from openevo.runtime.managed_reflector_mount import (
    ManagedReflectorCredentialMountReadiness,
    ManagedReflectorRuntimeReadiness,
)
from tests.framework_testkit import verified_builtin_registry


def _sha(character: str) -> str:
    return character * 64


def _runtime_readiness_from_mount(
    mount: ManagedReflectorCredentialMountReadiness | dict[str, object],
) -> ManagedReflectorRuntimeReadiness:
    mount_payload = (
        mount.model_dump(mode="json")
        if isinstance(mount, ManagedReflectorCredentialMountReadiness)
        else mount
    )
    payload: dict[str, object] = {
        "schema_version": "openevo.managed_reflector_readiness.v1",
        "authority_id": mount_payload["authority_id"],
        "container_authority_id": _sha("8"),
        "worker_launch_id": mount_payload["worker_launch_id"],
        "container_launch_id": "managed-reflector-runtime-readiness",
        "adoption_nonce_sha256": _sha("9"),
        "service_identity_digest": mount_payload["service_identity_digest"],
        "generation_digest": mount_payload["generation_digest"],
        "daemon_release_identity": mount_payload["daemon_release_identity"],
        "release_install_digest": mount_payload["release_install_digest"],
        "release_registry_digest": mount_payload["release_registry_digest"],
        "runtime_profile": mount_payload["runtime_profile"],
        "runtime_digest": mount_payload["runtime_digest"],
        "docker_host_path_identity": mount_payload["docker_host_path_identity"],
        "credential_mount_content_sha256": mount_payload["content_sha256"],
        "codex_binary": "/opt/codex/bin/codex",
        "expected_cli_version": "0.144.1",
        "actual_cli_version": "0.144.1",
        "model": "readiness-only",
        "auth_mode": "subscription",
        "capture_mode": "transcript",
        "path_fallback_allowed": False,
        "exit_status": 0,
        "container_authority_verified": True,
        "adoption_receipt_valid": True,
        "cleanup_verified": True,
        "codex_cli_started": True,
        "model_started": False,
        "created_at": "2026-07-29T01:00:01Z",
    }
    payload["content_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ManagedReflectorRuntimeReadiness.model_validate(payload)


def _agent_system_audit_receipt() -> AgentSystemAuditReceiptV1:
    payload = {
        "agent_system_audit_receipt_contract_version": "1",
        "enabled": True,
        "repair_count": 0,
        "finding_count": 0,
        "forbidden_literal_count": 7,
        "leakage_basis_sha256": _sha("b"),
    }
    payload["content_sha256"] = canonical_digest(payload)
    return AgentSystemAuditReceiptV1.model_validate(payload)


def _budget_receipt(target_id: str, method_id: str) -> ReflectorInferenceBudgetReceipt:
    payload = {
        "schema_version": "openevo.reflector_inference_budget_receipt.v1",
        "plan_id": "recovery-plan-test",
        "target_id": target_id,
        "method_id": method_id,
        "max_reflector_model_calls": 1,
        "actual_reflector_model_calls": 1,
    }
    payload["content_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ReflectorInferenceBudgetReceipt.model_validate(payload)


def _runtime_receipt(target_id: str) -> ReflectorRuntimeReceipt:
    return ReflectorRuntimeReceipt(
        request_id=f"request-{target_id}",
        session_id=f"session-{target_id}",
        runtime_profile="managed_science",
        runtime_digest=MANAGED_RUNTIME_RELEASES["managed_science"].trusted_digest.removeprefix(
            "sha256:"
        ),
        codex_binary="/opt/codex/bin/codex",
        actual_cli_version="0.144.1",
        model_name="gpt-5.5",
        reasoning_effort="high",
        auth_mode="subscription",
        capture_mode="transcript",
        path_fallback_allowed=False,
        exit_status=0,
        transcript_sha256=_sha("f"),
    )


def _inference_reservation(
    target_id: str,
    job_id: str,
) -> ReflectorInferenceReservationReceipt:
    runtime_receipt_sha256 = hashlib.sha256(
        json.dumps(
            _runtime_receipt(target_id).model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    payload = {
        "schema_version": "openevo.reflector_inference_reservation.v1",
        "reservation_id": f"reservation-{target_id}",
        "job_id": job_id,
        "request_id": f"request-{target_id}",
        "state": "completed",
        "max_model_calls": 1,
        "model_calls_started": 1,
        "started_at": "2026-07-29T01:00:00Z",
        "completed_at": "2026-07-29T01:00:01Z",
        "runtime_receipt_sha256": runtime_receipt_sha256,
    }
    payload["content_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ReflectorInferenceReservationReceipt.model_validate(payload)


def _admission_decision(
    target_id: str,
    job_id: str,
    report_sha256: str,
) -> ArtifactProposalDecisionReceipt:
    body = {
        "schema_version": "openevo.artifact_proposal_decision.v1",
        "decision_id": f"decision-{target_id}",
        "job_id": job_id,
        "artifact_type": ArtifactType(target_id),
        "action": ProposalAction.UPDATE,
        "parent_artifact_id": f"parent-{target_id}",
        "genesis": False,
        "proposal_artifact_ids": [f"proposal-{target_id}"],
        "selected_artifact_id": f"artifact-{target_id}-recovery",
        "rejected_artifact_ids": [],
        "active_artifact_id": f"artifact-{target_id}-recovery",
        "reason": "closed native admission evidence",
        "admission": ArtifactAdmissionEvidence(
            validator_id="core-native-recovery-admission-v1",
            schema_version="1",
            passed=True,
            report_sha256=report_sha256,
        ).model_dump(mode="json"),
        "content_admission": artifact_content_admission_receipt(
            basis=ArtifactContentAdmissionBasis(),
            payloads={
                f"artifact-{target_id}-recovery": {
                    "AGENTS.md": "Use a generic validation workflow."
                }
            },
        ).model_dump(mode="json"),
        "created_at": "2026-07-29T01:00:01Z",
    }
    body["proposal_artifact_ids"] = [f"artifact-{target_id}-recovery"]
    body["content_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ArtifactProposalDecisionReceipt.model_validate(body)


def _plan() -> ScienceSuccessorPlanV2:
    return ScienceSuccessorPlanV2(
        project_id="project-recovery",
        task_id="task-recovery",
        task_admission_id="admission-recovery",
        admission_sha256=_sha("1"),
        accepted_attempt_id="attempt-recovery",
        predecessor_project_head_id="head-recovery",
        normalized_evolution_intent_sha256=_sha("2"),
        enabled_methods=(
            ScienceSuccessorMethodPlanV2(
                target_id="agent_system",
                method_id="agent_system_gepa_reflector",
                output_artifact_type="agent_system",
            ),
            ScienceSuccessorMethodPlanV2(
                target_id="skill_bundle",
                method_id="skill_bundle_reflector",
                output_artifact_type="skill_bundle",
            ),
            ScienceSuccessorMethodPlanV2(
                target_id="text_memory",
                method_id="text_memory_expel_reflector",
                output_artifact_type="text_memory",
            ),
        ),
    )


def _dataset() -> SealedTranscriptDatasetV2:
    return SealedTranscriptDatasetV2(
        dataset_id="dataset-recovery",
        artifact_id="dataset-artifact-recovery",
        manifest_sha256=_sha("3"),
        record_count=1,
        task_id="task-recovery",
        task_admission_id="admission-recovery",
        accepted_attempt_id="attempt-recovery",
        capture_mode="transcript",
        token_level_metrics_available=False,
        sealed=True,
        training_feedback=TrainingFeedbackBindingV2(
            completed_dataset_id="dataset-recovery",
            completed_dataset_revision="dataset-recovery.v1",
            attachment_ids=("attachment-recovery",),
            attachment_sha256=(_sha("4"),),
            resolved_dataset_artifact_id="resolved-dataset-recovery",
            resolved_view_sha256=_sha("5"),
            task_scope_id="task-recovery",
        ),
    )


def _private_task_local_attachment() -> TrainingFeedbackAttachment:
    body = {
        "schema_version": "openevo.training_feedback_attachment.v2",
        "attachment_id": "attachment-recovery",
        "revision": 1,
        "session_id": "session-recovery",
        "task_id": "task-recovery",
        "task_scope_id": "task-recovery",
        "dataset_id": "dataset-recovery",
        "dataset_revision": "dataset-recovery.v1",
        "producer": "trusted_evaluator",
        "authority_id": "evaluator-authority-recovery",
        "authority": "evaluator_only",
        "feedback_class": "HARD_GT",
        "global_feedback": {},
        "task_local_feedback": {
            "benchmark_task_scope_id": "Astronomy_004",
            "source_files": ["private-current-task.csv"],
            "expected": "task-specific-value",
            "required_sections_missing": ["Methods", "Results", "Discussion"],
            "report_headings": [
                "Luminosity Calibration of QZO Survey",
                "Experiments",
                "Data and Pipeline",
                "Limitations and Reproducibility",
                "Related Work and Motivation",
            ],
        },
        "created_at": "2026-07-29T01:00:00Z",
        "source_session_result_sha256": _sha("a"),
        "source_dataset_manifest_sha256": _sha("3"),
        "status": "sealed",
    }
    body["content_sha256"] = canonical_digest(body)
    return TrainingFeedbackAttachment.model_validate(body)


def test_recovery_feedback_distinguishes_rollout_task_from_core_scope() -> None:
    raw = _private_task_local_attachment().model_dump(mode="json")
    raw["task_id"] = "rollout-task-recovery"
    raw["source_dataset_manifest_sha256"] = _sha("c")
    raw.pop("content_sha256")
    raw["content_sha256"] = canonical_digest(raw)
    attachment = TrainingFeedbackAttachment.model_validate(raw)
    completed = CompletedDatasetAuthority(
        completed_dataset_id="dataset-recovery",
        completed_dataset_revision="dataset-recovery.v1",
        dataset_artifact_id="dataset-artifact-recovery",
        dataset_manifest_sha256=_sha("3"),
        task_id="rollout-task-recovery",
        session_id="session-recovery",
        source_event_id="session:session-recovery",
        source_session_result_sha256=_sha("a"),
    )
    artifact = ArtifactResponse(
        artifact_id="resolved-dataset-recovery",
        type=ArtifactType.DATASET,
        name="resolved recovery dataset",
        version=1,
        state=ArtifactState.ACTIVE,
        uri="memory://resolved-dataset-recovery",
        manifest={
            "create_identity": "resolve-recovery-feedback",
            "derived_from_dataset_id": "dataset-recovery",
            "source_dataset_manifest_sha256": _sha("c"),
            "source_session_result_sha256": _sha("a"),
            "training_feedback_attachment_ids": [attachment.attachment_id],
            "training_feedback_attachment_sha256": [attachment.content_sha256],
            "records_sha256": _sha("b"),
        },
        promoted=True,
    )

    binding = _feedback_binding_from_authorities(
        context=SimpleNamespace(task=SimpleNamespace(task_id="task-recovery")),
        base_dataset=_dataset().model_copy(update={"training_feedback": None}),
        completed=completed,
        attachments=(attachment,),
        resolved_artifact=artifact,
    )

    assert binding.task_scope_id == "task-recovery"
    assert binding.completed_dataset_id == "dataset-recovery"


def test_recovery_leakage_basis_is_derived_from_verified_task_local_authority() -> None:
    raw_attachment = _private_task_local_attachment().model_dump(mode="json")
    raw_attachment["task_id"] = "rollout-task-recovery"
    raw_attachment.pop("content_sha256")
    raw_attachment["content_sha256"] = canonical_digest(raw_attachment)
    attachment = TrainingFeedbackAttachment.model_validate(raw_attachment)
    dataset = _dataset().model_copy(
        update={
            "training_feedback": _dataset().training_feedback.model_copy(
                update={"attachment_sha256": (attachment.content_sha256,)}
            )
        }
    )
    client = SimpleNamespace(
        get_training_feedback_attachment=lambda _attachment_id: attachment.model_dump(mode="json")
    )

    basis = ProductionScienceSuccessorPreparerV2._recovery_leakage_basis(
        SimpleNamespace(task=SimpleNamespace(task_id="task-recovery")),
        dataset,
        client,
        rollout_task_id="rollout-task-recovery",
    )

    assert basis["task_ids"] == [
        "Astronomy_004",
        "rollout-task-recovery",
        "task-recovery",
    ]
    assert basis["task_specific_feedback"] == [
        {
            "benchmark_task_scope_id": "Astronomy_004",
            "source_files": ["private-current-task.csv"],
            "source_titles": ["Luminosity Calibration of QZO Survey"],
        }
    ]
    serialized = json.dumps(basis, sort_keys=True)
    assert "Experiments" not in serialized
    assert "Data and Pipeline" not in serialized
    assert "Limitations and Reproducibility" not in serialized
    assert "Related Work and Motivation" not in serialized
    assert "task-specific-value" not in serialized

    with pytest.raises(
        ScienceSuccessorPreparationV2Error,
        match="verified feedback authority",
    ) as raised:
        ProductionScienceSuccessorPreparerV2._recovery_leakage_basis(
            SimpleNamespace(task=SimpleNamespace(task_id="another-task")),
            dataset,
            client,
            rollout_task_id="rollout-task-recovery",
        )
    assert raised.value.failure_code == "recovery_training_feedback_authority_mismatch"


def _source() -> ExhaustedSuccessorRecoveryAuthorityV1:
    plan = _plan()
    return ExhaustedSuccessorRecoveryAuthorityV1(
        project_id=plan.project_id,
        task_id=plan.task_id,
        task_admission_id=plan.task_admission_id,
        accepted_attempt_id=plan.accepted_attempt_id,
        successor_transition_id="transition-recovery-source",
        successor_transition_state="failed",
        successor_transition_state_sha256=_sha("6"),
        transition_attempt_count=2,
        transition_attempt_capacity=2,
        source_commit_absent=True,
        source_outputs_absent=True,
        predecessor_project_head_id=plan.predecessor_project_head_id,
        predecessor_project_head_sha256=_sha("7"),
        active_project_head_id=plan.predecessor_project_head_id,
        active_project_head_sha256=_sha("7"),
        plan_sha256=science_successor_plan_sha256(plan),
        source_core_generation=_sha("8"),
        source_release_identity=_sha("9"),
        source_provenance=HistoricalEvolutionSourceProvenanceV1(
            provenance_tier="legacy_supervisor_attested",
            core_native=False,
            authority_minted=False,
            attestation_id="attestation-recovery-source",
            attestation_sha256=_sha("a"),
            resolution_core_generation=_sha("b"),
            resolution_daemon_release_identity=_sha("c"),
            resolution_release_install_digest=_sha("d"),
            current_core_authorities_reproduced=True,
        ),
        dataset=_dataset(),
        failed_job=FailedEvolutionJobAuthorityV1(
            job_id="job-recovery-source",
            successor_transition_id="transition-recovery-source",
            target_id="agent_system",
            method_id="agent_system_gepa_reflector",
            artifact_type="agent_system",
            state="failed",
            retryable=False,
            output_artifact_ids=(),
            job_result_sha256=_sha("0"),
            failure_code="credential_mount_adoption_failed",
        ),
    )


def _historical_attestation(
    *,
    attestation_id: str = "attestation-recovery-source",
) -> HistoricalEvolutionSourceAttestationV1:
    body = {
        "historical_evolution_source_attestation_contract_version": "1",
        "attestation_id": attestation_id,
        "provenance_tier": "legacy_supervisor_attested",
        "core_native": False,
        "authority_minted": False,
        "supervisor_namespace": "legacy-supervisor-source",
        "supervisor_observed_state": "EVOLUTION_RUNNING",
        "supervisor_state_sha256": _sha("1"),
        "supervisor_database_sha256": _sha("2"),
        "supervisor_tree_sha256": _sha("3"),
        "legacy_supervisor_inventory_sha256": _sha("6"),
        "terminal_classification_sha256": _sha("7"),
        "source_transition_id": "transition-recovery-source",
        "failed_job_id": "job-recovery-source",
        "claimed_source_core_generation": _sha("8"),
        "claimed_source_release_identity": _sha("9"),
        "claimed_failure_code": "credential_mount_adoption_failed",
        "evidence_inventory": (
            HistoricalEvolutionEvidenceDigestV1(
                label="core_failure_authority",
                sha256=_sha("4"),
            ).model_dump(mode="json"),
            HistoricalEvolutionEvidenceDigestV1(
                label="framework_lock",
                sha256=_sha("5"),
            ).model_dump(mode="json"),
            HistoricalEvolutionEvidenceDigestV1(
                label="legacy_supervisor_inventory",
                sha256=_sha("6"),
            ).model_dump(mode="json"),
            HistoricalEvolutionEvidenceDigestV1(
                label="release_bundle_manifest",
                sha256=_sha("8"),
            ).model_dump(mode="json"),
            HistoricalEvolutionEvidenceDigestV1(
                label="terminal_classification",
                sha256=_sha("7"),
            ).model_dump(mode="json"),
        ),
    }
    body["content_sha256"] = canonical_digest(body)
    return HistoricalEvolutionSourceAttestationV1.model_validate(body)


def _source_resolution_request(
    *,
    idempotency_key: str = "resolve-historical-source-once",
    attestation: HistoricalEvolutionSourceAttestationV1 | None = None,
) -> ScienceSuccessorRecoverySourceResolveRequestV1:
    return ScienceSuccessorRecoverySourceResolveRequestV1(
        resolution_idempotency_key=idempotency_key,
        successor_transition_id="transition-recovery-source",
        failed_job_id="job-recovery-source",
        historical_attestation=attestation or _historical_attestation(),
        attachment_ids=("attachment-recovery",),
        resolved_dataset_artifact_id="resolved-dataset-recovery",
        transition_attempt_capacity=2,
    )


def _source_resolution_response(
    attestation: HistoricalEvolutionSourceAttestationV1,
) -> ScienceSuccessorRecoverySourceResponseV1:
    source = _source().model_copy(
        update={
            "source_provenance": HistoricalEvolutionSourceProvenanceV1(
                provenance_tier="legacy_supervisor_attested",
                core_native=False,
                authority_minted=False,
                attestation_id=attestation.attestation_id,
                attestation_sha256=attestation.content_sha256,
                resolution_core_generation=_sha("b"),
                resolution_daemon_release_identity=_sha("c"),
                resolution_release_install_digest=_sha("d"),
                current_core_authorities_reproduced=True,
            )
        }
    )
    return ScienceSuccessorRecoverySourceResponseV1(source=source, plan=_plan())


def test_historical_attestation_is_hash_bound_and_replaces_bare_identity_claims() -> None:
    attestation = _historical_attestation()
    assert attestation.supervisor_observed_state == "EVOLUTION_RUNNING"
    assert not hasattr(attestation, "supervisor_terminal_state")
    raw = attestation.model_dump(mode="python")
    raw["claimed_failure_code"] = "changed-failure"
    with pytest.raises(ValidationError, match="digest"):
        HistoricalEvolutionSourceAttestationV1.model_validate(raw)

    request = _source_resolution_request()
    legacy = request.model_dump(mode="python")
    legacy["source_core_generation"] = _sha("8")
    with pytest.raises(ValidationError, match="extra"):
        ScienceSuccessorRecoverySourceResolveRequestV1.model_validate(legacy)


def test_historical_attestation_requires_exact_evidence_labels_and_bound_digests() -> None:
    attestation = _historical_attestation()
    raw = attestation.model_dump(mode="json")
    raw.pop("content_sha256")
    raw["evidence_inventory"][0]["label"] = "unexpected-evidence"
    raw["content_sha256"] = canonical_digest(raw)
    with pytest.raises(ValidationError, match="exact closed set"):
        HistoricalEvolutionSourceAttestationV1.model_validate_json(
            json.dumps(raw, allow_nan=False, separators=(",", ":"), sort_keys=True)
        )

    raw = attestation.model_dump(mode="json")
    raw.pop("content_sha256")
    raw["legacy_supervisor_inventory_sha256"] = _sha("f")
    raw["content_sha256"] = canonical_digest(raw)
    with pytest.raises(ValidationError, match="identity fields"):
        HistoricalEvolutionSourceAttestationV1.model_validate_json(
            json.dumps(raw, allow_nan=False, separators=(",", ":"), sort_keys=True)
        )

    raw = attestation.model_dump(mode="json")
    raw["supervisor_terminal_state"] = raw["supervisor_observed_state"]
    raw["content_sha256"] = canonical_digest(
        {key: value for key, value in raw.items() if key != "content_sha256"}
    )
    with pytest.raises(ValidationError, match="extra"):
        HistoricalEvolutionSourceAttestationV1.model_validate_json(
            json.dumps(raw, allow_nan=False, separators=(",", ":"), sort_keys=True)
        )


def test_agent_system_audit_receipt_is_hash_bound_and_contains_no_literals() -> None:
    receipt = _agent_system_audit_receipt()
    assert receipt.forbidden_literal_count == 7
    public = json.dumps(receipt.model_dump(mode="json"), sort_keys=True)
    assert "Astronomy_004" not in public
    assert "private-current-task.csv" not in public

    raw = receipt.model_dump(mode="json")
    raw["repair_count"] = 1
    with pytest.raises(ValidationError, match="digest"):
        AgentSystemAuditReceiptV1.model_validate(raw)


@pytest.mark.parametrize("audit_variant", ["missing", "tampered"])
def test_run_recovery_method_rejects_invalid_agent_system_audit_before_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    audit_variant: str,
) -> None:
    """A worker proposal cannot reach admission before its typed audit passes."""

    request, binding, _health = _production_recovery_fixture()
    source = request.source
    recovery_id = "recovery-audit-ordering"
    intent = ScienceSuccessorRecoveryTargetIntentV1(
        operation_id="operation-audit-ordering",
        idempotency_key="audit-ordering-once",
        recovery_id=recovery_id,
        source_authority_sha256=science_successor_recovery_source_sha256(source),
        execution_identity=request.execution_identity,
        target=request.plan.enabled_methods[0],
        state="running",
        created_at="2026-07-29T01:00:00Z",
    )
    context = SimpleNamespace(
        plan=request.plan,
        transition=SimpleNamespace(
            state="failed",
            transition=SimpleNamespace(
                successor_transition_id=source.successor_transition_id,
            ),
        ),
        transition_attempt=SimpleNamespace(
            state="failed",
            transition_attempt_id="source-transition-attempt",
            ordinal=2,
        ),
    )
    registry = verified_builtin_registry(tmp_path / "registry")
    native_plan = registry.snapshot.compile_plan(
        plan_id="plan-audit-ordering",
        selections=(
            EvolutionTargetSelection(
                target_id="agent_system",
                enabled=True,
                method_id="agent_system_gepa_reflector",
                config={
                    "reflector_llm": {
                        "provider": "codex_cli",
                        "model": "gpt-5.5",
                        "reasoning_effort": "high",
                        "timeout_seconds": 30,
                        "runtime": default_managed_reflector_runtime().model_dump(mode="json"),
                    }
                },
            ),
        ),
        profile=EvolutionExecutionProfile(
            execution_mode="self_deployed",
            capture_mode="transcript",
            harness_id="codex",
        ),
    )
    selection = native_plan.selections[0]
    spec = CompiledEvolutionMethodSpec(
        artifact_type="agent_system",
        target_id="agent_system",
        handler_id=selection.handler_id,
        method=selection.method_id,
        requested_method="agent_system_gepa_reflector",
        prior_dataset_artifact_ids=(),
        job_type="agent_system_gepa_reflector",
        config=selection.config(),
        plan=native_plan,
        selection=selection,
        input_bindings=(),
    )

    class CompiledTask:
        @staticmethod
        def evolution_job_payloads_for_round(
            round_index,
            method_specs,
            *,
            dataset_artifact_id,
            context_artifact_ids,
        ):
            assert round_index == 0
            assert tuple(method_specs) == (spec,)
            assert dataset_artifact_id == (
                source.dataset.training_feedback.resolved_dataset_artifact_id
            )
            assert context_artifact_ids == {}
            return [
                {
                    "config": {
                        "agent_system_audit": {
                            "enabled": True,
                            "max_repair_attempts": 0,
                        }
                    },
                    "input_bindings": [],
                }
            ]

    job_id = "job-audit-ordering"
    artifact_id = "artifact-audit-ordering"
    runtime_receipt = _runtime_receipt("agent_system")
    reservation = _inference_reservation("agent_system", job_id)
    leakage_basis = {
        "task_ids": ["task-recovery"],
        "task_specific_feedback": [],
    }

    class Client:
        def __init__(self) -> None:
            self.proposal: ArtifactResponse | None = None
            self.promotion_reads = 0

        def get_internal_successor_artifact(self, transition_id, observed_artifact_id):
            assert transition_id == recovery_id
            assert observed_artifact_id == artifact_id
            assert self.proposal is not None
            return self.proposal.model_dump(mode="json")

        def get_internal_reflector_inference_reservation(self, observed_job_id):
            assert observed_job_id == job_id
            return reservation.model_dump(mode="json")

        def get_internal_successor_artifact_authority(self, *_args):
            self.promotion_reads += 1
            raise AssertionError("promotion authority must not be read")

    client = Client()
    preparer = ProductionScienceSuccessorPreparerV2(
        catalog=object(),
        ledger=object(),
        workspaces=object(),
        workspace_handoffs=object(),
        services=object(),
        executable_registry=registry,
        artifact_admission_root=tmp_path / "admission",
    )
    project = SimpleNamespace(
        config=SimpleNamespace(
            execution=SimpleNamespace(
                codex_model="gpt-5.5",
                reasoning_effort="high",
            ),
        )
    )
    monkeypatch.setattr(
        preparer,
        "_authority",
        lambda _context: (
            SimpleNamespace(
                receipt=SimpleNamespace(rollout_task_id="task-recovery")
            ),
            object(),
            project,
        ),
    )
    monkeypatch.setattr(
        preparer,
        "_compile_methods",
        lambda _context, **_kwargs: (
            SimpleNamespace(tasks=(CompiledTask(),)),
            (spec,),
        ),
    )
    monkeypatch.setattr(
        preparer,
        "_prior_context_artifacts",
        lambda _context, _client: ({}, (), {}),
    )
    monkeypatch.setattr(
        preparer,
        "_recovery_leakage_basis",
        lambda _context, _dataset, _client, *, rollout_task_id: leakage_basis,
    )

    def run_one_method(_client, *, spec, **_kwargs):
        budget_payload = {
            "schema_version": "openevo.reflector_inference_budget_receipt.v1",
            "plan_id": spec.plan.plan_id,
            "target_id": "agent_system",
            "method_id": "agent_system_gepa_reflector",
            "max_reflector_model_calls": 1,
            "actual_reflector_model_calls": 1,
        }
        budget_payload["content_sha256"] = canonical_digest(budget_payload)
        manifest = {
            "openevo_reflector_inference_budget": budget_payload,
            "reflector_runtime_receipt": runtime_receipt.model_dump(mode="json"),
        }
        if audit_variant == "tampered":
            manifest["agent_system_audit"] = {
                "enabled": True,
                "repair_count": 0,
                "finding_count": 0,
                "forbidden_literal_count": 1,
                "leakage_basis_sha256": _sha("0"),
            }
        client.proposal = ArtifactResponse(
            artifact_id=artifact_id,
            type=ArtifactType.AGENT_SYSTEM,
            name="unadmitted agent system",
            version=1,
            state="sealed",
            uri="file:///opaque/unadmitted-agent-system",
            manifest=manifest,
            promoted=False,
        )
        return SimpleNamespace(
            job_id=job_id,
            output=ScienceMethodOutputV2(
                target_id="agent_system",
                method_id="agent_system_gepa_reflector",
                artifact_id=artifact_id,
                artifact_type="agent_system",
                manifest_sha256=_sha("c"),
                byte_size=128,
                execution_boundary="outside_inference",
            ),
        )

    monkeypatch.setattr(preparer, "_run_one_method", run_one_method)
    admission_authority_calls: list[str] = []
    admission_decision_calls: list[str] = []

    def issue_authority(_service, *, producer):
        admission_authority_calls.append(producer)
        raise AssertionError("admission authority must not be issued")

    def decide(_service, *, authority, request):
        del authority, request
        admission_decision_calls.append("decide")
        raise AssertionError("admission decision must not run")

    monkeypatch.setattr(
        "openevo.backend.science_successor_preparer_v2."
        "NativeArtifactAdmissionService.issue_admission_authority",
        issue_authority,
    )
    monkeypatch.setattr(
        "openevo.backend.science_successor_preparer_v2.NativeArtifactAdmissionService.decide",
        decide,
    )

    with pytest.raises(
        ScienceSuccessorPreparationV2Error,
        match="lacks its sanitized audit summary",
    ):
        preparer.run_recovery_method(
            context,
            source.dataset,
            intent=intent,
            source=source,
            binding=binding,
            client=client,
        )

    assert admission_authority_calls == []
    assert admission_decision_calls == []
    assert client.promotion_reads == 0


def test_historical_source_resolution_is_append_only_idempotent_and_unique(
    tmp_path: Path,
) -> None:
    store = ScienceSuccessorRecoveryStoreV1(tmp_path / "recovery-store")
    attestation = _historical_attestation()
    request = _source_resolution_request(attestation=attestation)
    response = _source_resolution_response(attestation)

    first = store.seal_historical_source_resolution(request, response)
    replay = store.seal_historical_source_resolution(request, response)
    store.verify_historical_source_resolution(response)

    assert replay == first == response
    with pytest.raises(
        ScienceSuccessorRecoveryV1Error,
        match="another resolution",
    ):
        store.seal_historical_source_resolution(
            request.model_copy(
                update={"resolution_idempotency_key": "different-resolution-operation"}
            ),
            response,
        )


def test_historical_source_resolution_rejects_unsealed_or_conflicting_authority(
    tmp_path: Path,
) -> None:
    store = ScienceSuccessorRecoveryStoreV1(tmp_path / "recovery-store")
    attestation = _historical_attestation()
    request = _source_resolution_request(attestation=attestation)
    response = _source_resolution_response(attestation)

    with pytest.raises(
        ScienceSuccessorRecoveryV1Error,
        match="lacks its sealed resolution",
    ):
        store.verify_historical_source_resolution(response)

    conflicting = response.model_copy(
        update={
            "source": response.source.model_copy(
                update={
                    "source_provenance": response.source.source_provenance.model_copy(
                        update={"attestation_sha256": _sha("f")}
                    )
                }
            )
        }
    )
    with pytest.raises(
        ScienceSuccessorRecoveryV1Error,
        match="differs from its attestation",
    ):
        store.seal_historical_source_resolution(request, conflicting)


def _request(
    *,
    idempotency_key: str = "create-recovery-once",
    source: ExhaustedSuccessorRecoveryAuthorityV1 | None = None,
) -> ScienceSuccessorRecoveryCreateRequestV1:
    return ScienceSuccessorRecoveryCreateRequestV1(
        idempotency_key=idempotency_key,
        source=source or _source(),
        plan=_plan(),
        execution_identity=RecoveryExecutionIdentityV1(
            core_generation=_sha("a"),
            daemon_release_identity=_sha("b"),
            release_install_digest=_sha("5"),
            executable_registry_digest=_sha("c"),
            framework_lock_digest=_sha("d"),
            runtime_identity_digest=_sha("e"),
            runtime_image_digest=f"sha256:{_sha('f')}",
            service_identity_id="evolution-service-recovery",
            runtime_profile="managed_science",
            max_reflector_model_calls=1,
        ),
        target_authorization_order=(
            "agent_system",
            "text_memory",
            "skill_bundle",
        ),
        source_mutation_allowed=False,
        candidate_execution_allowed=False,
        automatic_target_progression=False,
        automatic_successor_commit=False,
    )


class _AuthorityReader(ScienceSuccessorRecoveryAuthorityReaderV1):
    def __init__(self, source: ExhaustedSuccessorRecoveryAuthorityV1) -> None:
        self.source = source
        self.reads = 0

    def read_exhausted_successor_authority(
        self,
        expected: ExhaustedSuccessorRecoveryAuthorityV1,
        plan: ScienceSuccessorPlanV2,
    ) -> ExhaustedSuccessorRecoveryAuthorityV1:
        assert expected.successor_transition_id == self.source.successor_transition_id
        assert plan == _plan()
        self.reads += 1
        return self.source


def _carried_authority_for_test(prior_recovery_id, operation):
    assert operation.result is not None
    payload = {
        "successor_recovery_carried_target_authority_contract_version": "1",
        "prior_recovery_id": operation.intent.recovery_id,
        "prior_intent": operation.intent,
        "prior_result": operation.result,
        "prior_intent_sha256": canonical_digest(operation.intent),
        "prior_result_sha256": canonical_digest(operation.result),
    }
    if operation.intent.recovery_id != prior_recovery_id:
        payload["carried_from_recovery_id"] = prior_recovery_id
    payload["content_sha256"] = canonical_digest(payload)
    return ScienceSuccessorRecoveryCarriedTargetAuthorityV1.model_validate(payload)


class _Executor(ScienceSuccessorRecoveryTargetExecutorV1):
    def __init__(self) -> None:
        self.readiness_calls: list[str] = []
        self.readiness_releases: list[str] = []
        self.execute_calls: list[str] = []
        self.recover_calls: list[str] = []
        self.supersession_calls: list[str] = []
        self.adopt_calls: list[str] = []

    def attest_recovery_supersession(
        self,
        *,
        prior,
        failure,
        source,
        plan,
        execution_identity,
        completed_operations=(),
    ):
        assert prior.record.source_transition_id == source.successor_transition_id
        assert plan == _plan()
        self.supersession_calls.append(prior.record.recovery_id)
        payload = {
            "successor_recovery_supersession_authority_contract_version": "1",
            "prior_recovery_id": prior.record.recovery_id,
            "prior_recovery_record_sha256": prior.record_sha256,
            "prior_operation_id": failure.operation_id,
            "prior_failure_sha256": canonical_digest(failure),
            "source_transition_id": source.successor_transition_id,
            "evolution_job_ids": tuple(
                operation.result.job_id
                for operation in completed_operations
                if operation.result is not None
                and (
                    operation.result.job_owner_successor_transition_id
                    or operation.result.recovery_id
                )
                == prior.record.recovery_id
            ),
            "evolution_job_inventory_sha256": _sha("9"),
            "paid_model_side_effect_absent": True,
            **({"carried_completed_targets": tuple(
                _carried_authority_for_test(prior.record.recovery_id, operation)
                for operation in completed_operations
            )} if completed_operations else {}),
            "core_generation": execution_identity.core_generation,
            "daemon_release_identity": execution_identity.daemon_release_identity,
            "created_at": "2026-07-29T01:00:02Z",
        }
        payload["content_sha256"] = canonical_digest(payload)
        return ScienceSuccessorRecoverySupersessionAuthorityV1.model_validate(
            payload
        )

    def check_target_readiness(self, preflight, *, source, plan):
        del source, plan
        self.readiness_calls.append(preflight.target.target_id)
        mount_payload = {
            "schema_version": ("openevo.managed_reflector_credential_mount_readiness.v1"),
            "authority_id": _sha("1"),
            "container_authority_id": _sha("5"),
            "worker_launch_id": f"mrl-{'2' * 32}",
            "container_launch_id": "recovery-readiness-container",
            "adoption_nonce_sha256": _sha("6"),
            "service_identity_digest": _sha("7"),
            "runtime_profile": preflight.execution_identity.runtime_profile,
            "runtime_digest": preflight.execution_identity.runtime_image_digest,
            "docker_host_path_identity": _sha("3"),
            "generation_digest": preflight.execution_identity.core_generation,
            "daemon_release_identity": (preflight.execution_identity.daemon_release_identity),
            "release_install_digest": preflight.execution_identity.release_install_digest,
            "release_registry_digest": (preflight.execution_identity.executable_registry_digest),
            "credential_target": "/openevo/credentials/codex",
            "container_uid": 1000,
            "container_gid": 1000,
            "authority_issued": True,
            "docker_mount_created": True,
            "container_path_visible": True,
            "container_user_can_read": True,
            "auth_file_read_only": True,
            "generation_matches": True,
            "release_identity_matches": True,
            "adoption_receipt_valid": True,
            "container_authority_verified": True,
            "cleanup_verified": True,
            "codex_cli_started": False,
            "model_started": False,
            "created_at": "2026-07-29T01:00:00Z",
        }
        mount_payload["content_sha256"] = hashlib.sha256(
            json.dumps(
                mount_payload,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        mount = ManagedReflectorCredentialMountReadiness.model_validate(mount_payload)
        return ScienceSuccessorRecoveryTargetReadinessV1(
            operation_id=preflight.operation_id,
            recovery_id=preflight.recovery_id,
            target_id=preflight.target.target_id,
            execution_identity=preflight.execution_identity,
            framework_lock_digest=(preflight.execution_identity.framework_lock_digest),
            runtime_identity_digest=(preflight.execution_identity.runtime_identity_digest),
            managed_reflector_credential_mount=mount,
            managed_reflector_runtime=_runtime_readiness_from_mount(mount),
            checked_at="2026-07-29T01:00:00Z",
        )

    def release_target_readiness(self, preflight):
        self.readiness_releases.append(preflight.target.target_id)

    def execute_target(self, intent, *, source, plan):
        del source, plan
        self.execute_calls.append(intent.target.target_id)
        job_id = f"job-{intent.target.target_id}-recovery"
        artifact_id = f"artifact-{intent.target.target_id}-recovery"
        runtime_receipt = _runtime_receipt(intent.target.target_id)
        reservation = _inference_reservation(intent.target.target_id, job_id)
        budget_receipt = _budget_receipt(
            intent.target.target_id,
            intent.target.method_id,
        )
        audit_receipt = (
            _agent_system_audit_receipt()
            if intent.target.output_artifact_type == "agent_system"
            else None
        )
        report_sha256 = science_successor_recovery_admission_report_sha256(
            job_id=job_id,
            target_id=intent.target.target_id,
            method_id=intent.target.method_id,
            proposal_artifact_id=artifact_id,
            proposal_payload_manifest_sha256=_sha("c"),
            registry_artifact_manifest_sha256=_sha("e"),
            agent_system_audit_receipt_sha256=(
                None if audit_receipt is None else audit_receipt.content_sha256
            ),
            reflector_runtime_receipt_sha256=canonical_digest(runtime_receipt),
            reflector_inference_reservation_sha256=reservation.content_sha256,
            reflector_inference_budget_sha256=budget_receipt.content_sha256,
        )
        return ScienceSuccessorRecoveryTargetResultV1(
            operation_id=intent.operation_id,
            recovery_id=intent.recovery_id,
            target_id=intent.target.target_id,
            method_id=intent.target.method_id,
            source_failed_job_id="job-recovery-source",
            source_failed_job_retried=False,
            job_id=job_id,
            job_state="succeeded",
            proposal_ids=(artifact_id,),
            admission_status="accepted",
            promotion_status="promoted",
            output=ScienceMethodOutputV2(
                target_id=intent.target.target_id,
                method_id=intent.target.method_id,
                artifact_id=artifact_id,
                artifact_type=intent.target.output_artifact_type,
                manifest_sha256=_sha("c"),
                byte_size=100,
                execution_boundary="outside_inference",
            ),
            registry_artifact_id=artifact_id,
            registry_manifest_sha256=_sha("c"),
            registry_artifact_manifest_sha256=_sha("e"),
            agent_system_audit=audit_receipt,
            admission_decision=_admission_decision(
                intent.target.target_id,
                job_id,
                report_sha256,
            ),
            reflector_runtime_receipt=runtime_receipt,
            reflector_inference_reservation=reservation,
            inference_budget_receipt=budget_receipt,
            completed_at="2026-07-29T01:00:01Z",
        )

    def recover_target(self, intent, *, source, plan):
        del source, plan
        self.recover_calls.append(intent.target.target_id)

    def adopt_target(self, intent, *, source, plan, adopted_job):
        assert adopted_job.target_id == intent.target.target_id
        self.adopt_calls.append(adopted_job.job_id)
        prior_execute_calls = list(self.execute_calls)
        result = self.execute_target(intent, source=source, plan=plan)
        self.execute_calls[:] = prior_execute_calls
        return result


def _coordinator(
    tmp_path: Path,
    *,
    source: ExhaustedSuccessorRecoveryAuthorityV1 | None = None,
) -> tuple[
    ScienceSuccessorRecoveryCoordinatorV1,
    _AuthorityReader,
    _Executor,
]:
    source = source or _source()
    authority = _AuthorityReader(source)
    executor = _Executor()
    store = ScienceSuccessorRecoveryStoreV1(tmp_path / "recovery-store")
    return (
        ScienceSuccessorRecoveryCoordinatorV1(
            store=store,
            authority_reader=authority,
            executor=executor,
            clock=lambda: "2026-07-29T01:00:00Z",
        ),
        authority,
        executor,
    )


def test_exhausted_failure_creates_append_only_target_gated_recovery(
    tmp_path: Path,
) -> None:
    coordinator, authority, executor = _coordinator(tmp_path)

    created = coordinator.create_recovery(_request())
    replayed = coordinator.create_recovery(_request())
    completed = coordinator.run_authorized_target(
        recovery_id=created.record.recovery_id,
        target_id="agent_system",
        idempotency_key="run-agent-system-once",
    )

    assert replayed == created
    assert created.record.state == "awaiting_target_authorization"
    assert completed.record.state == "awaiting_target_authorization"
    assert completed.record.active_target_id is None
    assert completed.record.completed_target_ids == ("agent_system",)
    assert completed.record.remaining_target_ids == (
        "text_memory",
        "skill_bundle",
    )
    assert executor.execute_calls == ["agent_system"]
    assert executor.recover_calls == []
    assert executor.readiness_calls == ["agent_system"]
    assert executor.readiness_releases == ["agent_system"]
    assert authority.reads == 3
    assert completed.record.source_transition_id == "transition-recovery-source"
    assert completed.record.source_failed_job_id == "job-recovery-source"
    assert completed.record.source_authority_sha256 == science_successor_recovery_source_sha256(
        _source()
    )


def test_completed_target_replay_never_executes_or_authorizes_the_next_target(
    tmp_path: Path,
) -> None:
    coordinator, _authority, executor = _coordinator(tmp_path)
    created = coordinator.create_recovery(_request())

    first = coordinator.run_authorized_target(
        recovery_id=created.record.recovery_id,
        target_id="agent_system",
        idempotency_key="run-agent-system-once",
    )
    replay = coordinator.run_authorized_target(
        recovery_id=created.record.recovery_id,
        target_id="agent_system",
        idempotency_key="run-agent-system-once",
    )

    assert replay == first
    assert executor.execute_calls == ["agent_system"]
    assert executor.readiness_calls == ["agent_system"]
    assert executor.readiness_releases == ["agent_system"]
    assert replay.record.remaining_target_ids == ("text_memory", "skill_bundle")
    assert all(
        target.state == "pending"
        for target in replay.record.targets
        if target.target_id != "agent_system"
    )


def test_each_remaining_target_needs_a_separate_authorization_and_commit_stays_pending(
    tmp_path: Path,
) -> None:
    coordinator, _authority, executor = _coordinator(tmp_path)
    snapshot = coordinator.create_recovery(_request())

    for target_id in ("agent_system", "text_memory", "skill_bundle"):
        snapshot = coordinator.run_authorized_target(
            recovery_id=snapshot.record.recovery_id,
            target_id=target_id,
            idempotency_key=f"run-{target_id}-once",
        )

    assert executor.execute_calls == ["agent_system", "text_memory", "skill_bundle"]
    assert snapshot.record.state == "all_targets_completed_awaiting_commit"
    assert snapshot.record.completed_target_ids == (
        "agent_system",
        "text_memory",
        "skill_bundle",
    )
    assert snapshot.record.remaining_target_ids == ()


def test_same_source_cannot_create_a_second_recovery(tmp_path: Path) -> None:
    coordinator, _authority, _executor = _coordinator(tmp_path)
    coordinator.create_recovery(_request())

    with pytest.raises(ScienceSuccessorRecoveryV1Error, match="already owns"):
        coordinator.create_recovery(_request(idempotency_key="different-operation"))


def test_legacy_unique_source_schema_migrates_without_changing_recovery_blobs(
    tmp_path: Path,
) -> None:
    source_store = ScienceSuccessorRecoveryStoreV1(tmp_path / "source-store")
    created = source_store.create_recovery(
        _request(),
        now="2026-07-29T01:00:00Z",
    )
    with sqlite3.connect(source_store.path) as source_connection:
        row = source_connection.execute(
            "SELECT recovery_id, source_transition_id, create_idempotency_key, "
            "request_sha256, request_json, record_sha256, record_json, version "
            "FROM successor_recoveries"
        ).fetchone()
    legacy_root = tmp_path / "legacy-store"
    legacy_root.mkdir()
    legacy_path = legacy_root / "science-successor-recovery-v1.sqlite3"
    with sqlite3.connect(legacy_path) as connection:
        connection.execute(
            "CREATE TABLE successor_recoveries ("
            "recovery_id TEXT PRIMARY KEY, "
            "source_transition_id TEXT NOT NULL UNIQUE, "
            "create_idempotency_key TEXT NOT NULL UNIQUE, "
            "request_sha256 TEXT NOT NULL, request_json BLOB NOT NULL, "
            "record_sha256 TEXT NOT NULL, record_json BLOB NOT NULL, "
            "version INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO successor_recoveries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )
        connection.commit()

    migrated = ScienceSuccessorRecoveryStoreV1(legacy_root)

    assert migrated.get_recovery(created.record.recovery_id) == created
    with sqlite3.connect(migrated.path) as connection:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'successor_recoveries'"
        ).fetchone()[0]
        assert "source_transition_id TEXT NOT NULL UNIQUE" not in sql
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("successor_transition_state", "pending", "literal"),
        ("transition_attempt_count", 1, "exhausted"),
        ("source_commit_absent", False, "literal"),
        ("source_outputs_absent", False, "literal"),
        ("active_project_head_id", "head-drift", "active project head"),
    ],
)
def test_source_authority_rejects_nonclosed_recovery_conditions(
    field: str,
    value: object,
    message: str,
) -> None:
    raw = _source().model_dump(mode="python")
    raw[field] = value
    with pytest.raises(ValidationError, match=message):
        ExhaustedSuccessorRecoveryAuthorityV1.model_validate(raw)


def test_request_rejects_dataset_attachment_view_or_plan_drift() -> None:
    plan = _plan()
    source = _source()

    for mutation in ("dataset", "attachment", "view", "plan"):
        raw = source.model_dump(mode="python")
        if mutation == "dataset":
            raw["dataset"]["task_id"] = "task-other"
        elif mutation == "attachment":
            raw["dataset"]["training_feedback"]["completed_dataset_id"] = "dataset-other"
        elif mutation == "view":
            raw["dataset"]["training_feedback"]["task_scope_id"] = "task-other"
        else:
            raw["plan_sha256"] = _sha("d")
        with pytest.raises(ValidationError):
            mutated = ExhaustedSuccessorRecoveryAuthorityV1.model_validate(raw)
            ScienceSuccessorRecoveryCreateRequestV1.model_validate(
                _request().model_dump(mode="python") | {"source": mutated}
            )

    request = _request()
    incompatible_plan = ScienceSuccessorPlanV2.model_validate(
        plan.model_dump(mode="python") | {"accepted_attempt_id": "attempt-other"}
    )
    with pytest.raises(ValidationError, match="plan"):
        ScienceSuccessorRecoveryCreateRequestV1.model_validate(
            request.model_dump(mode="python") | {"plan": incompatible_plan}
        )


def test_pre_job_failure_is_distinct_closed_recovery_authority() -> None:
    raw = _source().model_dump(mode="python")
    raw["failed_job"] = None
    raw["failed_pre_job_operation"] = FailedEvolutionPreJobOperationAuthorityV1(
        operation_id="successor-attempt-pre-job",
        successor_transition_id=raw["successor_transition_id"],
        target_id="agent_system",
        method_id="agent_system_gepa_reflector",
        artifact_type="agent_system",
        state="failed",
        retryable=False,
        evolution_job_ids=(),
        evolution_job_inventory_sha256=_sha("6"),
        operation_result_sha256=_sha("7"),
        failure_code="successor_transition_failed",
    )
    source = ExhaustedSuccessorRecoveryAuthorityV1.model_validate(raw)
    assert source.failed_job is None
    assert source.failed_pre_job_operation is not None
    assert source.failed_pre_job_operation.evolution_job_ids == ()

    with pytest.raises(ValidationError, match="exactly one"):
        ExhaustedSuccessorRecoveryAuthorityV1.model_validate(
            raw | {"failed_job": _source().failed_job}
        )


def test_recovery_requires_new_execution_identity() -> None:
    request = _request()
    with pytest.raises(ValidationError, match="new execution identity"):
        ScienceSuccessorRecoveryCreateRequestV1.model_validate(
            request.model_dump(mode="python")
            | {
                "execution_identity": request.execution_identity.model_copy(
                    update={
                        "core_generation": request.source.source_core_generation,
                        "daemon_release_identity": request.source.source_release_identity,
                    }
                )
            }
        )


def test_only_one_target_can_be_running_and_targets_need_explicit_authority(
    tmp_path: Path,
) -> None:
    coordinator, _authority, executor = _coordinator(tmp_path)
    created = coordinator.create_recovery(_request())
    store = coordinator.store
    intent, created_intent = store.begin_target(
        recovery_id=created.record.recovery_id,
        target_id="agent_system",
        idempotency_key="agent-system-running",
        now="2026-07-29T01:00:00Z",
    )
    assert created_intent is True
    assert intent.target.target_id == "agent_system"

    with pytest.raises(ScienceSuccessorRecoveryV1Error, match="already running"):
        store.begin_target(
            recovery_id=created.record.recovery_id,
            target_id="text_memory",
            idempotency_key="text-memory-too-early",
            now="2026-07-29T01:00:00Z",
        )
    ordered, _ordered_authority, ordered_executor = _coordinator(tmp_path / "order")
    ordered_created = ordered.create_recovery(_request())
    with pytest.raises(ScienceSuccessorRecoveryV1Error, match="authorization order"):
        ordered.run_authorized_target(
            recovery_id=ordered_created.record.recovery_id,
            target_id="skill_bundle",
            idempotency_key="skill-before-memory",
        )
    assert executor.execute_calls == []
    assert ordered_executor.execute_calls == []


def test_unknown_post_intent_outcome_fails_closed_without_second_execution(
    tmp_path: Path,
) -> None:
    coordinator, _authority, executor = _coordinator(tmp_path)
    created = coordinator.create_recovery(_request())
    coordinator.store.begin_target(
        recovery_id=created.record.recovery_id,
        target_id="agent_system",
        idempotency_key="agent-system-crashed",
        now="2026-07-29T01:00:00Z",
    )

    terminal = coordinator.run_authorized_target(
        recovery_id=created.record.recovery_id,
        target_id="agent_system",
        idempotency_key="agent-system-crashed",
    )

    assert executor.execute_calls == []
    assert executor.recover_calls == ["agent_system"]
    assert terminal.record.state == "failed"
    operation = coordinator.get_target_operation(
        created.record.recovery_id,
        "agent_system",
    )
    assert operation.failure is not None
    assert operation.failure.failure_code == "post_intent_execution_failed"


def test_fresh_core_readback_drift_blocks_target_before_intent(tmp_path: Path) -> None:
    coordinator, authority, executor = _coordinator(tmp_path)
    created = coordinator.create_recovery(_request())
    authority.source = ExhaustedSuccessorRecoveryAuthorityV1.model_validate(
        authority.source.model_dump(mode="python")
        | {"successor_transition_state_sha256": _sha("f")}
    )

    with pytest.raises(ScienceSuccessorRecoveryV1Error, match="authority changed"):
        coordinator.run_authorized_target(
            recovery_id=created.record.recovery_id,
            target_id="agent_system",
            idempotency_key="blocked-by-source-drift",
        )

    assert executor.execute_calls == []
    assert coordinator.store.get_recovery(created.record.recovery_id).record.state == (
        "awaiting_target_authorization"
    )


class _NotReadyExecutor(_Executor):
    def check_target_readiness(self, preflight, *, source, plan):
        del preflight, source, plan
        raise RuntimeError("synthetic managed reflector mount failure")


def test_reflector_mount_readiness_failure_precedes_target_intent(tmp_path: Path) -> None:
    source = _source()
    executor = _NotReadyExecutor()
    store = ScienceSuccessorRecoveryStoreV1(tmp_path / "recovery-store")
    coordinator = ScienceSuccessorRecoveryCoordinatorV1(
        store=store,
        authority_reader=_AuthorityReader(source),
        executor=executor,
        clock=lambda: "2026-07-29T01:00:00Z",
    )
    created = coordinator.create_recovery(_request(source=source))

    with pytest.raises(
        ScienceSuccessorRecoveryReadinessErrorV1,
        match="REFLECTOR_CREDENTIAL_MOUNT_NOT_READY",
    ):
        coordinator.run_authorized_target(
            recovery_id=created.record.recovery_id,
            target_id="agent_system",
            idempotency_key="blocked-before-intent",
        )

    observed = store.get_recovery(created.record.recovery_id)
    assert observed.record.state == "awaiting_target_authorization"
    assert store.get_target_operation_or_none(created.record.recovery_id, "agent_system") is None
    assert executor.execute_calls == []
    assert executor.recover_calls == []


class _RecoveringExecutor(_Executor):
    def recover_target(self, intent, *, source, plan):
        self.recover_calls.append(intent.target.target_id)
        return self.execute_target(intent, source=source, plan=plan)


def test_post_intent_recovery_seals_the_existing_operation_once(tmp_path: Path) -> None:
    source = _source()
    authority = _AuthorityReader(source)
    executor = _RecoveringExecutor()
    store = ScienceSuccessorRecoveryStoreV1(tmp_path / "recovery-store")
    coordinator = ScienceSuccessorRecoveryCoordinatorV1(
        store=store,
        authority_reader=authority,
        executor=executor,
        clock=lambda: "2026-07-29T01:00:00Z",
    )
    created = coordinator.create_recovery(_request(source=source))
    store.begin_target(
        recovery_id=created.record.recovery_id,
        target_id="agent_system",
        idempotency_key="agent-system-crashed",
        now="2026-07-29T01:00:00Z",
    )

    recovered = coordinator.run_authorized_target(
        recovery_id=created.record.recovery_id,
        target_id="agent_system",
        idempotency_key="agent-system-crashed",
    )
    replay = coordinator.run_authorized_target(
        recovery_id=created.record.recovery_id,
        target_id="agent_system",
        idempotency_key="agent-system-crashed",
    )

    assert recovered == replay
    assert executor.recover_calls == ["agent_system"]
    assert executor.execute_calls == ["agent_system"]


class _TerminalExecutor(_Executor):
    def execute_target(self, intent, *, source, plan):
        del source, plan
        self.execute_calls.append(intent.target.target_id)
        raise RecoveryTargetTerminalFailureV1(
            ScienceSuccessorRecoveryTargetFailureV1(
                operation_id=intent.operation_id,
                recovery_id=intent.recovery_id,
                target_id=intent.target.target_id,
                failure_code="managed_runtime_not_ready",
                failure_class="infrastructure",
                retryable=False,
                failure_evidence_sha256=_sha("e"),
                failed_at="2026-07-29T01:00:01Z",
            )
        )


def test_terminal_target_failure_is_append_only_and_never_auto_retried(
    tmp_path: Path,
) -> None:
    source = _source()
    authority = _AuthorityReader(source)
    executor = _TerminalExecutor()
    coordinator = ScienceSuccessorRecoveryCoordinatorV1(
        store=ScienceSuccessorRecoveryStoreV1(tmp_path / "recovery-store"),
        authority_reader=authority,
        executor=executor,
        clock=lambda: "2026-07-29T01:00:00Z",
    )
    created = coordinator.create_recovery(_request(source=source))

    failed = coordinator.run_authorized_target(
        recovery_id=created.record.recovery_id,
        target_id="agent_system",
        idempotency_key="agent-system-terminal",
    )
    replay = coordinator.run_authorized_target(
        recovery_id=created.record.recovery_id,
        target_id="agent_system",
        idempotency_key="agent-system-terminal",
    )

    assert failed == replay
    assert failed.record.state == "failed"
    assert executor.execute_calls == ["agent_system"]
    assert executor.recover_calls == []


def test_terminal_pre_job_recovery_can_be_superseded_once_without_mutation(
    tmp_path: Path,
) -> None:
    source = _source()
    executor = _TerminalExecutor()
    store = ScienceSuccessorRecoveryStoreV1(tmp_path / "recovery-store")
    coordinator = ScienceSuccessorRecoveryCoordinatorV1(
        store=store,
        authority_reader=_AuthorityReader(source),
        executor=executor,
        clock=lambda: "2026-07-29T01:00:00Z",
    )
    first = coordinator.create_recovery(_request(source=source))
    failed = coordinator.run_authorized_target(
        recovery_id=first.record.recovery_id,
        target_id="agent_system",
        idempotency_key="agent-system-terminal-for-supersession",
    )
    failed_before = store.get_recovery(failed.record.recovery_id)
    successor_request = _request(
        idempotency_key="create-superseding-recovery-once",
        source=source,
    ).model_copy(
        update={
            "supersedes_recovery_id": failed.record.recovery_id,
            "execution_identity": _request().execution_identity.model_copy(
                update={
                    "core_generation": _sha("6"),
                    "daemon_release_identity": _sha("7"),
                }
            ),
        }
    )

    successor = coordinator.create_recovery(successor_request)

    assert store.get_recovery(failed.record.recovery_id) == failed_before
    assert successor.record.supersedes_recovery_id == failed.record.recovery_id
    assert successor.record.supersession_authority is not None
    assert successor.record.supersession_authority.paid_model_side_effect_absent is True
    assert executor.supersession_calls == [failed.record.recovery_id]
    with pytest.raises(
        ScienceSuccessorRecoveryV1Error,
        match="already consumed",
    ):
        coordinator.create_recovery(
            successor_request.model_copy(
                update={"idempotency_key": "second-supersession-is-rejected"}
            )
        )


def test_supersession_carries_completed_prefix_without_reexecuting_targets(
    tmp_path: Path,
) -> None:
    source = _source()

    class FailSkillBundle(_Executor):
        def execute_target(self, intent, *, source, plan):
            if intent.target.target_id != "skill_bundle":
                return super().execute_target(intent, source=source, plan=plan)
            self.execute_calls.append(intent.target.target_id)
            raise RecoveryTargetTerminalFailureV1(
                ScienceSuccessorRecoveryTargetFailureV1(
                    operation_id=intent.operation_id,
                    recovery_id=intent.recovery_id,
                    target_id=intent.target.target_id,
                    failure_code="post_intent_execution_failed",
                    failure_class="infrastructure",
                    retryable=False,
                    failure_evidence_sha256=_sha("f"),
                    failed_at="2026-07-29T01:00:03Z",
                )
            )

    store = ScienceSuccessorRecoveryStoreV1(tmp_path / "recovery-store")
    first_executor = FailSkillBundle()
    first = ScienceSuccessorRecoveryCoordinatorV1(
        store=store,
        authority_reader=_AuthorityReader(source),
        executor=first_executor,
        clock=lambda: "2026-07-29T01:00:00Z",
    )
    created = first.create_recovery(_request(source=source))
    for target_id in ("agent_system", "text_memory", "skill_bundle"):
        terminal = first.run_authorized_target(
            recovery_id=created.record.recovery_id,
            target_id=target_id,
            idempotency_key=f"first-{target_id}",
        )
    assert terminal.record.state == "failed"
    assert terminal.record.completed_target_ids == ("agent_system", "text_memory")
    before = store.get_recovery(terminal.record.recovery_id)

    second_executor = _Executor()
    second = ScienceSuccessorRecoveryCoordinatorV1(
        store=store,
        authority_reader=_AuthorityReader(source),
        executor=second_executor,
        clock=lambda: "2026-07-29T01:00:04Z",
    )
    request = _request(
        idempotency_key="carry-completed-prefix-once",
        source=source,
    ).model_copy(
        update={
            "supersedes_recovery_id": terminal.record.recovery_id,
            "execution_identity": _request().execution_identity.model_copy(
                update={
                    "core_generation": _sha("6"),
                    "daemon_release_identity": _sha("7"),
                }
            ),
        }
    )
    successor = second.create_recovery(request)

    assert store.get_recovery(terminal.record.recovery_id) == before
    assert successor.record.completed_target_ids == ("agent_system", "text_memory")
    assert successor.record.remaining_target_ids == ("skill_bundle",)
    carried_operations = []
    for target_id in ("agent_system", "text_memory"):
        carried = store.get_target_operation(successor.record.recovery_id, target_id)
        carried_operations.append(carried)
        original = store.get_target_operation(terminal.record.recovery_id, target_id)
        assert carried == original
        assert _completed_operation_is_bound_to_prior_record(
            record=successor.record,
            target_id=target_id,
            operation=carried,
        )
        assert not _completed_operation_is_bound_to_prior_record(
            record=successor.record,
            target_id=target_id,
            operation=carried.model_copy(
                update={
                    "intent": carried.intent.model_copy(
                        update={"idempotency_key": f"tampered-{target_id}"}
                    )
                }
            ),
        )
        replay = second.run_authorized_target(
            recovery_id=successor.record.recovery_id,
            target_id=target_id,
            idempotency_key=f"new-{target_id}-must-not-run",
        )
        assert replay.record.completed_target_ids == ("agent_system", "text_memory")
    assert second_executor.execute_calls == []

    transitive_authorities = []
    for carried in carried_operations:
        assert carried.result is not None
        payload = {
            "successor_recovery_carried_target_authority_contract_version": "1",
            "prior_recovery_id": carried.intent.recovery_id,
            "carried_from_recovery_id": successor.record.recovery_id,
            "prior_intent": carried.intent,
            "prior_result": carried.result,
            "prior_intent_sha256": canonical_digest(carried.intent),
            "prior_result_sha256": canonical_digest(carried.result),
        }
        payload["content_sha256"] = canonical_digest(payload)
        transitive_authorities.append(
            ScienceSuccessorRecoveryCarriedTargetAuthorityV1.model_validate(payload)
        )
    transitive_payload = {
        "successor_recovery_supersession_authority_contract_version": "1",
        "prior_recovery_id": successor.record.recovery_id,
        "prior_recovery_record_sha256": canonical_digest(successor.record),
        "prior_operation_id": "operation-transitive-failure",
        "prior_failure_sha256": _sha("8"),
        "source_transition_id": source.successor_transition_id,
        "evolution_job_ids": (),
        "evolution_job_inventory_sha256": _sha("9"),
        "paid_model_side_effect_absent": True,
        "carried_completed_targets": tuple(transitive_authorities),
        "core_generation": _sha("6"),
        "daemon_release_identity": _sha("7"),
        "created_at": "2026-07-29T01:00:05Z",
    }
    transitive_payload["content_sha256"] = canonical_digest(transitive_payload)
    transitive = ScienceSuccessorRecoverySupersessionAuthorityV1.model_validate(
        transitive_payload
    )
    assert tuple(
        item.carried_from_recovery_id
        for item in transitive.carried_completed_targets
    ) == (successor.record.recovery_id, successor.record.recovery_id)

    completed = second.run_authorized_target(
        recovery_id=successor.record.recovery_id,
        target_id="skill_bundle",
        idempotency_key="new-skill-bundle-once",
    )
    assert completed.record.state == "all_targets_completed_awaiting_commit"
    assert second_executor.execute_calls == ["skill_bundle"]


def test_supersession_carries_a_virtual_completed_prefix_across_generations(
    tmp_path: Path,
) -> None:
    source = _source()

    class FailSkillBundle(_Executor):
        def execute_target(self, intent, *, source, plan):
            if intent.target.target_id != "skill_bundle":
                return super().execute_target(intent, source=source, plan=plan)
            raise RecoveryTargetTerminalFailureV1(
                ScienceSuccessorRecoveryTargetFailureV1(
                    operation_id=intent.operation_id,
                    recovery_id=intent.recovery_id,
                    target_id=intent.target.target_id,
                    failure_code="post_intent_execution_failed",
                    failure_class="infrastructure",
                    retryable=False,
                    failure_evidence_sha256=_sha("f"),
                    failed_at="2026-07-29T01:00:03Z",
                )
            )

    store = ScienceSuccessorRecoveryStoreV1(tmp_path / "recovery-store")
    first_coordinator = ScienceSuccessorRecoveryCoordinatorV1(
        store=store,
        authority_reader=_AuthorityReader(source),
        executor=FailSkillBundle(),
        clock=lambda: "2026-07-29T01:00:00Z",
    )
    first = first_coordinator.create_recovery(_request(source=source))
    for target_id in ("agent_system", "text_memory", "skill_bundle"):
        first_terminal = first_coordinator.run_authorized_target(
            recovery_id=first.record.recovery_id,
            target_id=target_id,
            idempotency_key=f"first-generation-{target_id}",
        )
    assert first_terminal.record.state == "failed"

    second_coordinator = ScienceSuccessorRecoveryCoordinatorV1(
        store=store,
        authority_reader=_AuthorityReader(source),
        executor=_TerminalExecutor(),
        clock=lambda: "2026-07-29T01:00:04Z",
    )
    second_request = _request(
        idempotency_key="second-generation-carry",
        source=source,
    ).model_copy(
        update={
            "supersedes_recovery_id": first_terminal.record.recovery_id,
            "execution_identity": _request().execution_identity.model_copy(
                update={
                    "core_generation": _sha("6"),
                    "daemon_release_identity": _sha("7"),
                }
            ),
        }
    )
    second = second_coordinator.create_recovery(second_request)
    second_terminal = second_coordinator.run_authorized_target(
        recovery_id=second.record.recovery_id,
        target_id="skill_bundle",
        idempotency_key="second-generation-skill-failure",
    )
    assert second_terminal.record.state == "failed"

    third_coordinator = ScienceSuccessorRecoveryCoordinatorV1(
        store=store,
        authority_reader=_AuthorityReader(source),
        executor=_Executor(),
        clock=lambda: "2026-07-29T01:00:05Z",
    )
    third_request = _request(
        idempotency_key="third-generation-carry",
        source=source,
    ).model_copy(
        update={
            "supersedes_recovery_id": second_terminal.record.recovery_id,
            "execution_identity": _request().execution_identity.model_copy(
                update={
                    "core_generation": _sha("a"),
                    "daemon_release_identity": _sha("b"),
                }
            ),
        }
    )
    third = third_coordinator.create_recovery(third_request)

    assert third.record.completed_target_ids == ("agent_system", "text_memory")
    carried_by_target = {
        item.prior_result.target_id: item
        for item in third.record.supersession_authority.carried_completed_targets
    }
    for target_id in third.record.completed_target_ids:
        carried = store.get_target_operation(third.record.recovery_id, target_id)
        assert carried == store.get_target_operation(first.record.recovery_id, target_id)
        assert carried.result is not None
        assert _recovery_seed_result_is_authorized(
            recovery_id=third.record.recovery_id,
            result=carried.result,
            carried_by_target=carried_by_target,
        )
    third_completed = third_coordinator.run_authorized_target(
        recovery_id=third.record.recovery_id,
        target_id="skill_bundle",
        idempotency_key="third-generation-skill-success",
    )
    local = store.get_target_operation(third.record.recovery_id, "skill_bundle")
    assert third_completed.record.state == "all_targets_completed_awaiting_commit"
    assert local.result is not None
    assert _recovery_seed_result_is_authorized(
        recovery_id=third.record.recovery_id,
        result=local.result,
        carried_by_target=carried_by_target,
    )
    assert _recovery_seed_target_inventory_matches(
        ("agent_system", "skill_bundle", "text_memory"),
        third_completed.record.completed_target_ids,
    )
    assert not _recovery_seed_target_inventory_matches(
        ("agent_system", "skill_bundle", "text_memory"),
        ("agent_system", "text_memory", "text_memory"),
    )


def test_native_recovery_failure_preserves_the_stable_preparer_failure_code() -> None:
    request = _request()
    intent = ScienceSuccessorRecoveryTargetIntentV1(
        operation_id="operation-stable-failure-code",
        idempotency_key="stable-failure-code-once",
        recovery_id="recovery-stable-failure-code",
        source_authority_sha256=science_successor_recovery_source_sha256(
            request.source
        ),
        execution_identity=request.execution_identity,
        target=request.plan.enabled_methods[0],
        state="running",
        created_at="2026-07-29T01:00:00Z",
    )
    runner = ProductionScienceSuccessorRecoveryNativeRunnerV1(
        context_provider=object(),
        preparer=object(),
        clock=lambda: datetime(2026, 7, 29, 1, 0, tzinfo=UTC),
    )

    failure = runner._terminal_failure(
        intent,
        ScienceSuccessorPreparationV2Error(
            "redacted field mismatch",
            failure_code="recovery_binding_framework_lock_mismatch",
        ),
    )

    assert failure.failure_code == "recovery_binding_framework_lock_mismatch"
    assert "redacted field mismatch" not in json.dumps(
        failure.model_dump(mode="json"), sort_keys=True
    )


class _UnhandledPostIntentExecutor(_Executor):
    def __init__(self, failure: Exception) -> None:
        super().__init__()
        self.failure = failure

    def execute_target(self, intent, *, source, plan):
        del source, plan
        self.execute_calls.append(intent.target.target_id)
        raise self.failure


class _AdoptionExecutor(_Executor):
    def attest_recovery_supersession(
        self,
        *,
        prior,
        failure,
        source,
        plan,
        execution_identity,
        completed_operations=(),
    ):
        assert plan == _plan()
        job_id = "job-agent-system-paid-once"
        reservation = _inference_reservation("agent_system", job_id)
        adopted_payload = {
            "successor_recovery_adopted_job_authority_contract_version": "1",
            "prior_recovery_id": prior.record.recovery_id,
            "prior_operation_id": failure.operation_id,
            "source_transition_id": source.successor_transition_id,
            "job_owner_successor_transition_id": prior.record.recovery_id,
            "target_id": "agent_system",
            "method_id": "agent_system_gepa_reflector",
            "artifact_type": "agent_system",
            "job_id": job_id,
            "job_state": "succeeded",
            "job_result_sha256": _sha("1"),
            "proposal_artifact_id": "artifact-agent_system-recovery",
            "proposal_payload_manifest_sha256": _sha("c"),
            "proposal_payload_byte_size": 100,
            "proposal_registry_manifest_sha256": _sha("e"),
            "proposal_promoted": False,
            "admission_decision_absent": True,
            "inference_reservation_sha256": reservation.content_sha256,
            "reflector_model_calls_started": 1,
            "created_at": "2026-07-29T01:00:02Z",
        }
        adopted_payload["content_sha256"] = canonical_digest(adopted_payload)
        adopted = ScienceSuccessorRecoveryAdoptedJobAuthorityV1.model_validate(
            adopted_payload
        )
        payload = {
            "successor_recovery_supersession_authority_contract_version": "1",
            "prior_recovery_id": prior.record.recovery_id,
            "prior_recovery_record_sha256": prior.record_sha256,
            "prior_operation_id": failure.operation_id,
            "prior_failure_sha256": canonical_digest(failure),
            "source_transition_id": source.successor_transition_id,
            "evolution_job_ids": tuple(
                operation.result.job_id
                for operation in completed_operations
                if operation.result is not None
                and (
                    operation.result.job_owner_successor_transition_id
                    or operation.result.recovery_id
                )
                == prior.record.recovery_id
            )
            + (job_id,),
            "evolution_job_inventory_sha256": _sha("2"),
            "paid_model_side_effect_absent": False,
            **({"carried_completed_targets": tuple(
                _carried_authority_for_test(prior.record.recovery_id, operation)
                for operation in completed_operations
            )} if completed_operations else {}),
            "adopted_job": adopted,
            "core_generation": execution_identity.core_generation,
            "daemon_release_identity": execution_identity.daemon_release_identity,
            "created_at": "2026-07-29T01:00:02Z",
        }
        payload["content_sha256"] = canonical_digest(payload)
        return ScienceSuccessorRecoverySupersessionAuthorityV1.model_validate(payload)

    def adopt_target(self, intent, *, source, plan, adopted_job):
        del source, plan
        self.adopt_calls.append(adopted_job.job_id)
        runtime_receipt = _runtime_receipt(intent.target.target_id)
        reservation = _inference_reservation(intent.target.target_id, adopted_job.job_id)
        budget = _budget_receipt(intent.target.target_id, intent.target.method_id)
        audit = _agent_system_audit_receipt()
        report_sha256 = science_successor_recovery_admission_report_sha256(
            job_id=adopted_job.job_id,
            target_id=intent.target.target_id,
            method_id=intent.target.method_id,
            proposal_artifact_id=adopted_job.proposal_artifact_id,
            proposal_payload_manifest_sha256=_sha("c"),
            registry_artifact_manifest_sha256=_sha("e"),
            agent_system_audit_receipt_sha256=audit.content_sha256,
            reflector_runtime_receipt_sha256=canonical_digest(runtime_receipt),
            reflector_inference_reservation_sha256=reservation.content_sha256,
            reflector_inference_budget_sha256=budget.content_sha256,
        )
        return ScienceSuccessorRecoveryTargetResultV1(
            operation_id=intent.operation_id,
            recovery_id=intent.recovery_id,
            target_id=intent.target.target_id,
            method_id=intent.target.method_id,
            source_failed_job_id="job-recovery-source",
            source_failed_job_retried=False,
            job_owner_successor_transition_id=(
                adopted_job.job_owner_successor_transition_id
            ),
            job_id=adopted_job.job_id,
            job_state="succeeded",
            proposal_ids=(adopted_job.proposal_artifact_id,),
            admission_status="accepted",
            promotion_status="promoted",
            output=ScienceMethodOutputV2(
                target_id=intent.target.target_id,
                method_id=intent.target.method_id,
                artifact_id=adopted_job.proposal_artifact_id,
                artifact_type=intent.target.output_artifact_type,
                manifest_sha256=_sha("c"),
                byte_size=100,
                execution_boundary="outside_inference",
            ),
            registry_artifact_id=adopted_job.proposal_artifact_id,
            registry_manifest_sha256=_sha("c"),
            registry_artifact_manifest_sha256=_sha("e"),
            agent_system_audit=audit,
            admission_decision=_admission_decision(
                intent.target.target_id,
                adopted_job.job_id,
                report_sha256,
            ),
            reflector_runtime_receipt=runtime_receipt,
            reflector_inference_reservation=reservation,
            inference_budget_receipt=budget,
            completed_at="2026-07-29T01:00:03Z",
        )


def test_paid_succeeded_job_is_adopted_append_only_without_second_execution(
    tmp_path: Path,
) -> None:
    source = _source()
    store = ScienceSuccessorRecoveryStoreV1(tmp_path / "recovery-store")
    failing = ScienceSuccessorRecoveryCoordinatorV1(
        store=store,
        authority_reader=_AuthorityReader(source),
        executor=_UnhandledPostIntentExecutor(RuntimeError("response lost")),
        clock=lambda: "2026-07-29T01:00:00Z",
    )
    first = failing.create_recovery(_request(source=source))
    failed = failing.run_authorized_target(
        recovery_id=first.record.recovery_id,
        target_id="agent_system",
        idempotency_key="paid-job-response-lost",
    )
    failed_before = store.get_recovery(failed.record.recovery_id)
    executor = _AdoptionExecutor()
    coordinator = ScienceSuccessorRecoveryCoordinatorV1(
        store=store,
        authority_reader=_AuthorityReader(source),
        executor=executor,
        clock=lambda: "2026-07-29T01:00:04Z",
    )
    request = _request(
        idempotency_key="adopt-paid-job-once",
        source=source,
    ).model_copy(
        update={
            "supersedes_recovery_id": failed.record.recovery_id,
            "execution_identity": _request().execution_identity.model_copy(
                update={
                    "core_generation": _sha("6"),
                    "daemon_release_identity": _sha("7"),
                }
            ),
        }
    )
    created = coordinator.create_recovery(request)
    completed = coordinator.run_authorized_target(
        recovery_id=created.record.recovery_id,
        target_id="agent_system",
        idempotency_key="adopt-existing-agent-system-job",
    )

    assert store.get_recovery(failed.record.recovery_id) == failed_before
    assert completed.record.completed_target_ids == ("agent_system",)
    assert executor.adopt_calls == ["job-agent-system-paid-once"]
    assert executor.execute_calls == []
    assert executor.recover_calls == []


def test_preparer_adopts_exact_succeeded_job_without_creating_another_job() -> None:
    job_id = "job-paid-agent-system-once"
    owner = "recovery-prior-paid-job"
    proposal_id = "artifact-adopted-agent-system"
    manifest = {"kind": "agent_system", "schema_version": 1}
    terminal = {
        "artifact_ids": [proposal_id],
        "error": None,
        "job_id": job_id,
        "outputs": [
            {
                "artifact_id": proposal_id,
                "type": "agent_system",
                "name": "adopted agent system",
                "manifest": manifest,
                "lineage": {},
                "compatibility": {},
                "scores": {},
                "promoted": False,
                "created_at": "2026-07-29T01:00:00Z",
                "payload_manifest_digest": _sha("c"),
                "payload_byte_size": 100,
                "payload_file_count": 1,
            }
        ],
        "retryable": None,
        "state": "succeeded",
        "successor_transition_id": owner,
    }
    reservation = _inference_reservation("agent_system", job_id)
    adopted_payload = {
        "successor_recovery_adopted_job_authority_contract_version": "1",
        "prior_recovery_id": owner,
        "prior_operation_id": "operation-prior-paid-job",
        "source_transition_id": "transition-recovery-source",
        "job_owner_successor_transition_id": owner,
        "target_id": "agent_system",
        "method_id": "agent_system_gepa_reflector",
        "artifact_type": "agent_system",
        "job_id": job_id,
        "job_state": "succeeded",
        "job_result_sha256": (
            science_successor_recovery_adoptable_job_result_sha256(terminal)
        ),
        "proposal_artifact_id": proposal_id,
        "proposal_payload_manifest_sha256": _sha("c"),
        "proposal_payload_byte_size": 100,
        "proposal_registry_manifest_sha256": canonical_digest(manifest),
        "proposal_promoted": False,
        "admission_decision_absent": True,
        "inference_reservation_sha256": reservation.content_sha256,
        "reflector_model_calls_started": 1,
        "created_at": "2026-07-29T01:00:01Z",
    }
    adopted_payload["content_sha256"] = canonical_digest(adopted_payload)
    adopted = ScienceSuccessorRecoveryAdoptedJobAuthorityV1.model_validate(
        adopted_payload
    )

    class AdoptionClient:
        create_calls = 0

        def get_internal_job_result(self, observed_job_id):
            assert observed_job_id == job_id
            return terminal

        def get_internal_successor_artifact(self, transition_id, artifact_id):
            assert (transition_id, artifact_id) == (owner, proposal_id)
            return ArtifactResponse(
                artifact_id=proposal_id,
                type=ArtifactType.AGENT_SYSTEM,
                name="adopted agent system",
                version=1,
                state=ArtifactState.SEALED,
                uri="memory://adopted-agent-system",
                manifest=manifest,
                promoted=False,
            ).model_dump(mode="json")

        def get_internal_reflector_inference_reservation(self, observed_job_id):
            assert observed_job_id == job_id
            return reservation.model_dump(mode="json")

        def create_plan_bound_job(self, _payload):
            self.create_calls += 1
            raise AssertionError("adoption must not create a job")

    client = AdoptionClient()
    output = object.__new__(
        ProductionScienceSuccessorPreparerV2
    )._adopt_recovery_job_output(
        client,
        spec=SimpleNamespace(
            target_id="agent_system",
            method="agent_system_gepa_reflector",
            artifact_type="agent_system",
        ),
        adopted_job=adopted,
    )

    assert output.job_id == job_id
    assert output.output.artifact_id == proposal_id
    assert client.create_calls == 0


@pytest.mark.parametrize(
    ("failure", "expected_class"),
    (
        (ValueError("admission rejected secret-value"), "deterministic"),
        (RuntimeError("HTTP transport exposed secret-value"), "infrastructure"),
    ),
)
def test_unhandled_post_intent_failure_is_terminal_redacted_and_not_replayed(
    tmp_path: Path,
    failure: Exception,
    expected_class: str,
) -> None:
    source = _source()
    executor = _UnhandledPostIntentExecutor(failure)
    coordinator = ScienceSuccessorRecoveryCoordinatorV1(
        store=ScienceSuccessorRecoveryStoreV1(tmp_path / "recovery-store"),
        authority_reader=_AuthorityReader(source),
        executor=executor,
        clock=lambda: "2026-07-29T01:00:00Z",
    )
    created = coordinator.create_recovery(_request(source=source))

    failed = coordinator.run_authorized_target(
        recovery_id=created.record.recovery_id,
        target_id="agent_system",
        idempotency_key="agent-system-unhandled-terminal",
    )
    replay = coordinator.run_authorized_target(
        recovery_id=created.record.recovery_id,
        target_id="agent_system",
        idempotency_key="agent-system-unhandled-terminal",
    )
    operation = coordinator.get_target_operation(
        created.record.recovery_id,
        "agent_system",
    )

    assert replay == failed
    assert failed.record.state == "failed"
    assert operation.state == "failed"
    assert operation.failure is not None
    assert operation.failure.failure_code == "post_intent_execution_failed"
    assert operation.failure.failure_class == expected_class
    assert "secret-value" not in operation.model_dump_json()
    assert executor.execute_calls == ["agent_system"]
    assert executor.recover_calls == []


def test_store_survives_restart_and_preserves_source_authority(tmp_path: Path) -> None:
    source = _source()
    root = tmp_path / "recovery-store"
    coordinator, _authority, _executor = _coordinator(tmp_path, source=source)
    created = coordinator.create_recovery(_request(source=source))
    source_before = source.model_dump_json()

    restarted = ScienceSuccessorRecoveryStoreV1(root)
    observed = restarted.get_recovery(created.record.recovery_id)

    assert observed == created
    assert source.model_dump_json() == source_before
    assert observed.record.source_authority_sha256 == science_successor_recovery_source_sha256(
        source
    )


@pytest.mark.parametrize(
    ("audit_update", "omit"),
    [
        ({}, True),
        ({"enabled": False}, False),
        ({"finding_count": 1}, False),
        ({"repair_count": 1}, False),
        ({"forbidden_literal_count": 0}, False),
    ],
)
def test_agent_system_target_result_rejects_missing_or_nonpassing_audit(
    tmp_path: Path,
    audit_update: dict[str, object],
    omit: bool,
) -> None:
    coordinator, _authority, _executor = _coordinator(tmp_path)
    created = coordinator.create_recovery(_request())
    coordinator.run_authorized_target(
        recovery_id=created.record.recovery_id,
        target_id="agent_system",
        idempotency_key="agent-system-audit-contract",
    )
    operation = coordinator.get_target_operation(
        created.record.recovery_id,
        "agent_system",
    )
    assert operation.result is not None
    raw = operation.result.model_dump(mode="python")
    if omit:
        raw["agent_system_audit"] = None
    else:
        audit = dict(raw["agent_system_audit"])
        audit.update(audit_update)
        audit["content_sha256"] = canonical_digest(
            {key: value for key, value in audit.items() if key != "content_sha256"}
        )
        raw["agent_system_audit"] = audit

    with pytest.raises(ValidationError, match="promoted output"):
        ScienceSuccessorRecoveryTargetResultV1.model_validate(raw)


@pytest.mark.parametrize("tamper", ["metadata_digest", "audit_swap"])
def test_target_result_cross_binding_rejects_metadata_or_audit_swap(
    tmp_path: Path,
    tamper: str,
) -> None:
    coordinator, _authority, _executor = _coordinator(tmp_path)
    created = coordinator.create_recovery(_request())
    coordinator.run_authorized_target(
        recovery_id=created.record.recovery_id,
        target_id="agent_system",
        idempotency_key="agent-system-cross-binding",
    )
    operation = coordinator.get_target_operation(
        created.record.recovery_id,
        "agent_system",
    )
    assert operation.result is not None
    raw = operation.result.model_dump(mode="python")
    if tamper == "metadata_digest":
        raw["registry_artifact_manifest_sha256"] = _sha("f")
    else:
        audit = dict(raw["agent_system_audit"])
        audit["leakage_basis_sha256"] = _sha("f")
        audit["content_sha256"] = canonical_digest(
            {key: value for key, value in audit.items() if key != "content_sha256"}
        )
        raw["agent_system_audit"] = audit

    with pytest.raises(ValidationError, match="promoted output"):
        ScienceSuccessorRecoveryTargetResultV1.model_validate(raw)


def test_target_result_tamper_is_rejected_by_record_cross_hash(tmp_path: Path) -> None:
    coordinator, _authority, _executor = _coordinator(tmp_path)
    created = coordinator.create_recovery(_request())
    completed = coordinator.run_authorized_target(
        recovery_id=created.record.recovery_id,
        target_id="agent_system",
        idempotency_key="agent-system-before-tamper",
    )
    path = coordinator.store.path
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE successor_recovery_target_operations SET result_json = ? "
            "WHERE recovery_id = ? AND target_id = ?",
            (b"{}", completed.record.recovery_id, "agent_system"),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ScienceSuccessorRecoveryV1Error):
        coordinator.store.get_target_operation(completed.record.recovery_id, "agent_system")


def test_model_contract_is_frozen_and_forbids_adapter_specific_fields() -> None:
    with pytest.raises(ValidationError):
        ExhaustedSuccessorRecoveryAuthorityV1.model_validate(
            _source().model_dump(mode="python") | {"benchmark_task_id": "forbidden"}
        )
    with pytest.raises(ValidationError):
        ScienceSuccessorRecoveryCreateRequestV1.model_validate(
            _request().model_dump(mode="python") | {"candidate_command": "forbidden"}
        )
    with pytest.raises(ValidationError):
        RecoveryExecutionIdentityV1.model_validate(
            _request().execution_identity.model_dump(mode="python")
            | {"credential_path": "/home/user/.codex"}
        )


class _CoreProvider:
    def authenticate(self, values: tuple[bytes, ...]) -> bool:
        return values == (b"Bearer successor-recovery-test",)


class _SourceResolver:
    def __init__(self) -> None:
        self.requests: list[ScienceSuccessorRecoverySourceResolveRequestV1] = []

    def resolve_exhausted_successor_authority(
        self,
        request: ScienceSuccessorRecoverySourceResolveRequestV1,
    ) -> ScienceSuccessorRecoverySourceResponseV1:
        self.requests.append(request)
        return _source_resolution_response(request.historical_attestation)


class _ProjectSeeder:
    def __init__(self) -> None:
        self.requests: list[ScienceSuccessorRecoveryProjectSeedRequestV1] = []

    def seed_project(
        self,
        request: ScienceSuccessorRecoveryProjectSeedRequestV1,
    ):
        self.requests.append(request)
        raise ScienceSuccessorRecoveryV1Error("synthetic seed fail closed")


def test_source_export_failure_projection_preserves_only_closed_codes() -> None:
    core = CoreTaskControlError(
        "task_owner_unavailable",
        "private diagnostic omitted",
        http_status=503,
        retryable=True,
    )
    assert _recovery_source_export_failure_code(core) == (
        "RECOVERY_SOURCE_CORE_TASK_OWNER_UNAVAILABLE"
    )
    evolution = EvolutionHttpStatusError(status_code=404, detail_code="not_found")
    assert _recovery_source_export_failure_code(evolution) == (
        "RECOVERY_SOURCE_EVOLUTION_HTTP_404_NOT_FOUND"
    )
    assert _recovery_source_export_failure_code(RuntimeError("private value")) == (
        "RECOVERY_SOURCE_EXCEPTION_RUNTIME_ERROR"
    )


def test_source_readback_failure_projection_preserves_phase_and_closed_code() -> None:
    core = CoreTaskControlError(
        "task_owner_unavailable",
        "private diagnostic omitted",
        http_status=503,
        retryable=True,
    )

    assert _recovery_source_readback_failure_code(core, phase="context") == (
        "RECOVERY_SOURCE_READBACK_CONTEXT_CORE_TASK_OWNER_UNAVAILABLE"
    )
    assert _recovery_source_readback_failure_code(
        RuntimeError("private value"),
        phase="unexpected-private-phase",
    ) == "RECOVERY_SOURCE_READBACK_UNKNOWN_EXCEPTION_RUNTIME_ERROR"


def test_project_seed_failure_projection_is_stable_and_redacted() -> None:
    error = ScienceSuccessorPreparationV2Error(
        "private path and credential detail",
        failure_code="recovery_seed_materialization_mismatch",
    )

    detail = _recovery_project_seed_failure_detail(error)

    assert detail == (
        "successor recovery project seed failed "
        "recovery source preparation recovery seed materialization mismatch"
    )
    assert "private" not in detail


def test_project_seed_authority_parses_canonical_json_wire_values() -> None:
    project_id = "project-recovery-destination"
    workspace = WorkspaceSnapshotRefV2(
        workspace_snapshot_id="workspace-recovery-destination",
        project_id=project_id,
        manifest_sha256=_sha("1"),
        entry_count=1,
        byte_size=1,
    )
    revision = EvolutionRevisionRefV2(
        evolution_revision_id="revision-recovery-destination",
        project_id=project_id,
        manifest_sha256=_sha("2"),
        artifact_count=3,
    )
    runtime = RuntimeContextSnapshotRefV2(
        runtime_context_snapshot_id="runtime-recovery-destination",
        project_id=project_id,
        evolution_revision_id=revision.evolution_revision_id,
        evolution_revision_manifest_sha256=revision.manifest_sha256,
        registry_sha256=_sha("3"),
        runtime_contract_sha256=_sha("4"),
        manifest_sha256=_sha("5"),
    )
    execution = EffectiveExecutionSnapshotRefV2(
        effective_execution_snapshot_id="execution-recovery-destination",
        project_id=project_id,
        execution_mode="codex_subscription_transcript",
        capture_mode="transcript",
        token_level_metrics_available=False,
        producer_id="producer-recovery-destination",
        snapshot_sha256=_sha("6"),
    )
    successor = ProjectHeadRefV2(
        project_head_id="head-recovery-destination-1",
        project_id=project_id,
        generation=1,
        predecessor_project_head_id="head-recovery-destination-0",
        workspace_snapshot=workspace,
        evolution_revision=revision,
        runtime_context_snapshot=runtime,
        effective_execution_snapshot=execution,
        registry_sha256=runtime.registry_sha256,
        manifest_sha256=_sha("7"),
    )
    target_ids = ("agent_system", "skill_bundle", "text_memory")
    artifacts = tuple(
        SuccessorArtifactContributionV2(
            target_id=target_id,
            artifact_id=f"artifact-{target_id}-recovery",
            artifact_type=target_id,
            owner_successor_transition_id=f"owner-{target_id}-recovery",
            origin="inherited",
        )
        for target_id in target_ids
    )
    result_sha256 = (_sha("8"), _sha("9"), _sha("a"))
    manifest = AtomicSuccessorRecoverySeedManifestV1(
        project_id=project_id,
        seed_request_id="recovery-seed-wire-roundtrip",
        recovery_id="recovery-wire-roundtrip",
        recovery_record_sha256=_sha("b"),
        source_project_id="project-recovery-source",
        source_transition_id="transition-recovery-source",
        source_authority_sha256=_sha("c"),
        predecessor_project_head_id="head-recovery-destination-0",
        predecessor_generation=0,
        predecessor_manifest_sha256=_sha("d"),
        successor_project_head_id=successor.project_head_id,
        successor_generation=1,
        successor_manifest_sha256=successor.manifest_sha256,
        workspace_snapshot_id=workspace.workspace_snapshot_id,
        workspace_manifest_sha256=workspace.manifest_sha256,
        evolution_revision_id=revision.evolution_revision_id,
        evolution_revision_manifest_sha256=revision.manifest_sha256,
        runtime_context_snapshot_id=runtime.runtime_context_snapshot_id,
        runtime_context_manifest_sha256=runtime.manifest_sha256,
        effective_execution_snapshot_id=execution.effective_execution_snapshot_id,
        effective_execution_snapshot_sha256=execution.snapshot_sha256,
        registry_sha256=runtime.registry_sha256,
        materialized_context_id="materialized-recovery-destination",
        materialized_context_manifest_sha256=_sha("e"),
        method_artifact_ids=tuple(item.artifact_id for item in artifacts),
        recovery_target_result_sha256=result_sha256,
        artifacts=artifacts,
        created_at=datetime(2026, 7, 30, 4, 0, tzinfo=UTC),
    )
    commit = AtomicSuccessorCommitV2(
        manifest_sha256=atomic_successor_manifest_sha256(manifest),
        manifest=manifest,
    )
    wire = {
        "successor_recovery_project_seed_authority_contract_version": "1",
        "seed_request_id": manifest.seed_request_id,
        "request_sha256": _sha("f"),
        "recovery_id": manifest.recovery_id,
        "recovery_record_sha256": manifest.recovery_record_sha256,
        "source_project_id": manifest.source_project_id,
        "source_transition_id": manifest.source_transition_id,
        "source_authority_sha256": manifest.source_authority_sha256,
        "destination_project_id": project_id,
        "predecessor_destination_project_head_id": (
            manifest.predecessor_project_head_id
        ),
        "successor_destination_project_head": successor.model_dump(mode="json"),
        "commit": commit.model_dump(mode="json"),
        "target_result_sha256": list(result_sha256),
        "created_at": "2026-07-30T04:00:00Z",
    }
    wire["content_sha256"] = canonical_digest(wire)

    authority = _successor_recovery_seed_authority_from_wire(wire)

    assert type(authority.commit.manifest) is AtomicSuccessorRecoverySeedManifestV1
    assert authority.commit == commit
    assert authority.target_result_sha256 == result_sha256
    assert isinstance(authority.commit.manifest.created_at, datetime)
    assert authority.commit.manifest.created_at == datetime(
        2026, 7, 30, 4, 0, tzinfo=UTC
    )
    encoded_before = json.dumps(
        wire,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded_after = json.dumps(
        authority.model_dump(mode="json"),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert encoded_after == encoded_before
    assert hashlib.sha256(encoded_after).hexdigest() == hashlib.sha256(
        encoded_before
    ).hexdigest()

    invalid_tuple = json.loads(encoded_before)
    invalid_tuple["commit"]["manifest"]["method_artifact_ids"] = "not-an-array"
    invalid_time = json.loads(encoded_before)
    invalid_time["commit"]["manifest"]["created_at"] = "not-a-time"
    missing_field = json.loads(encoded_before)
    del missing_field["commit"]["manifest"]["method_artifact_ids"]
    for invalid in (invalid_tuple, invalid_time, missing_field):
        with pytest.raises(ValidationError):
            _successor_recovery_seed_authority_from_wire(invalid)


def test_successor_recovery_uses_the_original_attempt_compatibility_tag() -> None:
    context = SimpleNamespace(
        accepted_attempt=SimpleNamespace(attempt_id="attempt-original"),
        task=SimpleNamespace(task_id="task-original"),
    )

    assert _successor_context_task_tag(context) == (
        "openevo_run_task:attempt-original:task-original"
    )


def test_recovery_seed_cleanup_does_not_mask_the_primary_failure() -> None:
    class BrokenClose:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True
            raise RuntimeError("private cleanup detail")

    client = BrokenClose()
    lease = BrokenClose()

    _close_recovery_seed_resources(
        client,
        lease,
        suppress_errors=True,
    )

    assert client.closed is True
    assert lease.closed is True

    with pytest.raises(RuntimeError, match="private cleanup detail"):
        _close_recovery_seed_resources(
            BrokenClose(),
            BrokenClose(),
            suppress_errors=False,
        )


def test_recovery_seed_contributions_are_sorted_inherited_references() -> None:
    results = tuple(
        SimpleNamespace(
            target_id=target_id,
            registry_artifact_id=f"artifact-{target_id}",
            output=SimpleNamespace(artifact_type=artifact_type),
            job_owner_successor_transition_id=owner,
            recovery_id="recovery-current",
        )
        for target_id, artifact_type, owner in (
            ("text_memory", "text_memory", "recovery-text-owner"),
            ("agent_system", "agent_system", "recovery-agent-owner"),
            ("skill_bundle", "skill_bundle", None),
        )
    )

    contributions = _recovery_seed_contributions(results)

    assert tuple(item.target_id for item in contributions) == (
        "agent_system",
        "skill_bundle",
        "text_memory",
    )
    assert tuple(item.owner_successor_transition_id for item in contributions) == (
        "recovery-agent-owner",
        "recovery-current",
        "recovery-text-owner",
    )
    assert all(item.origin == "inherited" for item in contributions)
    assert all(item.admission_action is None for item in contributions)
    assert all(item.proposal_artifact_ids == () for item in contributions)


def test_sealed_source_resolution_identity_survives_new_execution_lifecycle() -> None:
    provenance = _source().source_provenance

    # These immutable values deliberately identify an older resolver release;
    # current recovery execution identity is carried by the create request.
    assert provenance.resolution_core_generation == _sha("b")
    assert provenance.resolution_daemon_release_identity == _sha("c")
    _validate_sealed_recovery_source_provenance(provenance)

    with pytest.raises(
        ScienceSuccessorRecoveryV1Error,
        match="provenance is invalid",
    ):
        _validate_sealed_recovery_source_provenance(
            provenance.model_copy(update={"current_core_authorities_reproduced": False})
        )


def test_recovery_strict_inventory_accepts_its_json_wire_array() -> None:
    payload = {
        "successor_transition_id": "successor-wire-inventory",
        "job_ids": [],
    }
    payload["content_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    inventory = _validate_strict_wire_model(
        SuccessorTransitionJobInventoryResponse,
        payload,
    )

    assert inventory.job_ids == ()


def test_private_source_resolution_api_validates_strict_tuples_in_json_mode(
    tmp_path: Path,
) -> None:
    """JSON arrays remain the wire representation of strict tuple fields."""

    coordinator, _authority, _executor = _coordinator(tmp_path)
    resolver = _SourceResolver()
    app = create_core_control_v2_contract_app(_CoreProvider())
    install_core_science_successor_recovery_endpoint(
        app,
        coordinator,
        source_resolver=resolver,
    )
    request = _source_resolution_request()
    payload = request.model_dump(mode="json")
    assert isinstance(payload["attachment_ids"], list)
    assert isinstance(payload["historical_attestation"]["evidence_inventory"], list)

    with TestClient(app) as client:
        response = client.post(
            "/v2/internal/science-successor-recoveries/sources/resolve",
            json=payload,
            headers={"Authorization": "Bearer successor-recovery-test"},
        )

    assert response.status_code == 200, response.text
    assert resolver.requests == [request]
    assert response.json() == _source_resolution_response(
        request.historical_attestation
    ).model_dump(mode="json")


def test_private_source_resolution_api_rejects_non_contract_json_before_resolver(
    tmp_path: Path,
) -> None:
    coordinator, _authority, _executor = _coordinator(tmp_path)
    resolver = _SourceResolver()
    app = create_core_control_v2_contract_app(_CoreProvider())
    install_core_science_successor_recovery_endpoint(
        app,
        coordinator,
        source_resolver=resolver,
    )
    payload = _source_resolution_request().model_dump(mode="json")
    payload["adapter_bypass"] = True

    with TestClient(app) as client:
        response = client.post(
            "/v2/internal/science-successor-recoveries/sources/resolve",
            json=payload,
            headers={"Authorization": "Bearer successor-recovery-test"},
        )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "invalid recovery source resolution request"
    }
    assert resolver.requests == []


def test_private_recovery_seed_api_is_path_bound_and_fail_closed(
    tmp_path: Path,
) -> None:
    coordinator, _authority, _executor = _coordinator(tmp_path)
    seeder = _ProjectSeeder()
    app = create_core_control_v2_contract_app(_CoreProvider())
    install_core_science_successor_recovery_endpoint(
        app,
        coordinator,
        project_seeder=seeder,
    )
    request = ScienceSuccessorRecoveryProjectSeedRequestV1(
        idempotency_key="seed-once",
        recovery_id="recovery-seed-test",
        recovery_record_sha256=_sha("1"),
        destination_project_id="project-destination",
        expected_destination_project_head_id="project-head-genesis",
        expected_destination_project_head_sha256=_sha("2"),
    )
    headers = {"Authorization": "Bearer successor-recovery-test"}
    with TestClient(app) as client:
        mismatch = client.post(
            "/v2/internal/science-successor-recoveries/recovery-other/seed-project",
            json=request.model_dump(mode="json"),
            headers=headers,
        )
        closed = client.post(
            "/v2/internal/science-successor-recoveries/recovery-seed-test/seed-project",
            json=request.model_dump(mode="json"),
            headers=headers,
        )
    assert mismatch.status_code == 422
    assert closed.status_code == 409
    assert seeder.requests == [request]


def test_private_recovery_api_is_bearer_protected_hidden_and_idempotent(
    tmp_path: Path,
) -> None:
    coordinator, authority, executor = _coordinator(tmp_path)
    app = create_core_control_v2_contract_app(_CoreProvider())
    install_core_science_successor_recovery_endpoint(app, coordinator)
    request = _request()
    headers = {"Authorization": "Bearer successor-recovery-test"}

    with TestClient(app) as client:
        denied = client.post(
            "/v2/internal/science-successor-recoveries",
            json=request.model_dump(mode="json"),
        )
        created = client.post(
            "/v2/internal/science-successor-recoveries",
            json=request.model_dump(mode="json"),
            headers=headers,
        )
        assert created.status_code == 200, created.text
        recovery_id = created.json()["record"]["recovery_id"]
        ran = client.post(
            f"/v2/internal/science-successor-recoveries/{recovery_id}/targets/agent_system",
            json={"idempotency_key": "api-run-agent-system"},
            headers=headers,
        )
        replay = client.post(
            f"/v2/internal/science-successor-recoveries/{recovery_id}/targets/agent_system",
            json={"idempotency_key": "api-run-agent-system"},
            headers=headers,
        )
        readback = client.get(
            f"/v2/internal/science-successor-recoveries/{recovery_id}",
            headers=headers,
        )
        target_readback = client.get(
            f"/v2/internal/science-successor-recoveries/{recovery_id}/targets/agent_system",
            headers=headers,
        )

    assert denied.status_code == 401
    assert created.status_code == 200
    assert ran.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == ran.json() == readback.json()
    assert target_readback.status_code == 200
    target_result = target_readback.json()["result"]
    assert target_result["registry_artifact_manifest_sha256"] == _sha("e")
    assert target_result["registry_manifest_sha256"] == _sha("c")
    assert target_result["agent_system_audit"]["forbidden_literal_count"] == 7
    assert "private-current-task.csv" not in json.dumps(target_result, sort_keys=True)
    assert executor.execute_calls == ["agent_system"]
    assert executor.readiness_calls == ["agent_system"]
    assert authority.reads == 3
    assert not any(
        path.startswith("/v2/internal/science-successor-recoveries")
        for path in app.openapi()["paths"]
    )


def test_private_recovery_api_readiness_failure_persists_no_target_intent(
    tmp_path: Path,
) -> None:
    source = _source()
    executor = _NotReadyExecutor()
    coordinator = ScienceSuccessorRecoveryCoordinatorV1(
        store=ScienceSuccessorRecoveryStoreV1(tmp_path / "recovery-store"),
        authority_reader=_AuthorityReader(source),
        executor=executor,
        clock=lambda: "2026-07-29T01:00:00Z",
    )
    app = create_core_control_v2_contract_app(_CoreProvider())
    install_core_science_successor_recovery_endpoint(app, coordinator)
    headers = {"Authorization": "Bearer successor-recovery-test"}

    with TestClient(app) as client:
        created = client.post(
            "/v2/internal/science-successor-recoveries",
            json=_request(source=source).model_dump(mode="json"),
            headers=headers,
        )
        assert created.status_code == 200, created.text
        recovery_id = created.json()["record"]["recovery_id"]
        blocked = client.post(
            f"/v2/internal/science-successor-recoveries/{recovery_id}/targets/agent_system",
            json={"idempotency_key": "api-blocked-before-intent"},
            headers=headers,
        )

    assert blocked.status_code == 503
    assert blocked.json()["detail"] == "REFLECTOR_CREDENTIAL_MOUNT_NOT_READY"
    assert coordinator.store.get_target_operation_or_none(recovery_id, "agent_system") is None


class _Lease:
    def __init__(self, binding: ServiceRunBinding) -> None:
        self.binding = binding
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _RecoveryServices:
    def __init__(self, binding: ServiceRunBinding) -> None:
        self.binding = binding
        self.leases: list[_Lease] = []
        self.adopt_kwargs: list[dict] = []
        self.ensure_kwargs: list[dict] = []
        self.adopt_result = None

    def adopt_current_run_binding(self, execution_mode, **kwargs):
        assert execution_mode is ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT
        assert kwargs["codex_model"] == "gpt-5.5"
        assert kwargs["runtime_image"] == MANAGED_RUNTIME_IMAGES["managed_science"]
        self.adopt_kwargs.append(dict(kwargs))
        return self.adopt_result

    def ensure_run_binding(self, execution_mode, **kwargs):
        assert execution_mode is ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT
        assert kwargs["codex_model"] == "gpt-5.5"
        assert kwargs["runtime_image"] == MANAGED_RUNTIME_IMAGES["managed_science"]
        self.ensure_kwargs.append(dict(kwargs))
        lease = _Lease(self.binding)
        self.leases.append(lease)
        return SimpleNamespace(run_ready=True), lease


class _UnavailableRecoveryServices(_RecoveryServices):
    def ensure_run_binding(self, execution_mode, **kwargs):
        assert execution_mode is ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT
        assert kwargs["codex_model"] == "gpt-5.5"
        assert kwargs["runtime_image"] == MANAGED_RUNTIME_IMAGES["managed_science"]
        return SimpleNamespace(run_ready=False), None


class _FailingRecoveryServices(_RecoveryServices):
    def ensure_run_binding(self, execution_mode, **kwargs):
        del execution_mode, kwargs
        raise RuntimeError("private managed-runtime detail")


class _RejectingAdoptionServices(_RecoveryServices):
    def adopt_current_run_binding(self, execution_mode, **kwargs):
        del execution_mode, kwargs
        raise RuntimeError("private non-adoptable generation detail")

    def ensure_run_binding(self, execution_mode, **kwargs):
        del execution_mode, kwargs
        raise AssertionError("non-empty adoption failure must not restart services")


class _RestartedCoreRecoveryServices(_RecoveryServices):
    def adopt_current_run_binding(self, execution_mode, **kwargs):
        del execution_mode, kwargs
        raise SupervisorStateError("prior owner is not adoptable")


class _HealthClient:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.closed = False

    def get_internal_health(self) -> dict:
        return self.payload

    def close(self) -> None:
        self.closed = True


class _CloseFailingHealthClient(_HealthClient):
    def close(self) -> None:
        self.closed = True
        raise RuntimeError("private close detail")


class _NativeRunner:
    def __init__(self) -> None:
        self.execute_calls: list[str] = []

    def codex_model_for_target(self, *, source, plan):
        del source, plan
        return "gpt-5.5"

    def execute_native_target(self, intent, *, source, plan, binding, client):
        del source, plan, binding, client
        self.execute_calls.append(intent.target.target_id)
        return _Executor().execute_target(intent, source=_source(), plan=_plan())

    def recover_native_target(self, intent, *, source, plan, binding, client):
        del intent, source, plan, binding, client


def _production_recovery_fixture():
    release = MANAGED_RUNTIME_RELEASES["managed_science"]
    identity = InternalServiceIdentity(
        service_id="core-control",
        generation_digest=_sha("a"),
        registry_digest=_sha("c"),
        framework_lock_digest=_sha("d"),
        credential="successor-recovery-test-credential-" + "x" * 40,
    )
    binding = ServiceRunBinding(
        execution_mode=ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
        codex_model="gpt-5.5",
        runtime_image=MANAGED_RUNTIME_IMAGES["managed_science"],
        # Exercise the offline OCI execution identity. Recovery authorities
        # must canonicalize this loaded image ID back to the signed release
        # digest used by the worker mount receipt.
        runtime_image_immutable_reference=release.loaded_image_id,
        runtime_identity_digest=_sha("e"),
        generation_digest=identity.generation_digest,
        registry_digest=identity.registry_digest,
        framework_lock_digest=identity.framework_lock_digest,
        rollout_url="http://127.0.0.1:41001",
        evolution_backend_url="http://127.0.0.1:41002",
        gateway_url="http://127.0.0.1:41003",
        _identity=identity,
    )
    execution = _request().execution_identity.model_copy(
        update={
            "runtime_image_digest": release.trusted_digest,
            "service_identity_id": "core-reference-worker",
        }
    )
    request = ScienceSuccessorRecoveryCreateRequestV1.model_validate(
        _request().model_dump(mode="python") | {"execution_identity": execution}
    )
    mount_payload = {
        "schema_version": "openevo.managed_reflector_credential_mount_readiness.v1",
        "authority_id": _sha("1"),
        "container_authority_id": _sha("5"),
        "worker_launch_id": f"mrl-{'2' * 32}",
        "container_launch_id": "production-recovery-test-container",
        "adoption_nonce_sha256": _sha("6"),
        "service_identity_digest": _sha("7"),
        "runtime_profile": "managed_science",
        "runtime_digest": release.trusted_digest,
        "docker_host_path_identity": _sha("3"),
        "generation_digest": execution.core_generation,
        "daemon_release_identity": execution.daemon_release_identity,
        "release_install_digest": execution.release_install_digest,
        "release_registry_digest": execution.executable_registry_digest,
        "credential_target": "/openevo/credentials/codex",
        "container_uid": 1000,
        "container_gid": 1000,
        "authority_issued": True,
        "docker_mount_created": True,
        "container_path_visible": True,
        "container_user_can_read": True,
        "auth_file_read_only": True,
        "generation_matches": True,
        "release_identity_matches": True,
        "adoption_receipt_valid": True,
        "container_authority_verified": True,
        "cleanup_verified": True,
        "codex_cli_started": False,
        "model_started": False,
        "created_at": "2026-07-29T01:00:00Z",
    }
    mount_payload["content_sha256"] = hashlib.sha256(
        json.dumps(
            mount_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    health = {
        "internal_identity": {
            "auth_digest": _sha("4"),
            "generation_digest": execution.core_generation,
            "framework_lock_digest": execution.framework_lock_digest,
            "registry_digest": execution.executable_registry_digest,
            "service_id": "evolution-backend",
        },
        "workers": [
            {
                "worker_id": "core-reference-worker",
                "generation_digest": execution.core_generation,
                "registry_digest": execution.executable_registry_digest,
                "framework_lock_digest": execution.framework_lock_digest,
                "managed_reflector_credential_mount": mount_payload,
                "managed_reflector_runtime_readiness": (
                    _runtime_readiness_from_mount(mount_payload).model_dump(mode="json")
                ),
            }
        ],
    }
    return request, binding, health


def test_production_supersession_attestation_accepts_strict_json_inventory(
    tmp_path: Path,
) -> None:
    source = _source()
    terminal_executor = _TerminalExecutor()
    store = ScienceSuccessorRecoveryStoreV1(tmp_path / "prior-recovery")
    coordinator = ScienceSuccessorRecoveryCoordinatorV1(
        store=store,
        authority_reader=_AuthorityReader(source),
        executor=terminal_executor,
        clock=lambda: "2026-07-29T01:00:00Z",
    )
    first = coordinator.create_recovery(_request(source=source))
    failed = coordinator.run_authorized_target(
        recovery_id=first.record.recovery_id,
        target_id="agent_system",
        idempotency_key="terminal-before-job-for-json-inventory",
    )
    operation = store.get_target_operation(
        failed.record.recovery_id,
        "agent_system",
    )
    assert operation.failure is not None
    request, binding, _health = _production_recovery_fixture()
    inventory_payload = {
        "successor_transition_id": failed.record.recovery_id,
        "job_ids": [],
    }
    inventory_payload["content_sha256"] = canonical_digest(inventory_payload)

    class InventoryClient:
        closed = False

        def get_internal_successor_transition_job_inventory(self, transition_id):
            assert transition_id == failed.record.recovery_id
            return dict(inventory_payload)

        def close(self):
            self.closed = True

    inventory_client = InventoryClient()
    services = _RecoveryServices(binding)
    executor = ProductionScienceSuccessorRecoveryExecutorV1(
        services=services,
        native_runner=_NativeRunner(),
        daemon_release_identity=request.execution_identity.daemon_release_identity,
        release_install_digest=request.execution_identity.release_install_digest,
        evolution_factory=lambda _binding: inventory_client,
        clock=lambda: datetime(2026, 7, 29, 1, 0, 2, tzinfo=UTC),
    )

    authority = executor.attest_recovery_supersession(
        prior=failed,
        failure=operation.failure,
        source=source,
        plan=_plan(),
        execution_identity=request.execution_identity,
    )

    assert authority.evolution_job_ids == ()
    assert authority.paid_model_side_effect_absent is True
    assert inventory_client.closed is True
    assert services.leases[0].closed is True


def test_production_supersession_carries_a_previously_adopted_paid_job(
    tmp_path: Path,
) -> None:
    source = _source()
    production_request, binding, _health = _production_recovery_fixture()
    store = ScienceSuccessorRecoveryStoreV1(tmp_path / "carry-recovery")
    first_coordinator = ScienceSuccessorRecoveryCoordinatorV1(
        store=store,
        authority_reader=_AuthorityReader(source),
        executor=_UnhandledPostIntentExecutor(RuntimeError("response lost")),
        clock=lambda: "2026-07-29T01:00:00Z",
    )
    first = first_coordinator.create_recovery(
        _request(source=source).model_copy(
            update={
                "execution_identity": _request().execution_identity.model_copy(
                    update={
                        "core_generation": _sha("6"),
                        "daemon_release_identity": _sha("7"),
                    }
                )
            }
        )
    )
    first_failed = first_coordinator.run_authorized_target(
        recovery_id=first.record.recovery_id,
        target_id="agent_system",
        idempotency_key="carry-first-paid-job-failure",
    )

    class FailingAdoptionExecutor(_AdoptionExecutor):
        def adopt_target(self, intent, *, source, plan, adopted_job):
            del intent, source, plan, adopted_job
            raise ValueError("historical admission failed")

    second_executor = FailingAdoptionExecutor()
    second_coordinator = ScienceSuccessorRecoveryCoordinatorV1(
        store=store,
        authority_reader=_AuthorityReader(source),
        executor=second_executor,
        clock=lambda: "2026-07-29T01:00:01Z",
    )
    second = second_coordinator.create_recovery(
        _request(
            idempotency_key="carry-second-recovery",
            source=source,
        ).model_copy(
            update={
                "supersedes_recovery_id": first_failed.record.recovery_id,
                "execution_identity": production_request.execution_identity,
            }
        )
    )
    second_failed = second_coordinator.run_authorized_target(
        recovery_id=second.record.recovery_id,
        target_id="agent_system",
        idempotency_key="carry-second-adoption-failure",
    )
    second_operation = store.get_target_operation(
        second_failed.record.recovery_id,
        "agent_system",
    )
    assert second_operation.failure is not None

    job_id = "job-paid-carried-across-recoveries"
    proposal_id = "artifact-paid-carried-across-recoveries"
    owner = first_failed.record.recovery_id
    manifest = {"kind": "agent_system", "schema_version": 1}
    terminal = {
        "artifact_ids": [proposal_id],
        "error": None,
        "job_id": job_id,
        "outputs": [
            {
                "artifact_id": proposal_id,
                "type": "agent_system",
                "name": "carried agent system",
                "manifest": manifest,
                "lineage": {},
                "compatibility": {},
                "scores": {},
                "promoted": False,
                "created_at": "2026-07-29T01:00:00Z",
                "payload_manifest_digest": _sha("c"),
                "payload_byte_size": 100,
                "payload_file_count": 1,
            }
        ],
        "retryable": None,
        "state": "succeeded",
        "successor_transition_id": owner,
    }
    reservation = _inference_reservation("agent_system", job_id)
    first_operation = store.get_target_operation(
        first_failed.record.recovery_id,
        "agent_system",
    )
    assert first_operation.failure is not None
    adopted_payload = {
        "successor_recovery_adopted_job_authority_contract_version": "1",
        "prior_recovery_id": owner,
        "prior_operation_id": first_operation.failure.operation_id,
        "source_transition_id": source.successor_transition_id,
        "job_owner_successor_transition_id": owner,
        "target_id": "agent_system",
        "method_id": "agent_system_gepa_reflector",
        "artifact_type": "agent_system",
        "job_id": job_id,
        "job_state": "succeeded",
        "job_result_sha256": (
            science_successor_recovery_adoptable_job_result_sha256(terminal)
        ),
        "proposal_artifact_id": proposal_id,
        "proposal_payload_manifest_sha256": _sha("c"),
        "proposal_payload_byte_size": 100,
        "proposal_registry_manifest_sha256": canonical_digest(manifest),
        "proposal_promoted": False,
        "admission_decision_absent": True,
        "inference_reservation_sha256": reservation.content_sha256,
        "reflector_model_calls_started": 1,
        "created_at": "2026-07-29T01:00:02Z",
    }
    adopted_payload["content_sha256"] = canonical_digest(adopted_payload)
    adopted = ScienceSuccessorRecoveryAdoptedJobAuthorityV1.model_validate(
        adopted_payload
    )
    prior_supersession = second_failed.record.supersession_authority
    assert prior_supersession is not None
    supersession_payload = prior_supersession.model_dump(mode="json")
    supersession_payload["adopted_job"] = adopted
    supersession_payload["evolution_job_ids"] = (job_id,)
    supersession_payload["paid_model_side_effect_absent"] = False
    supersession_payload.pop("content_sha256")
    supersession_payload["content_sha256"] = canonical_digest(supersession_payload)
    supersession = ScienceSuccessorRecoverySupersessionAuthorityV1.model_validate(
        supersession_payload
    )
    carried_record = second_failed.record.model_copy(
        update={"supersession_authority": supersession}
    )
    carried_prior = type(second_failed)(
        record=carried_record,
        record_sha256=canonical_digest(carried_record),
    )

    inventory_payload = {
        "successor_transition_id": carried_record.recovery_id,
        "job_ids": [],
    }
    inventory_payload["content_sha256"] = canonical_digest(inventory_payload)

    class CarryClient:
        closed = False

        def get_internal_successor_transition_job_inventory(self, transition_id):
            assert transition_id == carried_record.recovery_id
            return dict(inventory_payload)

        def get_internal_job_result(self, observed_job_id):
            assert observed_job_id == job_id
            return terminal

        def get_internal_successor_artifact(self, transition_id, artifact_id):
            assert (transition_id, artifact_id) == (owner, proposal_id)
            return ArtifactResponse(
                artifact_id=proposal_id,
                type=ArtifactType.AGENT_SYSTEM,
                name="carried agent system",
                version=1,
                state=ArtifactState.SEALED,
                uri="memory://carried-agent-system",
                manifest=manifest,
                promoted=False,
            ).model_dump(mode="json")

        def get_internal_reflector_inference_reservation(self, observed_job_id):
            assert observed_job_id == job_id
            return reservation.model_dump(mode="json")

        def close(self):
            self.closed = True

    carry_client = CarryClient()
    services = _RecoveryServices(binding)
    executor = ProductionScienceSuccessorRecoveryExecutorV1(
        services=services,
        native_runner=_NativeRunner(),
        daemon_release_identity=(
            production_request.execution_identity.daemon_release_identity
        ),
        release_install_digest=(
            production_request.execution_identity.release_install_digest
        ),
        evolution_factory=lambda _binding: carry_client,
        clock=lambda: datetime(2026, 7, 29, 1, 0, 3, tzinfo=UTC),
    )

    authority = executor.attest_recovery_supersession(
        prior=carried_prior,
        failure=second_operation.failure,
        source=source,
        plan=_plan(),
        execution_identity=production_request.execution_identity,
    )

    assert authority.evolution_job_ids == ()
    assert authority.paid_model_side_effect_absent is False
    assert authority.adopted_job == adopted
    assert authority.carried_adopted_job_authority_sha256 == adopted.content_sha256
    assert carry_client.closed is True
    assert services.leases[0].closed is True


def test_production_executor_uses_current_worker_readiness_before_intent(
    tmp_path: Path,
) -> None:
    request, binding, health = _production_recovery_fixture()
    services = _RecoveryServices(binding)
    client = _HealthClient(health)
    runner = _NativeRunner()
    executor = ProductionScienceSuccessorRecoveryExecutorV1(
        services=services,
        native_runner=runner,
        daemon_release_identity=request.execution_identity.daemon_release_identity,
        release_install_digest=request.execution_identity.release_install_digest,
        evolution_factory=lambda _binding: client,
        clock=lambda: datetime(
            2026,
            7,
            29,
            1,
            0,
            tzinfo=UTC,
        ),
    )
    store = ScienceSuccessorRecoveryStoreV1(tmp_path / "recovery-store")
    coordinator = ScienceSuccessorRecoveryCoordinatorV1(
        store=store,
        authority_reader=_AuthorityReader(request.source),
        executor=executor,
        clock=lambda: "2026-07-29T01:00:00Z",
    )
    created = coordinator.create_recovery(request)

    completed = coordinator.run_authorized_target(
        recovery_id=created.record.recovery_id,
        target_id="agent_system",
        idempotency_key="production-agent-system-once",
    )

    assert completed.record.completed_target_ids == ("agent_system",)
    assert runner.execute_calls == ["agent_system"]
    assert services.leases[0].closed is True
    assert client.closed is True


def test_managed_reflector_readiness_reissues_generation_credential_authority() -> None:
    request, binding, health = _production_recovery_fixture()
    services = _RecoveryServices(binding)
    client = _HealthClient(health)
    provider = ProductionManagedReflectorReadinessProviderV1(
        services=services,
        daemon_release_identity=request.execution_identity.daemon_release_identity,
        release_install_digest=request.execution_identity.release_install_digest,
        evolution_factory=lambda _binding: client,
    )

    readiness = provider.read_managed_reflector_readiness()

    assert readiness.codex_cli_started is True
    assert readiness.model_started is False
    assert readiness.managed_reflector_credential_mount.adoption_receipt_valid is True
    assert readiness.managed_reflector_runtime.actual_cli_version == "0.144.1"
    assert readiness.managed_reflector_runtime.model_started is False
    assert len(services.leases) == 1
    assert services.leases[0].closed is True
    assert client.closed is True
    assert services.adopt_kwargs == [
        {
            "codex_model": "gpt-5.5",
            "runtime_image": MANAGED_RUNTIME_IMAGES["managed_science"],
            "total_timeout": 120.0,
        }
    ]
    assert services.ensure_kwargs == [
        {
            "codex_model": "gpt-5.5",
            "runtime_image": MANAGED_RUNTIME_IMAGES["managed_science"],
            "total_timeout": 120.0,
        }
    ]


def test_managed_reflector_readiness_reissues_after_typed_core_restart() -> None:
    request, binding, health = _production_recovery_fixture()
    services = _RestartedCoreRecoveryServices(binding)
    client = _HealthClient(health)
    provider = ProductionManagedReflectorReadinessProviderV1(
        services=services,
        daemon_release_identity=request.execution_identity.daemon_release_identity,
        release_install_digest=request.execution_identity.release_install_digest,
        evolution_factory=lambda _binding: client,
    )

    readiness = provider.read_managed_reflector_readiness()

    assert readiness.model_started is False
    assert len(services.ensure_kwargs) == 1
    assert services.leases[0].closed is True


def test_managed_reflector_readiness_adopts_current_generation_without_ensure() -> None:
    request, binding, health = _production_recovery_fixture()
    services = _RecoveryServices(binding)
    lease = _Lease(binding)
    services.adopt_result = (SimpleNamespace(run_ready=True), lease)
    client = _HealthClient(health)
    provider = ProductionManagedReflectorReadinessProviderV1(
        services=services,
        daemon_release_identity=request.execution_identity.daemon_release_identity,
        release_install_digest=request.execution_identity.release_install_digest,
        evolution_factory=lambda _binding: client,
    )

    readiness = provider.read_managed_reflector_readiness()

    assert readiness.managed_reflector_credential_mount.adoption_receipt_valid is True
    assert readiness.managed_reflector_runtime.codex_cli_started is True
    assert len(services.adopt_kwargs) == 1
    assert services.ensure_kwargs == []
    assert lease.closed is True
    assert client.closed is True


@pytest.mark.parametrize("value", [True, 0, 29.9, 600.1, float("nan")])
def test_managed_reflector_readiness_rejects_invalid_total_timeout(value) -> None:
    request, binding, _health = _production_recovery_fixture()

    with pytest.raises(
        ValueError,
        match="timeout must be between 30 and 600 seconds",
    ):
        ProductionManagedReflectorReadinessProviderV1(
            services=_RecoveryServices(binding),
            daemon_release_identity=request.execution_identity.daemon_release_identity,
            release_install_digest=request.execution_identity.release_install_digest,
            total_timeout_seconds=value,
        )


def test_managed_reflector_readiness_rejects_unavailable_service_binding() -> None:
    request, binding, _health = _production_recovery_fixture()
    provider = ProductionManagedReflectorReadinessProviderV1(
        services=_UnavailableRecoveryServices(binding),
        daemon_release_identity=request.execution_identity.daemon_release_identity,
        release_install_digest=request.execution_identity.release_install_digest,
    )

    with pytest.raises(
        ScienceSuccessorRecoveryV1Error,
        match="lacks a verified service binding",
    ):
        provider.read_managed_reflector_readiness()


def test_managed_reflector_readiness_sanitizes_runtime_binding_failure() -> None:
    request, binding, _health = _production_recovery_fixture()
    provider = ProductionManagedReflectorReadinessProviderV1(
        services=_FailingRecoveryServices(binding),
        daemon_release_identity=request.execution_identity.daemon_release_identity,
        release_install_digest=request.execution_identity.release_install_digest,
    )

    with pytest.raises(
        ScienceSuccessorRecoveryV1Error,
        match="could not acquire a verified service binding",
    ) as captured:
        provider.read_managed_reflector_readiness()
    assert "private managed-runtime detail" not in str(captured.value)


def test_managed_reflector_readiness_never_restarts_non_adoptable_generation() -> None:
    request, binding, _health = _production_recovery_fixture()
    provider = ProductionManagedReflectorReadinessProviderV1(
        services=_RejectingAdoptionServices(binding),
        daemon_release_identity=request.execution_identity.daemon_release_identity,
        release_install_digest=request.execution_identity.release_install_digest,
    )

    with pytest.raises(
        ScienceSuccessorRecoveryV1Error,
        match="could not acquire a verified service binding",
    ) as captured:
        provider.read_managed_reflector_readiness()
    assert "private non-adoptable generation detail" not in str(captured.value)


def test_managed_reflector_readiness_get_is_sanitized_503() -> None:
    request, binding, _health = _production_recovery_fixture()
    provider = ProductionManagedReflectorReadinessProviderV1(
        services=_FailingRecoveryServices(binding),
        daemon_release_identity=request.execution_identity.daemon_release_identity,
        release_install_digest=request.execution_identity.release_install_digest,
    )
    app = create_core_control_v2_contract_app(_CoreProvider())
    install_core_managed_reflector_readiness_endpoint(app, provider)

    with TestClient(app) as client:
        response = client.get(
            "/v2/internal/managed-reflector/readiness",
            headers={"Authorization": "Bearer successor-recovery-test"},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "REFLECTOR_CREDENTIAL_MOUNT_NOT_READY"}
    assert "private managed-runtime detail" not in response.text


def test_managed_reflector_readiness_releases_lease_when_client_close_fails() -> None:
    request, binding, health = _production_recovery_fixture()
    services = _RecoveryServices(binding)
    client = _CloseFailingHealthClient(health)
    provider = ProductionManagedReflectorReadinessProviderV1(
        services=services,
        daemon_release_identity=request.execution_identity.daemon_release_identity,
        release_install_digest=request.execution_identity.release_install_digest,
        evolution_factory=lambda _binding: client,
    )

    with pytest.raises(
        ScienceSuccessorRecoveryV1Error,
        match="could not verify the registered worker",
    ) as captured:
        provider.read_managed_reflector_readiness()
    assert "private close detail" not in str(captured.value)
    assert len(services.leases) == 1
    assert services.leases[0].closed is True
    assert client.closed is True


def test_production_executor_rejects_current_worker_drift_without_intent(
    tmp_path: Path,
) -> None:
    request, binding, health = _production_recovery_fixture()
    health["workers"][0]["generation_digest"] = _sha("9")
    services = _RecoveryServices(binding)
    executor = ProductionScienceSuccessorRecoveryExecutorV1(
        services=services,
        native_runner=_NativeRunner(),
        daemon_release_identity=request.execution_identity.daemon_release_identity,
        release_install_digest=request.execution_identity.release_install_digest,
        evolution_factory=lambda _binding: _HealthClient(health),
    )
    store = ScienceSuccessorRecoveryStoreV1(tmp_path / "recovery-store")
    coordinator = ScienceSuccessorRecoveryCoordinatorV1(
        store=store,
        authority_reader=_AuthorityReader(request.source),
        executor=executor,
        clock=lambda: "2026-07-29T01:00:00Z",
    )
    created = coordinator.create_recovery(request)

    with pytest.raises(ScienceSuccessorRecoveryReadinessErrorV1):
        coordinator.run_authorized_target(
            recovery_id=created.record.recovery_id,
            target_id="agent_system",
            idempotency_key="drift-blocked-before-intent",
        )

    assert store.get_target_operation_or_none(created.record.recovery_id, "agent_system") is None
    assert services.leases[0].closed is True
