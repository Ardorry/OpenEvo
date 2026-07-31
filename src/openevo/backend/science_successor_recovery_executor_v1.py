"""Production target executor for append-only science successor recovery.

This module owns the generation lease that bridges the no-model readiness
check and durable target-intent persistence.  It reads the currently
registered evolution worker receipt through the generation-bound internal
identity and delegates the actual native job to a trusted runner.  It never
constructs an artifact revision itself.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import httpx

from openevo.backend.run_control import CoreRunControlError
from openevo.backend.science_successor import (
    ScienceSuccessorPlanV2,
    ScienceSuccessorPreparationContextV2,
    TrainingFeedbackBindingV2,
    science_successor_plan_sha256,
)
from openevo.backend.science_successor_preparer_v2 import (
    ProductionScienceSuccessorPreparerV2,
    ScienceSuccessorPreparationV2Error,
)
from openevo.backend.science_successor_recovery_control_v1 import (
    ManagedReflectorReadinessResponseV1,
)
from openevo.backend.science_successor_recovery_v1 import (
    ExhaustedSuccessorRecoveryAuthorityV1,
    FailedEvolutionJobAuthorityV1,
    FailedEvolutionPreJobOperationAuthorityV1,
    HistoricalEvolutionSourceProvenanceV1,
    RecoveryExecutionIdentityV1,
    RecoveryTargetTerminalFailureV1,
    ScienceSuccessorRecoveryAdoptedJobAuthorityV1,
    ScienceSuccessorRecoveryCarriedTargetAuthorityV1,
    ScienceSuccessorRecoveryProjectSeedAuthorityV1,
    ScienceSuccessorRecoveryProjectSeedRequestV1,
    ScienceSuccessorRecoveryRecordV1,
    ScienceSuccessorRecoverySnapshotV1,
    ScienceSuccessorRecoverySourceResolveRequestV1,
    ScienceSuccessorRecoverySourceResponseV1,
    ScienceSuccessorRecoveryStoreV1,
    ScienceSuccessorRecoverySupersessionAuthorityV1,
    ScienceSuccessorRecoveryTargetFailureV1,
    ScienceSuccessorRecoveryTargetIntentV1,
    ScienceSuccessorRecoveryTargetOperationV1,
    ScienceSuccessorRecoveryTargetPreflightV1,
    ScienceSuccessorRecoveryTargetReadinessV1,
    ScienceSuccessorRecoveryTargetResultV1,
    ScienceSuccessorRecoveryV1Error,
    science_successor_recovery_adoptable_job_result_sha256,
    science_successor_recovery_source_sha256,
)
from openevo.backend.service_supervisor import (
    ServiceExecutionMode,
    ServiceRunBinding,
    SupervisorStateError,
)
from openevo.evolution.framework.contracts import canonical_digest
from openevo.evolution.models import (
    ArtifactResponse,
    ArtifactState,
    FailedPlanBoundJobAuthorityResponse,
    ReflectorInferenceReservationReceipt,
    SuccessorTransitionJobInventoryResponse,
)
from openevo.evolution.training_feedback import (
    CompletedDatasetAuthority,
    EvolutionDatasetViewReceipt,
    ResolvedEvolutionDatasetView,
    TrainingFeedbackAttachment,
)
from openevo.experiments.clients import (
    EvolutionClientProtocol,
    EvolutionHttpClient,
    EvolutionHttpStatusError,
)
from openevo.projects.science.compiler import MANAGED_RUNTIME_IMAGES
from openevo.runtime.managed import (
    MANAGED_CODEX_DEFAULT_MODEL,
    MANAGED_CODEX_VERSION,
    managed_runtime_release_authority_digest,
)
from openevo.runtime.managed_candidate_readiness import (
    managed_candidate_readiness_failure_code,
)
from openevo.runtime.managed_reflector_mount import (
    ManagedReflectorCredentialMountReadiness,
    ManagedReflectorRuntimeReadiness,
)


class ScienceSuccessorRecoveryNativeTargetRunnerV1(Protocol):
    """Native successor-job port; implementations use the production preparer."""

    def codex_model_for_target(
        self,
        *,
        source: ExhaustedSuccessorRecoveryAuthorityV1,
        plan: ScienceSuccessorPlanV2,
    ) -> str: ...

    def execute_native_target(
        self,
        intent: ScienceSuccessorRecoveryTargetIntentV1,
        *,
        source: ExhaustedSuccessorRecoveryAuthorityV1,
        plan: ScienceSuccessorPlanV2,
        binding: ServiceRunBinding,
        client: EvolutionClientProtocol,
    ) -> ScienceSuccessorRecoveryTargetResultV1: ...

    def recover_native_target(
        self,
        intent: ScienceSuccessorRecoveryTargetIntentV1,
        *,
        source: ExhaustedSuccessorRecoveryAuthorityV1,
        plan: ScienceSuccessorPlanV2,
        binding: ServiceRunBinding,
        client: EvolutionClientProtocol,
    ) -> (
        ScienceSuccessorRecoveryTargetResultV1 | ScienceSuccessorRecoveryTargetFailureV1 | None
    ): ...

    def adopt_native_target(
        self,
        intent: ScienceSuccessorRecoveryTargetIntentV1,
        *,
        source: ExhaustedSuccessorRecoveryAuthorityV1,
        plan: ScienceSuccessorPlanV2,
        adopted_job: ScienceSuccessorRecoveryAdoptedJobAuthorityV1,
        binding: ServiceRunBinding,
        client: EvolutionClientProtocol,
    ) -> ScienceSuccessorRecoveryTargetResultV1: ...


class ScienceSuccessorRecoveryServiceControlV1(Protocol):
    def adopt_current_run_binding(
        self,
        execution_mode: ServiceExecutionMode,
        *,
        model_ref: str | None = None,
        codex_model: str | None = None,
        runtime_image: str | None = None,
        total_timeout: float | None = None,
    ) -> tuple[object, object] | None: ...

    def ensure_run_binding(
        self,
        execution_mode: ServiceExecutionMode,
        *,
        model_ref: str | None = None,
        codex_model: str | None = None,
        runtime_image: str | None = None,
        total_timeout: float | None = None,
    ) -> tuple[object, object | None]: ...

    def run_binding(self) -> ServiceRunBinding: ...


class ScienceSuccessorRecoveryContextProviderV1(Protocol):
    def recovery_successor_context(
        self,
        source: ExhaustedSuccessorRecoveryAuthorityV1,
        plan: ScienceSuccessorPlanV2,
    ) -> ScienceSuccessorPreparationContextV2: ...

    def recovery_successor_source_context(
        self,
        successor_transition_id: str,
    ) -> tuple[ScienceSuccessorPreparationContextV2, int]: ...


class ScienceSuccessorRecoveryProjectSeedOwnerV1(Protocol):
    def seed_successor_recovery_project(
        self,
        request: ScienceSuccessorRecoveryProjectSeedRequestV1,
        *,
        source: ExhaustedSuccessorRecoveryAuthorityV1,
        recovery,
        results: tuple[ScienceSuccessorRecoveryTargetResultV1, ...],
        binding: ServiceRunBinding,
        evolution: EvolutionClientProtocol,
    ) -> ScienceSuccessorRecoveryProjectSeedAuthorityV1: ...

    def successor_recovery_project_seed(
        self,
        seed_request_id: str,
    ) -> ScienceSuccessorRecoveryProjectSeedAuthorityV1: ...


def _recovery_source_export_failure_code(exc: Exception) -> str:
    """Project one non-secret closed code from a source-export failure chain."""

    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, EvolutionHttpStatusError):
            detail = re.sub(r"[^A-Za-z0-9]+", "_", current.detail_code).strip("_").upper()
            return f"RECOVERY_SOURCE_EVOLUTION_HTTP_{current.status_code}_{detail}"
        if isinstance(current, CoreRunControlError):
            code = re.sub(r"[^A-Za-z0-9]+", "_", current.code).strip("_").upper()
            return f"RECOVERY_SOURCE_CORE_{code}"[:96]
        if isinstance(current, ScienceSuccessorPreparationV2Error):
            code = re.sub(
                r"[^A-Za-z0-9]+",
                "_",
                current.failure_code,
            ).strip("_").upper()
            return f"RECOVERY_SOURCE_PREPARATION_{code}"[:128]
        if isinstance(current, SupervisorStateError):
            return "RECOVERY_SOURCE_SERVICE_LEASE_CONFLICT"
        if isinstance(current, httpx.ConnectError):
            return "RECOVERY_SOURCE_EVOLUTION_CONNECT_FAILED"
        if isinstance(current, httpx.TimeoutException):
            return "RECOVERY_SOURCE_EVOLUTION_TIMEOUT"
        if isinstance(current, httpx.TransportError):
            return "RECOVERY_SOURCE_EVOLUTION_TRANSPORT_FAILED"
        if isinstance(current, (TypeError, ValueError, KeyError)):
            return "RECOVERY_SOURCE_AUTHORITY_VALIDATION_FAILED"
        current = current.__cause__ or current.__context__
    exception_name = type(exc).__name__
    closed_name = re.sub(r"(?<!^)(?=[A-Z])", "_", exception_name).upper()
    if re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", closed_name):
        return f"RECOVERY_SOURCE_EXCEPTION_{closed_name}"
    return "RECOVERY_SOURCE_EXPORT_UNCLASSIFIED"


def _recovery_source_readback_failure_code(exc: Exception, *, phase: str) -> str:
    """Return one stable, secret-free code for recovery source readback.

    Source readback is deliberately stricter than source export.  Keep the
    failing phase visible without exposing exception text, remote paths, or
    service credentials.  In particular, context lookup and managed-service
    binding happen before any Evolution HTTP client exists and must not escape
    the recovery API as an unstructured connection close.
    """

    if phase not in {
        "context",
        "service_binding",
        "source_attestation",
        "evolution_readback",
    }:
        phase = "unknown"
    suffix = _recovery_source_export_failure_code(exc)
    prefix = "RECOVERY_SOURCE_"
    suffix = suffix.removeprefix(prefix)
    return f"RECOVERY_SOURCE_READBACK_{phase.upper()}_{suffix}"[:160]


def _recovery_project_seed_failure_detail(exc: Exception) -> str:
    """Project a nested seed failure to one stable, non-secret detail."""

    code = _recovery_source_export_failure_code(exc)
    normalized = re.sub(r"[^A-Za-z0-9]+", " ", code).strip().lower()
    return f"successor recovery project seed failed {normalized}"[:160]


def _close_recovery_seed_resources(
    client: EvolutionClientProtocol | None,
    lease: object,
    *,
    suppress_errors: bool,
) -> None:
    """Close both seed resources without masking an earlier operation error."""

    first_error: Exception | None = None
    for resource in (client, lease):
        if resource is None:
            continue
        close = getattr(resource, "close", None)
        if not callable(close):
            continue
        try:
            close()
        except Exception as exc:  # noqa: BLE001 - arbitrary resource cleanup.
            if first_error is None:
                first_error = exc
    if first_error is not None and not suppress_errors:
        raise first_error


def _validate_sealed_recovery_source_provenance(
    provenance: HistoricalEvolutionSourceProvenanceV1,
) -> None:
    """Validate the immutable resolver identity without rebinding it.

    ``resolution_*`` identifies the Core release that sealed the unique
    historical source response.  It is intentionally not the execution
    identity of a later recovery lifecycle.  The recovery create contract
    carries a separate generation-bound ``execution_identity`` and requires
    that identity to differ from the historical source.  Requiring the old
    resolver generation to equal the current worker generation would make an
    append-only recovery impossible after any diagnostic Core upgrade.
    """

    if (
        provenance.provenance_tier != "legacy_supervisor_attested"
        or provenance.core_native is not False
        or provenance.authority_minted is not False
        or provenance.current_core_authorities_reproduced is not True
    ):
        raise ScienceSuccessorRecoveryV1Error(
            "historical source resolution provenance is invalid"
        )


def _validate_strict_wire_model(model_type, payload: Mapping[str, object]):
    """Validate one strict Evolution response using its JSON wire semantics.

    Evolution encodes closed tuple inventories as JSON arrays.  Passing the
    decoded Python ``list`` directly to a strict Pydantic model rejects the
    standards-compliant response before its content-addressed validators can
    run.  Re-encode the already-bounded object and validate in JSON mode, just
    as the authenticated Core-control request endpoints do.
    """

    return model_type.model_validate_json(
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


class ProductionScienceSuccessorRecoveryAuthorityReaderV1:
    """Cross-check caller-supplied historical authority against durable Core stores."""

    def __init__(
        self,
        *,
        context_provider: ScienceSuccessorRecoveryContextProviderV1,
        preparer: ProductionScienceSuccessorPreparerV2,
        services: ScienceSuccessorRecoveryServiceControlV1,
        attestation_store: ScienceSuccessorRecoveryStoreV1,
        daemon_release_identity: str,
        release_install_digest: str,
        evolution_factory: (Callable[[ServiceRunBinding], EvolutionClientProtocol] | None) = None,
    ) -> None:
        self._context_provider = context_provider
        self._preparer = preparer
        self._services = services
        self._attestation_store = attestation_store
        self._daemon_release_identity = _require_sha256(
            daemon_release_identity,
            "daemon_release_identity",
        )
        self._release_install_digest = _require_sha256(
            release_install_digest,
            "release_install_digest",
        )
        self._evolution_factory = evolution_factory or (
            lambda binding: EvolutionHttpClient(
                binding.evolution_backend_url,
                headers=binding.request_headers(),
            )
        )

    def resolve_exhausted_successor_authority(
        self,
        request: ScienceSuccessorRecoverySourceResolveRequestV1,
    ) -> ScienceSuccessorRecoverySourceResponseV1:
        """Construct closed source authority from ledgers plus historical identity."""

        context, attempt_count = self._context_provider.recovery_successor_source_context(
            request.successor_transition_id
        )
        plan = context.plan
        model = self._preparer.recovery_codex_model(context)
        _snapshot, lease = self._services.ensure_run_binding(
            ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
            codex_model=model,
            runtime_image=MANAGED_RUNTIME_IMAGES["managed_science"],
        )
        binding = getattr(lease, "binding", None)
        if lease is None or type(binding) is not ServiceRunBinding:
            raise ScienceSuccessorRecoveryV1Error(
                "recovery source export lacks a managed service binding"
            )
        client: EvolutionClientProtocol | None = None
        try:
            client = self._evolution_factory(binding)
            base_dataset = self._preparer.recovery_source_dataset(
                context,
                client=client,
            )
            completed = CompletedDatasetAuthority.model_validate(
                client.get_completed_dataset_authority(base_dataset.dataset_id)
            )
            attachments = tuple(
                TrainingFeedbackAttachment.model_validate(
                    client.get_training_feedback_attachment(attachment_id)
                )
                for attachment_id in request.attachment_ids
            )
            resolved_artifact = ArtifactResponse.model_validate(
                client.get_artifact(request.resolved_dataset_artifact_id)
            )
            feedback_binding = _feedback_binding_from_authorities(
                context=context,
                base_dataset=base_dataset,
                completed=completed,
                attachments=attachments,
                resolved_artifact=resolved_artifact,
            )
            dataset = base_dataset.model_copy(update={"training_feedback": feedback_binding})
            predecessor = context.task.admission.predecessor_project_head
            if attempt_count != request.transition_attempt_capacity:
                raise ScienceSuccessorRecoveryV1Error(
                    "failed source transition does not match recovery export request"
                )
            attestation = request.historical_attestation
            failed_job: FailedEvolutionJobAuthorityV1 | None = None
            failed_pre_job_operation: FailedEvolutionPreJobOperationAuthorityV1 | None = None
            if request.failed_job_id is not None:
                failed = _validate_strict_wire_model(
                    FailedPlanBoundJobAuthorityResponse,
                    client.get_internal_failed_plan_bound_job_authority(
                        request.failed_job_id
                    ),
                )
                if (
                    failed.successor_transition_id != request.successor_transition_id
                    or failed.output_artifact_ids != ()
                    or len(failed.declared_output_artifact_types) != 1
                ):
                    raise ScienceSuccessorRecoveryV1Error(
                        "failed source transition does not match recovery export request"
                    )
                failed_job = FailedEvolutionJobAuthorityV1(
                    job_id=failed.job_id,
                    successor_transition_id=failed.successor_transition_id,
                    target_id=failed.target_id,
                    method_id=failed.method_id,
                    artifact_type=failed.declared_output_artifact_types[0],
                    state="failed",
                    retryable=False,
                    output_artifact_ids=(),
                    job_result_sha256=failed.job_result_sha256,
                    failure_code=attestation.claimed_failure_code,
                )
            else:
                inventory = _validate_strict_wire_model(
                    SuccessorTransitionJobInventoryResponse,
                    client.get_internal_successor_transition_job_inventory(
                        request.successor_transition_id
                    ),
                )
                attempt = context.transition_attempt
                first_target = plan.enabled_methods[0] if plan.enabled_methods else None
                error = attempt.error
                if (
                    request.failed_pre_job_operation_id is None
                    or inventory.successor_transition_id
                    != request.successor_transition_id
                    or inventory.job_ids != ()
                    or attempt.transition_attempt_id
                    != request.failed_pre_job_operation_id
                    or attempt.state != "failed"
                    or error is None
                    or error.retryable is not False
                    or first_target is None
                    or error.code != attestation.claimed_failure_code
                ):
                    raise ScienceSuccessorRecoveryV1Error(
                        "failed pre-job operation does not match recovery export request"
                    )
                failed_pre_job_operation = FailedEvolutionPreJobOperationAuthorityV1(
                    operation_id=attempt.transition_attempt_id,
                    successor_transition_id=request.successor_transition_id,
                    target_id=first_target.target_id,
                    method_id=first_target.method_id,
                    artifact_type=first_target.output_artifact_type,
                    state="failed",
                    retryable=False,
                    evolution_job_ids=(),
                    evolution_job_inventory_sha256=inventory.content_sha256,
                    operation_result_sha256=canonical_digest(error),
                    failure_code=error.code,
                )
            source = ExhaustedSuccessorRecoveryAuthorityV1(
                project_id=context.task.project_id,
                task_id=context.task.task_id,
                task_admission_id=context.task.admission.task_admission_id,
                accepted_attempt_id=context.accepted_attempt.attempt_id,
                successor_transition_id=request.successor_transition_id,
                successor_transition_state="failed",
                successor_transition_state_sha256=canonical_digest(context.transition),
                transition_attempt_count=attempt_count,
                transition_attempt_capacity=request.transition_attempt_capacity,
                source_commit_absent=True,
                source_outputs_absent=True,
                predecessor_project_head_id=predecessor.project_head_id,
                predecessor_project_head_sha256=predecessor.manifest_sha256,
                active_project_head_id=predecessor.project_head_id,
                active_project_head_sha256=predecessor.manifest_sha256,
                plan_sha256=science_successor_plan_sha256(plan),
                source_core_generation=attestation.claimed_source_core_generation,
                source_release_identity=attestation.claimed_source_release_identity,
                source_provenance=HistoricalEvolutionSourceProvenanceV1(
                    provenance_tier="legacy_supervisor_attested",
                    core_native=False,
                    authority_minted=False,
                    attestation_id=attestation.attestation_id,
                    attestation_sha256=attestation.content_sha256,
                    resolution_core_generation=binding.generation_digest,
                    resolution_daemon_release_identity=(self._daemon_release_identity),
                    resolution_release_install_digest=(self._release_install_digest),
                    current_core_authorities_reproduced=True,
                ),
                dataset=dataset,
                failed_job=failed_job,
                failed_pre_job_operation=failed_pre_job_operation,
            )
            response = ScienceSuccessorRecoverySourceResponseV1(
                source=source,
                plan=plan,
            )
            completed_readback = self._validate_completed_dataset(source, client)
            self._validate_feedback_view(
                source,
                client,
                completed=completed_readback,
            )
            self._validate_failed_source(source, context, client)
            return self._attestation_store.seal_historical_source_resolution(
                request,
                response,
            )
        except ScienceSuccessorRecoveryV1Error:
            raise
        except Exception as exc:
            raise ScienceSuccessorRecoveryV1Error(
                _recovery_source_export_failure_code(exc)
            ) from exc
        finally:
            if client is not None:
                close_client = getattr(client, "close", None)
                if callable(close_client):
                    close_client()
            close_lease = getattr(lease, "close", None)
            if callable(close_lease):
                close_lease()

    def read_exhausted_successor_authority(
        self,
        expected: ExhaustedSuccessorRecoveryAuthorityV1,
        plan: ScienceSuccessorPlanV2,
    ) -> ExhaustedSuccessorRecoveryAuthorityV1:
        phase = "context"
        lease = None
        client: EvolutionClientProtocol | None = None
        try:
            context = self._context_provider.recovery_successor_context(expected, plan)
            model = self._preparer.recovery_codex_model(context)
            phase = "service_binding"
            _snapshot, lease = self._services.ensure_run_binding(
                ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
                codex_model=model,
                runtime_image=MANAGED_RUNTIME_IMAGES["managed_science"],
            )
            binding = getattr(lease, "binding", None)
            if lease is None or type(binding) is not ServiceRunBinding:
                raise ScienceSuccessorRecoveryV1Error(
                    "recovery source readback lacks a managed service binding"
                )
            phase = "source_attestation"
            provenance = expected.source_provenance
            _validate_sealed_recovery_source_provenance(provenance)
            self._attestation_store.verify_historical_source_resolution(
                ScienceSuccessorRecoverySourceResponseV1(
                    source=expected,
                    plan=plan,
                )
            )
            phase = "evolution_readback"
            client = self._evolution_factory(binding)
            self._preparer.validate_recovery_source_dataset(
                context,
                expected.dataset,
                client=client,
            )
            completed = self._validate_completed_dataset(expected, client)
            self._validate_feedback_view(expected, client, completed=completed)
            self._validate_failed_source(expected, context, client)
            return expected
        except ScienceSuccessorRecoveryV1Error:
            raise
        except Exception as exc:
            raise ScienceSuccessorRecoveryV1Error(
                _recovery_source_readback_failure_code(exc, phase=phase)
            ) from exc
        finally:
            if client is not None:
                close_client = getattr(client, "close", None)
                if callable(close_client):
                    close_client()
            close_lease = getattr(lease, "close", None)
            if callable(close_lease):
                close_lease()

    @staticmethod
    def _validate_completed_dataset(
        expected: ExhaustedSuccessorRecoveryAuthorityV1,
        client: EvolutionClientProtocol,
    ) -> CompletedDatasetAuthority:
        feedback = expected.dataset.training_feedback
        assert feedback is not None
        authority = CompletedDatasetAuthority.model_validate(
            client.get_completed_dataset_authority(expected.dataset.dataset_id)
        )
        if (
            authority.completed_dataset_id != expected.dataset.dataset_id
            or authority.completed_dataset_revision != feedback.completed_dataset_revision
            or authority.dataset_artifact_id != expected.dataset.artifact_id
            or authority.dataset_manifest_sha256 != expected.dataset.manifest_sha256
        ):
            raise ScienceSuccessorRecoveryV1Error(
                "completed dataset differs from recovery source authority"
            )
        return authority

    @staticmethod
    def _validate_feedback_view(
        expected: ExhaustedSuccessorRecoveryAuthorityV1,
        client: EvolutionClientProtocol,
        *,
        completed: CompletedDatasetAuthority,
    ) -> None:
        feedback = expected.dataset.training_feedback
        assert feedback is not None
        attachments = tuple(
            TrainingFeedbackAttachment.model_validate(
                client.get_training_feedback_attachment(attachment_id)
            )
            for attachment_id in feedback.attachment_ids
        )
        if (
            tuple(item.attachment_id for item in attachments) != feedback.attachment_ids
            or tuple(item.content_sha256 for item in attachments) != feedback.attachment_sha256
            or any(
                item.dataset_id != expected.dataset.dataset_id
                or item.dataset_revision != feedback.completed_dataset_revision
                or item.task_id != completed.task_id
                or item.task_scope_id != feedback.task_scope_id
                or item.status != "sealed"
                or item.authority.value != "evaluator_only"
                or item.source_session_result_sha256
                != completed.source_session_result_sha256
                for item in attachments
            )
        ):
            raise ScienceSuccessorRecoveryV1Error(
                "training feedback attachments differ from recovery source authority"
            )
        artifact = ArtifactResponse.model_validate(
            client.get_artifact(feedback.resolved_dataset_artifact_id)
        )
        manifest = artifact.manifest
        overlay = [item.task_local_feedback for item in attachments if item.task_local_feedback]
        receipt = EvolutionDatasetViewReceipt(
            resolution_id=str(manifest.get("create_identity") or ""),
            source_dataset_id=str(manifest.get("derived_from_dataset_id") or ""),
            source_dataset_manifest_sha256=str(
                manifest.get("source_dataset_manifest_sha256") or ""
            ),
            source_session_result_sha256=str(manifest.get("source_session_result_sha256") or ""),
            attachment_ids=tuple(manifest.get("training_feedback_attachment_ids") or ()),
            attachment_sha256=tuple(manifest.get("training_feedback_attachment_sha256") or ()),
            records_sha256=str(manifest.get("records_sha256") or ""),
            manifest_sha256=canonical_digest(manifest),
            task_local_overlay_sha256=(canonical_digest(overlay) if overlay else None),
        )
        resolved = ResolvedEvolutionDatasetView(
            completed_dataset_id=expected.dataset.dataset_id,
            completed_dataset_revision=feedback.completed_dataset_revision,
            attachments=attachments,
            dataset_view=receipt,
            dataset_artifact=artifact,
            resolved_view_sha256=feedback.resolved_view_sha256,
        )
        if (
            resolved.dataset_artifact.artifact_id != feedback.resolved_dataset_artifact_id
            or resolved.dataset_view.source_dataset_id != expected.dataset.dataset_id
            or resolved.dataset_view.source_session_result_sha256
            != completed.source_session_result_sha256
            or any(
                item.source_dataset_manifest_sha256
                != resolved.dataset_view.source_dataset_manifest_sha256
                for item in attachments
            )
        ):
            raise ScienceSuccessorRecoveryV1Error(
                "resolved evolution view differs from recovery source authority"
            )

    @staticmethod
    def _validate_failed_source(
        expected: ExhaustedSuccessorRecoveryAuthorityV1,
        context: ScienceSuccessorPreparationContextV2,
        client: EvolutionClientProtocol,
    ) -> None:
        if expected.failed_pre_job_operation is not None:
            observed = _validate_strict_wire_model(
                SuccessorTransitionJobInventoryResponse,
                client.get_internal_successor_transition_job_inventory(
                    expected.successor_transition_id
                ),
            )
            failed = expected.failed_pre_job_operation
            attempt = context.transition_attempt
            error = attempt.error
            if (
                observed.successor_transition_id != expected.successor_transition_id
                or observed.job_ids != ()
                or observed.content_sha256 != failed.evolution_job_inventory_sha256
                or attempt.transition_attempt_id != failed.operation_id
                or attempt.state != "failed"
                or error is None
                or error.retryable is not False
                or error.code != failed.failure_code
                or canonical_digest(error) != failed.operation_result_sha256
            ):
                raise ScienceSuccessorRecoveryV1Error(
                    "failed pre-job operation differs from recovery source authority"
                )
            return
        failed_job = expected.failed_job
        assert failed_job is not None
        observed = _validate_strict_wire_model(
            FailedPlanBoundJobAuthorityResponse,
            client.get_internal_failed_plan_bound_job_authority(failed_job.job_id)
        )
        if (
            observed.job_id != failed_job.job_id
            or observed.successor_transition_id != expected.successor_transition_id
            or observed.target_id != failed_job.target_id
            or observed.method_id != failed_job.method_id
            or observed.declared_output_artifact_types != (failed_job.artifact_type,)
            or observed.output_artifact_ids != ()
            or observed.job_result_sha256 != failed_job.job_result_sha256
        ):
            raise ScienceSuccessorRecoveryV1Error(
                "failed evolution job differs from recovery source authority"
            )


class ProductionScienceSuccessorRecoveryNativeRunnerV1:
    """Reuse the production preparer for one fresh native evolution job."""

    def __init__(
        self,
        *,
        context_provider: ScienceSuccessorRecoveryContextProviderV1,
        preparer: ProductionScienceSuccessorPreparerV2,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._context_provider = context_provider
        self._preparer = preparer
        self._clock = clock or (lambda: datetime.now(UTC))

    def codex_model_for_target(
        self,
        *,
        source: ExhaustedSuccessorRecoveryAuthorityV1,
        plan: ScienceSuccessorPlanV2,
    ) -> str:
        context = self._context_provider.recovery_successor_context(source, plan)
        return self._preparer.recovery_codex_model(context)

    def execute_native_target(
        self,
        intent: ScienceSuccessorRecoveryTargetIntentV1,
        *,
        source: ExhaustedSuccessorRecoveryAuthorityV1,
        plan: ScienceSuccessorPlanV2,
        binding: ServiceRunBinding,
        client: EvolutionClientProtocol,
    ) -> ScienceSuccessorRecoveryTargetResultV1:
        try:
            return self._run(
                intent,
                source=source,
                plan=plan,
                binding=binding,
                client=client,
            )
        except ScienceSuccessorPreparationV2Error as exc:
            raise RecoveryTargetTerminalFailureV1(self._terminal_failure(intent, exc)) from exc

    def recover_native_target(
        self,
        intent: ScienceSuccessorRecoveryTargetIntentV1,
        *,
        source: ExhaustedSuccessorRecoveryAuthorityV1,
        plan: ScienceSuccessorPlanV2,
        binding: ServiceRunBinding,
        client: EvolutionClientProtocol,
    ) -> ScienceSuccessorRecoveryTargetResultV1 | ScienceSuccessorRecoveryTargetFailureV1:
        try:
            return self._run(
                intent,
                source=source,
                plan=plan,
                binding=binding,
                client=client,
            )
        except ScienceSuccessorPreparationV2Error as exc:
            return self._terminal_failure(intent, exc)

    def adopt_native_target(
        self,
        intent: ScienceSuccessorRecoveryTargetIntentV1,
        *,
        source: ExhaustedSuccessorRecoveryAuthorityV1,
        plan: ScienceSuccessorPlanV2,
        adopted_job: ScienceSuccessorRecoveryAdoptedJobAuthorityV1,
        binding: ServiceRunBinding,
        client: EvolutionClientProtocol,
    ) -> ScienceSuccessorRecoveryTargetResultV1:
        """Complete admission for one already-paid, succeeded native job."""

        try:
            context = self._context_provider.recovery_successor_context(source, plan)
            return self._preparer.run_recovery_method(
                context,
                source.dataset,
                intent=intent,
                source=source,
                binding=binding,
                client=client,
                adopted_job=adopted_job,
            )
        except ScienceSuccessorPreparationV2Error as exc:
            raise RecoveryTargetTerminalFailureV1(
                self._terminal_failure(intent, exc)
            ) from exc


    def _run(
        self,
        intent: ScienceSuccessorRecoveryTargetIntentV1,
        *,
        source: ExhaustedSuccessorRecoveryAuthorityV1,
        plan: ScienceSuccessorPlanV2,
        binding: ServiceRunBinding,
        client: EvolutionClientProtocol,
    ) -> ScienceSuccessorRecoveryTargetResultV1:
        context = self._context_provider.recovery_successor_context(source, plan)
        return self._preparer.run_recovery_method(
            context,
            source.dataset,
            intent=intent,
            source=source,
            binding=binding,
            client=client,
        )

    def _terminal_failure(
        self,
        intent: ScienceSuccessorRecoveryTargetIntentV1,
        exc: ScienceSuccessorPreparationV2Error,
    ) -> ScienceSuccessorRecoveryTargetFailureV1:
        failure_class = "infrastructure" if exc.retryable else "deterministic"
        evidence = {
            "error_type": type(exc).__name__,
            "failure_class": failure_class,
            "operation_id": intent.operation_id,
            "recovery_id": intent.recovery_id,
            "target_id": intent.target.target_id,
        }
        digest = hashlib.sha256(
            json.dumps(
                evidence,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return ScienceSuccessorRecoveryTargetFailureV1(
            operation_id=intent.operation_id,
            recovery_id=intent.recovery_id,
            target_id=intent.target.target_id,
            failure_code=exc.failure_code,
            failure_class=failure_class,
            retryable=False,
            failure_evidence_sha256=digest,
            failed_at=self._clock().astimezone(UTC).isoformat().replace("+00:00", "Z"),
        )


class ProductionScienceSuccessorRecoveryProjectSeederV1:
    """Materialize completed recovery outputs into one unused Core project."""

    def __init__(
        self,
        *,
        store: ScienceSuccessorRecoveryStoreV1,
        authority_reader: ProductionScienceSuccessorRecoveryAuthorityReaderV1,
        owner: ScienceSuccessorRecoveryProjectSeedOwnerV1,
        services: ScienceSuccessorRecoveryServiceControlV1,
        native_runner: ProductionScienceSuccessorRecoveryNativeRunnerV1,
        evolution_factory: (
            Callable[[ServiceRunBinding], EvolutionClientProtocol] | None
        ) = None,
    ) -> None:
        self._store = store
        self._authority_reader = authority_reader
        self._owner = owner
        self._services = services
        self._native_runner = native_runner
        self._evolution_factory = evolution_factory or (
            lambda binding: EvolutionHttpClient(
                binding.evolution_backend_url,
                headers=binding.request_headers(),
            )
        )

    def seed_project(
        self,
        request: ScienceSuccessorRecoveryProjectSeedRequestV1,
    ) -> ScienceSuccessorRecoveryProjectSeedAuthorityV1:
        try:
            request = ScienceSuccessorRecoveryProjectSeedRequestV1.model_validate(
                request.model_dump(mode="python")
            )
            create_request = self._store.get_create_request(request.recovery_id)
            source = self._authority_reader.read_exhausted_successor_authority(
                create_request.source,
                create_request.plan,
            )
            recovery = self._store.get_recovery(request.recovery_id)
            if (
                recovery.record_sha256 != request.recovery_record_sha256
                or recovery.record.state != "all_targets_completed_awaiting_commit"
            ):
                raise ScienceSuccessorRecoveryV1Error(
                    "recovery project seed requires all target results"
                )
            results: list[ScienceSuccessorRecoveryTargetResultV1] = []
            for target_id in recovery.record.target_authorization_order:
                operation = self._store.get_target_operation(
                    request.recovery_id,
                    target_id,
                )
                if operation.state != "succeeded" or operation.result is None:
                    raise ScienceSuccessorRecoveryV1Error(
                        "recovery project seed target authority is incomplete"
                    )
                results.append(operation.result)
            model = self._native_runner.codex_model_for_target(
                source=source,
                plan=create_request.plan,
            )
            _snapshot, lease = self._services.ensure_run_binding(
                ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
                codex_model=model,
                runtime_image=MANAGED_RUNTIME_IMAGES["managed_science"],
            )
            binding = getattr(lease, "binding", None)
            if lease is None or type(binding) is not ServiceRunBinding:
                close_lease = getattr(lease, "close", None)
                if callable(close_lease):
                    close_lease()
                raise ScienceSuccessorRecoveryV1Error(
                    "recovery project seed lacks a managed service binding"
                )
            client: EvolutionClientProtocol | None = None
            primary_error = False
            try:
                client = self._evolution_factory(binding)
                return self._owner.seed_successor_recovery_project(
                    request,
                    source=source,
                    recovery=recovery,
                    results=tuple(results),
                    binding=binding,
                    evolution=client,
                )
            except BaseException:
                primary_error = True
                raise
            finally:
                _close_recovery_seed_resources(
                    client,
                    lease,
                    suppress_errors=primary_error,
                )
        except ScienceSuccessorRecoveryV1Error:
            raise
        except Exception as exc:
            raise ScienceSuccessorRecoveryV1Error(
                _recovery_project_seed_failure_detail(exc)
            ) from exc


@dataclass(slots=True)
class _ActiveReadinessGate:
    preflight: ScienceSuccessorRecoveryTargetPreflightV1
    readiness: ScienceSuccessorRecoveryTargetReadinessV1
    binding: ServiceRunBinding
    lease: object
    client: EvolutionClientProtocol

    def close(self) -> None:
        try:
            close_client = getattr(self.client, "close", None)
            if callable(close_client):
                close_client()
        finally:
            close_lease = getattr(self.lease, "close", None)
            if callable(close_lease):
                close_lease()


class ProductionManagedReflectorReadinessProviderV1:
    """Ensure and read the current worker through Core's private binding.

    A freshly started Core intentionally cannot recover the prior process-local
    Codex credential authority.  The readiness endpoint therefore performs the
    same managed-runtime verification used by candidate and successor
    execution before it asks for a binding.  This is still a no-model probe: it
    may launch the managed service group and its Docker mount adoption canary,
    but it never persists an evolution intent or starts Codex.
    """

    def __init__(
        self,
        *,
        services: ScienceSuccessorRecoveryServiceControlV1,
        daemon_release_identity: str,
        release_install_digest: str,
        codex_model: str = MANAGED_CODEX_DEFAULT_MODEL,
        runtime_image: str = MANAGED_RUNTIME_IMAGES["managed_science"],
        total_timeout_seconds: float = 120.0,
        evolution_factory: (Callable[[ServiceRunBinding], EvolutionClientProtocol] | None) = None,
    ) -> None:
        if not codex_model or not codex_model.strip():
            raise ValueError("managed reflector readiness requires a Codex model")
        if runtime_image != MANAGED_RUNTIME_IMAGES["managed_science"]:
            raise ValueError("managed reflector readiness requires managed_science")
        if (
            isinstance(total_timeout_seconds, bool)
            or not isinstance(total_timeout_seconds, (int, float))
            or not 30.0 <= float(total_timeout_seconds) <= 600.0
        ):
            raise ValueError(
                "managed reflector readiness timeout must be between 30 and 600 seconds"
            )
        self._services = services
        self._daemon_release_identity = _require_sha256(
            daemon_release_identity,
            "daemon release identity",
        )
        self._release_install_digest = _require_sha256(
            release_install_digest,
            "managed service release install digest",
        )
        self._codex_model = codex_model.strip()
        self._runtime_image = runtime_image
        self._total_timeout_seconds = float(total_timeout_seconds)
        self._evolution_factory = evolution_factory or (
            lambda binding: EvolutionHttpClient(
                binding.evolution_backend_url,
                headers=binding.request_headers(),
            )
        )

    def read_managed_reflector_readiness(
        self,
    ) -> ManagedReflectorReadinessResponseV1:
        try:
            try:
                current = self._services.adopt_current_run_binding(
                    ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
                    codex_model=self._codex_model,
                    runtime_image=self._runtime_image,
                    total_timeout=self._total_timeout_seconds,
                )
            except SupervisorStateError:
                # A Core restart intentionally invalidates process-local
                # adoption authority.  The typed empty-owner state may still
                # be replaced through the ordinary verified ensure path.
                current = None
            if current is None:
                snapshot, lease = self._services.ensure_run_binding(
                    ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
                    codex_model=self._codex_model,
                    runtime_image=self._runtime_image,
                    total_timeout=self._total_timeout_seconds,
                )
            else:
                snapshot, lease = current
        except Exception as exc:
            raise ScienceSuccessorRecoveryV1Error(
                "managed reflector readiness could not acquire a verified service binding"
            ) from exc
        binding = getattr(lease, "binding", None)
        if (
            lease is None
            or type(binding) is not ServiceRunBinding
            or getattr(snapshot, "run_ready", False) is not True
        ):
            if lease is not None:
                close = getattr(lease, "close", None)
                if callable(close):
                    close()
            try:
                service_error_code = snapshot.service("gateway").error_code
            except (AttributeError, KeyError):
                service_error_code = None
            error = ScienceSuccessorRecoveryV1Error(
                "managed reflector readiness lacks a verified service binding"
            )
            error.code = managed_candidate_readiness_failure_code(service_error_code)
            raise error
        try:
            client: EvolutionClientProtocol | None = None
            try:
                client = self._evolution_factory(binding)
                mount, runtime_readiness = _read_current_worker_readiness(
                    binding=binding,
                    client=client,
                )
            finally:
                try:
                    if client is not None:
                        close = getattr(client, "close", None)
                        if callable(close):
                            close()
                finally:
                    lease.close()
        except ScienceSuccessorRecoveryV1Error:
            raise
        except Exception as exc:
            raise ScienceSuccessorRecoveryV1Error(
                "managed reflector readiness could not verify the registered worker"
            ) from exc
        if mount.release_install_digest != self._release_install_digest:
            raise ScienceSuccessorRecoveryV1Error(
                "current managed reflector install identity changed"
            )
        mount_daemon_release = getattr(mount, "daemon_release_identity", None)
        if mount_daemon_release != self._daemon_release_identity:
            raise ScienceSuccessorRecoveryV1Error(
                "current managed reflector daemon release identity changed"
            )
        execution = RecoveryExecutionIdentityV1(
            core_generation=binding.generation_digest,
            daemon_release_identity=self._daemon_release_identity,
            release_install_digest=self._release_install_digest,
            executable_registry_digest=binding.registry_digest,
            framework_lock_digest=binding.framework_lock_digest,
            runtime_identity_digest=binding.runtime_identity_digest,
            runtime_image_digest=(
                managed_runtime_release_authority_digest(
                    profile="managed_science",
                    image=binding.runtime_image_immutable_reference,
                )
            ),
            service_identity_id="core-reference-worker",
            runtime_profile="managed_science",
            max_reflector_model_calls=1,
        )
        return ManagedReflectorReadinessResponseV1(
            execution_identity=execution,
            managed_reflector_credential_mount=mount,
            managed_reflector_runtime=runtime_readiness,
            codex_cli_started=True,
            model_started=False,
        )


def _completed_operation_is_bound_to_prior_record(
    *,
    record: ScienceSuccessorRecoveryRecordV1,
    target_id: str,
    operation: ScienceSuccessorRecoveryTargetOperationV1,
) -> bool:
    """Accept local results or the exact sealed authority carried by ``record``.

    A completed target can survive more than one append-only supersession.  Its
    immutable intent and result continue to name the recovery that originally
    performed the paid side effect; rewriting those identifiers would destroy
    the evidence chain.  A non-local operation is therefore valid only when the
    immediately prior record already sealed the exact same carried authority.
    """

    result = operation.result
    if result is None:
        return False
    if (
        operation.intent.recovery_id == record.recovery_id
        and result.recovery_id == record.recovery_id
    ):
        return True
    supersession = record.supersession_authority
    if supersession is None:
        return False
    authority = next(
        (
            item
            for item in supersession.carried_completed_targets
            if item.prior_result.target_id == target_id
        ),
        None,
    )
    return bool(
        authority is not None
        and authority.prior_intent == operation.intent
        and authority.prior_result == result
        and authority.prior_intent_sha256 == canonical_digest(operation.intent)
        and authority.prior_result_sha256 == canonical_digest(result)
    )


class ProductionScienceSuccessorRecoveryExecutorV1:
    """Hold one current-worker readiness gate across intent and native job."""

    def __init__(
        self,
        *,
        services: ScienceSuccessorRecoveryServiceControlV1,
        native_runner: ScienceSuccessorRecoveryNativeTargetRunnerV1,
        daemon_release_identity: str,
        release_install_digest: str,
        runtime_image: str = MANAGED_RUNTIME_IMAGES["managed_science"],
        evolution_factory: (Callable[[ServiceRunBinding], EvolutionClientProtocol] | None) = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if runtime_image != MANAGED_RUNTIME_IMAGES["managed_science"]:
            raise ValueError("recovery executor requires the managed_science image")
        self._services = services
        self._native_runner = native_runner
        self._daemon_release_identity = _require_sha256(
            daemon_release_identity,
            "daemon release identity",
        )
        self._release_install_digest = _require_sha256(
            release_install_digest,
            "managed service release install digest",
        )
        self._runtime_image = runtime_image
        self._evolution_factory = evolution_factory or (
            lambda binding: EvolutionHttpClient(
                binding.evolution_backend_url,
                headers=binding.request_headers(),
            )
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._active: dict[str, _ActiveReadinessGate] = {}

    def check_target_readiness(
        self,
        preflight: ScienceSuccessorRecoveryTargetPreflightV1,
        *,
        source: ExhaustedSuccessorRecoveryAuthorityV1,
        plan: ScienceSuccessorPlanV2,
    ) -> ScienceSuccessorRecoveryTargetReadinessV1:
        preflight = ScienceSuccessorRecoveryTargetPreflightV1.model_validate(
            preflight.model_dump(mode="python")
        )
        with self._lock:
            existing = self._active.get(preflight.operation_id)
            if existing is not None:
                if existing.preflight != preflight:
                    raise ScienceSuccessorRecoveryV1Error(
                        "recovery readiness operation identity changed"
                    )
                return existing.readiness
            if self._active:
                raise ScienceSuccessorRecoveryV1Error(
                    "another recovery target owns the managed service lease"
                )
            codex_model = self._native_runner.codex_model_for_target(
                source=source,
                plan=plan,
            )
            if not isinstance(codex_model, str) or not codex_model:
                raise ScienceSuccessorRecoveryV1Error(
                    "recovery target does not own a canonical Codex model"
                )
            snapshot, lease = self._services.ensure_run_binding(
                ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
                codex_model=codex_model,
                runtime_image=self._runtime_image,
            )
            binding = getattr(lease, "binding", None)
            if (
                lease is None
                or type(binding) is not ServiceRunBinding
                or getattr(snapshot, "run_ready", False) is not True
            ):
                if lease is not None:
                    close = getattr(lease, "close", None)
                    if callable(close):
                        close()
                raise ScienceSuccessorRecoveryV1Error(
                    "managed recovery service binding is not ready"
                )
            client: EvolutionClientProtocol | None = None
            try:
                self._validate_binding(preflight, binding)
                client = self._evolution_factory(binding)
                readiness = self._read_current_worker_readiness(
                    preflight,
                    binding=binding,
                    client=client,
                )
                self._active[preflight.operation_id] = _ActiveReadinessGate(
                    preflight=preflight,
                    readiness=readiness,
                    binding=binding,
                    lease=lease,
                    client=client,
                )
                return readiness
            except Exception:
                if client is not None:
                    close_client = getattr(client, "close", None)
                    if callable(close_client):
                        close_client()
                close_lease = getattr(lease, "close", None)
                if callable(close_lease):
                    close_lease()
                raise

    def release_target_readiness(
        self,
        preflight: ScienceSuccessorRecoveryTargetPreflightV1,
    ) -> None:
        with self._lock:
            gate = self._active.pop(preflight.operation_id, None)
        if gate is not None:
            if gate.preflight != preflight:
                gate.close()
                raise ScienceSuccessorRecoveryV1Error(
                    "recovery readiness release identity changed"
                )
            gate.close()

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
    ) -> ScienceSuccessorRecoverySupersessionAuthorityV1:
        """Prove either an empty inventory or one safely adoptable paid job."""

        record = prior.record
        target = next(
            (
                item
                for item in record.targets
                if item.operation_id == failure.operation_id
            ),
            None,
        )
        if (
            record.state != "failed"
            or record.source_transition_id != source.successor_transition_id
            or record.source_authority_sha256
            != science_successor_recovery_source_sha256(source)
            or target is None
            or target.state != "failed"
            or target.failure_sha256 != canonical_digest(failure)
            or failure.recovery_id != record.recovery_id
            or failure.target_id != target.target_id
        ):
            raise ScienceSuccessorRecoveryV1Error(
                "failed recovery cannot authorize an append-only supersession"
            )
        if len(completed_operations) != len(record.completed_target_ids):
            raise ScienceSuccessorRecoveryV1Error(
                "recovery supersession completed target inventory is incomplete"
            )
        carried_completed_targets: list[
            ScienceSuccessorRecoveryCarriedTargetAuthorityV1
        ] = []
        for target_id, operation in zip(
            record.completed_target_ids,
            completed_operations,
            strict=True,
        ):
            target_state = next(
                item for item in record.targets if item.target_id == target_id
            )
            if (
                operation.state != "succeeded"
                or operation.result is None
                or not _completed_operation_is_bound_to_prior_record(
                    record=record,
                    target_id=target_id,
                    operation=operation,
                )
                or operation.intent.target.target_id != target_id
                or operation.result.target_id != target_id
                or target_state.operation_id != operation.intent.operation_id
                or target_state.result_sha256 != canonical_digest(operation.result)
            ):
                raise ScienceSuccessorRecoveryV1Error(
                    "recovery supersession completed target evidence is invalid"
                )
            carried_payload = {
                "successor_recovery_carried_target_authority_contract_version": "1",
                "prior_recovery_id": operation.intent.recovery_id,
                "prior_intent": operation.intent,
                "prior_result": operation.result,
                "prior_intent_sha256": canonical_digest(operation.intent),
                "prior_result_sha256": canonical_digest(operation.result),
            }
            if operation.intent.recovery_id != record.recovery_id:
                carried_payload["carried_from_recovery_id"] = record.recovery_id
            carried_payload["content_sha256"] = canonical_digest(carried_payload)
            carried_completed_targets.append(
                ScienceSuccessorRecoveryCarriedTargetAuthorityV1.model_validate(
                    carried_payload
                )
            )
        codex_model = self._native_runner.codex_model_for_target(
            source=source,
            plan=plan,
        )
        _snapshot, lease = self._services.ensure_run_binding(
            ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
            codex_model=codex_model,
            runtime_image=self._runtime_image,
        )
        binding = getattr(lease, "binding", None)
        if lease is None or type(binding) is not ServiceRunBinding:
            if lease is not None:
                close = getattr(lease, "close", None)
                if callable(close):
                    close()
            raise ScienceSuccessorRecoveryV1Error(
                "recovery supersession lacks a managed service binding"
            )
        client: EvolutionClientProtocol | None = None
        try:
            self._validate_execution_identity(execution_identity, binding)
            client = self._evolution_factory(binding)
            inventory = _validate_strict_wire_model(
                SuccessorTransitionJobInventoryResponse,
                client.get_internal_successor_transition_job_inventory(
                    record.recovery_id
                ),
            )
            if inventory.successor_transition_id != record.recovery_id:
                raise ScienceSuccessorRecoveryV1Error(
                    "failed recovery Evolution inventory identity changed"
                )
            carried_local_job_ids: list[str] = []
            for carried in carried_completed_targets:
                result = carried.prior_result
                job_owner = (
                    result.job_owner_successor_transition_id or result.recovery_id
                )
                terminal = client.get_internal_job_result(result.job_id)
                outputs = terminal.get("outputs") if isinstance(terminal, Mapping) else None
                selected = (
                    []
                    if not isinstance(outputs, list)
                    else [
                        item
                        for item in outputs
                        if isinstance(item, dict)
                        and item.get("artifact_id") == result.registry_artifact_id
                        and item.get("type") == result.output.artifact_type
                    ]
                )
                artifact = ArtifactResponse.model_validate(
                    client.get_internal_successor_artifact(
                        job_owner,
                        result.registry_artifact_id,
                    )
                )
                reservation = ReflectorInferenceReservationReceipt.model_validate(
                    client.get_internal_reflector_inference_reservation(result.job_id)
                )
                if (
                    not isinstance(terminal, Mapping)
                    or terminal.get("job_id") != result.job_id
                    or terminal.get("state") != "succeeded"
                    or terminal.get("error") is not None
                    or terminal.get("successor_transition_id") != job_owner
                    or len(selected) != 1
                    or selected[0].get("payload_manifest_digest")
                    != result.registry_manifest_sha256
                    or artifact.artifact_id != result.registry_artifact_id
                    or artifact.type.value != result.output.artifact_type
                    or artifact.state is not ArtifactState.SEALED
                    or artifact.promoted is not True
                    or canonical_digest(artifact.manifest)
                    != result.registry_artifact_manifest_sha256
                    or reservation.content_sha256
                    != result.reflector_inference_reservation.content_sha256
                    or reservation.state != "completed"
                    or reservation.model_calls_started != 1
                ):
                    raise ScienceSuccessorRecoveryV1Error(
                        "recovery supersession carried target authority changed"
                    )
                if job_owner == record.recovery_id:
                    carried_local_job_ids.append(result.job_id)
            if any(item not in inventory.job_ids for item in carried_local_job_ids):
                raise ScienceSuccessorRecoveryV1Error(
                    "failed recovery inventory omitted a completed target job"
                )
            residual_job_ids = tuple(
                item for item in inventory.job_ids if item not in carried_local_job_ids
            )
            adopted_job = None
            carried_adopted_job_authority_sha256 = None
            if residual_job_ids:
                if (
                    len(residual_job_ids) != 1
                    or failure.failure_code != "post_intent_execution_failed"
                ):
                    raise ScienceSuccessorRecoveryV1Error(
                        "failed recovery owns a non-adoptable paid Evolution side effect"
                    )
                job_id = residual_job_ids[0]
                terminal = client.get_internal_job_result(job_id)
                expected_target = next(
                    (
                        item
                        for item in plan.enabled_methods
                        if item.target_id == failure.target_id
                    ),
                    None,
                )
                outputs = terminal.get("outputs") if isinstance(terminal, Mapping) else None
                selected = (
                    []
                    if expected_target is None or not isinstance(outputs, list)
                    else [
                        item
                        for item in outputs
                        if isinstance(item, dict)
                        and item.get("type") == expected_target.output_artifact_type
                    ]
                )
                if (
                    not isinstance(terminal, Mapping)
                    or terminal.get("job_id") != job_id
                    or terminal.get("state") != "succeeded"
                    or terminal.get("error") is not None
                    or terminal.get("successor_transition_id") != record.recovery_id
                    or expected_target is None
                    or len(selected) != 1
                    or selected[0].get("promoted") is not False
                ):
                    raise ScienceSuccessorRecoveryV1Error(
                        "failed recovery paid job lacks a unique succeeded proposal"
                    )
                selected_output = selected[0]
                proposal_id = selected_output.get("artifact_id")
                payload_sha256 = selected_output.get("payload_manifest_digest")
                payload_size = selected_output.get("payload_byte_size")
                if (
                    not isinstance(proposal_id, str)
                    or not isinstance(payload_sha256, str)
                    or not isinstance(payload_size, int)
                    or isinstance(payload_size, bool)
                ):
                    raise ScienceSuccessorRecoveryV1Error(
                        "failed recovery paid proposal identity is incomplete"
                    )
                proposal = ArtifactResponse.model_validate(
                    client.get_internal_successor_artifact(
                        record.recovery_id,
                        proposal_id,
                    )
                )
                reservation = ReflectorInferenceReservationReceipt.model_validate(
                    client.get_internal_reflector_inference_reservation(job_id)
                )
                if (
                    proposal.type.value != expected_target.output_artifact_type
                    or proposal.state is not ArtifactState.SEALED
                    or proposal.promoted is not False
                    or proposal.manifest != selected_output.get("manifest")
                    or reservation.job_id != job_id
                    or reservation.state != "completed"
                    or reservation.model_calls_started != 1
                ):
                    raise ScienceSuccessorRecoveryV1Error(
                        "failed recovery paid proposal authority is invalid"
                    )
                adopted_payload = {
                    "successor_recovery_adopted_job_authority_contract_version": "1",
                    "prior_recovery_id": record.recovery_id,
                    "prior_operation_id": failure.operation_id,
                    "source_transition_id": record.source_transition_id,
                    "job_owner_successor_transition_id": record.recovery_id,
                    "target_id": expected_target.target_id,
                    "method_id": expected_target.method_id,
                    "artifact_type": expected_target.output_artifact_type,
                    "job_id": job_id,
                    "job_state": "succeeded",
                    "job_result_sha256": (
                        science_successor_recovery_adoptable_job_result_sha256(
                            terminal
                        )
                    ),
                    "proposal_artifact_id": proposal_id,
                    "proposal_payload_manifest_sha256": payload_sha256,
                    "proposal_payload_byte_size": payload_size,
                    "proposal_registry_manifest_sha256": canonical_digest(
                        proposal.manifest
                    ),
                    "proposal_promoted": False,
                    "admission_decision_absent": True,
                    "inference_reservation_sha256": reservation.content_sha256,
                    "reflector_model_calls_started": 1,
                    "created_at": self._clock().astimezone(UTC).isoformat().replace(
                        "+00:00", "Z"
                    ),
                }
                adopted_payload["content_sha256"] = canonical_digest(adopted_payload)
                adopted_job = ScienceSuccessorRecoveryAdoptedJobAuthorityV1.model_validate(
                    adopted_payload
                )
            elif (
                record.supersession_authority is not None
                and record.supersession_authority.adopted_job is not None
            ):
                prior_adopted = record.supersession_authority.adopted_job
                expected_target = next(
                    (
                        item
                        for item in plan.enabled_methods
                        if item.target_id == failure.target_id
                    ),
                    None,
                )
                terminal = client.get_internal_job_result(prior_adopted.job_id)
                outputs = terminal.get("outputs") if isinstance(terminal, Mapping) else None
                selected = (
                    []
                    if expected_target is None or not isinstance(outputs, list)
                    else [
                        item
                        for item in outputs
                        if isinstance(item, dict)
                        and item.get("type") == expected_target.output_artifact_type
                    ]
                )
                proposal = ArtifactResponse.model_validate(
                    client.get_internal_successor_artifact(
                        prior_adopted.job_owner_successor_transition_id,
                        prior_adopted.proposal_artifact_id,
                    )
                )
                reservation = ReflectorInferenceReservationReceipt.model_validate(
                    client.get_internal_reflector_inference_reservation(
                        prior_adopted.job_id
                    )
                )
                if (
                    expected_target is None
                    or prior_adopted.target_id != failure.target_id
                    or prior_adopted.method_id != expected_target.method_id
                    or prior_adopted.artifact_type
                    != expected_target.output_artifact_type
                    or not isinstance(terminal, Mapping)
                    or terminal.get("job_id") != prior_adopted.job_id
                    or terminal.get("state") != "succeeded"
                    or terminal.get("error") is not None
                    or terminal.get("successor_transition_id")
                    != prior_adopted.job_owner_successor_transition_id
                    or science_successor_recovery_adoptable_job_result_sha256(
                        terminal
                    )
                    != prior_adopted.job_result_sha256
                    or len(selected) != 1
                    or selected[0].get("artifact_id")
                    != prior_adopted.proposal_artifact_id
                    or selected[0].get("payload_manifest_digest")
                    != prior_adopted.proposal_payload_manifest_sha256
                    or selected[0].get("payload_byte_size")
                    != prior_adopted.proposal_payload_byte_size
                    or selected[0].get("promoted") is not False
                    or proposal.artifact_id != prior_adopted.proposal_artifact_id
                    or proposal.type.value != prior_adopted.artifact_type
                    or proposal.state is not ArtifactState.SEALED
                    or proposal.promoted is not False
                    or canonical_digest(proposal.manifest)
                    != prior_adopted.proposal_registry_manifest_sha256
                    or proposal.manifest != selected[0].get("manifest")
                    or reservation.job_id != prior_adopted.job_id
                    or reservation.state != "completed"
                    or reservation.model_calls_started != 1
                    or reservation.content_sha256
                    != prior_adopted.inference_reservation_sha256
                ):
                    raise ScienceSuccessorRecoveryV1Error(
                        "failed recovery carried paid proposal authority is invalid"
                    )
                adopted_job = prior_adopted
                carried_adopted_job_authority_sha256 = (
                    prior_adopted.content_sha256
                )
            payload = {
                "successor_recovery_supersession_authority_contract_version": "1",
                "prior_recovery_id": record.recovery_id,
                "prior_recovery_record_sha256": prior.record_sha256,
                "prior_operation_id": failure.operation_id,
                "prior_failure_sha256": canonical_digest(failure),
                "source_transition_id": record.source_transition_id,
                "evolution_job_ids": inventory.job_ids,
                "evolution_job_inventory_sha256": inventory.content_sha256,
                "paid_model_side_effect_absent": adopted_job is None,
                "core_generation": execution_identity.core_generation,
                "daemon_release_identity": (
                    execution_identity.daemon_release_identity
                ),
                "created_at": self._clock().astimezone(UTC).isoformat().replace(
                    "+00:00", "Z"
                ),
            }
            if adopted_job is not None:
                payload["adopted_job"] = adopted_job
            if carried_completed_targets:
                payload["carried_completed_targets"] = tuple(
                    carried_completed_targets
                )
            if carried_adopted_job_authority_sha256 is not None:
                payload["carried_adopted_job_authority_sha256"] = (
                    carried_adopted_job_authority_sha256
                )
            payload["content_sha256"] = canonical_digest(payload)
            return ScienceSuccessorRecoverySupersessionAuthorityV1.model_validate(
                payload
            )
        finally:
            if client is not None:
                close_client = getattr(client, "close", None)
                if callable(close_client):
                    close_client()
            close_lease = getattr(lease, "close", None)
            if callable(close_lease):
                close_lease()

    def execute_target(
        self,
        intent: ScienceSuccessorRecoveryTargetIntentV1,
        *,
        source: ExhaustedSuccessorRecoveryAuthorityV1,
        plan: ScienceSuccessorPlanV2,
    ) -> ScienceSuccessorRecoveryTargetResultV1:
        gate = self._gate_for_intent(intent)
        return self._native_runner.execute_native_target(
            intent,
            source=source,
            plan=plan,
            binding=gate.binding,
            client=gate.client,
        )

    def recover_target(
        self,
        intent: ScienceSuccessorRecoveryTargetIntentV1,
        *,
        source: ExhaustedSuccessorRecoveryAuthorityV1,
        plan: ScienceSuccessorPlanV2,
    ) -> ScienceSuccessorRecoveryTargetResultV1 | ScienceSuccessorRecoveryTargetFailureV1 | None:
        gate = self._gate_for_intent(intent)
        return self._native_runner.recover_native_target(
            intent,
            source=source,
            plan=plan,
            binding=gate.binding,
            client=gate.client,
        )

    def adopt_target(
        self,
        intent: ScienceSuccessorRecoveryTargetIntentV1,
        *,
        source: ExhaustedSuccessorRecoveryAuthorityV1,
        plan: ScienceSuccessorPlanV2,
        adopted_job: ScienceSuccessorRecoveryAdoptedJobAuthorityV1,
    ) -> ScienceSuccessorRecoveryTargetResultV1:
        gate = self._gate_for_intent(intent)
        if (
            adopted_job.target_id != intent.target.target_id
            or adopted_job.method_id != intent.target.method_id
            or adopted_job.artifact_type != intent.target.output_artifact_type
        ):
            raise ScienceSuccessorRecoveryV1Error(
                "adopted paid job differs from the authorized recovery target"
            )
        return self._native_runner.adopt_native_target(
            intent,
            source=source,
            plan=plan,
            adopted_job=adopted_job,
            binding=gate.binding,
            client=gate.client,
        )

    def _gate_for_intent(
        self,
        intent: ScienceSuccessorRecoveryTargetIntentV1,
    ) -> _ActiveReadinessGate:
        with self._lock:
            gate = self._active.get(intent.operation_id)
            if gate is None:
                raise ScienceSuccessorRecoveryV1Error(
                    "native recovery target lacks its pre-intent readiness lease"
                )
            preflight = gate.preflight
            if (
                intent.recovery_id != preflight.recovery_id
                or intent.source_authority_sha256 != preflight.source_authority_sha256
                or intent.execution_identity != preflight.execution_identity
                or intent.target != preflight.target
            ):
                raise ScienceSuccessorRecoveryV1Error(
                    "native recovery target differs from its readiness lease"
                )
            return gate

    def _validate_binding(
        self,
        preflight: ScienceSuccessorRecoveryTargetPreflightV1,
        binding: ServiceRunBinding,
    ) -> None:
        self._validate_execution_identity(preflight.execution_identity, binding)

    def _validate_execution_identity(
        self,
        identity: RecoveryExecutionIdentityV1,
        binding: ServiceRunBinding,
    ) -> None:
        immutable_digest = managed_runtime_release_authority_digest(
            profile="managed_science",
            image=binding.runtime_image_immutable_reference,
        )
        if (
            binding.generation_digest != identity.core_generation
            or identity.daemon_release_identity != self._daemon_release_identity
            or identity.release_install_digest != self._release_install_digest
            or binding.registry_digest != identity.executable_registry_digest
            or binding.framework_lock_digest != identity.framework_lock_digest
            or binding.runtime_identity_digest != identity.runtime_identity_digest
            or immutable_digest != identity.runtime_image_digest
            or binding.runtime_image != MANAGED_RUNTIME_IMAGES["managed_science"]
            or identity.service_identity_id != "core-reference-worker"
        ):
            raise ScienceSuccessorRecoveryV1Error(
                "current managed service binding differs from recovery execution identity"
            )

    def _read_current_worker_readiness(
        self,
        preflight: ScienceSuccessorRecoveryTargetPreflightV1,
        *,
        binding: ServiceRunBinding,
        client: EvolutionClientProtocol,
    ) -> ScienceSuccessorRecoveryTargetReadinessV1:
        receipt, runtime_readiness = _read_current_worker_readiness(
            binding=binding,
            client=client,
        )
        identity = preflight.execution_identity
        if (
            receipt.generation_digest != identity.core_generation
            or receipt.daemon_release_identity != identity.daemon_release_identity
            or receipt.release_install_digest != identity.release_install_digest
            or receipt.release_registry_digest != identity.executable_registry_digest
            or receipt.runtime_profile != identity.runtime_profile
            or receipt.runtime_digest != identity.runtime_image_digest
            or receipt.codex_cli_started is not False
            or receipt.model_started is not False
            or runtime_readiness.generation_digest != identity.core_generation
            or runtime_readiness.daemon_release_identity
            != identity.daemon_release_identity
            or runtime_readiness.release_install_digest
            != identity.release_install_digest
            or runtime_readiness.release_registry_digest
            != identity.executable_registry_digest
            or runtime_readiness.runtime_profile != identity.runtime_profile
            or runtime_readiness.runtime_digest != identity.runtime_image_digest
            or runtime_readiness.credential_mount_content_sha256
            != receipt.content_sha256
            or runtime_readiness.codex_cli_started is not True
            or runtime_readiness.model_started is not False
        ):
            raise ScienceSuccessorRecoveryV1Error(
                "current managed reflector credential mount identity changed"
            )
        return ScienceSuccessorRecoveryTargetReadinessV1(
            operation_id=preflight.operation_id,
            recovery_id=preflight.recovery_id,
            target_id=preflight.target.target_id,
            execution_identity=identity,
            framework_lock_digest=binding.framework_lock_digest,
            runtime_identity_digest=binding.runtime_identity_digest,
            managed_reflector_credential_mount=receipt,
            managed_reflector_runtime=runtime_readiness,
            checked_at=self._clock().astimezone(UTC).isoformat().replace("+00:00", "Z"),
        )


def _require_sha256(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _feedback_binding_from_authorities(
    *,
    context: ScienceSuccessorPreparationContextV2,
    base_dataset,
    completed: CompletedDatasetAuthority,
    attachments: tuple[TrainingFeedbackAttachment, ...],
    resolved_artifact: ArtifactResponse,
) -> TrainingFeedbackBindingV2:
    if (
        not attachments
        or completed.completed_dataset_id != base_dataset.dataset_id
        or completed.dataset_artifact_id != base_dataset.artifact_id
        or completed.dataset_manifest_sha256 != base_dataset.manifest_sha256
        or any(
            item.dataset_id != base_dataset.dataset_id
            or item.dataset_revision != completed.completed_dataset_revision
            or item.task_id != completed.task_id
            or item.task_scope_id != context.task.task_id
            or item.authority.value != "evaluator_only"
            or item.status != "sealed"
            or item.source_session_result_sha256 != completed.source_session_result_sha256
            for item in attachments
        )
    ):
        raise ScienceSuccessorRecoveryV1Error(
            "feedback authorities do not match the sealed recovery dataset"
        )
    manifest = resolved_artifact.manifest
    overlay = [item.task_local_feedback for item in attachments if item.task_local_feedback]
    view = EvolutionDatasetViewReceipt(
        resolution_id=str(manifest.get("create_identity") or ""),
        source_dataset_id=str(manifest.get("derived_from_dataset_id") or ""),
        source_dataset_manifest_sha256=str(manifest.get("source_dataset_manifest_sha256") or ""),
        source_session_result_sha256=str(manifest.get("source_session_result_sha256") or ""),
        attachment_ids=tuple(manifest.get("training_feedback_attachment_ids") or ()),
        attachment_sha256=tuple(manifest.get("training_feedback_attachment_sha256") or ()),
        records_sha256=str(manifest.get("records_sha256") or ""),
        manifest_sha256=canonical_digest(manifest),
        task_local_overlay_sha256=(canonical_digest(overlay) if overlay else None),
    )
    receipt_body = {
        "completed_dataset_id": completed.completed_dataset_id,
        "completed_dataset_revision": completed.completed_dataset_revision,
        "attachment_ids": [item.attachment_id for item in attachments],
        "attachment_sha256": [item.content_sha256 for item in attachments],
        "dataset_view": view.model_dump(mode="json"),
        "dataset_artifact_id": resolved_artifact.artifact_id,
    }
    resolved = ResolvedEvolutionDatasetView(
        completed_dataset_id=completed.completed_dataset_id,
        completed_dataset_revision=completed.completed_dataset_revision,
        attachments=attachments,
        dataset_view=view,
        dataset_artifact=resolved_artifact,
        resolved_view_sha256=canonical_digest(receipt_body),
    )
    if (
        resolved.dataset_view.source_dataset_id != base_dataset.dataset_id
        or resolved.dataset_view.source_session_result_sha256
        != completed.source_session_result_sha256
        or any(
            item.source_dataset_manifest_sha256
            != resolved.dataset_view.source_dataset_manifest_sha256
            for item in attachments
        )
        or resolved.dataset_view.attachment_ids
        != tuple(item.attachment_id for item in attachments)
        or resolved.dataset_view.attachment_sha256
        != tuple(item.content_sha256 for item in attachments)
    ):
        raise ScienceSuccessorRecoveryV1Error(
            "resolved feedback view does not match the sealed recovery dataset"
        )
    return TrainingFeedbackBindingV2(
        completed_dataset_id=completed.completed_dataset_id,
        completed_dataset_revision=completed.completed_dataset_revision,
        attachment_ids=tuple(item.attachment_id for item in attachments),
        attachment_sha256=tuple(item.content_sha256 for item in attachments),
        resolved_dataset_artifact_id=resolved.dataset_artifact.artifact_id,
        resolved_view_sha256=resolved.resolved_view_sha256,
        task_scope_id=context.task.task_id,
    )


def _read_current_worker_mount(
    *,
    binding: ServiceRunBinding,
    client: EvolutionClientProtocol,
) -> ManagedReflectorCredentialMountReadiness:
    mount, _runtime = _read_current_worker_readiness(binding=binding, client=client)
    return mount


def _read_current_worker_readiness(
    *,
    binding: ServiceRunBinding,
    client: EvolutionClientProtocol,
) -> tuple[ManagedReflectorCredentialMountReadiness, ManagedReflectorRuntimeReadiness]:
    raw = client.get_internal_health()
    if not isinstance(raw, Mapping):
        raise ScienceSuccessorRecoveryV1Error("evolution health did not return a closed object")
    internal = raw.get("internal_identity")
    workers = raw.get("workers")
    expected_internal = {
        "framework_lock_digest": binding.framework_lock_digest,
        "generation_digest": binding.generation_digest,
        "registry_digest": binding.registry_digest,
        "service_id": "evolution-backend",
    }
    if (
        not isinstance(internal, Mapping)
        or any(internal.get(key) != value for key, value in expected_internal.items())
        or not isinstance(workers, list)
    ):
        raise ScienceSuccessorRecoveryV1Error(
            "evolution health identity differs from the current service binding"
        )
    matches = [
        worker
        for worker in workers
        if isinstance(worker, Mapping)
        and worker.get("worker_id") == "core-reference-worker"
        and worker.get("generation_digest") == binding.generation_digest
        and worker.get("registry_digest") == binding.registry_digest
        and worker.get("framework_lock_digest") == binding.framework_lock_digest
    ]
    if len(matches) != 1:
        raise ScienceSuccessorRecoveryV1Error(
            "current managed reflector worker is not uniquely registered"
        )
    receipt = ManagedReflectorCredentialMountReadiness.model_validate(
        matches[0].get("managed_reflector_credential_mount")
    )
    runtime_readiness = ManagedReflectorRuntimeReadiness.model_validate(
        matches[0].get("managed_reflector_runtime_readiness")
    )
    if (
        receipt.generation_digest != binding.generation_digest
        or receipt.release_registry_digest != binding.registry_digest
        or receipt.runtime_profile != "managed_science"
        or receipt.runtime_digest
        != managed_runtime_release_authority_digest(
            profile="managed_science",
            image=binding.runtime_image_immutable_reference,
        )
        or receipt.codex_cli_started is not False
        or receipt.model_started is not False
    ):
        raise ScienceSuccessorRecoveryV1Error(
            "current managed reflector credential mount differs from service binding"
        )
    if (
        runtime_readiness.authority_id != receipt.authority_id
        or runtime_readiness.worker_launch_id != receipt.worker_launch_id
        or runtime_readiness.credential_mount_content_sha256 != receipt.content_sha256
        or runtime_readiness.generation_digest != binding.generation_digest
        or runtime_readiness.release_registry_digest != binding.registry_digest
        or runtime_readiness.runtime_profile != "managed_science"
        or runtime_readiness.runtime_digest
        != managed_runtime_release_authority_digest(
            profile="managed_science",
            image=binding.runtime_image_immutable_reference,
        )
        or runtime_readiness.codex_cli_started is not True
        or runtime_readiness.model_started is not False
        or runtime_readiness.expected_cli_version != MANAGED_CODEX_VERSION
        or runtime_readiness.actual_cli_version != MANAGED_CODEX_VERSION
    ):
        raise ScienceSuccessorRecoveryV1Error(
            "current managed reflector runtime differs from service binding"
        )
    return receipt, runtime_readiness


__all__ = [
    "ProductionManagedReflectorReadinessProviderV1",
    "ProductionScienceSuccessorRecoveryAuthorityReaderV1",
    "ProductionScienceSuccessorRecoveryExecutorV1",
    "ProductionScienceSuccessorRecoveryNativeRunnerV1",
    "ProductionScienceSuccessorRecoveryProjectSeederV1",
    "ScienceSuccessorRecoveryContextProviderV1",
    "ScienceSuccessorRecoveryNativeTargetRunnerV1",
    "ScienceSuccessorRecoveryProjectSeedOwnerV1",
    "ScienceSuccessorRecoveryServiceControlV1",
]
