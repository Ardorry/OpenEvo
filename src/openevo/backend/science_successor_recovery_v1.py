"""Append-only, target-gated recovery for exhausted science successors.

This module deliberately does not retry or mutate the failed successor.  It
binds a new execution identity to immutable source authority, persists an
operation intent before each external side effect, and authorizes exactly one
evolution target at a time.  A completed target returns the recovery to an
explicit waiting state; neither another target nor a successor commit is
started automatically.

The benchmark adapter is responsible for obtaining the closed source evidence
from Core and for choosing when to authorize the next target.  Model execution
stays behind ``ScienceSuccessorRecoveryTargetExecutorV1``.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openevo.backend.contracts.v2 import models as m2
from openevo.backend.science_successor import (
    ScienceMethodOutputV2,
    ScienceSuccessorMethodPlanV2,
    ScienceSuccessorPlanV2,
    SealedTranscriptDatasetV2,
    science_successor_plan_sha256,
)
from openevo.evolution.admission import ArtifactProposalDecisionReceipt, ProposalAction
from openevo.evolution.framework.execution import (
    ReflectorInferenceBudgetReceipt,
    ReflectorRuntimeReceipt,
)
from openevo.evolution.models import ReflectorInferenceReservationReceipt
from openevo.evolution.revisions import AtomicSuccessorCommitV2
from openevo.runtime.managed_reflector_mount import (
    ManagedReflectorCredentialMountReadiness,
    ManagedReflectorRuntimeReadiness,
)

_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_TIMESTAMP_PATTERN = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z$"
)
_MAX_RECORD_BYTES = 1_048_576
_HISTORICAL_EVOLUTION_EVIDENCE_LABELS = (
    "core_failure_authority",
    "framework_lock",
    "legacy_supervisor_inventory",
    "release_bundle_manifest",
    "terminal_classification",
)


class ScienceSuccessorRecoveryV1Error(RuntimeError):
    """A target-gated recovery authority or durable state is invalid."""


class ScienceSuccessorRecoveryReadinessErrorV1(ScienceSuccessorRecoveryV1Error):
    """The managed reflector mount is not ready before intent persistence."""

    code = "REFLECTOR_CREDENTIAL_MOUNT_NOT_READY"

    def __init__(self) -> None:
        super().__init__(self.code)


class _RecoveryModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


def _canonical_bytes(value: BaseModel | dict[str, object]) -> bytes:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > _MAX_RECORD_BYTES:
        raise ScienceSuccessorRecoveryV1Error("recovery evidence exceeds its byte bound")
    return encoded


def _sha256(value: BaseModel | dict[str, object]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


class FailedEvolutionJobAuthorityV1(_RecoveryModel):
    """Immutable terminal evidence for the job that exhausted its retry path."""

    failed_evolution_job_authority_contract_version: Literal["1"] = "1"
    job_id: str = Field(pattern=_ID_PATTERN)
    successor_transition_id: str = Field(pattern=_ID_PATTERN)
    target_id: str = Field(pattern=_ID_PATTERN)
    method_id: str = Field(pattern=_ID_PATTERN)
    artifact_type: Literal[
        "text_memory",
        "skill_bundle",
        "agent_system",
        "parametric_memory",
    ]
    state: Literal["failed"]
    retryable: Literal[False]
    output_artifact_ids: tuple[str, ...] = Field(default=(), max_length=0)
    job_result_sha256: str = Field(pattern=_SHA256_PATTERN)
    failure_code: str = Field(pattern=_ID_PATTERN)


class FailedEvolutionPreJobOperationAuthorityV1(_RecoveryModel):
    """Terminal successor-attempt evidence before an Evolution job existed.

    This is deliberately a distinct authority rather than a synthetic failed
    job.  Core reproduces the transition attempt and Evolution reproduces the
    empty transition-job inventory before the authority may be sealed.
    """

    failed_evolution_pre_job_operation_authority_contract_version: Literal["1"] = "1"
    operation_id: str = Field(pattern=_ID_PATTERN)
    successor_transition_id: str = Field(pattern=_ID_PATTERN)
    target_id: str = Field(pattern=_ID_PATTERN)
    method_id: str = Field(pattern=_ID_PATTERN)
    artifact_type: Literal[
        "text_memory",
        "skill_bundle",
        "agent_system",
        "parametric_memory",
    ]
    state: Literal["failed"]
    retryable: Literal[False]
    evolution_job_ids: tuple[str, ...] = Field(default=(), max_length=0)
    evolution_job_inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    operation_result_sha256: str = Field(pattern=_SHA256_PATTERN)
    failure_code: str = Field(pattern=_ID_PATTERN)


def _exactly_one_failed_source(
    *,
    failed_job: FailedEvolutionJobAuthorityV1 | None,
    failed_pre_job_operation: FailedEvolutionPreJobOperationAuthorityV1 | None,
) -> None:
    if (failed_job is None) == (failed_pre_job_operation is None):
        raise ValueError("recovery source requires exactly one failed side-effect authority")


class HistoricalEvolutionEvidenceDigestV1(_RecoveryModel):
    """One closed, non-secret historical evidence-file identity."""

    label: str = Field(pattern=_ID_PATTERN)
    sha256: str = Field(pattern=_SHA256_PATTERN)


class HistoricalEvolutionSourceAttestationV1(_RecoveryModel):
    """Caller-supplied legacy evidence, explicitly not minted by Core.

    Core accepts this only as an attested historical provenance tier and then
    independently reproduces every live transition/dataset/feedback/job
    authority before sealing a source resolution.  It never upgrades the old
    generation or failure claim into Core-native historical authority.
    """

    historical_evolution_source_attestation_contract_version: Literal["1"] = "1"
    attestation_id: str = Field(pattern=_ID_PATTERN)
    provenance_tier: Literal["legacy_supervisor_attested"]
    core_native: Literal[False]
    authority_minted: Literal[False]
    supervisor_namespace: str = Field(pattern=_ID_PATTERN)
    supervisor_observed_state: str = Field(pattern=_ID_PATTERN)
    supervisor_state_sha256: str = Field(pattern=_SHA256_PATTERN)
    supervisor_database_sha256: str = Field(pattern=_SHA256_PATTERN)
    supervisor_tree_sha256: str = Field(pattern=_SHA256_PATTERN)
    legacy_supervisor_inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    terminal_classification_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_transition_id: str = Field(pattern=_ID_PATTERN)
    failed_job_id: str | None = Field(
        default=None,
        pattern=_ID_PATTERN,
        exclude_if=lambda value: value is None,
    )
    failed_pre_job_operation_id: str | None = Field(
        default=None,
        pattern=_ID_PATTERN,
        exclude_if=lambda value: value is None,
    )
    claimed_source_core_generation: str = Field(pattern=_SHA256_PATTERN)
    claimed_source_release_identity: str = Field(pattern=_SHA256_PATTERN)
    claimed_failure_code: str = Field(pattern=_ID_PATTERN)
    evidence_inventory: tuple[HistoricalEvolutionEvidenceDigestV1, ...] = Field(
        min_length=len(_HISTORICAL_EVOLUTION_EVIDENCE_LABELS),
        max_length=len(_HISTORICAL_EVOLUTION_EVIDENCE_LABELS),
    )
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _closed_attestation(self) -> HistoricalEvolutionSourceAttestationV1:
        if (self.failed_job_id is None) == (self.failed_pre_job_operation_id is None):
            raise ValueError("historical attestation requires exactly one failed source identity")
        labels = tuple(item.label for item in self.evidence_inventory)
        if labels != _HISTORICAL_EVOLUTION_EVIDENCE_LABELS:
            raise ValueError("historical evidence inventory is not the exact closed set")
        evidence = {item.label: item.sha256 for item in self.evidence_inventory}
        if (
            evidence["legacy_supervisor_inventory"]
            != self.legacy_supervisor_inventory_sha256
            or evidence["terminal_classification"]
            != self.terminal_classification_sha256
        ):
            raise ValueError("historical evidence identity fields differ from inventory")
        if _sha256(self.model_dump(mode="json", exclude={"content_sha256"})) != (
            self.content_sha256
        ):
            raise ValueError("historical source attestation digest is invalid")
        return self


class HistoricalEvolutionSourceProvenanceV1(_RecoveryModel):
    """Current Core receipt for resolving one non-Core-native legacy claim."""

    historical_evolution_source_provenance_contract_version: Literal["1"] = "1"
    provenance_tier: Literal["legacy_supervisor_attested"]
    core_native: Literal[False]
    authority_minted: Literal[False]
    attestation_id: str = Field(pattern=_ID_PATTERN)
    attestation_sha256: str = Field(pattern=_SHA256_PATTERN)
    resolution_core_generation: str = Field(pattern=_SHA256_PATTERN)
    resolution_daemon_release_identity: str = Field(pattern=_SHA256_PATTERN)
    resolution_release_install_digest: str = Field(pattern=_SHA256_PATTERN)
    current_core_authorities_reproduced: Literal[True]


class ExhaustedSuccessorRecoveryAuthorityV1(_RecoveryModel):
    """Closed source authority required before a new recovery may exist."""

    exhausted_successor_recovery_authority_contract_version: Literal["1"] = "1"
    project_id: str = Field(pattern=_ID_PATTERN)
    task_id: str = Field(pattern=_ID_PATTERN)
    task_admission_id: str = Field(pattern=_ID_PATTERN)
    accepted_attempt_id: str = Field(pattern=_ID_PATTERN)
    successor_transition_id: str = Field(pattern=_ID_PATTERN)
    successor_transition_state: Literal["failed"]
    successor_transition_state_sha256: str = Field(pattern=_SHA256_PATTERN)
    transition_attempt_count: int = Field(ge=1, le=100)
    transition_attempt_capacity: int = Field(ge=1, le=100)
    source_commit_absent: Literal[True]
    source_outputs_absent: Literal[True]
    predecessor_project_head_id: str = Field(pattern=_ID_PATTERN)
    predecessor_project_head_sha256: str = Field(pattern=_SHA256_PATTERN)
    active_project_head_id: str = Field(pattern=_ID_PATTERN)
    active_project_head_sha256: str = Field(pattern=_SHA256_PATTERN)
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_core_generation: str = Field(pattern=_SHA256_PATTERN)
    source_release_identity: str = Field(pattern=_SHA256_PATTERN)
    source_provenance: HistoricalEvolutionSourceProvenanceV1
    dataset: SealedTranscriptDatasetV2
    failed_job: FailedEvolutionJobAuthorityV1 | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    failed_pre_job_operation: FailedEvolutionPreJobOperationAuthorityV1 | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _closed_exhausted_authority(self) -> ExhaustedSuccessorRecoveryAuthorityV1:
        feedback = self.dataset.training_feedback
        _exactly_one_failed_source(
            failed_job=self.failed_job,
            failed_pre_job_operation=self.failed_pre_job_operation,
        )
        if self.transition_attempt_count != self.transition_attempt_capacity:
            raise ValueError("source successor retry capacity is not exhausted")
        if (
            self.active_project_head_id != self.predecessor_project_head_id
            or self.active_project_head_sha256 != self.predecessor_project_head_sha256
        ):
            raise ValueError("source active project head is not the exact predecessor")
        failed = self.failed_job or self.failed_pre_job_operation
        assert failed is not None
        if failed.successor_transition_id != self.successor_transition_id:
            raise ValueError("failed side effect belongs to another successor transition")
        if (
            self.dataset.task_id != self.task_id
            or self.dataset.task_admission_id != self.task_admission_id
            or self.dataset.accepted_attempt_id != self.accepted_attempt_id
        ):
            raise ValueError("sealed dataset belongs to another task authority")
        if feedback is None:
            raise ValueError("recovery requires a sealed training feedback binding")
        if (
            feedback.completed_dataset_id != self.dataset.dataset_id
            or feedback.task_scope_id != self.task_id
        ):
            raise ValueError("training feedback binding does not match the sealed dataset")
        return self


def science_successor_recovery_failure_id(
    source: ExhaustedSuccessorRecoveryAuthorityV1,
) -> str:
    """Return the real failed job or pre-job operation identity."""

    if source.failed_job is not None:
        return source.failed_job.job_id
    if source.failed_pre_job_operation is not None:
        return source.failed_pre_job_operation.operation_id
    raise ScienceSuccessorRecoveryV1Error("recovery source has no failed side-effect authority")


def science_successor_recovery_failed_target(
    source: ExhaustedSuccessorRecoveryAuthorityV1,
) -> tuple[str, str, str]:
    """Return target, method, and artifact type from the exact source failure."""

    failed = source.failed_job or source.failed_pre_job_operation
    if failed is None:
        raise ScienceSuccessorRecoveryV1Error("recovery source has no failed side-effect authority")
    return failed.target_id, failed.method_id, failed.artifact_type


def science_successor_recovery_source_sha256(
    source: ExhaustedSuccessorRecoveryAuthorityV1,
) -> str:
    if type(source) is not ExhaustedSuccessorRecoveryAuthorityV1:
        raise TypeError("recovery source digest requires exact source authority")
    return _sha256(source)


def science_successor_recovery_adoptable_job_result_sha256(
    terminal: Mapping[str, object],
) -> str:
    """Hash immutable terminal job evidence while excluding promotion state."""

    raw_outputs = terminal.get("outputs")
    outputs: list[object] = []
    if isinstance(raw_outputs, list):
        outputs = [
            ({key: value for key, value in item.items() if key != "promoted"})
            if isinstance(item, dict)
            else item
            for item in raw_outputs
        ]
    basis = {
        "artifact_ids": terminal.get("artifact_ids"),
        "error": terminal.get("error"),
        "job_id": terminal.get("job_id"),
        "outputs": outputs,
        "retryable": terminal.get("retryable"),
        "state": terminal.get("state"),
        "successor_transition_id": terminal.get("successor_transition_id"),
    }
    return _sha256(basis)


class RecoveryExecutionIdentityV1(_RecoveryModel):
    """New generation-bound identity that owns recovered target operations."""

    recovery_execution_identity_contract_version: Literal["1"] = "1"
    core_generation: str = Field(pattern=_SHA256_PATTERN)
    daemon_release_identity: str = Field(pattern=_SHA256_PATTERN)
    release_install_digest: str = Field(pattern=_SHA256_PATTERN)
    executable_registry_digest: str = Field(pattern=_SHA256_PATTERN)
    framework_lock_digest: str = Field(pattern=_SHA256_PATTERN)
    runtime_identity_digest: str = Field(pattern=_SHA256_PATTERN)
    runtime_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    service_identity_id: str = Field(pattern=_ID_PATTERN)
    runtime_profile: Literal["managed_science"]
    max_reflector_model_calls: Literal[1]


class ScienceSuccessorRecoveryCreateRequestV1(_RecoveryModel):
    """Create a new append-only successor recovery, without executing a target."""

    successor_recovery_create_contract_version: Literal["1"] = "1"
    idempotency_key: str = Field(pattern=_ID_PATTERN)
    source: ExhaustedSuccessorRecoveryAuthorityV1
    plan: ScienceSuccessorPlanV2
    execution_identity: RecoveryExecutionIdentityV1
    supersedes_recovery_id: str | None = Field(
        default=None,
        pattern=_ID_PATTERN,
        exclude_if=lambda value: value is None,
    )
    target_authorization_order: tuple[str, ...] = Field(min_length=1, max_length=128)
    source_mutation_allowed: Literal[False]
    candidate_execution_allowed: Literal[False]
    automatic_target_progression: Literal[False]
    automatic_successor_commit: Literal[False]

    @model_validator(mode="after")
    def _exact_source_plan(self) -> ScienceSuccessorRecoveryCreateRequestV1:
        source = self.source
        plan = self.plan
        plan_targets = tuple(item.target_id for item in plan.enabled_methods)
        failed_target_id, failed_method_id, failed_artifact_type = (
            science_successor_recovery_failed_target(source)
        )
        if (
            plan.project_id != source.project_id
            or plan.task_id != source.task_id
            or plan.task_admission_id != source.task_admission_id
            or plan.accepted_attempt_id != source.accepted_attempt_id
            or plan.predecessor_project_head_id != source.predecessor_project_head_id
            or science_successor_plan_sha256(plan) != source.plan_sha256
        ):
            raise ValueError("recovery plan does not match the immutable source authority")
        if (
            len(self.target_authorization_order) != len(set(self.target_authorization_order))
            or set(self.target_authorization_order) != set(plan_targets)
            or self.target_authorization_order[0] != failed_target_id
        ):
            raise ValueError("recovery target authorization order is not the exact plan inventory")
        failed_target = next(
            (
                item
                for item in plan.enabled_methods
                if item.target_id == failed_target_id
            ),
            None,
        )
        if (
            failed_target is None
            or failed_target.method_id != failed_method_id
            or failed_target.output_artifact_type != failed_artifact_type
        ):
            raise ValueError("source failed job does not match the successor plan")
        if (
            self.execution_identity.core_generation == source.source_core_generation
            or self.execution_identity.daemon_release_identity
            == source.source_release_identity
        ):
            raise ValueError("recovery requires a new execution identity")
        return self


class ScienceSuccessorRecoverySourceResolveRequestV1(_RecoveryModel):
    """Closed legacy attestation plus IDs Core must independently reproduce."""

    recovery_source_resolve_contract_version: Literal["1"] = "1"
    resolution_idempotency_key: str = Field(pattern=_ID_PATTERN)
    successor_transition_id: str = Field(pattern=_ID_PATTERN)
    failed_job_id: str | None = Field(
        default=None,
        pattern=_ID_PATTERN,
        exclude_if=lambda value: value is None,
    )
    failed_pre_job_operation_id: str | None = Field(
        default=None,
        pattern=_ID_PATTERN,
        exclude_if=lambda value: value is None,
    )
    historical_attestation: HistoricalEvolutionSourceAttestationV1
    attachment_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    resolved_dataset_artifact_id: str = Field(pattern=_ID_PATTERN)
    transition_attempt_capacity: int = Field(ge=1, le=100)

    @model_validator(mode="after")
    def _closed_ids(self) -> ScienceSuccessorRecoverySourceResolveRequestV1:
        if (self.failed_job_id is None) == (self.failed_pre_job_operation_id is None):
            raise ValueError("recovery resolution requires exactly one failed source identity")
        if (
            self.attachment_ids != tuple(sorted(self.attachment_ids))
            or len(self.attachment_ids) != len(set(self.attachment_ids))
            or self.historical_attestation.source_transition_id
            != self.successor_transition_id
            or self.historical_attestation.failed_job_id != self.failed_job_id
            or self.historical_attestation.failed_pre_job_operation_id
            != self.failed_pre_job_operation_id
        ):
            raise ValueError("recovery source IDs differ from the closed attestation")
        return self


class ScienceSuccessorRecoverySourceResponseV1(_RecoveryModel):
    recovery_source_response_contract_version: Literal["1"] = "1"
    source: ExhaustedSuccessorRecoveryAuthorityV1
    plan: ScienceSuccessorPlanV2

    @model_validator(mode="after")
    def _same_plan(self) -> ScienceSuccessorRecoverySourceResponseV1:
        if science_successor_plan_sha256(self.plan) != self.source.plan_sha256:
            raise ValueError("resolved recovery source plan digest is inconsistent")
        return self


class AgentSystemAuditReceiptV1(_RecoveryModel):
    """Sanitized native audit summary; protected literals remain private."""

    agent_system_audit_receipt_contract_version: Literal["1"] = "1"
    enabled: bool
    repair_count: int = Field(ge=0, le=100)
    finding_count: int = Field(ge=0, le=100_000)
    forbidden_literal_count: int = Field(ge=0, le=100_000)
    leakage_basis_sha256: str = Field(pattern=_SHA256_PATTERN)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _digest(self) -> AgentSystemAuditReceiptV1:
        payload = self.model_dump(mode="json", exclude={"content_sha256"})
        if _sha256(payload) != self.content_sha256:
            raise ValueError("agent-system audit receipt digest is invalid")
        return self


def science_successor_recovery_admission_report_sha256(
    *,
    job_id: str,
    target_id: str,
    method_id: str,
    proposal_artifact_id: str,
    proposal_payload_manifest_sha256: str,
    registry_artifact_manifest_sha256: str,
    agent_system_audit_receipt_sha256: str | None,
    reflector_runtime_receipt_sha256: str,
    reflector_inference_reservation_sha256: str,
    reflector_inference_budget_sha256: str,
) -> str:
    """Bind native proposal metadata and sanitized audit to admission evidence."""

    return _sha256(
        {
            "job_id": job_id,
            "target_id": target_id,
            "method_id": method_id,
            "proposal_artifact_id": proposal_artifact_id,
            "proposal_payload_manifest_sha256": proposal_payload_manifest_sha256,
            "registry_artifact_manifest_sha256": registry_artifact_manifest_sha256,
            "agent_system_audit_receipt_sha256": (
                agent_system_audit_receipt_sha256
            ),
            "reflector_runtime_receipt_sha256": reflector_runtime_receipt_sha256,
            "reflector_inference_reservation_sha256": (
                reflector_inference_reservation_sha256
            ),
            "reflector_inference_budget_sha256": reflector_inference_budget_sha256,
        }
    )


class ScienceSuccessorRecoveryTargetIntentV1(_RecoveryModel):
    successor_recovery_target_intent_contract_version: Literal["1"] = "1"
    operation_id: str = Field(pattern=_ID_PATTERN)
    idempotency_key: str = Field(pattern=_ID_PATTERN)
    recovery_id: str = Field(pattern=_ID_PATTERN)
    source_authority_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_identity: RecoveryExecutionIdentityV1
    target: ScienceSuccessorMethodPlanV2
    state: Literal["running"]
    created_at: str = Field(pattern=_TIMESTAMP_PATTERN)


class ScienceSuccessorRecoveryTargetResultV1(_RecoveryModel):
    """Verified promotion result for one explicitly authorized target."""

    successor_recovery_target_result_contract_version: Literal["1"] = "1"
    operation_id: str = Field(pattern=_ID_PATTERN)
    recovery_id: str = Field(pattern=_ID_PATTERN)
    target_id: str = Field(pattern=_ID_PATTERN)
    method_id: str = Field(pattern=_ID_PATTERN)
    source_failed_job_id: str = Field(pattern=_ID_PATTERN)
    source_failure_kind: Literal["evolution_job", "pre_job_operation"] = Field(
        default="evolution_job",
        exclude_if=lambda value: value == "evolution_job",
    )
    source_failed_job_retried: Literal[False]
    job_owner_successor_transition_id: str | None = Field(
        default=None,
        pattern=_ID_PATTERN,
        exclude_if=lambda value: value is None,
    )
    job_id: str = Field(pattern=_ID_PATTERN)
    job_state: Literal["succeeded"]
    proposal_ids: tuple[str, ...] = Field(min_length=1, max_length=128)
    admission_status: Literal["accepted"]
    promotion_status: Literal["promoted"]
    output: ScienceMethodOutputV2
    registry_artifact_id: str = Field(pattern=_ID_PATTERN)
    registry_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    registry_artifact_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    agent_system_audit: AgentSystemAuditReceiptV1 | None = None
    admission_decision: ArtifactProposalDecisionReceipt
    reflector_runtime_receipt: ReflectorRuntimeReceipt
    reflector_inference_reservation: ReflectorInferenceReservationReceipt
    inference_budget_receipt: ReflectorInferenceBudgetReceipt
    completed_at: str = Field(pattern=_TIMESTAMP_PATTERN)

    @model_validator(mode="after")
    def _exact_promoted_output(self) -> ScienceSuccessorRecoveryTargetResultV1:
        job_owner = self.job_owner_successor_transition_id or self.recovery_id
        if len(self.proposal_ids) != len(set(self.proposal_ids)):
            raise ValueError("recovery proposal IDs must be unique")
        if (
            self.output.target_id != self.target_id
            or self.output.method_id != self.method_id
            or self.output.artifact_id != self.registry_artifact_id
            or self.output.manifest_sha256 != self.registry_manifest_sha256
            or (
                (self.output.artifact_type == "agent_system")
                != (self.agent_system_audit is not None)
            )
            or (
                self.agent_system_audit is not None
                and (
                    self.agent_system_audit.enabled is not True
                    or self.agent_system_audit.repair_count != 0
                    or self.agent_system_audit.finding_count != 0
                    or self.agent_system_audit.forbidden_literal_count <= 0
                )
            )
            or self.admission_decision.job_id != self.job_id
            or self.admission_decision.artifact_type.value
            != self.output.artifact_type
            or self.admission_decision.action is not ProposalAction.UPDATE
            or self.admission_decision.proposal_artifact_ids != self.proposal_ids
            or self.admission_decision.selected_artifact_id
            != self.registry_artifact_id
            or self.admission_decision.active_artifact_id
            != self.registry_artifact_id
            or self.admission_decision.admission.passed is not True
            or self.admission_decision.admission.report_sha256
            != science_successor_recovery_admission_report_sha256(
                job_id=self.job_id,
                target_id=self.target_id,
                method_id=self.method_id,
                proposal_artifact_id=self.registry_artifact_id,
                proposal_payload_manifest_sha256=self.registry_manifest_sha256,
                registry_artifact_manifest_sha256=(
                    self.registry_artifact_manifest_sha256
                ),
                agent_system_audit_receipt_sha256=(
                    None
                    if self.agent_system_audit is None
                    else self.agent_system_audit.content_sha256
                ),
                reflector_runtime_receipt_sha256=_sha256(
                    self.reflector_runtime_receipt
                ),
                reflector_inference_reservation_sha256=(
                    self.reflector_inference_reservation.content_sha256
                ),
                reflector_inference_budget_sha256=(
                    self.inference_budget_receipt.content_sha256
                ),
            )
            or self.reflector_runtime_receipt.runtime_profile != "managed_science"
            or self.reflector_inference_reservation.job_id != self.job_id
            or self.reflector_inference_reservation.state != "completed"
            or self.reflector_inference_reservation.runtime_receipt_sha256
            != _sha256(self.reflector_runtime_receipt)
            or self.inference_budget_receipt.target_id != self.target_id
            or self.inference_budget_receipt.method_id != self.method_id
            or self.inference_budget_receipt.actual_reflector_model_calls != 1
            or (
                self.output.owner_successor_transition_id is not None
                and self.output.owner_successor_transition_id != job_owner
            )
        ):
            raise ValueError("recovery registry result differs from the promoted output")
        return self


class ScienceSuccessorRecoveryCarriedTargetAuthorityV1(_RecoveryModel):
    """Exact succeeded target evidence carried into one append-only successor.

    The original intent and result remain owned by the prior recovery.  The new
    recovery may only treat them as an immutable completed prefix; it cannot
    execute, recover, or re-admit the target.
    """

    successor_recovery_carried_target_authority_contract_version: Literal["1"] = "1"
    prior_recovery_id: str = Field(pattern=_ID_PATTERN)
    carried_from_recovery_id: str | None = Field(
        default=None,
        pattern=_ID_PATTERN,
        exclude_if=lambda value: value is None,
    )
    prior_intent: ScienceSuccessorRecoveryTargetIntentV1
    prior_result: ScienceSuccessorRecoveryTargetResultV1
    prior_intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    prior_result_sha256: str = Field(pattern=_SHA256_PATTERN)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _closed_authority(
        self,
    ) -> ScienceSuccessorRecoveryCarriedTargetAuthorityV1:
        intent = self.prior_intent
        result = self.prior_result
        if (
            intent.recovery_id != self.prior_recovery_id
            or result.recovery_id != self.prior_recovery_id
            or self.carried_from_recovery_id == self.prior_recovery_id
            or result.operation_id != intent.operation_id
            or result.target_id != intent.target.target_id
            or result.method_id != intent.target.method_id
            or result.output.artifact_type != intent.target.output_artifact_type
            or self.prior_intent_sha256 != _sha256(intent)
            or self.prior_result_sha256 != _sha256(result)
            or _sha256(self.model_dump(mode="json", exclude={"content_sha256"}))
            != self.content_sha256
        ):
            raise ValueError("carried recovery target authority is invalid")
        return self


class ScienceSuccessorRecoveryAdoptedJobAuthorityV1(_RecoveryModel):
    """Current-Core proof for adopting one completed paid target job.

    The prior recovery remains terminal.  This authority permits only the
    deterministic admission/promotion tail for the exact sealed proposal; it
    never authorizes another Evolution job or model call.
    """

    successor_recovery_adopted_job_authority_contract_version: Literal["1"] = "1"
    prior_recovery_id: str = Field(pattern=_ID_PATTERN)
    prior_operation_id: str = Field(pattern=_ID_PATTERN)
    source_transition_id: str = Field(pattern=_ID_PATTERN)
    job_owner_successor_transition_id: str = Field(pattern=_ID_PATTERN)
    target_id: str = Field(pattern=_ID_PATTERN)
    method_id: str = Field(pattern=_ID_PATTERN)
    artifact_type: Literal[
        "text_memory",
        "skill_bundle",
        "agent_system",
        "parametric_memory",
    ]
    job_id: str = Field(pattern=_ID_PATTERN)
    job_state: Literal["succeeded"]
    job_result_sha256: str = Field(pattern=_SHA256_PATTERN)
    proposal_artifact_id: str = Field(pattern=_ID_PATTERN)
    proposal_payload_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    proposal_payload_byte_size: int = Field(ge=0, le=m2.MAX_SNAPSHOT_BYTES)
    proposal_registry_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    proposal_promoted: Literal[False]
    admission_decision_absent: Literal[True]
    inference_reservation_sha256: str = Field(pattern=_SHA256_PATTERN)
    reflector_model_calls_started: Literal[1]
    created_at: str = Field(pattern=_TIMESTAMP_PATTERN)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _closed_authority(
        self,
    ) -> ScienceSuccessorRecoveryAdoptedJobAuthorityV1:
        if (
            self.job_owner_successor_transition_id != self.prior_recovery_id
            or _sha256(self.model_dump(mode="json", exclude={"content_sha256"}))
            != self.content_sha256
        ):
            raise ValueError("recovery adopted job authority digest is invalid")
        return self


class ScienceSuccessorRecoveryTargetFailureV1(_RecoveryModel):
    """Sanitized terminal failure evidence; raw stderr and secrets are excluded."""

    successor_recovery_target_failure_contract_version: Literal["1"] = "1"
    operation_id: str = Field(pattern=_ID_PATTERN)
    recovery_id: str = Field(pattern=_ID_PATTERN)
    target_id: str = Field(pattern=_ID_PATTERN)
    failure_code: str = Field(pattern=_ID_PATTERN)
    failure_class: Literal["infrastructure", "deterministic", "policy"]
    retryable: Literal[False]
    failure_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)
    failed_at: str = Field(pattern=_TIMESTAMP_PATTERN)


class ScienceSuccessorRecoverySupersessionAuthorityV1(_RecoveryModel):
    """Current-Core proof for one append-only failed-recovery successor.

    The closed alternatives are either an empty pre-model inventory, or one
    succeeded paid job whose sealed proposal may be adopted without rerunning
    inference.  The failed recovery itself is never changed.
    """

    successor_recovery_supersession_authority_contract_version: Literal["1"] = "1"
    prior_recovery_id: str = Field(pattern=_ID_PATTERN)
    prior_recovery_record_sha256: str = Field(pattern=_SHA256_PATTERN)
    prior_operation_id: str = Field(pattern=_ID_PATTERN)
    prior_failure_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_transition_id: str = Field(pattern=_ID_PATTERN)
    evolution_job_ids: tuple[str, ...] = Field(default=(), max_length=128)
    evolution_job_inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    paid_model_side_effect_absent: bool
    carried_completed_targets: tuple[
        ScienceSuccessorRecoveryCarriedTargetAuthorityV1, ...
    ] = Field(
        default=(),
        max_length=128,
        exclude_if=lambda value: not value,
    )
    adopted_job: ScienceSuccessorRecoveryAdoptedJobAuthorityV1 | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    carried_adopted_job_authority_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN,
        exclude_if=lambda value: value is None,
    )
    core_generation: str = Field(pattern=_SHA256_PATTERN)
    daemon_release_identity: str = Field(pattern=_SHA256_PATTERN)
    created_at: str = Field(pattern=_TIMESTAMP_PATTERN)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _closed_authority(self) -> ScienceSuccessorRecoverySupersessionAuthorityV1:
        carried_target_ids = tuple(
            item.prior_result.target_id for item in self.carried_completed_targets
        )
        if (
            len(carried_target_ids) != len(set(carried_target_ids))
            or any(
                (item.carried_from_recovery_id or item.prior_recovery_id)
                != self.prior_recovery_id
                for item in self.carried_completed_targets
            )
        ):
            raise ValueError("carried recovery target inventory is inconsistent")
        carried_local_job_ids = tuple(
            item.prior_result.job_id
            for item in self.carried_completed_targets
            if (
                item.prior_result.job_owner_successor_transition_id
                or item.prior_result.recovery_id
            )
            == self.prior_recovery_id
        )
        residual_job_ids = tuple(
            item for item in self.evolution_job_ids if item not in carried_local_job_ids
        )
        if (
            len(self.evolution_job_ids) != len(set(self.evolution_job_ids))
            or any(item not in self.evolution_job_ids for item in carried_local_job_ids)
        ):
            raise ValueError("recovery Evolution inventory does not bind carried targets")
        if self.adopted_job is None:
            if (
                residual_job_ids
                or self.paid_model_side_effect_absent is not True
                or self.carried_adopted_job_authority_sha256 is not None
            ):
                raise ValueError("empty recovery supersession authority is inconsistent")
        elif self.carried_adopted_job_authority_sha256 is None:
            if (
                residual_job_ids != (self.adopted_job.job_id,)
                or self.paid_model_side_effect_absent is not False
                or self.adopted_job.prior_recovery_id != self.prior_recovery_id
                or self.adopted_job.prior_operation_id != self.prior_operation_id
                or self.adopted_job.source_transition_id != self.source_transition_id
            ):
                raise ValueError("paid recovery supersession authority is inconsistent")
        elif (
            residual_job_ids
            or self.paid_model_side_effect_absent is not False
            or self.carried_adopted_job_authority_sha256
            != self.adopted_job.content_sha256
            or self.adopted_job.prior_recovery_id == self.prior_recovery_id
            or self.adopted_job.source_transition_id != self.source_transition_id
        ):
            raise ValueError("carried recovery adoption authority is inconsistent")
        payload = self.model_dump(mode="json", exclude={"content_sha256"})
        if _sha256(payload) != self.content_sha256:
            raise ValueError("recovery supersession authority digest is invalid")
        return self


class RecoveryTargetTerminalFailureV1(RuntimeError):
    """Executor signal that a typed, non-retryable failure is authoritative."""

    def __init__(self, failure: ScienceSuccessorRecoveryTargetFailureV1) -> None:
        if type(failure) is not ScienceSuccessorRecoveryTargetFailureV1:
            raise TypeError("terminal recovery failure requires exact typed evidence")
        self.failure = failure
        super().__init__("recovery target failed terminally")


class ScienceSuccessorRecoveryTargetStateV1(_RecoveryModel):
    target_id: str = Field(pattern=_ID_PATTERN)
    method_id: str = Field(pattern=_ID_PATTERN)
    artifact_type: Literal[
        "text_memory",
        "skill_bundle",
        "agent_system",
        "parametric_memory",
    ]
    state: Literal["pending", "running", "succeeded", "failed"]
    operation_id: str | None = Field(default=None, pattern=_ID_PATTERN)
    result_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    failure_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _state_evidence(self) -> ScienceSuccessorRecoveryTargetStateV1:
        if self.state == "pending" and any(
            value is not None
            for value in (self.operation_id, self.result_sha256, self.failure_sha256)
        ):
            raise ValueError("pending recovery target exposes operation evidence")
        if self.state == "running" and (
            self.operation_id is None
            or self.result_sha256 is not None
            or self.failure_sha256 is not None
        ):
            raise ValueError("running recovery target evidence is incomplete")
        if self.state == "succeeded" and (
            self.operation_id is None
            or self.result_sha256 is None
            or self.failure_sha256 is not None
        ):
            raise ValueError("succeeded recovery target evidence is incomplete")
        if self.state == "failed" and (
            self.operation_id is None
            or self.result_sha256 is not None
            or self.failure_sha256 is None
        ):
            raise ValueError("failed recovery target evidence is incomplete")
        return self


class ScienceSuccessorRecoveryRecordV1(_RecoveryModel):
    successor_recovery_record_contract_version: Literal["1"] = "1"
    recovery_id: str = Field(pattern=_ID_PATTERN)
    source_transition_id: str = Field(pattern=_ID_PATTERN)
    source_failed_job_id: str = Field(pattern=_ID_PATTERN)
    source_failure_kind: Literal["evolution_job", "pre_job_operation"] = Field(
        default="evolution_job",
        exclude_if=lambda value: value == "evolution_job",
    )
    source_authority_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_transition_state_sha256: str = Field(pattern=_SHA256_PATTERN)
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_identity: RecoveryExecutionIdentityV1
    supersedes_recovery_id: str | None = Field(
        default=None,
        pattern=_ID_PATTERN,
        exclude_if=lambda value: value is None,
    )
    supersession_authority: ScienceSuccessorRecoverySupersessionAuthorityV1 | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    target_authorization_order: tuple[str, ...] = Field(min_length=1, max_length=128)
    state: Literal[
        "awaiting_target_authorization",
        "target_running",
        "all_targets_completed_awaiting_commit",
        "failed",
    ]
    active_target_id: str | None = Field(default=None, pattern=_ID_PATTERN)
    completed_target_ids: tuple[str, ...] = Field(default=(), max_length=128)
    remaining_target_ids: tuple[str, ...] = Field(default=(), max_length=128)
    targets: tuple[ScienceSuccessorRecoveryTargetStateV1, ...] = Field(
        min_length=1,
        max_length=128,
    )
    version: int = Field(ge=1)
    created_at: str = Field(pattern=_TIMESTAMP_PATTERN)
    updated_at: str = Field(pattern=_TIMESTAMP_PATTERN)

    @model_validator(mode="after")
    def _closed_record(self) -> ScienceSuccessorRecoveryRecordV1:
        if (self.supersedes_recovery_id is None) != (
            self.supersession_authority is None
        ):
            raise ValueError("recovery supersession identity is incomplete")
        carried_targets = (
            ()
            if self.supersession_authority is None
            else self.supersession_authority.carried_completed_targets
        )
        carried_target_ids = tuple(
            item.prior_result.target_id for item in carried_targets
        )
        adopted_target_index = len(carried_target_ids)
        if self.supersession_authority is not None and (
            self.supersession_authority.prior_recovery_id
            != self.supersedes_recovery_id
            or self.supersession_authority.source_transition_id
            != self.source_transition_id
            or self.supersession_authority.core_generation
            != self.execution_identity.core_generation
            or self.supersession_authority.daemon_release_identity
            != self.execution_identity.daemon_release_identity
            or (
                self.supersession_authority.adopted_job is not None
                and (
                    adopted_target_index >= len(self.target_authorization_order)
                    or self.supersession_authority.adopted_job.target_id
                    != self.target_authorization_order[adopted_target_index]
                )
            )
            or carried_target_ids
            != self.target_authorization_order[: len(carried_target_ids)]
        ):
            raise ValueError("recovery supersession authority differs from the record")
        target_ids = tuple(item.target_id for item in self.targets)
        if target_ids != self.target_authorization_order:
            raise ValueError("recovery target states are not in authorization order")
        completed = tuple(item.target_id for item in self.targets if item.state == "succeeded")
        remaining = tuple(item.target_id for item in self.targets if item.state == "pending")
        running = tuple(item.target_id for item in self.targets if item.state == "running")
        failed = tuple(item.target_id for item in self.targets if item.state == "failed")
        if (
            completed != self.completed_target_ids
            or remaining != self.remaining_target_ids
            or len(running) > 1
            or self.active_target_id != (running[0] if running else None)
        ):
            raise ValueError("recovery target state summary is inconsistent")
        expected_prefix = self.target_authorization_order[: len(completed)]
        if completed != expected_prefix:
            raise ValueError("recovery completed targets are not a contiguous prefix")
        if self.state == "awaiting_target_authorization" and (
            running or failed or not remaining or self.active_target_id is not None
        ):
            raise ValueError("waiting recovery state is inconsistent")
        if self.state == "target_running" and (len(running) != 1 or failed):
            raise ValueError("running recovery state is inconsistent")
        if self.state == "all_targets_completed_awaiting_commit" and (
            running or failed or remaining or len(completed) != len(self.targets)
        ):
            raise ValueError("complete recovery state is inconsistent")
        if self.state == "failed" and (len(failed) != 1 or running):
            raise ValueError("failed recovery state is inconsistent")
        return self


class ScienceSuccessorRecoverySnapshotV1(_RecoveryModel):
    record: ScienceSuccessorRecoveryRecordV1
    record_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _digest(self) -> ScienceSuccessorRecoverySnapshotV1:
        if _sha256(self.record) != self.record_sha256:
            raise ValueError("recovery record digest is inconsistent")
        return self


class ScienceSuccessorRecoveryProjectSeedRequestV1(_RecoveryModel):
    """Explicit authorization to seed one unused destination project."""

    successor_recovery_project_seed_request_contract_version: Literal["1"] = "1"
    idempotency_key: str = Field(pattern=_ID_PATTERN)
    recovery_id: str = Field(pattern=_ID_PATTERN)
    recovery_record_sha256: str = Field(pattern=_SHA256_PATTERN)
    destination_project_id: str = Field(pattern=_ID_PATTERN)
    expected_destination_project_head_id: str = Field(pattern=_ID_PATTERN)
    expected_destination_project_head_sha256: str = Field(pattern=_SHA256_PATTERN)


class ScienceSuccessorRecoveryProjectSeedAuthorityV1(_RecoveryModel):
    """Durable append-only receipt for the recovery-seeded Core head."""

    successor_recovery_project_seed_authority_contract_version: Literal["1"] = "1"
    seed_request_id: str = Field(pattern=_ID_PATTERN)
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    recovery_id: str = Field(pattern=_ID_PATTERN)
    recovery_record_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_project_id: str = Field(pattern=_ID_PATTERN)
    source_transition_id: str = Field(pattern=_ID_PATTERN)
    source_authority_sha256: str = Field(pattern=_SHA256_PATTERN)
    destination_project_id: str = Field(pattern=_ID_PATTERN)
    predecessor_destination_project_head_id: str = Field(pattern=_ID_PATTERN)
    successor_destination_project_head: m2.ProjectHeadRefV2
    commit: AtomicSuccessorCommitV2
    target_result_sha256: tuple[str, ...] = Field(min_length=3, max_length=3)
    created_at: str = Field(pattern=_TIMESTAMP_PATTERN)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _closed_seed_authority(self) -> ScienceSuccessorRecoveryProjectSeedAuthorityV1:
        manifest = self.commit.manifest
        if (
            self.successor_destination_project_head.project_id
            != self.destination_project_id
            or self.successor_destination_project_head.predecessor_project_head_id
            != self.predecessor_destination_project_head_id
            or manifest.project_id != self.destination_project_id
            or getattr(manifest, "seed_request_id", None) != self.seed_request_id
            or getattr(manifest, "recovery_id", None) != self.recovery_id
            or getattr(manifest, "recovery_record_sha256", None)
            != self.recovery_record_sha256
            or getattr(manifest, "source_project_id", None) != self.source_project_id
            or getattr(manifest, "source_transition_id", None)
            != self.source_transition_id
            or getattr(manifest, "source_authority_sha256", None)
            != self.source_authority_sha256
            or getattr(manifest, "successor_project_head_id", None)
            != self.successor_destination_project_head.project_head_id
            or tuple(getattr(manifest, "recovery_target_result_sha256", ()))
            != self.target_result_sha256
        ):
            raise ValueError("successor recovery seed authority is not closed")
        payload = self.model_dump(mode="json", exclude={"content_sha256"})
        if _sha256(payload) != self.content_sha256:
            raise ValueError("successor recovery seed authority digest is invalid")
        return self


class ScienceSuccessorRecoveryTargetPreflightV1(_RecoveryModel):
    """Deterministic target identity checked before its durable intent exists."""

    successor_recovery_target_preflight_contract_version: Literal["1"] = "1"
    operation_id: str = Field(pattern=_ID_PATTERN)
    idempotency_key: str = Field(pattern=_ID_PATTERN)
    recovery_id: str = Field(pattern=_ID_PATTERN)
    source_authority_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_identity: RecoveryExecutionIdentityV1
    target: ScienceSuccessorMethodPlanV2


class ScienceSuccessorRecoveryTargetReadinessV1(_RecoveryModel):
    """Typed no-model gate for the exact target operation and service generation."""

    successor_recovery_target_readiness_contract_version: Literal["1"] = "1"
    operation_id: str = Field(pattern=_ID_PATTERN)
    recovery_id: str = Field(pattern=_ID_PATTERN)
    target_id: str = Field(pattern=_ID_PATTERN)
    execution_identity: RecoveryExecutionIdentityV1
    framework_lock_digest: str = Field(pattern=_SHA256_PATTERN)
    runtime_identity_digest: str = Field(pattern=_SHA256_PATTERN)
    managed_reflector_credential_mount: ManagedReflectorCredentialMountReadiness
    managed_reflector_runtime: ManagedReflectorRuntimeReadiness
    checked_at: str = Field(pattern=_TIMESTAMP_PATTERN)

    @model_validator(mode="after")
    def _exact_managed_mount_identity(self) -> ScienceSuccessorRecoveryTargetReadinessV1:
        identity = self.execution_identity
        mount = self.managed_reflector_credential_mount
        runtime = self.managed_reflector_runtime
        if (
            mount.generation_digest != identity.core_generation
            or mount.release_install_digest != identity.release_install_digest
            or mount.release_registry_digest != identity.executable_registry_digest
            or self.framework_lock_digest != identity.framework_lock_digest
            or self.runtime_identity_digest != identity.runtime_identity_digest
            or mount.runtime_profile != identity.runtime_profile
            or mount.runtime_digest != identity.runtime_image_digest
            or mount.codex_cli_started is not False
            or mount.model_started is not False
            or runtime.authority_id != mount.authority_id
            or runtime.worker_launch_id != mount.worker_launch_id
            or runtime.credential_mount_content_sha256 != mount.content_sha256
            or runtime.generation_digest != identity.core_generation
            or runtime.release_install_digest != identity.release_install_digest
            or runtime.release_registry_digest != identity.executable_registry_digest
            or runtime.runtime_profile != identity.runtime_profile
            or runtime.runtime_digest != identity.runtime_image_digest
            or runtime.codex_cli_started is not True
            or runtime.model_started is not False
        ):
            raise ValueError(
                "managed reflector runtime differs from recovery execution identity"
            )
        return self


class ScienceSuccessorRecoveryTargetOperationV1(_RecoveryModel):
    intent: ScienceSuccessorRecoveryTargetIntentV1
    state: Literal["running", "succeeded", "failed"]
    result: ScienceSuccessorRecoveryTargetResultV1 | None = None
    failure: ScienceSuccessorRecoveryTargetFailureV1 | None = None

    @model_validator(mode="after")
    def _terminal_payload(self) -> ScienceSuccessorRecoveryTargetOperationV1:
        if self.state == "running" and (self.result is not None or self.failure is not None):
            raise ValueError("running target operation carries terminal evidence")
        if self.state == "succeeded" and (self.result is None or self.failure is not None):
            raise ValueError("succeeded target operation evidence is incomplete")
        if self.state == "failed" and (self.result is not None or self.failure is None):
            raise ValueError("failed target operation evidence is incomplete")
        return self


class ScienceSuccessorRecoveryAuthorityReaderV1(Protocol):
    """Fresh Core readback used to prove the source is still immutable."""

    def read_exhausted_successor_authority(
        self,
        expected: ExhaustedSuccessorRecoveryAuthorityV1,
        plan: ScienceSuccessorPlanV2,
    ) -> ExhaustedSuccessorRecoveryAuthorityV1: ...


class ScienceSuccessorRecoveryTargetExecutorV1(Protocol):
    """Side-effect port implemented by the verified successor preparer."""

    def check_target_readiness(
        self,
        preflight: ScienceSuccessorRecoveryTargetPreflightV1,
        *,
        source: ExhaustedSuccessorRecoveryAuthorityV1,
        plan: ScienceSuccessorPlanV2,
    ) -> ScienceSuccessorRecoveryTargetReadinessV1: ...

    def release_target_readiness(
        self,
        preflight: ScienceSuccessorRecoveryTargetPreflightV1,
    ) -> None: ...

    def attest_recovery_supersession(
        self,
        *,
        prior: ScienceSuccessorRecoverySnapshotV1,
        failure: ScienceSuccessorRecoveryTargetFailureV1,
        source: ExhaustedSuccessorRecoveryAuthorityV1,
        plan: ScienceSuccessorPlanV2,
        execution_identity: RecoveryExecutionIdentityV1,
        completed_operations: tuple[
            ScienceSuccessorRecoveryTargetOperationV1, ...
        ] = (),
    ) -> ScienceSuccessorRecoverySupersessionAuthorityV1: ...

    def execute_target(
        self,
        intent: ScienceSuccessorRecoveryTargetIntentV1,
        *,
        source: ExhaustedSuccessorRecoveryAuthorityV1,
        plan: ScienceSuccessorPlanV2,
    ) -> ScienceSuccessorRecoveryTargetResultV1: ...

    def recover_target(
        self,
        intent: ScienceSuccessorRecoveryTargetIntentV1,
        *,
        source: ExhaustedSuccessorRecoveryAuthorityV1,
        plan: ScienceSuccessorPlanV2,
    ) -> (
        ScienceSuccessorRecoveryTargetResultV1 | ScienceSuccessorRecoveryTargetFailureV1 | None
    ): ...

    def adopt_target(
        self,
        intent: ScienceSuccessorRecoveryTargetIntentV1,
        *,
        source: ExhaustedSuccessorRecoveryAuthorityV1,
        plan: ScienceSuccessorPlanV2,
        adopted_job: ScienceSuccessorRecoveryAdoptedJobAuthorityV1,
    ) -> ScienceSuccessorRecoveryTargetResultV1: ...


def _replace_target(
    record: ScienceSuccessorRecoveryRecordV1,
    target_id: str,
    replacement: ScienceSuccessorRecoveryTargetStateV1,
    *,
    state: Literal[
        "awaiting_target_authorization",
        "target_running",
        "all_targets_completed_awaiting_commit",
        "failed",
    ],
    now: str,
) -> ScienceSuccessorRecoveryRecordV1:
    targets = tuple(
        replacement if item.target_id == target_id else item for item in record.targets
    )
    completed = tuple(item.target_id for item in targets if item.state == "succeeded")
    remaining = tuple(item.target_id for item in targets if item.state == "pending")
    running = tuple(item.target_id for item in targets if item.state == "running")
    return ScienceSuccessorRecoveryRecordV1.model_validate(
        record.model_dump(mode="python")
        | {
            "state": state,
            "active_target_id": running[0] if running else None,
            "completed_target_ids": completed,
            "remaining_target_ids": remaining,
            "targets": targets,
            "version": record.version + 1,
            "updated_at": now,
        }
    )


def _recovery_target_operation_id(
    *,
    recovery_id: str,
    target_id: str,
    idempotency_key: str,
    source_authority_sha256: str,
    execution_identity: RecoveryExecutionIdentityV1,
) -> str:
    operation_seed = _canonical_bytes(
        {
            "recovery_id": recovery_id,
            "target_id": target_id,
            "idempotency_key": idempotency_key,
            "source_authority_sha256": source_authority_sha256,
            "execution_identity": execution_identity.model_dump(mode="json"),
        }
    )
    return f"recovery-target-{hashlib.sha256(operation_seed).hexdigest()[:32]}"


class ScienceSuccessorRecoveryStoreV1:
    """SQLite-backed immutable source and at-most-once target-operation ledger."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("recovery store root must be an absolute Path")
        self.root = root.resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.path = self.root / "science-successor-recovery-v1.sqlite3"
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS successor_recoveries (
                        recovery_id TEXT PRIMARY KEY,
                        source_transition_id TEXT NOT NULL,
                        create_idempotency_key TEXT NOT NULL UNIQUE,
                        request_sha256 TEXT NOT NULL,
                        request_json BLOB NOT NULL,
                        record_sha256 TEXT NOT NULL,
                        record_json BLOB NOT NULL,
                        version INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS successor_recovery_target_operations (
                        recovery_id TEXT NOT NULL,
                        target_id TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        operation_id TEXT NOT NULL UNIQUE,
                        intent_sha256 TEXT NOT NULL,
                        intent_json BLOB NOT NULL,
                        state TEXT NOT NULL,
                        result_json BLOB,
                        failure_json BLOB,
                        PRIMARY KEY (recovery_id, target_id),
                        FOREIGN KEY (recovery_id)
                            REFERENCES successor_recoveries(recovery_id)
                    );
                    CREATE TABLE IF NOT EXISTS historical_successor_source_resolutions (
                        source_transition_id TEXT NOT NULL,
                        failed_job_id TEXT NOT NULL,
                        resolution_idempotency_key TEXT NOT NULL UNIQUE,
                        attestation_id TEXT NOT NULL UNIQUE,
                        request_sha256 TEXT NOT NULL,
                        request_json BLOB NOT NULL,
                        response_sha256 TEXT NOT NULL,
                        response_json BLOB NOT NULL,
                        PRIMARY KEY (source_transition_id, failed_job_id)
                    );
                    """
                )
                self._migrate_closed_source_uniqueness(connection)
            finally:
                connection.close()
        os.chmod(self.path, 0o600)

    @staticmethod
    def _migrate_closed_source_uniqueness(connection: sqlite3.Connection) -> None:
        """Permit append-only supersessions while preserving every old blob.

        The original v1 table used a table-level UNIQUE constraint on source
        transition.  SQLite cannot drop that constraint in place.  Rebuild the
        parent table transactionally with byte-for-byte copied request/record
        blobs; target-operation rows and their foreign-key identities remain
        unchanged.
        """

        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'successor_recoveries'"
        ).fetchone()
        sql = "" if row is None else str(row["sql"] or "")
        if "source_transition_id TEXT NOT NULL UNIQUE" not in sql:
            return
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE successor_recoveries_migrating (
                    recovery_id TEXT PRIMARY KEY,
                    source_transition_id TEXT NOT NULL,
                    create_idempotency_key TEXT NOT NULL UNIQUE,
                    request_sha256 TEXT NOT NULL,
                    request_json BLOB NOT NULL,
                    record_sha256 TEXT NOT NULL,
                    record_json BLOB NOT NULL,
                    version INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO successor_recoveries_migrating
                SELECT recovery_id, source_transition_id, create_idempotency_key,
                       request_sha256, request_json, record_sha256, record_json,
                       version
                FROM successor_recoveries
                """
            )
            connection.execute("DROP TABLE successor_recoveries")
            connection.execute(
                "ALTER TABLE successor_recoveries_migrating "
                "RENAME TO successor_recoveries"
            )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ScienceSuccessorRecoveryV1Error(
                "recovery store foreign keys changed during supersession migration"
            )

    @staticmethod
    def _decode_model(model_type, raw: bytes):
        if len(raw) > _MAX_RECORD_BYTES:
            raise ScienceSuccessorRecoveryV1Error("persisted recovery row exceeds its bound")
        try:
            return model_type.model_validate_json(raw)
        except Exception as exc:
            raise ScienceSuccessorRecoveryV1Error(
                "persisted recovery row is not valid closed evidence"
            ) from exc

    @staticmethod
    def _load_bounded_blob(
        connection: sqlite3.Connection,
        *,
        table: str,
        column: str,
        where: str,
        values: tuple[object, ...],
    ) -> bytes:
        allowed = {
            ("successor_recoveries", "request_json", "recovery_id = ?"),
            ("successor_recoveries", "record_json", "recovery_id = ?"),
            (
                "successor_recovery_target_operations",
                "intent_json",
                "recovery_id = ? AND target_id = ?",
            ),
            (
                "successor_recovery_target_operations",
                "result_json",
                "recovery_id = ? AND target_id = ?",
            ),
            (
                "successor_recovery_target_operations",
                "failure_json",
                "recovery_id = ? AND target_id = ?",
            ),
            (
                "historical_successor_source_resolutions",
                "request_json",
                "source_transition_id = ? AND failed_job_id = ?",
            ),
            (
                "historical_successor_source_resolutions",
                "response_json",
                "source_transition_id = ? AND failed_job_id = ?",
            ),
        }
        if (table, column, where) not in allowed:
            raise AssertionError("unapproved recovery store query")
        length_row = connection.execute(
            f"SELECT length(CAST({column} AS BLOB)) AS value_length FROM {table} WHERE {where}",
            values,
        ).fetchone()
        if length_row is None or length_row["value_length"] is None:
            raise ScienceSuccessorRecoveryV1Error("recovery evidence was not found")
        length = int(length_row["value_length"])
        if not 0 < length <= _MAX_RECORD_BYTES:
            raise ScienceSuccessorRecoveryV1Error("persisted recovery row exceeds its bound")
        row = connection.execute(
            f"SELECT CASE WHEN length(CAST({column} AS BLOB)) = ? "
            f"THEN {column} ELSE NULL END AS value FROM {table} WHERE {where}",
            (length, *values),
        ).fetchone()
        if row is None or row["value"] is None:
            raise ScienceSuccessorRecoveryV1Error("recovery evidence changed during readback")
        raw = bytes(row["value"])
        if len(raw) != length:
            raise ScienceSuccessorRecoveryV1Error("recovery evidence length changed")
        return raw

    def seal_historical_source_resolution(
        self,
        request: ScienceSuccessorRecoverySourceResolveRequestV1,
        response: ScienceSuccessorRecoverySourceResponseV1,
    ) -> ScienceSuccessorRecoverySourceResponseV1:
        """Append one unique legacy resolution after current-Core reproduction."""

        request = ScienceSuccessorRecoverySourceResolveRequestV1.model_validate(
            request.model_dump(mode="python")
        )
        response = ScienceSuccessorRecoverySourceResponseV1.model_validate(
            response.model_dump(mode="python")
        )
        provenance = response.source.source_provenance
        if (
            response.source.successor_transition_id != request.successor_transition_id
            or (
                response.source.failed_job is not None
                and response.source.failed_job.job_id != request.failed_job_id
            )
            or (
                response.source.failed_pre_job_operation is not None
                and response.source.failed_pre_job_operation.operation_id
                != request.failed_pre_job_operation_id
            )
            or provenance.attestation_id
            != request.historical_attestation.attestation_id
            or provenance.attestation_sha256
            != request.historical_attestation.content_sha256
        ):
            raise ScienceSuccessorRecoveryV1Error(
                "historical source resolution differs from its attestation"
            )
        request_bytes = _canonical_bytes(request)
        response_bytes = _canonical_bytes(response)
        request_sha256 = hashlib.sha256(request_bytes).hexdigest()
        response_sha256 = hashlib.sha256(response_bytes).hexdigest()
        key = (
            request.successor_transition_id,
            request.failed_job_id or request.failed_pre_job_operation_id,
        )
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT resolution_idempotency_key, attestation_id, "
                    "request_sha256, response_sha256 FROM "
                    "historical_successor_source_resolutions WHERE "
                    "(source_transition_id = ? AND failed_job_id = ?) OR "
                    "resolution_idempotency_key = ? OR attestation_id = ?",
                    (
                        *key,
                        request.resolution_idempotency_key,
                        request.historical_attestation.attestation_id,
                    ),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["resolution_idempotency_key"]
                        != request.resolution_idempotency_key
                        or existing["attestation_id"]
                        != request.historical_attestation.attestation_id
                        or existing["request_sha256"] != request_sha256
                        or existing["response_sha256"] != response_sha256
                    ):
                        raise ScienceSuccessorRecoveryV1Error(
                            "historical source already owns another resolution"
                        )
                    stored = self._load_bounded_blob(
                        connection,
                        table="historical_successor_source_resolutions",
                        column="response_json",
                        where="source_transition_id = ? AND failed_job_id = ?",
                        values=key,
                    )
                    if hashlib.sha256(stored).hexdigest() != response_sha256:
                        raise ScienceSuccessorRecoveryV1Error(
                            "historical source resolution digest is invalid"
                        )
                    connection.execute("COMMIT")
                    return self._decode_model(
                        ScienceSuccessorRecoverySourceResponseV1,
                        stored,
                    )
                connection.execute(
                    "INSERT INTO historical_successor_source_resolutions "
                    "(source_transition_id, failed_job_id, "
                    "resolution_idempotency_key, attestation_id, request_sha256, "
                    "request_json, response_sha256, response_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        *key,
                        request.resolution_idempotency_key,
                        request.historical_attestation.attestation_id,
                        request_sha256,
                        request_bytes,
                        response_sha256,
                        response_bytes,
                    ),
                )
                connection.execute("COMMIT")
                return response
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    def verify_historical_source_resolution(
        self,
        response: ScienceSuccessorRecoverySourceResponseV1,
    ) -> None:
        """Require exact durable provenance before any recovery readback."""

        response = ScienceSuccessorRecoverySourceResponseV1.model_validate(
            response.model_dump(mode="python")
        )
        key = (
            response.source.successor_transition_id,
            science_successor_recovery_failure_id(response.source),
        )
        expected = _canonical_bytes(response)
        expected_sha256 = hashlib.sha256(expected).hexdigest()
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN")
                row = connection.execute(
                    "SELECT response_sha256, attestation_id FROM "
                    "historical_successor_source_resolutions WHERE "
                    "source_transition_id = ? AND failed_job_id = ?",
                    key,
                ).fetchone()
                if (
                    row is None
                    or row["response_sha256"] != expected_sha256
                    or row["attestation_id"]
                    != response.source.source_provenance.attestation_id
                ):
                    raise ScienceSuccessorRecoveryV1Error(
                        "historical source lacks its sealed resolution"
                    )
                stored = self._load_bounded_blob(
                    connection,
                    table="historical_successor_source_resolutions",
                    column="response_json",
                    where="source_transition_id = ? AND failed_job_id = ?",
                    values=key,
                )
                if stored != expected:
                    raise ScienceSuccessorRecoveryV1Error(
                        "historical source resolution changed"
                    )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    def create_recovery(
        self,
        request: ScienceSuccessorRecoveryCreateRequestV1,
        *,
        supersession_authority: (
            ScienceSuccessorRecoverySupersessionAuthorityV1 | None
        ) = None,
        now: str,
    ) -> ScienceSuccessorRecoverySnapshotV1:
        request = ScienceSuccessorRecoveryCreateRequestV1.model_validate(
            request.model_dump(mode="python")
        )
        request_bytes = _canonical_bytes(request)
        request_sha = hashlib.sha256(request_bytes).hexdigest()
        recovery_id = f"recovery-{request_sha[:32]}"
        targets_by_id = {item.target_id: item for item in request.plan.enabled_methods}
        carried_by_target = {
            item.prior_result.target_id: item
            for item in (
                ()
                if supersession_authority is None
                else supersession_authority.carried_completed_targets
            )
        }
        targets = tuple(
            ScienceSuccessorRecoveryTargetStateV1(
                target_id=target_id,
                method_id=targets_by_id[target_id].method_id,
                artifact_type=targets_by_id[target_id].output_artifact_type,
                state=("succeeded" if target_id in carried_by_target else "pending"),
                operation_id=(
                    carried_by_target[target_id].prior_intent.operation_id
                    if target_id in carried_by_target
                    else None
                ),
                result_sha256=(
                    carried_by_target[target_id].prior_result_sha256
                    if target_id in carried_by_target
                    else None
                ),
            )
            for target_id in request.target_authorization_order
        )
        completed_target_ids = tuple(
            item.target_id for item in targets if item.state == "succeeded"
        )
        remaining_target_ids = tuple(
            item.target_id for item in targets if item.state == "pending"
        )
        record = ScienceSuccessorRecoveryRecordV1(
            recovery_id=recovery_id,
            source_transition_id=request.source.successor_transition_id,
            source_failed_job_id=science_successor_recovery_failure_id(request.source),
            source_failure_kind=(
                "evolution_job"
                if request.source.failed_job is not None
                else "pre_job_operation"
            ),
            source_authority_sha256=science_successor_recovery_source_sha256(request.source),
            source_transition_state_sha256=request.source.successor_transition_state_sha256,
            plan_sha256=request.source.plan_sha256,
            execution_identity=request.execution_identity,
            supersedes_recovery_id=request.supersedes_recovery_id,
            supersession_authority=supersession_authority,
            target_authorization_order=request.target_authorization_order,
            state=(
                "awaiting_target_authorization"
                if remaining_target_ids
                else "all_targets_completed_awaiting_commit"
            ),
            completed_target_ids=completed_target_ids,
            remaining_target_ids=remaining_target_ids,
            targets=targets,
            version=1,
            created_at=now,
            updated_at=now,
        )
        record_bytes = _canonical_bytes(record)
        record_sha = hashlib.sha256(record_bytes).hexdigest()
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT recovery_id, request_sha256 FROM successor_recoveries "
                    "WHERE create_idempotency_key = ?",
                    (request.idempotency_key,),
                ).fetchone()
                if existing is not None:
                    existing_id = str(existing["recovery_id"])
                    if existing_id == recovery_id and existing["request_sha256"] == request_sha:
                        connection.execute("COMMIT")
                        return self.get_recovery(existing_id)
                    raise ScienceSuccessorRecoveryV1Error(
                        "recovery idempotency key already owns another request"
                    )
                source_rows = connection.execute(
                    "SELECT recovery_id, record_sha256, record_json FROM "
                    "successor_recoveries WHERE source_transition_id = ? "
                    "ORDER BY rowid",
                    (request.source.successor_transition_id,),
                ).fetchall()
                if request.supersedes_recovery_id is None:
                    if source_rows or supersession_authority is not None:
                        raise ScienceSuccessorRecoveryV1Error(
                            "source successor already owns a different recovery"
                        )
                else:
                    prior_row = next(
                        (
                            row
                            for row in source_rows
                            if row["recovery_id"] == request.supersedes_recovery_id
                        ),
                        None,
                    )
                    prior_records = tuple(
                        self._decode_model(
                            ScienceSuccessorRecoveryRecordV1,
                            bytes(row["record_json"]),
                        )
                        for row in source_rows
                    )
                    if (
                        prior_row is None
                        or supersession_authority is None
                        or any(
                            item.supersedes_recovery_id
                            == request.supersedes_recovery_id
                            for item in prior_records
                        )
                    ):
                        raise ScienceSuccessorRecoveryV1Error(
                            "recovery supersession lineage is missing or already consumed"
                        )
                    prior_record = self._decode_model(
                        ScienceSuccessorRecoveryRecordV1,
                        bytes(prior_row["record_json"]),
                    )
                    failed_targets = tuple(
                        item for item in prior_record.targets if item.state == "failed"
                    )
                    failure_row = connection.execute(
                        "SELECT operation_id, failure_json FROM "
                        "successor_recovery_target_operations WHERE recovery_id = ? "
                        "AND state = 'failed'",
                        (prior_record.recovery_id,),
                    ).fetchone()
                    failure = (
                        None
                        if failure_row is None
                        else self._decode_model(
                            ScienceSuccessorRecoveryTargetFailureV1,
                            bytes(failure_row["failure_json"]),
                        )
                    )
                    if (
                        prior_record.state != "failed"
                        or len(failed_targets) != 1
                        or failure is None
                        or prior_record.execution_identity.core_generation
                        == request.execution_identity.core_generation
                        or prior_record.execution_identity.daemon_release_identity
                        == request.execution_identity.daemon_release_identity
                        or supersession_authority.prior_recovery_id
                        != prior_record.recovery_id
                        or supersession_authority.prior_recovery_record_sha256
                        != prior_row["record_sha256"]
                        or supersession_authority.prior_operation_id
                        != failure.operation_id
                        or supersession_authority.prior_failure_sha256
                        != _sha256(failure)
                        or supersession_authority.source_transition_id
                        != request.source.successor_transition_id
                        or supersession_authority.core_generation
                        != request.execution_identity.core_generation
                        or supersession_authority.daemon_release_identity
                        != request.execution_identity.daemon_release_identity
                    ):
                        raise ScienceSuccessorRecoveryV1Error(
                            "recovery supersession authority differs from terminal evidence"
                        )
                    prior_completed = tuple(
                        item for item in prior_record.targets if item.state == "succeeded"
                    )
                    carried = supersession_authority.carried_completed_targets
                    if (
                        tuple(item.target_id for item in prior_completed)
                        != tuple(item.prior_result.target_id for item in carried)
                    ):
                        raise ScienceSuccessorRecoveryV1Error(
                            "recovery supersession omitted completed target authority"
                        )
                    for prior_target, authority in zip(
                        prior_completed,
                        carried,
                        strict=True,
                    ):
                        prior_operation = self._operation_in_transaction(
                            connection,
                            prior_record.recovery_id,
                            prior_target.target_id,
                        )
                        if (
                            prior_operation.state != "succeeded"
                            or prior_operation.result is None
                            or prior_operation.intent != authority.prior_intent
                            or prior_operation.result != authority.prior_result
                            or prior_target.operation_id
                            != authority.prior_intent.operation_id
                            or prior_target.result_sha256
                            != authority.prior_result_sha256
                        ):
                            raise ScienceSuccessorRecoveryV1Error(
                                "carried recovery target differs from prior sealed evidence"
                            )
                connection.execute(
                    "INSERT INTO successor_recoveries "
                    "(recovery_id, source_transition_id, create_idempotency_key, "
                    "request_sha256, request_json, record_sha256, record_json, version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        recovery_id,
                        request.source.successor_transition_id,
                        request.idempotency_key,
                        request_sha,
                        request_bytes,
                        record_sha,
                        record_bytes,
                        record.version,
                    ),
                )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
        return ScienceSuccessorRecoverySnapshotV1(record=record, record_sha256=record_sha)

    def get_recovery(self, recovery_id: str) -> ScienceSuccessorRecoverySnapshotV1:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN")
                row = connection.execute(
                    "SELECT record_sha256 FROM successor_recoveries WHERE recovery_id = ?",
                    (recovery_id,),
                ).fetchone()
                if row is None:
                    raise ScienceSuccessorRecoveryV1Error("successor recovery was not found")
                raw = self._load_bounded_blob(
                    connection,
                    table="successor_recoveries",
                    column="record_json",
                    where="recovery_id = ?",
                    values=(recovery_id,),
                )
                digest = hashlib.sha256(raw).hexdigest()
                if digest != row["record_sha256"]:
                    raise ScienceSuccessorRecoveryV1Error("persisted recovery digest is invalid")
                record = self._decode_model(ScienceSuccessorRecoveryRecordV1, raw)
                result = ScienceSuccessorRecoverySnapshotV1(
                    record=record,
                    record_sha256=digest,
                )
                connection.execute("COMMIT")
                return result
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    def get_create_request(
        self,
        recovery_id: str,
    ) -> ScienceSuccessorRecoveryCreateRequestV1:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN")
                row = connection.execute(
                    "SELECT request_sha256 FROM successor_recoveries WHERE recovery_id = ?",
                    (recovery_id,),
                ).fetchone()
                if row is None:
                    raise ScienceSuccessorRecoveryV1Error("successor recovery was not found")
                raw = self._load_bounded_blob(
                    connection,
                    table="successor_recoveries",
                    column="request_json",
                    where="recovery_id = ?",
                    values=(recovery_id,),
                )
                if hashlib.sha256(raw).hexdigest() != row["request_sha256"]:
                    raise ScienceSuccessorRecoveryV1Error("persisted recovery request was changed")
                request = self._decode_model(ScienceSuccessorRecoveryCreateRequestV1, raw)
                if f"recovery-{hashlib.sha256(raw).hexdigest()[:32]}" != recovery_id:
                    raise ScienceSuccessorRecoveryV1Error(
                        "persisted recovery request does not bind the recovery ID"
                    )
                connection.execute("COMMIT")
                return request
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    def begin_target(
        self,
        *,
        recovery_id: str,
        target_id: str,
        idempotency_key: str,
        now: str,
    ) -> tuple[ScienceSuccessorRecoveryTargetIntentV1, bool]:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT idempotency_key, intent_sha256 FROM "
                    "successor_recovery_target_operations "
                    "WHERE recovery_id = ? AND target_id = ?",
                    (recovery_id, target_id),
                ).fetchone()
                if existing is not None:
                    raw = self._load_bounded_blob(
                        connection,
                        table="successor_recovery_target_operations",
                        column="intent_json",
                        where="recovery_id = ? AND target_id = ?",
                        values=(recovery_id, target_id),
                    )
                    if (
                        existing["idempotency_key"] != idempotency_key
                        or hashlib.sha256(raw).hexdigest() != existing["intent_sha256"]
                    ):
                        raise ScienceSuccessorRecoveryV1Error(
                            "recovery target already owns a different operation"
                        )
                    intent = self._decode_model(ScienceSuccessorRecoveryTargetIntentV1, raw)
                    connection.execute("COMMIT")
                    return intent, False

                snapshot = self._snapshot_in_transaction(connection, recovery_id)
                record = snapshot.record
                if record.state == "target_running":
                    raise ScienceSuccessorRecoveryV1Error(
                        "another recovery target is already running"
                    )
                if record.state != "awaiting_target_authorization":
                    raise ScienceSuccessorRecoveryV1Error(
                        "recovery is not awaiting target authorization"
                    )
                expected_target = record.target_authorization_order[
                    len(record.completed_target_ids)
                ]
                if target_id != expected_target:
                    raise ScienceSuccessorRecoveryV1Error(
                        "target violates the explicit recovery authorization order"
                    )
                target_state = next(item for item in record.targets if item.target_id == target_id)
                target = ScienceSuccessorMethodPlanV2(
                    target_id=target_state.target_id,
                    method_id=target_state.method_id,
                    output_artifact_type=target_state.artifact_type,
                )
                operation_id = _recovery_target_operation_id(
                    recovery_id=recovery_id,
                    target_id=target_id,
                    idempotency_key=idempotency_key,
                    source_authority_sha256=record.source_authority_sha256,
                    execution_identity=record.execution_identity,
                )
                intent = ScienceSuccessorRecoveryTargetIntentV1(
                    operation_id=operation_id,
                    idempotency_key=idempotency_key,
                    recovery_id=recovery_id,
                    source_authority_sha256=record.source_authority_sha256,
                    execution_identity=record.execution_identity,
                    target=target,
                    state="running",
                    created_at=now,
                )
                intent_bytes = _canonical_bytes(intent)
                running_target = target_state.model_copy(
                    update={"state": "running", "operation_id": operation_id}
                )
                updated = _replace_target(
                    record,
                    target_id,
                    running_target,
                    state="target_running",
                    now=now,
                )
                self._update_record(connection, snapshot, updated)
                connection.execute(
                    "INSERT INTO successor_recovery_target_operations "
                    "(recovery_id, target_id, idempotency_key, operation_id, "
                    "intent_sha256, intent_json, state, result_json, failure_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'running', NULL, NULL)",
                    (
                        recovery_id,
                        target_id,
                        idempotency_key,
                        operation_id,
                        hashlib.sha256(intent_bytes).hexdigest(),
                        intent_bytes,
                    ),
                )
                connection.execute("COMMIT")
                return intent, True
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    def prepare_target_preflight(
        self,
        *,
        recovery_id: str,
        target_id: str,
        idempotency_key: str,
    ) -> ScienceSuccessorRecoveryTargetPreflightV1:
        """Return a deterministic no-write identity for the next target gate."""

        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN")
                snapshot = self._snapshot_in_transaction(connection, recovery_id)
                record = snapshot.record
                existing = connection.execute(
                    "SELECT idempotency_key, intent_sha256 FROM "
                    "successor_recovery_target_operations "
                    "WHERE recovery_id = ? AND target_id = ?",
                    (recovery_id, target_id),
                ).fetchone()
                if existing is not None:
                    raw = self._load_bounded_blob(
                        connection,
                        table="successor_recovery_target_operations",
                        column="intent_json",
                        where="recovery_id = ? AND target_id = ?",
                        values=(recovery_id, target_id),
                    )
                    if (
                        existing["idempotency_key"] != idempotency_key
                        or hashlib.sha256(raw).hexdigest() != existing["intent_sha256"]
                    ):
                        raise ScienceSuccessorRecoveryV1Error(
                            "recovery target already owns a different operation"
                        )
                    intent = self._decode_model(
                        ScienceSuccessorRecoveryTargetIntentV1,
                        raw,
                    )
                    preflight = ScienceSuccessorRecoveryTargetPreflightV1(
                        operation_id=intent.operation_id,
                        idempotency_key=intent.idempotency_key,
                        recovery_id=intent.recovery_id,
                        source_authority_sha256=intent.source_authority_sha256,
                        execution_identity=intent.execution_identity,
                        target=intent.target,
                    )
                    connection.execute("COMMIT")
                    return preflight
                if record.state == "target_running":
                    raise ScienceSuccessorRecoveryV1Error(
                        "another recovery target is already running"
                    )
                if record.state != "awaiting_target_authorization":
                    raise ScienceSuccessorRecoveryV1Error(
                        "recovery is not awaiting target authorization"
                    )
                expected_target = record.target_authorization_order[
                    len(record.completed_target_ids)
                ]
                if target_id != expected_target:
                    raise ScienceSuccessorRecoveryV1Error(
                        "target violates the explicit recovery authorization order"
                    )
                target_state = next(item for item in record.targets if item.target_id == target_id)
                preflight = ScienceSuccessorRecoveryTargetPreflightV1(
                    operation_id=_recovery_target_operation_id(
                        recovery_id=recovery_id,
                        target_id=target_id,
                        idempotency_key=idempotency_key,
                        source_authority_sha256=record.source_authority_sha256,
                        execution_identity=record.execution_identity,
                    ),
                    idempotency_key=idempotency_key,
                    recovery_id=recovery_id,
                    source_authority_sha256=record.source_authority_sha256,
                    execution_identity=record.execution_identity,
                    target=ScienceSuccessorMethodPlanV2(
                        target_id=target_state.target_id,
                        method_id=target_state.method_id,
                        output_artifact_type=target_state.artifact_type,
                    ),
                )
                connection.execute("COMMIT")
                return preflight
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    def get_target_operation(
        self,
        recovery_id: str,
        target_id: str,
    ) -> ScienceSuccessorRecoveryTargetOperationV1:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN")
                row = connection.execute(
                    "SELECT state, intent_sha256, result_json IS NOT NULL AS has_result, "
                    "failure_json IS NOT NULL AS has_failure "
                    "FROM successor_recovery_target_operations "
                    "WHERE recovery_id = ? AND target_id = ?",
                    (recovery_id, target_id),
                ).fetchone()
                if row is None:
                    record = self._snapshot_in_transaction(
                        connection,
                        recovery_id,
                    ).record
                    carried = (
                        ()
                        if record.supersession_authority is None
                        else record.supersession_authority.carried_completed_targets
                    )
                    authority = next(
                        (
                            item
                            for item in carried
                            if item.prior_result.target_id == target_id
                        ),
                        None,
                    )
                    if authority is None:
                        raise ScienceSuccessorRecoveryV1Error(
                            "recovery target operation was not found"
                        )
                    operation = ScienceSuccessorRecoveryTargetOperationV1(
                        intent=authority.prior_intent,
                        state="succeeded",
                        result=authority.prior_result,
                    )
                    self._verify_operation_record_binding(record, operation)
                    connection.execute("COMMIT")
                    return operation
                intent_raw = self._load_bounded_blob(
                    connection,
                    table="successor_recovery_target_operations",
                    column="intent_json",
                    where="recovery_id = ? AND target_id = ?",
                    values=(recovery_id, target_id),
                )
                if hashlib.sha256(intent_raw).hexdigest() != row["intent_sha256"]:
                    raise ScienceSuccessorRecoveryV1Error(
                        "persisted recovery target intent was changed"
                    )
                intent = self._decode_model(
                    ScienceSuccessorRecoveryTargetIntentV1,
                    intent_raw,
                )
                result = None
                failure = None
                if row["has_result"]:
                    result = self._decode_model(
                        ScienceSuccessorRecoveryTargetResultV1,
                        self._load_bounded_blob(
                            connection,
                            table="successor_recovery_target_operations",
                            column="result_json",
                            where="recovery_id = ? AND target_id = ?",
                            values=(recovery_id, target_id),
                        ),
                    )
                if row["has_failure"]:
                    failure = self._decode_model(
                        ScienceSuccessorRecoveryTargetFailureV1,
                        self._load_bounded_blob(
                            connection,
                            table="successor_recovery_target_operations",
                            column="failure_json",
                            where="recovery_id = ? AND target_id = ?",
                            values=(recovery_id, target_id),
                        ),
                    )
                operation = ScienceSuccessorRecoveryTargetOperationV1(
                    intent=intent,
                    state=str(row["state"]),
                    result=result,
                    failure=failure,
                )
                self._verify_operation_record_binding(
                    self._snapshot_in_transaction(connection, recovery_id).record,
                    operation,
                )
                connection.execute("COMMIT")
                return operation
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    def get_target_operation_or_none(
        self,
        recovery_id: str,
        target_id: str,
    ) -> ScienceSuccessorRecoveryTargetOperationV1 | None:
        with self._lock:
            connection = self._connect()
            try:
                exists = connection.execute(
                    "SELECT 1 FROM successor_recovery_target_operations "
                    "WHERE recovery_id = ? AND target_id = ?",
                    (recovery_id, target_id),
                ).fetchone()
            finally:
                connection.close()
        if exists is None:
            try:
                return self.get_target_operation(recovery_id, target_id)
            except ScienceSuccessorRecoveryV1Error as exc:
                if str(exc) != "recovery target operation was not found":
                    raise
                return None
        return self.get_target_operation(recovery_id, target_id)

    def complete_target(
        self,
        result: ScienceSuccessorRecoveryTargetResultV1,
    ) -> ScienceSuccessorRecoverySnapshotV1:
        result = ScienceSuccessorRecoveryTargetResultV1.model_validate(
            result.model_dump(mode="python")
        )
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                operation = self._operation_in_transaction(
                    connection,
                    result.recovery_id,
                    result.target_id,
                )
                if operation.state == "succeeded":
                    if operation.result != result:
                        raise ScienceSuccessorRecoveryV1Error(
                            "recovery target result conflicts with sealed evidence"
                        )
                    snapshot = self._snapshot_in_transaction(connection, result.recovery_id)
                    connection.execute("COMMIT")
                    return snapshot
                if (
                    operation.state != "running"
                    or operation.intent.operation_id != result.operation_id
                ):
                    raise ScienceSuccessorRecoveryV1Error(
                        "recovery target is not owned by this running operation"
                    )
                request = self._request_in_transaction(connection, result.recovery_id)
                if (
                    result.source_failed_job_id
                    != science_successor_recovery_failure_id(request.source)
                    or result.source_failure_kind
                    != (
                        "evolution_job"
                        if request.source.failed_job is not None
                        else "pre_job_operation"
                    )
                    or result.target_id != operation.intent.target.target_id
                    or result.method_id != operation.intent.target.method_id
                    or result.output.artifact_type != operation.intent.target.output_artifact_type
                ):
                    raise ScienceSuccessorRecoveryV1Error(
                        "recovery target result differs from its immutable intent"
                    )
                snapshot = self._snapshot_in_transaction(connection, result.recovery_id)
                result_bytes = _canonical_bytes(result)
                succeeded = next(
                    item for item in snapshot.record.targets if item.target_id == result.target_id
                ).model_copy(
                    update={
                        "state": "succeeded",
                        "operation_id": result.operation_id,
                        "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
                    }
                )
                remaining_count = sum(
                    item.state == "pending"
                    for item in snapshot.record.targets
                    if item.target_id != result.target_id
                )
                state = (
                    "awaiting_target_authorization"
                    if remaining_count
                    else "all_targets_completed_awaiting_commit"
                )
                updated = _replace_target(
                    snapshot.record,
                    result.target_id,
                    succeeded,
                    state=state,
                    now=result.completed_at,
                )
                self._update_record(connection, snapshot, updated)
                cursor = connection.execute(
                    "UPDATE successor_recovery_target_operations "
                    "SET state = 'succeeded', result_json = ? "
                    "WHERE recovery_id = ? AND target_id = ? AND state = 'running'",
                    (result_bytes, result.recovery_id, result.target_id),
                )
                if cursor.rowcount != 1:
                    raise ScienceSuccessorRecoveryV1Error(
                        "recovery target result lost its running operation fence"
                    )
                connection.execute("COMMIT")
                return ScienceSuccessorRecoverySnapshotV1(
                    record=updated,
                    record_sha256=_sha256(updated),
                )
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    def fail_target(
        self,
        failure: ScienceSuccessorRecoveryTargetFailureV1,
    ) -> ScienceSuccessorRecoverySnapshotV1:
        failure = ScienceSuccessorRecoveryTargetFailureV1.model_validate(
            failure.model_dump(mode="python")
        )
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                operation = self._operation_in_transaction(
                    connection,
                    failure.recovery_id,
                    failure.target_id,
                )
                if operation.state == "failed":
                    if operation.failure != failure:
                        raise ScienceSuccessorRecoveryV1Error(
                            "recovery target failure conflicts with sealed evidence"
                        )
                    snapshot = self._snapshot_in_transaction(connection, failure.recovery_id)
                    connection.execute("COMMIT")
                    return snapshot
                if (
                    operation.state != "running"
                    or operation.intent.operation_id != failure.operation_id
                ):
                    raise ScienceSuccessorRecoveryV1Error(
                        "recovery target failure is not owned by this operation"
                    )
                snapshot = self._snapshot_in_transaction(connection, failure.recovery_id)
                failure_bytes = _canonical_bytes(failure)
                failed = next(
                    item for item in snapshot.record.targets if item.target_id == failure.target_id
                ).model_copy(
                    update={
                        "state": "failed",
                        "operation_id": failure.operation_id,
                        "failure_sha256": hashlib.sha256(failure_bytes).hexdigest(),
                    }
                )
                updated = _replace_target(
                    snapshot.record,
                    failure.target_id,
                    failed,
                    state="failed",
                    now=failure.failed_at,
                )
                self._update_record(connection, snapshot, updated)
                cursor = connection.execute(
                    "UPDATE successor_recovery_target_operations "
                    "SET state = 'failed', failure_json = ? "
                    "WHERE recovery_id = ? AND target_id = ? AND state = 'running'",
                    (failure_bytes, failure.recovery_id, failure.target_id),
                )
                if cursor.rowcount != 1:
                    raise ScienceSuccessorRecoveryV1Error(
                        "recovery target failure lost its running operation fence"
                    )
                connection.execute("COMMIT")
                return ScienceSuccessorRecoverySnapshotV1(
                    record=updated,
                    record_sha256=_sha256(updated),
                )
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    def _snapshot_in_transaction(
        self,
        connection: sqlite3.Connection,
        recovery_id: str,
    ) -> ScienceSuccessorRecoverySnapshotV1:
        row = connection.execute(
            "SELECT record_sha256 FROM successor_recoveries WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone()
        if row is None:
            raise ScienceSuccessorRecoveryV1Error("successor recovery was not found")
        raw = self._load_bounded_blob(
            connection,
            table="successor_recoveries",
            column="record_json",
            where="recovery_id = ?",
            values=(recovery_id,),
        )
        digest = hashlib.sha256(raw).hexdigest()
        if digest != row["record_sha256"]:
            raise ScienceSuccessorRecoveryV1Error("persisted recovery digest is invalid")
        return ScienceSuccessorRecoverySnapshotV1(
            record=self._decode_model(ScienceSuccessorRecoveryRecordV1, raw),
            record_sha256=digest,
        )

    def _request_in_transaction(
        self,
        connection: sqlite3.Connection,
        recovery_id: str,
    ) -> ScienceSuccessorRecoveryCreateRequestV1:
        row = connection.execute(
            "SELECT request_sha256 FROM successor_recoveries WHERE recovery_id = ?",
            (recovery_id,),
        ).fetchone()
        if row is None:
            raise ScienceSuccessorRecoveryV1Error("successor recovery was not found")
        raw = self._load_bounded_blob(
            connection,
            table="successor_recoveries",
            column="request_json",
            where="recovery_id = ?",
            values=(recovery_id,),
        )
        if hashlib.sha256(raw).hexdigest() != row["request_sha256"]:
            raise ScienceSuccessorRecoveryV1Error("persisted recovery request was changed")
        return self._decode_model(ScienceSuccessorRecoveryCreateRequestV1, raw)

    def _operation_in_transaction(
        self,
        connection: sqlite3.Connection,
        recovery_id: str,
        target_id: str,
    ) -> ScienceSuccessorRecoveryTargetOperationV1:
        row = connection.execute(
            "SELECT state, intent_sha256, result_json IS NOT NULL AS has_result, "
            "failure_json IS NOT NULL AS has_failure "
            "FROM successor_recovery_target_operations "
            "WHERE recovery_id = ? AND target_id = ?",
            (recovery_id, target_id),
        ).fetchone()
        if row is None:
            record = self._snapshot_in_transaction(connection, recovery_id).record
            carried = (
                ()
                if record.supersession_authority is None
                else record.supersession_authority.carried_completed_targets
            )
            authority = next(
                (
                    item
                    for item in carried
                    if item.prior_result.target_id == target_id
                ),
                None,
            )
            if authority is None:
                raise ScienceSuccessorRecoveryV1Error(
                    "recovery target operation was not found"
                )
            operation = ScienceSuccessorRecoveryTargetOperationV1(
                intent=authority.prior_intent,
                state="succeeded",
                result=authority.prior_result,
            )
            self._verify_operation_record_binding(record, operation)
            return operation
        intent_raw = self._load_bounded_blob(
            connection,
            table="successor_recovery_target_operations",
            column="intent_json",
            where="recovery_id = ? AND target_id = ?",
            values=(recovery_id, target_id),
        )
        if hashlib.sha256(intent_raw).hexdigest() != row["intent_sha256"]:
            raise ScienceSuccessorRecoveryV1Error("persisted recovery target intent was changed")
        intent = self._decode_model(
            ScienceSuccessorRecoveryTargetIntentV1,
            intent_raw,
        )
        result = None
        failure = None
        if row["has_result"]:
            result = self._decode_model(
                ScienceSuccessorRecoveryTargetResultV1,
                self._load_bounded_blob(
                    connection,
                    table="successor_recovery_target_operations",
                    column="result_json",
                    where="recovery_id = ? AND target_id = ?",
                    values=(recovery_id, target_id),
                ),
            )
        if row["has_failure"]:
            failure = self._decode_model(
                ScienceSuccessorRecoveryTargetFailureV1,
                self._load_bounded_blob(
                    connection,
                    table="successor_recovery_target_operations",
                    column="failure_json",
                    where="recovery_id = ? AND target_id = ?",
                    values=(recovery_id, target_id),
                ),
            )
        operation = ScienceSuccessorRecoveryTargetOperationV1(
            intent=intent,
            state=str(row["state"]),
            result=result,
            failure=failure,
        )
        self._verify_operation_record_binding(
            self._snapshot_in_transaction(connection, recovery_id).record,
            operation,
        )
        return operation

    @staticmethod
    def _verify_operation_record_binding(
        record: ScienceSuccessorRecoveryRecordV1,
        operation: ScienceSuccessorRecoveryTargetOperationV1,
    ) -> None:
        target = next(
            (
                item
                for item in record.targets
                if item.target_id == operation.intent.target.target_id
            ),
            None,
        )
        if target is None or target.operation_id != operation.intent.operation_id:
            raise ScienceSuccessorRecoveryV1Error(
                "recovery target operation is not bound to the recovery record"
            )
        if operation.state == "running" and target.state != "running":
            raise ScienceSuccessorRecoveryV1Error(
                "running recovery operation differs from the recovery record"
            )
        if operation.state == "succeeded" and (
            target.state != "succeeded"
            or operation.result is None
            or target.result_sha256 != _sha256(operation.result)
        ):
            raise ScienceSuccessorRecoveryV1Error(
                "succeeded recovery operation differs from the recovery record"
            )
        if operation.state == "failed" and (
            target.state != "failed"
            or operation.failure is None
            or target.failure_sha256 != _sha256(operation.failure)
        ):
            raise ScienceSuccessorRecoveryV1Error(
                "failed recovery operation differs from the recovery record"
            )

    @staticmethod
    def _update_record(
        connection: sqlite3.Connection,
        previous: ScienceSuccessorRecoverySnapshotV1,
        updated: ScienceSuccessorRecoveryRecordV1,
    ) -> None:
        updated_bytes = _canonical_bytes(updated)
        updated_sha = hashlib.sha256(updated_bytes).hexdigest()
        cursor = connection.execute(
            "UPDATE successor_recoveries SET record_sha256 = ?, record_json = ?, "
            "version = ? WHERE recovery_id = ? AND version = ? AND record_sha256 = ?",
            (
                updated_sha,
                updated_bytes,
                updated.version,
                updated.recovery_id,
                previous.record.version,
                previous.record_sha256,
            ),
        )
        if cursor.rowcount != 1:
            raise ScienceSuccessorRecoveryV1Error("recovery record changed concurrently")


class ScienceSuccessorRecoveryCoordinatorV1:
    """Verify source authority and execute one explicit target authorization."""

    def __init__(
        self,
        *,
        store: ScienceSuccessorRecoveryStoreV1,
        authority_reader: ScienceSuccessorRecoveryAuthorityReaderV1,
        executor: ScienceSuccessorRecoveryTargetExecutorV1,
        clock: Callable[[], str],
    ) -> None:
        self.store = store
        self._authority_reader = authority_reader
        self._executor = executor
        self._clock = clock

    def _verify_source(
        self,
        expected: ExhaustedSuccessorRecoveryAuthorityV1,
        plan: ScienceSuccessorPlanV2,
    ) -> ExhaustedSuccessorRecoveryAuthorityV1:
        observed = self._authority_reader.read_exhausted_successor_authority(
            expected,
            plan,
        )
        if type(observed) is not ExhaustedSuccessorRecoveryAuthorityV1:
            raise ScienceSuccessorRecoveryV1Error(
                "Core returned an invalid recovery source authority type"
            )
        if observed != expected or science_successor_recovery_source_sha256(
            observed
        ) != science_successor_recovery_source_sha256(expected):
            raise ScienceSuccessorRecoveryV1Error(
                "Core recovery source authority changed or no longer matches"
            )
        return observed

    def create_recovery(
        self,
        request: ScienceSuccessorRecoveryCreateRequestV1,
    ) -> ScienceSuccessorRecoverySnapshotV1:
        request = ScienceSuccessorRecoveryCreateRequestV1.model_validate(
            request.model_dump(mode="python")
        )
        source = self._verify_source(request.source, request.plan)
        supersession_authority = None
        if request.supersedes_recovery_id is not None:
            prior = self.store.get_recovery(request.supersedes_recovery_id)
            failed_targets = tuple(
                item for item in prior.record.targets if item.state == "failed"
            )
            if prior.record.state != "failed" or len(failed_targets) != 1:
                raise ScienceSuccessorRecoveryV1Error(
                    "only one terminal failed recovery can be superseded"
                )
            operation = self.store.get_target_operation(
                prior.record.recovery_id,
                failed_targets[0].target_id,
            )
            if operation.state != "failed" or operation.failure is None:
                raise ScienceSuccessorRecoveryV1Error(
                    "recovery supersession lacks terminal failure evidence"
                )
            completed_operations = tuple(
                self.store.get_target_operation(
                    prior.record.recovery_id,
                    target_id,
                )
                for target_id in prior.record.completed_target_ids
            )
            supersession_authority = self._executor.attest_recovery_supersession(
                prior=prior,
                failure=operation.failure,
                source=source,
                plan=request.plan,
                execution_identity=request.execution_identity,
                completed_operations=completed_operations,
            )
        return self.store.create_recovery(
            request,
            supersession_authority=supersession_authority,
            now=self._clock(),
        )

    def get_recovery(self, recovery_id: str) -> ScienceSuccessorRecoverySnapshotV1:
        """Read durable recovery authority without exposing the store to API code."""

        return self.store.get_recovery(recovery_id)

    def get_target_operation(
        self,
        recovery_id: str,
        target_id: str,
    ) -> ScienceSuccessorRecoveryTargetOperationV1:
        """Read one immutable target intent/result without authorizing work."""

        return self.store.get_target_operation(recovery_id, target_id)

    def run_authorized_target(
        self,
        *,
        recovery_id: str,
        target_id: str,
        idempotency_key: str,
    ) -> ScienceSuccessorRecoverySnapshotV1:
        request = self.store.get_create_request(recovery_id)
        source = self._verify_source(request.source, request.plan)
        snapshot = self.store.get_recovery(recovery_id)
        record = snapshot.record
        if (
            record.source_transition_id != source.successor_transition_id
            or record.source_failed_job_id
            != science_successor_recovery_failure_id(source)
            or record.source_failure_kind
            != (
                "evolution_job" if source.failed_job is not None else "pre_job_operation"
            )
            or record.source_authority_sha256 != science_successor_recovery_source_sha256(source)
            or record.source_transition_state_sha256 != source.successor_transition_state_sha256
            or record.plan_sha256 != science_successor_plan_sha256(request.plan)
            or record.execution_identity != request.execution_identity
            or record.target_authorization_order != request.target_authorization_order
        ):
            raise ScienceSuccessorRecoveryV1Error(
                "recovery record no longer matches its immutable create request"
            )
        existing = self.store.get_target_operation_or_none(recovery_id, target_id)
        if existing is not None and existing.state in {"succeeded", "failed"}:
            carried_target_ids = (
                ()
                if record.supersession_authority is None
                else tuple(
                    item.prior_result.target_id
                    for item in record.supersession_authority.carried_completed_targets
                )
            )
            if existing.state == "succeeded" and target_id in carried_target_ids:
                return self.store.get_recovery(recovery_id)
            if existing.intent.idempotency_key != idempotency_key:
                raise ScienceSuccessorRecoveryV1Error(
                    "recovery target already owns a different operation"
                )
            return self.store.get_recovery(recovery_id)
        preflight = self.store.prepare_target_preflight(
            recovery_id=recovery_id,
            target_id=target_id,
            idempotency_key=idempotency_key,
        )
        try:
            readiness = self._executor.check_target_readiness(
                preflight,
                source=source,
                plan=request.plan,
            )
            if type(readiness) is not ScienceSuccessorRecoveryTargetReadinessV1:
                raise TypeError("recovery readiness has the wrong type")
            if (
                readiness.operation_id != preflight.operation_id
                or readiness.recovery_id != preflight.recovery_id
                or readiness.target_id != preflight.target.target_id
                or readiness.execution_identity != preflight.execution_identity
            ):
                raise ValueError("recovery readiness differs from its target preflight")
        except Exception as exc:
            raise ScienceSuccessorRecoveryReadinessErrorV1() from exc
        try:
            intent, created = self.store.begin_target(
                recovery_id=recovery_id,
                target_id=target_id,
                idempotency_key=idempotency_key,
                now=self._clock(),
            )
            if (
                intent.operation_id != preflight.operation_id
                or intent.idempotency_key != preflight.idempotency_key
                or intent.recovery_id != preflight.recovery_id
                or intent.source_authority_sha256 != preflight.source_authority_sha256
                or intent.execution_identity != preflight.execution_identity
                or intent.target != preflight.target
            ):
                raise ScienceSuccessorRecoveryV1Error(
                    "durable recovery target intent differs from its passed readiness gate"
                )
            operation = self.store.get_target_operation(recovery_id, target_id)
            if operation.state in {"succeeded", "failed"}:
                return self.store.get_recovery(recovery_id)
            try:
                adopted_job = (
                    None
                    if record.supersession_authority is None
                    else record.supersession_authority.adopted_job
                )
                if adopted_job is not None and adopted_job.target_id == target_id:
                    terminal = self._executor.adopt_target(
                        intent,
                        source=source,
                        plan=request.plan,
                        adopted_job=adopted_job,
                    )
                elif created:
                    terminal = self._executor.execute_target(
                        intent,
                        source=source,
                        plan=request.plan,
                    )
                else:
                    terminal = self._executor.recover_target(
                        intent,
                        source=source,
                        plan=request.plan,
                    )
                    if terminal is None:
                        raise ScienceSuccessorRecoveryV1Error(
                            "recovery target outcome is indeterminate; execution is not repeated"
                        )
            except RecoveryTargetTerminalFailureV1 as exc:
                return self.store.fail_target(exc.failure)
            except Exception as exc:
                # Once the intent exists, an ordinary returned exception is a
                # terminal, append-only outcome.  Replaying here could repeat a
                # paid inference whose response was lost.  Literal process
                # death remains recoverable through ``recover_target`` because
                # it never passes through this handler.
                failure_class: Literal["infrastructure", "deterministic"] = (
                    "deterministic"
                    if isinstance(
                        exc,
                        (ValueError, TypeError, ScienceSuccessorRecoveryV1Error),
                    )
                    else "infrastructure"
                )
                failure = ScienceSuccessorRecoveryTargetFailureV1(
                    operation_id=intent.operation_id,
                    recovery_id=intent.recovery_id,
                    target_id=intent.target.target_id,
                    failure_code="post_intent_execution_failed",
                    failure_class=failure_class,
                    retryable=False,
                    failure_evidence_sha256=_sha256(
                        {
                            "error_type": type(exc).__name__,
                            "failure_class": failure_class,
                            "operation_id": intent.operation_id,
                            "recovery_id": intent.recovery_id,
                            "target_id": intent.target.target_id,
                        }
                    ),
                    failed_at=self._clock(),
                )
                return self.store.fail_target(failure)
            if type(terminal) is ScienceSuccessorRecoveryTargetFailureV1:
                return self.store.fail_target(terminal)
            if type(terminal) is not ScienceSuccessorRecoveryTargetResultV1:
                raise ScienceSuccessorRecoveryV1Error(
                    "recovery executor returned an invalid terminal result"
                )
            return self.store.complete_target(terminal)
        finally:
            self._executor.release_target_readiness(preflight)


__all__ = [
    "AgentSystemAuditReceiptV1",
    "ExhaustedSuccessorRecoveryAuthorityV1",
    "FailedEvolutionJobAuthorityV1",
    "FailedEvolutionPreJobOperationAuthorityV1",
    "HistoricalEvolutionEvidenceDigestV1",
    "HistoricalEvolutionSourceAttestationV1",
    "HistoricalEvolutionSourceProvenanceV1",
    "RecoveryExecutionIdentityV1",
    "RecoveryTargetTerminalFailureV1",
    "ScienceSuccessorRecoveryAdoptedJobAuthorityV1",
    "ScienceSuccessorRecoveryCarriedTargetAuthorityV1",
    "ScienceSuccessorRecoveryAuthorityReaderV1",
    "ScienceSuccessorRecoveryCoordinatorV1",
    "ScienceSuccessorRecoveryCreateRequestV1",
    "ScienceSuccessorRecoveryProjectSeedAuthorityV1",
    "ScienceSuccessorRecoveryProjectSeedRequestV1",
    "ScienceSuccessorRecoveryReadinessErrorV1",
    "ScienceSuccessorRecoveryRecordV1",
    "ScienceSuccessorRecoverySnapshotV1",
    "ScienceSuccessorRecoverySourceResolveRequestV1",
    "ScienceSuccessorRecoverySourceResponseV1",
    "ScienceSuccessorRecoveryStoreV1",
    "ScienceSuccessorRecoveryTargetExecutorV1",
    "ScienceSuccessorRecoveryTargetFailureV1",
    "ScienceSuccessorRecoveryTargetIntentV1",
    "ScienceSuccessorRecoveryTargetOperationV1",
    "ScienceSuccessorRecoveryTargetPreflightV1",
    "ScienceSuccessorRecoveryTargetReadinessV1",
    "ScienceSuccessorRecoveryTargetResultV1",
    "ScienceSuccessorRecoveryV1Error",
    "science_successor_recovery_admission_report_sha256",
    "science_successor_recovery_adoptable_job_result_sha256",
    "science_successor_recovery_failed_target",
    "science_successor_recovery_failure_id",
    "science_successor_recovery_source_sha256",
]
