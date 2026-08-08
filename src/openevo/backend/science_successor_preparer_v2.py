"""Production preparation of one immutable v2 science successor.

This module consumes only the captured Attempt evidence and private Daemon
authorities.  It never derives successor state from a v1 project/run record and
never exposes an Evolution or workspace host path through the v2 contract.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx

from openevo.backend.contracts.v2 import models as m2
from openevo.backend.contracts.v2.store import ProjectRecordV2
from openevo.backend.science_execution_v2 import (
    ScienceAttemptExecutionRecordV2,
    compile_science_evolution_experiment_v2,
)
from openevo.backend.science_successor import (
    AcceptedWorkspaceResultV2,
    ScienceMethodOutputV2,
    ScienceSuccessorCleanupContextV2,
    ScienceSuccessorCleanupReceiptV2,
    ScienceSuccessorPreparationContextV2,
    SealedTranscriptDatasetV2,
    SuccessorMaterializationV2,
    TrainingFeedbackBindingV2,
    ValidatedScienceOutputsV2,
    science_successor_plan_sha256,
)
from openevo.backend.science_successor_recovery_v1 import (
    AgentSystemAuditReceiptV1,
    ExhaustedSuccessorRecoveryAuthorityV1,
    ScienceSuccessorRecoveryAdoptedJobAuthorityV1,
    ScienceSuccessorRecoveryCarriedTargetAuthorityV1,
    ScienceSuccessorRecoveryTargetIntentV1,
    ScienceSuccessorRecoveryTargetResultV1,
    science_successor_recovery_admission_report_sha256,
    science_successor_recovery_adoptable_job_result_sha256,
    science_successor_recovery_failure_id,
    science_successor_recovery_source_sha256,
)
from openevo.backend.service_supervisor import (
    ServiceExecutionMode,
    ServiceRunBinding,
)
from openevo.backend.workspace_handoff_v2 import (
    WorkspaceHandoffStoreV2,
    WorkspaceResultReceiptV2,
)
from openevo.evolution.admission import (
    ArtifactAdmissionEvidence,
    ArtifactContentAdmissionBasis,
    ArtifactProposalDecisionRequest,
    DeterministicNativeArtifactAdmissionPolicy,
    NativeArtifactAdmissionPolicy,
    NativeArtifactAdmissionService,
    NativeArtifactProposalSet,
    ProposalAction,
)
from openevo.evolution.context_materialization import MaterializedContext
from openevo.evolution.context_projection import ContextProjectionResolveRequest
from openevo.evolution.framework import canonical_digest, canonical_json
from openevo.evolution.framework.builtins import (
    VerifiedExecutableRegistry,
    require_verified_executable_registry,
)
from openevo.evolution.framework.execution import (
    ReflectorInferenceBudgetReceipt,
    ReflectorRuntimeReceipt,
)
from openevo.evolution.framework.handlers import RuntimeDestinationRoots
from openevo.evolution.framework.profiles import execution_profile_for_release_mode
from openevo.evolution.methods import (
    TASK_SPECIFIC_LITERAL_KEYS,
    task_specific_report_titles,
)
from openevo.evolution.models import (
    ArtifactResponse,
    ArtifactState,
    ArtifactType,
    DatasetCreateResponse,
    JobCreateResponse,
    JobState,
    ReflectorInferenceReservationReceipt,
    SucceededPlanBoundJobAuthorityResponse,
    SuccessorArtifactAuthorityResponse,
    SuccessorTransitionJobInventoryResponse,
)
from openevo.evolution.planned_jobs import PlanBoundJobCreateRequest
from openevo.evolution.revisions import (
    AtomicEvolutionAbandonManifestV2,
    AtomicHistoricalRestoreManifestV2,
    AtomicSuccessorManifestV2,
    AtomicSuccessorRecoverySeedManifestV1,
    SuccessorArtifactContributionV2,
)
from openevo.evolution.training_feedback import (
    EvolutionDatasetViewResolveRequest,
    ResolvedEvolutionDatasetView,
    TrainingFeedbackAttachment,
    TrainingFeedbackAttachmentCreateRequest,
)
from openevo.experiments.clients import (
    EvolutionClientProtocol,
    EvolutionHttpClient,
    EvolutionHttpStatusError,
)
from openevo.experiments.compiler import (
    CompiledEvolutionMethodSpec,
    CompiledExperiment,
)
from openevo.projects.science.compiler import MANAGED_RUNTIME_IMAGES
from openevo.runtime.managed import (
    MANAGED_WORKSPACE,
    managed_runtime_release_authority_digest,
)

_CONTEXT_ARTIFACT_TYPES = (
    "dataset",
    "text_memory",
    "parametric_memory",
    "skill_bundle",
    "agent_system",
)


def _materialized_context_from_wire(
    payload: Mapping[str, object],
) -> MaterializedContext:
    """Decode one strict materialized-context HTTP response as JSON wire data.

    Evolution's authenticated HTTP client returns a decoded JSON mapping, so
    closed tuple inventories are represented as lists.  Re-entering strict
    Python-mode validation rejects that valid wire representation before its
    content and identity validators run.  Canonically re-encode the bounded
    mapping and use Pydantic's JSON-mode decoder at this transport boundary.
    """

    return MaterializedContext.model_validate_json(
        json.dumps(
            dict(payload),
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _recovery_seed_result_is_authorized(
    *,
    recovery_id: str,
    result: ScienceSuccessorRecoveryTargetResultV1,
    carried_by_target: dict[str, ScienceSuccessorRecoveryCarriedTargetAuthorityV1],
) -> bool:
    if result.recovery_id == recovery_id:
        return True
    authority = carried_by_target.get(result.target_id)
    return authority is not None and authority.prior_result == result


def _recovery_seed_target_inventory_matches(
    expected_targets: tuple[str, ...],
    observed_targets: tuple[str, ...],
) -> bool:
    return (
        len(observed_targets) == len(expected_targets)
        and len(set(observed_targets)) == len(observed_targets)
        and set(observed_targets) == set(expected_targets)
    )


def _recovery_seed_contributions(
    results: tuple[ScienceSuccessorRecoveryTargetResultV1, ...],
) -> tuple[SuccessorArtifactContributionV2, ...]:
    """Project verified recovery outputs as inherited destination context.

    Admission and promotion remain anchored by the immutable target-result
    hashes in the recovery seed manifest.  They are not replayed as a new
    destination admission action: doing so would incorrectly combine an
    inherited contribution with ``admission_action=update``.  The original
    artifact owner is retained so the context service can read the exact
    registry authority that produced (or adopted) the artifact.
    """

    return tuple(
        sorted(
            (
                SuccessorArtifactContributionV2(
                    target_id=item.target_id,
                    artifact_id=item.registry_artifact_id,
                    artifact_type=item.output.artifact_type,
                    owner_successor_transition_id=(
                        item.job_owner_successor_transition_id
                        or item.recovery_id
                    ),
                    origin="inherited",
                )
                for item in results
            ),
            key=lambda item: item.target_id,
        )
    )


def _successor_context_task_tag(
    context: ScienceSuccessorPreparationContextV2,
) -> str:
    """Return the stable compatibility tag shared by normal and recovery seeds.

    Recovery materializes already-admitted artifacts for the same accepted
    attempt.  Giving the recovery operation its own task tag makes those
    immutable artifacts incompatible with the destination context even though
    their task and attempt authority is unchanged.
    """

    return (
        "openevo_run_task:"
        f"{context.accepted_attempt.attempt_id}:"
        f"{context.task.task_id}"
    )


@dataclass(frozen=True, slots=True)
class _NativeMethodJobOutput:
    job_id: str
    output: ScienceMethodOutputV2


class ScienceSuccessorPreparationV2Error(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        failure_code: str = "science_successor_preparation_failed",
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.failure_code = failure_code


class TrainingFeedbackProviderV2(Protocol):
    """Benchmark-owned policy that supplies evaluator output to Core transport."""

    def create_attachment_request(
        self,
        *,
        context: ScienceSuccessorPreparationContextV2,
        dataset: SealedTranscriptDatasetV2,
        completed_dataset_revision: str,
    ) -> TrainingFeedbackAttachmentCreateRequest | None: ...


def _successor_dataset_idempotency_key(
    context: ScienceSuccessorPreparationContextV2,
    record: ScienceAttemptExecutionRecordV2,
) -> str:
    if record.receipt is None or record.evidence is None:
        raise ScienceSuccessorPreparationV2Error(
            "captured Attempt is missing dataset source evidence"
        )
    identity = canonical_digest(
        {
            "schema_version": "1",
            "project_id": context.task.project_id,
            "task_id": context.task.task_id,
            "task_admission_id": (
                context.task.admission.task_admission_id
            ),
            "accepted_attempt_id": context.accepted_attempt.attempt_id,
            "rollout_task_id": record.receipt.rollout_task_id,
            "session_id": record.receipt.session_id,
            "session_result_sha256": (
                record.evidence.session_result_sha256
            ),
        }
    )
    return f"science-successor-dataset-{identity}"


class _ProjectCatalogV2(Protocol):
    def get_project(self, project_id: str) -> ProjectRecordV2: ...


class _ScienceLedgerV2(Protocol):
    def get_attempt_execution(
        self,
        task_id: str,
        attempt_id: str,
    ) -> ScienceAttemptExecutionRecordV2: ...

    def get_captured_session_result(self, task_id: str, attempt_id: str): ...

    def prior_dataset_artifact_ids_for_head(
        self,
        project_head_id: str,
    ) -> tuple[str, ...]: ...

    def successor_commit_for_project_head(self, project_head_id: str): ...


class _WorkspaceStoreV2(Protocol):
    def create_upload(
        self,
        project_id: str,
        request: m2.WorkspaceUploadCreateV2,
        *,
        idempotency_key: str,
        now: datetime,
    ) -> tuple[m2.WorkspaceUploadSessionV2, bool]: ...

    def get_upload(
        self,
        project_id: str,
        upload_id: str,
    ) -> m2.WorkspaceUploadSessionV2: ...

    def put_chunk(
        self,
        project_id: str,
        upload_id: str,
        *,
        chunk_index: int,
        chunk: bytes,
        chunk_sha256: str,
        chunk_byte_size: int,
        if_match: str,
        idempotency_key: str,
        now: datetime,
    ) -> tuple[m2.WorkspaceUploadSessionV2, bool]: ...

    def finalize_upload(
        self,
        project_id: str,
        upload_id: str,
        request: m2.WorkspaceUploadFinalizeV2,
        *,
        if_match: str,
        idempotency_key: str,
        now: datetime,
    ) -> tuple[m2.WorkspaceUploadSessionV2, bool]: ...


class _ServiceOwnerV2(Protocol):
    def ensure_run_binding(
        self,
        execution_mode: ServiceExecutionMode,
        *,
        model_ref: str | None = None,
        codex_model: str | None = None,
        runtime_image: str | None = None,
        total_timeout: float | None = None,
    ) -> tuple[object, object | None]: ...


class ProductionScienceSuccessorPreparerV2:
    """Seal, evolve, materialize, and capture one complete successor."""

    def __init__(
        self,
        *,
        catalog: _ProjectCatalogV2,
        ledger: _ScienceLedgerV2,
        workspaces: _WorkspaceStoreV2,
        workspace_handoffs: WorkspaceHandoffStoreV2,
        services: _ServiceOwnerV2,
        executable_registry: VerifiedExecutableRegistry,
        evolution_factory: (Callable[[ServiceRunBinding], EvolutionClientProtocol] | None) = None,
        training_feedback_provider: TrainingFeedbackProviderV2 | None = None,
        training_feedback_required: bool = False,
        official_frozen_mode: bool = False,
        artifact_admission_root: str | Path | None = None,
        planned_job_http_evidence_root: str | Path | None = None,
        artifact_admission_policy: NativeArtifactAdmissionPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        poll_interval_seconds: float = 1.0,
        max_poll_attempts: int = 7200,
    ) -> None:
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, int | float)
            or not math.isfinite(poll_interval_seconds)
            or poll_interval_seconds < 0
            or isinstance(max_poll_attempts, bool)
            or not isinstance(max_poll_attempts, int)
            or max_poll_attempts < 1
        ):
            raise ValueError("v2 successor polling configuration is invalid")
        self._catalog = catalog
        self._ledger = ledger
        self._workspaces = workspaces
        self._handoffs = workspace_handoffs
        self._services = services
        self._registry = require_verified_executable_registry(executable_registry)
        self._evolution_factory = evolution_factory
        self._planned_job_http_evidence_root = (
            None
            if planned_job_http_evidence_root is None
            else Path(planned_job_http_evidence_root).absolute()
        )
        if official_frozen_mode and (
            training_feedback_provider is not None or training_feedback_required
        ):
            raise ValueError("official frozen mode forbids training feedback")
        self._training_feedback_provider = training_feedback_provider
        self._training_feedback_required = training_feedback_required
        self._official_frozen_mode = official_frozen_mode
        self._artifact_admission_root = (
            None
            if artifact_admission_root is None
            else Path(artifact_admission_root).absolute()
        )
        self._artifact_admission_policy = (
            artifact_admission_policy
            or DeterministicNativeArtifactAdmissionPolicy()
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._poll_interval = float(poll_interval_seconds)
        self._max_poll_attempts = max_poll_attempts
        self._stop = threading.Event()

    def request_stop(self) -> None:
        """Interrupt bounded polling during Daemon shutdown."""

        self._stop.set()

    def _require_running(self) -> None:
        if self._stop.is_set():
            raise ScienceSuccessorPreparationV2Error(
                "successor preparation is stopping",
                retryable=True,
            )

    def seal_dataset(
        self,
        context: ScienceSuccessorPreparationContextV2,
    ) -> SealedTranscriptDatasetV2:
        self._require_running()
        record, result, project = self._authority(context)
        assert record.evidence is not None
        assert record.receipt is not None
        dataset_name = (
            f"{context.task.project_id}:{context.task.task_id}:"
            f"{context.accepted_attempt.attempt_id}"
        )
        idempotency_key = _successor_dataset_idempotency_key(
            context,
            record,
        )
        with self._evolution(context, record, project) as (_binding, client):
            raw = client.create_dataset(
                {
                    "idempotency_key": idempotency_key,
                    "name": dataset_name,
                    "purpose": "openevo_science_successor_v2",
                    "query": {
                        "source": "openevo",
                        "event_types": ["openevo.session_completed"],
                        "status": ["COMPLETED"],
                        "policy_version": record.evidence.policy_version,
                        "source_event_id": (
                            f"session:{record.receipt.session_id}"
                        ),
                        "task_id": record.receipt.rollout_task_id,
                        "session_id": record.receipt.session_id,
                    },
                    "limits": {
                        "max_events": 1,
                        "max_traces": record.evidence.transcript_record_count,
                    },
                }
            )
            dataset = DatasetCreateResponse.model_validate(raw)
            artifact = ArtifactResponse.model_validate(client.get_artifact(dataset.artifact_id))
        self._require_running()
        return self._sealed_dataset_receipt(
            context,
            dataset=dataset,
            artifact=artifact,
            expected_name=dataset_name,
            expected_idempotency_key=idempotency_key,
            expected_record=record,
            expected_record_count=record.evidence.transcript_record_count,
            captured_trace_count=len(result.trajectory.traces),
        )

    def recover_dataset(
        self,
        context: ScienceSuccessorPreparationContextV2,
        *,
        dataset_id: str,
        manifest_sha256: str,
    ) -> SealedTranscriptDatasetV2:
        self._require_running()
        record, result, project = self._authority(context)
        assert record.evidence is not None
        dataset_name = (
            f"{context.task.project_id}:{context.task.task_id}:"
            f"{context.accepted_attempt.attempt_id}"
        )
        idempotency_key = _successor_dataset_idempotency_key(
            context,
            record,
        )
        with self._evolution(context, record, project) as (_binding, client):
            dataset = DatasetCreateResponse.model_validate(client.get_dataset(dataset_id))
            artifact = ArtifactResponse.model_validate(client.get_artifact(dataset.artifact_id))
        self._require_running()
        receipt = self._sealed_dataset_receipt(
            context,
            dataset=dataset,
            artifact=artifact,
            expected_name=dataset_name,
            expected_idempotency_key=idempotency_key,
            expected_record=record,
            expected_record_count=record.evidence.transcript_record_count,
            captured_trace_count=len(result.trajectory.traces),
        )
        if receipt.dataset_id != dataset_id or receipt.manifest_sha256 != manifest_sha256:
            raise ScienceSuccessorPreparationV2Error(
                "recovered transcript dataset differs from the transition journal"
            )
        return receipt

    def _sealed_dataset_receipt(
        self,
        context: ScienceSuccessorPreparationContextV2,
        *,
        dataset: DatasetCreateResponse,
        artifact: ArtifactResponse,
        expected_name: str,
        expected_idempotency_key: str,
        expected_record: ScienceAttemptExecutionRecordV2,
        expected_record_count: int,
        captured_trace_count: int,
    ) -> SealedTranscriptDatasetV2:
        assert expected_record.receipt is not None
        assert expected_record.evidence is not None
        receipt = expected_record.receipt
        evidence = expected_record.evidence
        manifest = artifact.manifest
        event_ids = manifest.get("event_ids")
        source_event_evidence = manifest.get("source_event_evidence")
        expected_query = {
            "source": "openevo",
            "event_types": ["openevo.session_completed"],
            "status": ["COMPLETED"],
            "reward_min": None,
            "policy_version": evidence.policy_version,
            "task_tags": [],
            "source_event_id": f"session:{receipt.session_id}",
            "task_id": receipt.rollout_task_id,
            "session_id": receipt.session_id,
        }
        expected_query = {
            key: value
            for key, value in expected_query.items()
            if value is not None
        }
        if (
            dataset.event_count != 1
            or dataset.trace_count != expected_record_count
            or dataset.trace_count != captured_trace_count
            or artifact.artifact_id != dataset.artifact_id
            or artifact.type is not ArtifactType.DATASET
            or artifact.state is not ArtifactState.ACTIVE
            or artifact.promoted is not True
            or artifact.name != expected_name
            or artifact.compatibility
            != {"purpose": "openevo_science_successor_v2"}
            or manifest.get("dataset_id") != dataset.dataset_id
            or manifest.get("name") != expected_name
            or manifest.get("purpose")
            != "openevo_science_successor_v2"
            or manifest.get("query") != expected_query
            or manifest.get("limits")
            != {
                "max_events": 1,
                "max_traces": expected_record_count,
            }
            or not isinstance(event_ids, list)
            or len(event_ids) != 1
            or not isinstance(event_ids[0], str)
            or not event_ids[0]
            or manifest.get("event_count") != 1
            or manifest.get("trace_count") != dataset.trace_count
            or manifest.get("records_path") != "records.jsonl"
            or not isinstance(manifest.get("records_uri"), str)
            or not manifest["records_uri"]
            or type(manifest.get("records_byte_size")) is not int
            or manifest["records_byte_size"] < 0
            or not isinstance(manifest.get("records_sha256"), str)
            or re.fullmatch(
                r"[0-9a-f]{64}",
                manifest["records_sha256"],
            )
            is None
            or manifest.get("create_identity")
            != expected_idempotency_key
            or source_event_evidence
            != {
                "event_id": event_ids[0],
                "source": "openevo",
                "event_type": "openevo.session_completed",
                "source_event_id": f"session:{receipt.session_id}",
                "task_id": receipt.rollout_task_id,
                "session_id": receipt.session_id,
                "session_result_sha256": evidence.session_result_sha256,
            }
        ):
            raise ScienceSuccessorPreparationV2Error(
                "dataset artifact differs from the sealed transcript authority"
            )
        return SealedTranscriptDatasetV2(
            dataset_id=dataset.dataset_id,
            artifact_id=dataset.artifact_id,
            manifest_sha256=canonical_digest(manifest),
            record_count=dataset.trace_count,
            task_id=context.task.task_id,
            task_admission_id=context.task.admission.task_admission_id,
            accepted_attempt_id=context.accepted_attempt.attempt_id,
            capture_mode="transcript",
            token_level_metrics_available=False,
            sealed=True,
        )

    def resolve_training_feedback(
        self,
        context: ScienceSuccessorPreparationContextV2,
        dataset: SealedTranscriptDatasetV2,
    ) -> SealedTranscriptDatasetV2:
        self._require_running()
        record, _result, project = self._authority(context)
        assert record.receipt is not None
        provider = self._training_feedback_provider
        required = self._training_feedback_required or _project_requires_training_feedback(
            project
        )
        if provider is None and not required:
            return dataset
        if self._official_frozen_mode:
            if required or provider is not None:
                raise ScienceSuccessorPreparationV2Error(
                    "official frozen mode forbids training feedback"
                )
            return dataset
        with self._evolution(context, record, project) as (_binding, client):
            raw_source = client.get_artifact(dataset.artifact_id)
        source_artifact = ArtifactResponse.model_validate(raw_source)
        revision = f"{source_artifact.artifact_id}.v{source_artifact.version}"
        attachment: TrainingFeedbackAttachment | None = None
        if provider is not None:
            request = provider.create_attachment_request(
                context=context,
                dataset=dataset,
                completed_dataset_revision=revision,
            )
            if request is not None:
                request = TrainingFeedbackAttachmentCreateRequest.model_validate(request)
                if (
                    request.session_id != record.receipt.session_id
                    or request.task_id != record.receipt.rollout_task_id
                    or request.completed_dataset_id != dataset.dataset_id
                    or request.completed_dataset_revision != revision
                    or request.task_scope_id != context.task.task_id
                ):
                    raise ScienceSuccessorPreparationV2Error(
                        "training feedback provider returned mismatched authority"
                    )
                with self._evolution(context, record, project) as (_binding, client):
                    attachment = TrainingFeedbackAttachment.model_validate(
                        client.create_training_feedback_attachment(
                            request.model_dump(mode="json")
                        )
                    )
                if (
                    attachment.session_id != request.session_id
                    or attachment.task_id != request.task_id
                    or attachment.task_scope_id != request.task_scope_id
                    or attachment.dataset_id != request.completed_dataset_id
                    or attachment.dataset_revision
                    != request.completed_dataset_revision
                    or attachment.producer != request.producer
                    or attachment.feedback_class is not request.feedback_class
                    or attachment.global_feedback != request.global_feedback
                    or attachment.task_local_feedback != request.task_local_feedback
                    or attachment.status != "sealed"
                ):
                    raise ScienceSuccessorPreparationV2Error(
                        "training feedback service returned mismatched authority"
                    )
        else:
            with self._evolution(context, record, project) as (_binding, client):
                listed = client.list_training_feedback_attachments_for_session(
                    record.receipt.session_id
                )
            candidates = [
                TrainingFeedbackAttachment.model_validate(item)
                for item in listed.get("attachments", [])
                if isinstance(item, Mapping)
            ] if isinstance(listed, Mapping) else []
            exact = [
                item
                for item in candidates
                if item.session_id == record.receipt.session_id
                and item.task_id == record.receipt.rollout_task_id
                and item.task_scope_id == context.task.task_id
                and item.dataset_id == dataset.dataset_id
                and item.dataset_revision == revision
            ]
            if len(exact) > 1:
                raise ScienceSuccessorPreparationV2Error(
                    "multiple trusted feedback attachments match one successor"
                )
            attachment = None if not exact else exact[0]
        if attachment is None:
            if required:
                raise ScienceSuccessorPreparationV2Error(
                    "trusted evaluator feedback is pending",
                    retryable=True,
                )
            return dataset
        with self._evolution(context, record, project) as (_binding, client):
            resolve_request = EvolutionDatasetViewResolveRequest(
                completed_dataset_id=dataset.dataset_id,
                completed_dataset_revision=revision,
                task_id=record.receipt.rollout_task_id,
                task_scope_id=context.task.task_id,
                attachment_ids=(attachment.attachment_id,),
            )
            resolved = ResolvedEvolutionDatasetView.model_validate(
                client.resolve_evolution_dataset_view(
                    resolve_request.model_dump(mode="json")
                )
            )
        if (
            resolved.completed_dataset_id != dataset.dataset_id
            or resolved.completed_dataset_revision != revision
            or resolved.attachments != (attachment,)
            or resolved.dataset_view.attachment_ids != (attachment.attachment_id,)
            or resolved.dataset_view.attachment_sha256
            != (attachment.content_sha256,)
        ):
            raise ScienceSuccessorPreparationV2Error(
                "resolved training feedback view differs from its attachment"
            )
        return dataset.model_copy(
            update={
                "training_feedback": TrainingFeedbackBindingV2(
                    completed_dataset_id=dataset.dataset_id,
                    completed_dataset_revision=revision,
                    attachment_ids=(attachment.attachment_id,),
                    attachment_sha256=(attachment.content_sha256,),
                    resolved_dataset_artifact_id=(
                        resolved.dataset_artifact.artifact_id
                    ),
                    resolved_view_sha256=resolved.resolved_view_sha256,
                    task_scope_id=context.task.task_id,
                )
            }
        )

    def run_methods(
        self,
        context: ScienceSuccessorPreparationContextV2,
        dataset: SealedTranscriptDatasetV2,
    ) -> tuple[ScienceMethodOutputV2, ...]:
        self._require_running()
        record, _result, project = self._authority(context)
        if (
            dataset.task_id != context.task.task_id
            or dataset.accepted_attempt_id != context.accepted_attempt.attempt_id
        ):
            raise ScienceSuccessorPreparationV2Error(
                "successor method dataset has different immutable ownership"
            )
        with self._evolution(context, record, project) as (binding, client):
            compiled, methods = self._compile_methods(
                context,
                project=project,
                binding=binding,
                record=record,
            )
            (
                prior_context,
                _prior_composition,
                prior_owner_by_target,
            ) = self._prior_context_artifacts(context, client)
            compiled_task = compiled.tasks[0]
            evolution_dataset_artifact_id = (
                dataset.artifact_id
                if dataset.training_feedback is None
                else dataset.training_feedback.resolved_dataset_artifact_id
            )
            legacy_payloads = compiled_task.evolution_job_payloads_for_round(
                0,
                methods,
                dataset_artifact_id=evolution_dataset_artifact_id,
                context_artifact_ids=prior_context,
            )
            if dataset.training_feedback is not None:
                feedback_lineage = {
                    "completed_dataset_id": dataset.dataset_id,
                    "completed_dataset_artifact_id": dataset.artifact_id,
                    "training_feedback_attachment_ids": list(
                        dataset.training_feedback.attachment_ids
                    ),
                    "training_feedback_attachment_sha256": list(
                        dataset.training_feedback.attachment_sha256
                    ),
                    "resolved_view_sha256": (
                        dataset.training_feedback.resolved_view_sha256
                    ),
                }
                for payload in legacy_payloads:
                    config = payload.get("config")
                    if not isinstance(config, dict):
                        raise ScienceSuccessorPreparationV2Error(
                            "compiled evolution job payload is incomplete"
                        )
                    prior_lineage = config.get("lineage", {})
                    if not isinstance(prior_lineage, dict):
                        raise ScienceSuccessorPreparationV2Error(
                            "compiled evolution lineage is invalid"
                        )
                    config["lineage"] = {
                        **prior_lineage,
                        "training_feedback": feedback_lineage,
                    }
            content_admission_basis = self._content_admission_basis(
                context,
                dataset,
                client,
            )
            outputs = tuple(
                self._run_one_method(
                    client,
                    spec=spec,
                    legacy_payload=legacy_payload,
                    transition_attempt_id=(
                        context.transition_attempt.transition_attempt_id
                    ),
                    transition_attempt_ordinal=(
                        context.transition_attempt.ordinal
                    ),
                    successor_transition_id=(
                        context.transition.transition.successor_transition_id
                    ),
                    predecessor_successor_transition_id=(
                        prior_owner_by_target.get(spec.target_id)
                    ),
                    parent_artifact_id=(
                        prior_context.get(spec.artifact_type, [None])[0]
                        if prior_context.get(spec.artifact_type)
                        else None
                    ),
                    content_admission_basis=content_admission_basis,
                    retry_terminal_job=True,
                ).output
                for spec, legacy_payload in zip(
                    methods,
                    legacy_payloads,
                    strict=True,
                )
            )
        return outputs

    def reconcile_completed_methods(
        self,
        context: ScienceSuccessorPreparationContextV2,
        dataset: SealedTranscriptDatasetV2,
    ) -> tuple[ScienceMethodOutputV2, ...]:
        """Recover the exact historical paid outputs without recompiling them.

        A later release has a different executable-registry identity even when
        it is reading the same durable Evolution store.  Recompiling under the
        later registry would compare historical jobs to a new distribution
        digest and make a no-model commit-tail recovery impossible.  Evolution
        therefore closes each succeeded job against its own persisted plan and
        envelope; this method additionally binds that authority to the exact
        failed transition, source plan targets, prior artifact owners, terminal
        result, and admission receipt.
        """

        self._require_running()
        record, _result, project = self._authority(context)
        if (
            context.transition_attempt.reconciliation_only is not True
            or context.transition_attempt.reconciliation_source_attempt_id
            is None
            or dataset.task_id != context.task.task_id
            or dataset.accepted_attempt_id
            != context.accepted_attempt.attempt_id
        ):
            raise ScienceSuccessorPreparationV2Error(
                "completed-method reconciliation authority is invalid"
            )
        transition_id = context.transition.transition.successor_transition_id
        with self._evolution(context, record, project) as (_binding, client):
            (
                _prior_context,
                _prior_composition,
                prior_owner_by_target,
            ) = self._prior_context_artifacts(context, client)
            inventory = SuccessorTransitionJobInventoryResponse.model_validate_json(
                json.dumps(
                    client.get_internal_successor_transition_job_inventory(
                        transition_id
                    ),
                    ensure_ascii=True,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            content_basis = self._content_admission_basis(
                context,
                dataset,
                client,
            )
            outputs: list[ScienceMethodOutputV2] = []
            observed_job_ids: list[str] = []
            observed_plan_authorities: set[tuple[str, str, str]] = set()
            for planned in context.plan.enabled_methods:
                job = SucceededPlanBoundJobAuthorityResponse.model_validate_json(
                    json.dumps(
                        client.get_internal_succeeded_plan_bound_job_authority(
                            transition_id,
                            planned.target_id,
                        ),
                        ensure_ascii=True,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                observed_plan_authorities.add(
                    (
                        job.plan_id,
                        job.registry_snapshot_digest,
                        job.plan_digest,
                    )
                )
                terminal = client.get_internal_job_result(job.job_id)
                raw_outputs = (
                    terminal.get("outputs")
                    if isinstance(terminal, Mapping)
                    else None
                )
                target_outputs = (
                    []
                    if not isinstance(raw_outputs, list)
                    else [
                        item
                        for item in raw_outputs
                        if isinstance(item, dict)
                        and item.get("type")
                        == planned.output_artifact_type
                    ]
                )
                selected = [
                    item
                    for item in target_outputs
                    if item.get("promoted") is True
                ]
                if (
                    job.successor_transition_id != transition_id
                    or job.target_id != planned.target_id
                    or job.method_id != planned.method_id
                    or job.job_type != planned.method_id
                    or job.predecessor_successor_transition_id
                    != prior_owner_by_target.get(planned.target_id)
                    or planned.output_artifact_type
                    not in job.declared_output_artifact_types
                    or job.attempt_count != 1
                    or not isinstance(terminal, Mapping)
                    or terminal.get("job_id") != job.job_id
                    or terminal.get("state") != JobState.SUCCEEDED.value
                    or terminal.get("error") is not None
                    or terminal.get("retryable") is not None
                    or terminal.get("successor_transition_id")
                    != transition_id
                    or canonical_digest(terminal) != job.job_result_sha256
                    or terminal.get("artifact_ids")
                    != [
                        item.get("artifact_id")
                        for item in raw_outputs or []
                        if isinstance(item, dict)
                    ]
                    or tuple(terminal.get("artifact_ids") or ())
                    != job.output_artifact_ids
                    or not target_outputs
                    or len(selected) != 1
                ):
                    raise ScienceSuccessorPreparationV2Error(
                        "completed method job authority is inconsistent"
                    )
                raw_selected = selected[0]
                artifact_id = raw_selected.get("artifact_id")
                if not isinstance(artifact_id, str):
                    raise ScienceSuccessorPreparationV2Error(
                        "completed method selected artifact identity is invalid"
                    )
                artifact = SuccessorArtifactAuthorityResponse.model_validate(
                    client.get_internal_successor_artifact_authority(
                        transition_id,
                        artifact_id,
                    )
                )
                proposal_ids = tuple(
                    sorted(
                        str(item["artifact_id"])
                        for item in target_outputs
                        if isinstance(item.get("artifact_id"), str)
                    )
                )
                if (
                    artifact.successor_transition_id != transition_id
                    or artifact.job_id != job.job_id
                    or artifact.artifact.artifact_id != artifact_id
                    or artifact.artifact.type.value
                    != planned.output_artifact_type
                    or artifact.artifact.state is not ArtifactState.SEALED
                    or artifact.artifact.promoted is not True
                    or artifact.proposal_artifact_ids != proposal_ids
                    or artifact.admission_decision_id is None
                    or artifact.admission_decision_sha256 is None
                    or artifact.content_admission is None
                    or artifact.content_admission.passed is not True
                    or artifact.content_admission.basis_sha256
                    != content_basis.content_sha256
                    or raw_selected.get("payload_manifest_digest")
                    != artifact.payload_manifest_sha256
                    or raw_selected.get("payload_byte_size")
                    != artifact.payload_byte_size
                ):
                    raise ScienceSuccessorPreparationV2Error(
                        "completed method admission authority is inconsistent"
                    )
                outputs.append(
                    ScienceMethodOutputV2(
                        target_id=planned.target_id,
                        method_id=planned.method_id,
                        artifact_id=artifact_id,
                        artifact_type=planned.output_artifact_type,
                        manifest_sha256=artifact.payload_manifest_sha256,
                        byte_size=artifact.payload_byte_size,
                        execution_boundary="outside_inference",
                        evolution_job_id=job.job_id,
                        admission_action="update",
                        admission_decision_id=(
                            artifact.admission_decision_id
                        ),
                        admission_decision_sha256=(
                            artifact.admission_decision_sha256
                        ),
                        content_admission_sha256=(
                            artifact.content_admission.content_sha256
                        ),
                        content_admission=artifact.content_admission,
                        proposal_artifact_ids=(
                            artifact.proposal_artifact_ids
                        ),
                        origin="produced",
                        owner_successor_transition_id=transition_id,
                    )
                )
                observed_job_ids.append(job.job_id)
            if (
                inventory.successor_transition_id != transition_id
                or tuple(sorted(observed_job_ids)) != inventory.job_ids
                or len(observed_job_ids)
                != len(context.plan.enabled_methods)
                or len(observed_plan_authorities) != 1
            ):
                raise ScienceSuccessorPreparationV2Error(
                    "completed method job inventory is not exact"
                )
        self._require_running()
        return tuple(outputs)

    def _reconcile_completed_methods_same_generation(
        self,
        context: ScienceSuccessorPreparationContextV2,
        dataset: SealedTranscriptDatasetV2,
    ) -> tuple[ScienceMethodOutputV2, ...]:
        """Read already-admitted method outputs without a job mutation API.

        This is intentionally separate from :meth:`run_methods`: the method
        never calls ``create_plan_bound_job`` or ``retry_plan_bound_job``.
        Every enabled target must already own one exact succeeded job and one
        selected UPDATE artifact under the failed source transition.
        """

        self._require_running()
        record, _result, project = self._authority(context)
        if (
            dataset.task_id != context.task.task_id
            or dataset.accepted_attempt_id != context.accepted_attempt.attempt_id
        ):
            raise ScienceSuccessorPreparationV2Error(
                "completed-method reconciliation authority is invalid"
            )
        transition_id = context.transition.transition.successor_transition_id
        with self._evolution(context, record, project) as (binding, client):
            compiled, methods = self._compile_methods(
                context,
                project=project,
                binding=binding,
                record=record,
            )
            (
                prior_context,
                _prior_composition,
                prior_owner_by_target,
            ) = self._prior_context_artifacts(context, client)
            evolution_dataset_artifact_id = (
                dataset.artifact_id
                if dataset.training_feedback is None
                else dataset.training_feedback.resolved_dataset_artifact_id
            )
            legacy_payloads = compiled.tasks[0].evolution_job_payloads_for_round(
                0,
                methods,
                dataset_artifact_id=evolution_dataset_artifact_id,
                context_artifact_ids=prior_context,
            )
            if dataset.training_feedback is not None:
                feedback_lineage = {
                    "completed_dataset_id": dataset.dataset_id,
                    "completed_dataset_artifact_id": dataset.artifact_id,
                    "training_feedback_attachment_ids": list(
                        dataset.training_feedback.attachment_ids
                    ),
                    "training_feedback_attachment_sha256": list(
                        dataset.training_feedback.attachment_sha256
                    ),
                    "resolved_view_sha256": (
                        dataset.training_feedback.resolved_view_sha256
                    ),
                }
                for payload in legacy_payloads:
                    config = payload.get("config")
                    if not isinstance(config, dict):
                        raise ScienceSuccessorPreparationV2Error(
                            "compiled evolution job payload is incomplete"
                        )
                    prior_lineage = config.get("lineage", {})
                    if not isinstance(prior_lineage, dict):
                        raise ScienceSuccessorPreparationV2Error(
                            "compiled evolution lineage is invalid"
                        )
                    config["lineage"] = {
                        **prior_lineage,
                        "training_feedback": feedback_lineage,
                    }
            inventory = SuccessorTransitionJobInventoryResponse.model_validate_json(
                json.dumps(
                    client.get_internal_successor_transition_job_inventory(
                        transition_id
                    ),
                    ensure_ascii=True,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            content_basis = self._content_admission_basis(
                context,
                dataset,
                client,
            )
            outputs: list[ScienceMethodOutputV2] = []
            observed_job_ids: list[str] = []
            for spec, legacy_payload in zip(
                methods,
                legacy_payloads,
                strict=True,
            ):
                expected_request = _plan_bound_request(
                    spec,
                    legacy_payload,
                    successor_transition_id=transition_id,
                    predecessor_successor_transition_id=(
                        prior_owner_by_target.get(spec.target_id)
                    ),
                    content_admission_basis=content_basis,
                    promoted=False,
                )
                job = SucceededPlanBoundJobAuthorityResponse.model_validate_json(
                    json.dumps(
                        client.get_internal_succeeded_plan_bound_job_authority(
                            transition_id,
                            spec.target_id,
                        ),
                        ensure_ascii=True,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                terminal = client.get_internal_job_result(job.job_id)
                raw_outputs = (
                    terminal.get("outputs")
                    if isinstance(terminal, Mapping)
                    else None
                )
                target_outputs = (
                    []
                    if not isinstance(raw_outputs, list)
                    else [
                        item
                        for item in raw_outputs
                        if isinstance(item, dict)
                        and item.get("type") == spec.artifact_type
                    ]
                )
                selected = [
                    item for item in target_outputs if item.get("promoted") is True
                ]
                if (
                    job.successor_transition_id != transition_id
                    or job.plan_id != spec.plan.plan_id
                    or job.plan_digest != canonical_digest(spec.plan)
                    or job.target_id != spec.target_id
                    or job.method_id != spec.method
                    or job.method_identity_digest
                    != spec.selection.method_identity_digest
                    or job.job_type != expected_request.job_type
                    or job.predecessor_successor_transition_id
                    != expected_request.predecessor_successor_transition_id
                    or job.core_config_sha256
                    != canonical_digest(expected_request.core_config)
                    or job.input_bindings_sha256
                    != canonical_digest(
                        {
                            "input_bindings": [
                                binding.model_dump(mode="json")
                                for binding in expected_request.input_bindings
                            ]
                        }
                    )
                    or job.declared_output_artifact_types
                    != tuple(
                        self._registry.snapshot.methods[
                            spec.method
                        ].output_artifact_types
                    )
                    or job.priority != expected_request.priority
                    or job.attempt_count != 1
                    or not isinstance(terminal, Mapping)
                    or terminal.get("job_id") != job.job_id
                    or terminal.get("state") != JobState.SUCCEEDED.value
                    or terminal.get("error") is not None
                    or terminal.get("retryable") is not None
                    or terminal.get("successor_transition_id") != transition_id
                    or canonical_digest(terminal) != job.job_result_sha256
                    or terminal.get("artifact_ids")
                    != [
                        item.get("artifact_id")
                        for item in raw_outputs or []
                        if isinstance(item, dict)
                    ]
                    or tuple(terminal.get("artifact_ids") or ())
                    != job.output_artifact_ids
                    or not target_outputs
                    or len(selected) != 1
                ):
                    raise ScienceSuccessorPreparationV2Error(
                        "completed method job authority is inconsistent"
                    )
                raw_selected = selected[0]
                artifact_id = raw_selected.get("artifact_id")
                if not isinstance(artifact_id, str):
                    raise ScienceSuccessorPreparationV2Error(
                        "completed method selected artifact identity is invalid"
                    )
                artifact = SuccessorArtifactAuthorityResponse.model_validate(
                    client.get_internal_successor_artifact_authority(
                        transition_id,
                        artifact_id,
                    )
                )
                proposal_ids = tuple(
                    sorted(
                        str(item["artifact_id"])
                        for item in target_outputs
                        if isinstance(item.get("artifact_id"), str)
                    )
                )
                if (
                    artifact.successor_transition_id != transition_id
                    or artifact.job_id != job.job_id
                    or artifact.artifact.artifact_id != artifact_id
                    or artifact.artifact.type.value != spec.artifact_type
                    or artifact.artifact.state is not ArtifactState.SEALED
                    or artifact.artifact.promoted is not True
                    or artifact.proposal_artifact_ids != proposal_ids
                    or artifact.admission_decision_id is None
                    or artifact.admission_decision_sha256 is None
                    or artifact.content_admission is None
                    or artifact.content_admission.passed is not True
                    or artifact.content_admission.basis_sha256
                    != content_basis.content_sha256
                    or raw_selected.get("payload_manifest_digest")
                    != artifact.payload_manifest_sha256
                    or raw_selected.get("payload_byte_size")
                    != artifact.payload_byte_size
                ):
                    raise ScienceSuccessorPreparationV2Error(
                        "completed method admission authority is inconsistent"
                    )
                outputs.append(
                    ScienceMethodOutputV2(
                        target_id=spec.target_id,
                        method_id=spec.method,
                        artifact_id=artifact_id,
                        artifact_type=spec.artifact_type,
                        manifest_sha256=artifact.payload_manifest_sha256,
                        byte_size=artifact.payload_byte_size,
                        execution_boundary="outside_inference",
                        evolution_job_id=job.job_id,
                        admission_action="update",
                        admission_decision_id=(artifact.admission_decision_id),
                        admission_decision_sha256=(
                            artifact.admission_decision_sha256
                        ),
                        content_admission_sha256=(
                            artifact.content_admission.content_sha256
                        ),
                        content_admission=artifact.content_admission,
                        proposal_artifact_ids=artifact.proposal_artifact_ids,
                        origin="produced",
                        owner_successor_transition_id=transition_id,
                    )
                )
                observed_job_ids.append(job.job_id)
            if (
                inventory.successor_transition_id != transition_id
                or tuple(sorted(observed_job_ids)) != inventory.job_ids
                or len(observed_job_ids) != len(context.plan.enabled_methods)
            ):
                raise ScienceSuccessorPreparationV2Error(
                    "completed method job inventory is not exact"
                )
        self._require_running()
        return tuple(outputs)

    @staticmethod
    def _content_admission_basis(
        context: ScienceSuccessorPreparationContextV2,
        dataset: SealedTranscriptDatasetV2,
        client: EvolutionClientProtocol,
    ) -> ArtifactContentAdmissionBasis:
        """Build a private, sealed leakage basis for every global artifact type."""

        task_ids: set[str] = {context.task.task_id, dataset.task_id}
        classified: dict[str, set[str]] = {
            "file_names": set(),
            "entities": set(),
            "values": set(),
            "dois": set(),
            "report_sentences": set(),
            "evaluator_terms": set(),
            "absolute_paths": set(),
        }
        binding = dataset.training_feedback
        if binding is not None:
            attachments = tuple(
                TrainingFeedbackAttachment.model_validate(
                    client.get_training_feedback_attachment(attachment_id)
                )
                for attachment_id in binding.attachment_ids
            )
            if (
                tuple(item.attachment_id for item in attachments)
                != binding.attachment_ids
                or tuple(item.content_sha256 for item in attachments)
                != binding.attachment_sha256
                or any(
                    item.status != "sealed"
                    or item.authority.value != "evaluator_only"
                    or item.dataset_id != dataset.dataset_id
                    or item.dataset_revision
                    != binding.completed_dataset_revision
                    or item.task_scope_id != binding.task_scope_id
                    for item in attachments
                )
            ):
                raise ScienceSuccessorPreparationV2Error(
                    "content admission basis differs from sealed feedback authority"
                )
            for attachment in attachments:
                task_ids.update({attachment.task_id, attachment.task_scope_id})
                ProductionScienceSuccessorPreparerV2._classify_content_literals(
                    attachment.task_local_feedback,
                    classified,
                )
        return ArtifactContentAdmissionBasis(
            task_ids=_sorted_literals(task_ids),
            **{
                key: _sorted_literals(values)
                for key, values in classified.items()
            },
        )

    @staticmethod
    def _classify_content_literals(
        value: object,
        target: dict[str, set[str]],
        *,
        key: str = "",
    ) -> None:
        normalized_key = key.strip().lower().replace("-", "_")
        if isinstance(value, dict):
            for nested_key, nested in value.items():
                ProductionScienceSuccessorPreparerV2._classify_content_literals(
                    nested,
                    target,
                    key=str(nested_key),
                )
            return
        if isinstance(value, list | tuple):
            for nested in value:
                ProductionScienceSuccessorPreparerV2._classify_content_literals(
                    nested,
                    target,
                    key=key,
                )
            return
        if isinstance(value, int | float) and not isinstance(value, bool):
            target["values"].add(str(value))
            return
        if not isinstance(value, str):
            return
        text = value.strip()
        if not text or len(text.encode("utf-8")) > 4096:
            return
        if normalized_key in {
            "data_file",
            "data_files",
            "file_name",
            "file_names",
            "source_file",
            "source_files",
        }:
            target["file_names"].add(text)
        elif normalized_key in {
            "entity",
            "entities",
            "target_entity",
            "target_entities",
            "article_title",
            "article_titles",
            "target_paper_title",
        }:
            target["entities"].add(text)
        elif normalized_key in {"doi", "dois"}:
            target["dois"].add(text)
        elif normalized_key in {
            "report_sentence",
            "report_sentences",
            "source_sentence",
            "source_sentences",
        }:
            target["report_sentences"].add(text)
        elif normalized_key in {
            "evaluator_term",
            "evaluator_terms",
            "judge_term",
            "judge_terms",
        }:
            target["evaluator_terms"].add(text)
        elif normalized_key in {"absolute_path", "absolute_paths"} or text.startswith("/"):
            target["absolute_paths"].add(text)
        elif normalized_key in TASK_SPECIFIC_LITERAL_KEYS:
            target["values"].add(text)

    def run_recovery_method(
        self,
        context: ScienceSuccessorPreparationContextV2,
        dataset: SealedTranscriptDatasetV2,
        *,
        intent: ScienceSuccessorRecoveryTargetIntentV1,
        source: ExhaustedSuccessorRecoveryAuthorityV1,
        binding: ServiceRunBinding,
        client: EvolutionClientProtocol,
        adopted_job: ScienceSuccessorRecoveryAdoptedJobAuthorityV1 | None = None,
    ) -> ScienceSuccessorRecoveryTargetResultV1:
        """Run one fresh plan-bound method for an append-only recovery.

        The caller owns the current generation lease and verified worker-mount
        readiness.  This method deliberately does not enter ``_evolution``:
        that context manager pins ordinary successor work to the captured
        Attempt generation, while recovery is explicitly bound to a new
        execution identity.  It still uses the same compiler, planned-job
        endpoint, promotion/admission authority, and artifact registry.
        """

        self._require_running()
        try:
            record, _result, project = self._authority(context)
        except ScienceSuccessorPreparationV2Error as exc:
            raise ScienceSuccessorPreparationV2Error(
                "recovery source Attempt authority is incomplete or changed",
                retryable=exc.retryable,
                failure_code="recovery_source_attempt_authority_invalid",
            ) from exc
        authority_checks = (
            (
                "recovery_source_transition_not_failed",
                context.transition.state == "failed",
            ),
            (
                "recovery_source_transition_attempt_not_failed",
                context.transition_attempt.state == "failed",
            ),
            (
                "recovery_source_transition_identity_mismatch",
                context.transition.transition.successor_transition_id
                == source.successor_transition_id,
            ),
            (
                "recovery_source_plan_identity_mismatch",
                science_successor_plan_sha256(context.plan) == source.plan_sha256,
            ),
            ("recovery_source_dataset_identity_mismatch", dataset == source.dataset),
            (
                "recovery_operation_reuses_source_transition_identity",
                intent.recovery_id != source.successor_transition_id,
            ),
            (
                "recovery_source_authority_digest_mismatch",
                intent.source_authority_sha256
                == science_successor_recovery_source_sha256(source),
            ),
            (
                "recovery_binding_generation_mismatch",
                intent.execution_identity.core_generation == binding.generation_digest,
            ),
            (
                "recovery_binding_registry_mismatch",
                intent.execution_identity.executable_registry_digest
                == binding.registry_digest,
            ),
            (
                "recovery_binding_framework_lock_mismatch",
                intent.execution_identity.framework_lock_digest
                == binding.framework_lock_digest,
            ),
            (
                "recovery_binding_runtime_identity_mismatch",
                intent.execution_identity.runtime_identity_digest
                == binding.runtime_identity_digest,
            ),
            (
                "recovery_binding_runtime_image_mismatch",
                intent.execution_identity.runtime_image_digest
                == managed_runtime_release_authority_digest(
                    profile="managed_science",
                    image=binding.runtime_image_immutable_reference,
                ),
            ),
        )
        failed_authority_code = next(
            (code for code, passed in authority_checks if not passed),
            None,
        )
        if failed_authority_code is not None:
            raise ScienceSuccessorPreparationV2Error(
                "recovery method authority differs from its immutable source or binding",
                failure_code=failed_authority_code,
            )
        compiled, methods = self._compile_methods(
            context,
            project=project,
            binding=binding,
            record=record,
        )
        method_by_target = {item.target_id: item for item in methods}
        spec = method_by_target.get(intent.target.target_id)
        if (
            spec is None
            or spec.method != intent.target.method_id
            or spec.artifact_type != intent.target.output_artifact_type
        ):
            raise ScienceSuccessorPreparationV2Error(
                "recovery target differs from the compiled successor method",
                failure_code="recovery_compiled_target_identity_mismatch",
            )
        prior_context, _prior_composition, prior_owner_by_target = (
            self._prior_context_artifacts(context, client)
        )
        evolution_dataset_artifact_id = (
            dataset.artifact_id
            if dataset.training_feedback is None
            else dataset.training_feedback.resolved_dataset_artifact_id
        )
        payloads = compiled.tasks[0].evolution_job_payloads_for_round(
            0,
            methods,
            dataset_artifact_id=evolution_dataset_artifact_id,
            context_artifact_ids=prior_context,
        )
        payload_by_target = {
            item.target_id: payload
            for item, payload in zip(methods, payloads, strict=True)
        }
        legacy_payload = payload_by_target[spec.target_id]
        if dataset.training_feedback is not None:
            config = legacy_payload.get("config")
            if not isinstance(config, dict):
                raise ScienceSuccessorPreparationV2Error(
                    "compiled recovery evolution payload is incomplete",
                    failure_code="recovery_compiled_payload_incomplete",
                )
            prior_lineage = config.get("lineage", {})
            if not isinstance(prior_lineage, dict):
                raise ScienceSuccessorPreparationV2Error(
                    "compiled recovery evolution lineage is invalid",
                    failure_code="recovery_compiled_lineage_invalid",
                )
            config["lineage"] = {
                **prior_lineage,
                "training_feedback": {
                    "completed_dataset_id": dataset.dataset_id,
                    "completed_dataset_artifact_id": dataset.artifact_id,
                    "training_feedback_attachment_ids": list(
                        dataset.training_feedback.attachment_ids
                    ),
                    "training_feedback_attachment_sha256": list(
                        dataset.training_feedback.attachment_sha256
                    ),
                    "resolved_view_sha256": (
                        dataset.training_feedback.resolved_view_sha256
                    ),
                },
            }
        max_reflector_model_calls = (
            intent.execution_identity.max_reflector_model_calls
        )
        recovery_user_config = spec.selection.config()
        if spec.method == "agent_system_gepa_reflector":
            configured_strategies = recovery_user_config.get("mutation_strategies")
            if isinstance(configured_strategies, list) and configured_strategies:
                recovery_user_config["mutation_strategies"] = [
                    configured_strategies[0]
                ]
            recovery_user_config["candidate_count"] = 1
        recovery_selection = spec.selection.model_copy(
            update={
                "config_json": canonical_json(recovery_user_config),
                "config_digest": canonical_digest(recovery_user_config),
            }
        )
        recovery_plan_identity = canonical_digest(
            {
                "operation_id": intent.operation_id,
                "recovery_id": intent.recovery_id,
                "source_authority_sha256": intent.source_authority_sha256,
                "source_plan_sha256": canonical_digest(spec.plan),
                "target_id": spec.target_id,
            }
        )
        recovery_spec = replace(
            spec,
            config=recovery_user_config,
            selection=recovery_selection,
            plan=spec.plan.model_copy(
                update={
                    "plan_id": f"recovery-plan-{recovery_plan_identity}",
                    "selections": tuple(
                        recovery_selection
                        if selection.target_id == spec.target_id
                        else selection
                        for selection in spec.plan.selections
                    ),
                }
            ),
        )
        recovery_payload = dict(legacy_payload)
        recovery_payload["config"] = {
            **legacy_payload["config"],
            **recovery_user_config,
        }
        recovery_leakage_basis: dict[str, object] | None = None
        if spec.method == "agent_system_gepa_reflector":
            audit = recovery_payload["config"].get("agent_system_audit")
            audit = dict(audit) if isinstance(audit, dict) else {}
            recovery_leakage_basis = self._recovery_leakage_basis(
                context,
                dataset,
                client,
                rollout_task_id=record.receipt.rollout_task_id,
            )
            audit["leakage_basis"] = recovery_leakage_basis
            recovery_payload["config"]["agent_system_audit"] = audit
        job_owner_successor_transition_id = intent.recovery_id
        if adopted_job is None:
            job_output = self._run_one_method(
                client,
                spec=recovery_spec,
                legacy_payload=recovery_payload,
                transition_attempt_id=intent.operation_id,
                transition_attempt_ordinal=1,
                successor_transition_id=intent.recovery_id,
                predecessor_successor_transition_id=(
                    prior_owner_by_target.get(spec.target_id)
                ),
                retry_terminal_job=False,
                max_reflector_model_calls=max_reflector_model_calls,
                apply_admission=False,
            )
        else:
            if (
                adopted_job.target_id != spec.target_id
                or adopted_job.method_id != spec.method
                or adopted_job.artifact_type != spec.artifact_type
                or adopted_job.source_transition_id
                != source.successor_transition_id
            ):
                raise ScienceSuccessorPreparationV2Error(
                    "adopted recovery job differs from the compiled target",
                    failure_code="recovery_adopted_job_target_mismatch",
                )
            job_owner_successor_transition_id = (
                adopted_job.job_owner_successor_transition_id
            )
            job_output = self._adopt_recovery_job_output(
                client,
                spec=recovery_spec,
                adopted_job=adopted_job,
            )
        output = job_output.output
        proposal = ArtifactResponse.model_validate(
            client.get_internal_successor_artifact(
                job_owner_successor_transition_id,
                output.artifact_id,
            )
        )
        budget_receipt = ReflectorInferenceBudgetReceipt.model_validate(
            proposal.manifest.get(
                "openevo_reflector_inference_budget"
            )
        )
        runtime_receipt = ReflectorRuntimeReceipt.model_validate(
            proposal.manifest.get("reflector_runtime_receipt")
        )
        inference_reservation = ReflectorInferenceReservationReceipt.model_validate(
            client.get_internal_reflector_inference_reservation(job_output.job_id)
        )
        if (
            adopted_job is None
            and budget_receipt.plan_id != recovery_spec.plan.plan_id
        ):
            raise ScienceSuccessorPreparationV2Error(
                "recovery proposal budget differs from its newly authorized plan",
                failure_code="recovery_proposal_budget_plan_mismatch",
            )
        if (
            (
                source.failed_job is not None
                and job_output.job_id == source.failed_job.job_id
            )
            or proposal.artifact_id != output.artifact_id
            or proposal.type.value != output.artifact_type
            or proposal.promoted is not False
            or budget_receipt.target_id != spec.target_id
            or budget_receipt.method_id != spec.method
            or budget_receipt.max_reflector_model_calls
            != max_reflector_model_calls
            or budget_receipt.actual_reflector_model_calls != 1
            or runtime_receipt.runtime_profile
            != intent.execution_identity.runtime_profile
            or runtime_receipt.runtime_digest
            != intent.execution_identity.runtime_image_digest.removeprefix("sha256:")
            or runtime_receipt.model_name != project.config.execution.codex_model
            or runtime_receipt.reasoning_effort
            != (project.config.execution.reasoning_effort or "high")
            or inference_reservation.job_id != job_output.job_id
            or inference_reservation.state != "completed"
            or inference_reservation.runtime_receipt_sha256
            != canonical_digest(runtime_receipt)
        ):
            raise ScienceSuccessorPreparationV2Error(
                "recovery proposal evidence differs from the native method output"
            )
        parent_artifact_ids = prior_context.get(spec.artifact_type, [])
        if len(parent_artifact_ids) > 1 or self._artifact_admission_root is None:
            raise ScienceSuccessorPreparationV2Error(
                "recovery proposal has ambiguous parent authority or no admission service"
            )
        parent_artifact_id = (
            None if not parent_artifact_ids else parent_artifact_ids[0]
        )
        registry_artifact_manifest_sha256 = canonical_digest(proposal.manifest)
        audit_receipt = None
        if output.artifact_type == "agent_system":
            raw_audit = proposal.manifest.get("agent_system_audit")
            required_audit_fields = {
                "enabled",
                "repair_count",
                "finding_count",
                "forbidden_literal_count",
                "leakage_basis_sha256",
            }
            if (
                not isinstance(raw_audit, dict)
                or set(raw_audit) != required_audit_fields
                or recovery_leakage_basis is None
                or raw_audit.get("enabled") is not True
                or raw_audit.get("repair_count") != 0
                or raw_audit.get("finding_count") != 0
                or type(raw_audit.get("forbidden_literal_count")) is not int
                or raw_audit["forbidden_literal_count"] <= 0
                or raw_audit.get("leakage_basis_sha256")
                != canonical_digest(recovery_leakage_basis)
            ):
                raise ScienceSuccessorPreparationV2Error(
                    "recovery agent-system artifact lacks its sanitized audit summary"
                )
            audit_payload = {
                "agent_system_audit_receipt_contract_version": "1",
                **{key: raw_audit[key] for key in sorted(required_audit_fields)},
            }
            audit_payload["content_sha256"] = canonical_digest(audit_payload)
            try:
                audit_receipt = AgentSystemAuditReceiptV1.model_validate(
                    audit_payload
                )
            except Exception as exc:
                raise ScienceSuccessorPreparationV2Error(
                    "recovery agent-system artifact audit summary is invalid"
                ) from exc
        admission_report_sha256 = science_successor_recovery_admission_report_sha256(
            job_id=job_output.job_id,
            target_id=spec.target_id,
            method_id=spec.method,
            proposal_artifact_id=output.artifact_id,
            proposal_payload_manifest_sha256=output.manifest_sha256,
            registry_artifact_manifest_sha256=registry_artifact_manifest_sha256,
            agent_system_audit_receipt_sha256=(
                None if audit_receipt is None else audit_receipt.content_sha256
            ),
            reflector_runtime_receipt_sha256=canonical_digest(runtime_receipt),
            reflector_inference_reservation_sha256=(
                inference_reservation.content_sha256
            ),
            reflector_inference_budget_sha256=budget_receipt.content_sha256,
        )
        decision_id = (
            "recovery-admission-"
            + canonical_digest(
                {
                    "operation_id": intent.operation_id,
                    "job_id": job_output.job_id,
                    "artifact_id": output.artifact_id,
                }
            )[:40]
        )
        admission_service = NativeArtifactAdmissionService(
            registry=client,
            root=self._artifact_admission_root,
        )
        admission_authority = admission_service.issue_admission_authority(
            producer="core-science-successor-recovery-v1"
        )
        admission_decision = admission_service.decide(
            authority=admission_authority,
            request=ArtifactProposalDecisionRequest(
                decision_id=decision_id,
                job_id=job_output.job_id,
                artifact_type=ArtifactType(output.artifact_type),
                action=ProposalAction.UPDATE,
                parent_artifact_id=parent_artifact_id,
                genesis=parent_artifact_id is None,
                selected_artifact_id=output.artifact_id,
                reason="Core native recovery proposal passed closed structural admission",
                admission=ArtifactAdmissionEvidence(
                    validator_id="core-native-recovery-admission-v1",
                    schema_version="1",
                    passed=True,
                    report_sha256=admission_report_sha256,
                ),
                content_admission_basis=self._content_admission_basis(
                    context,
                    dataset,
                    client,
                ),
            ),
        )
        authority = SuccessorArtifactAuthorityResponse.model_validate(
            client.get_internal_successor_artifact_authority(
                job_owner_successor_transition_id,
                output.artifact_id,
            )
        )
        if (
            authority.successor_transition_id
            != job_owner_successor_transition_id
            or authority.job_id != job_output.job_id
            or authority.artifact.artifact_id != output.artifact_id
            or authority.artifact.type.value != output.artifact_type
            or authority.artifact.promoted is not True
            or authority.artifact.manifest != proposal.manifest
            or canonical_digest(authority.artifact.manifest)
            != registry_artifact_manifest_sha256
            or authority.payload_manifest_sha256 != output.manifest_sha256
            or authority.payload_byte_size != output.byte_size
            or admission_decision.job_id != authority.job_id
            or admission_decision.active_artifact_id != output.artifact_id
            or not admission_decision.content_admission.passed
            or authority.content_admission
            != admission_decision.content_admission
            or admission_decision.content_sha256
            != canonical_digest(
                admission_decision.model_dump(
                    mode="json",
                    exclude={"content_sha256"},
                )
            )
        ):
            raise ScienceSuccessorPreparationV2Error(
                "recovery promotion authority differs from its admission decision"
            )
        admitted_output = output.model_copy(
            update={
                "evolution_job_id": job_output.job_id,
                "admission_action": "update",
                "admission_decision_id": admission_decision.decision_id,
                "admission_decision_sha256": admission_decision.content_sha256,
                "content_admission_sha256": (
                    admission_decision.content_admission.content_sha256
                ),
                "content_admission": admission_decision.content_admission,
                "proposal_artifact_ids": admission_decision.proposal_artifact_ids,
                "origin": "produced",
                "owner_successor_transition_id": (
                    job_owner_successor_transition_id
                ),
            }
        )
        return ScienceSuccessorRecoveryTargetResultV1(
            operation_id=intent.operation_id,
            recovery_id=intent.recovery_id,
            target_id=intent.target.target_id,
            method_id=intent.target.method_id,
            source_failed_job_id=science_successor_recovery_failure_id(source),
            source_failure_kind=(
                "evolution_job"
                if source.failed_job is not None
                else "pre_job_operation"
            ),
            source_failed_job_retried=False,
            job_owner_successor_transition_id=(
                job_owner_successor_transition_id
            ),
            job_id=authority.job_id,
            job_state="succeeded",
            proposal_ids=authority.proposal_artifact_ids,
            admission_status="accepted",
            promotion_status="promoted",
            output=admitted_output,
            registry_artifact_id=authority.artifact.artifact_id,
            registry_manifest_sha256=authority.payload_manifest_sha256,
            registry_artifact_manifest_sha256=(
                registry_artifact_manifest_sha256
            ),
            agent_system_audit=audit_receipt,
            admission_decision=admission_decision,
            reflector_runtime_receipt=runtime_receipt,
            reflector_inference_reservation=inference_reservation,
            inference_budget_receipt=budget_receipt,
            completed_at=self._clock().astimezone(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
        )

    def materialize_recovery_project_seed(
        self,
        context: ScienceSuccessorPreparationContextV2,
        *,
        source: ExhaustedSuccessorRecoveryAuthorityV1,
        recovery_id: str,
        destination_predecessor: m2.ProjectHeadRefV2,
        results: tuple[ScienceSuccessorRecoveryTargetResultV1, ...],
        binding: ServiceRunBinding,
        client: EvolutionClientProtocol,
        carried_completed_targets: tuple[
            ScienceSuccessorRecoveryCarriedTargetAuthorityV1, ...
        ] = (),
    ) -> tuple[
        m2.EvolutionRevisionRefV2,
        SuccessorMaterializationV2,
        tuple[SuccessorArtifactContributionV2, ...],
    ]:
        """Materialize verified recovery outputs for one fresh Core project."""

        self._require_running()
        _record, _result, source_project = self._authority(context)
        assert _record.evidence is not None
        expected_targets = tuple(item.target_id for item in context.plan.enabled_methods)
        observed_targets = tuple(item.target_id for item in results)
        carried_by_target = {
            item.prior_result.target_id: item for item in carried_completed_targets
        }
        if (
            context.transition.state != "failed"
            or context.transition_attempt.state != "failed"
            or destination_predecessor.generation != 0
            or destination_predecessor.predecessor_project_head_id is not None
            or destination_predecessor.evolution_revision.artifact_count != 0
            or destination_predecessor.registry_sha256 != binding.registry_digest
            or expected_targets != ("agent_system", "skill_bundle", "text_memory")
            or not _recovery_seed_target_inventory_matches(
                expected_targets,
                observed_targets,
            )
            or len({item.registry_artifact_id for item in results}) != 3
            or len(carried_by_target) != len(carried_completed_targets)
            or any(
                not _recovery_seed_result_is_authorized(
                    recovery_id=recovery_id,
                    result=item,
                    carried_by_target=carried_by_target,
                )
                or item.job_state != "succeeded"
                or item.admission_status != "accepted"
                or item.promotion_status != "promoted"
                or item.output.origin != "produced"
                or item.output.owner_successor_transition_id
                != (
                    item.job_owner_successor_transition_id
                    or item.recovery_id
                )
                or item.output.artifact_id != item.registry_artifact_id
                for item in results
            )
        ):
            raise ScienceSuccessorPreparationV2Error(
                "successor recovery outputs cannot seed the destination project",
                failure_code="recovery_seed_input_authority_mismatch",
            )
        composition = _recovery_seed_contributions(results)
        revision_payload = {
            "artifacts": [item.model_dump(mode="json") for item in composition],
            "destination_project_id": destination_predecessor.project_id,
            "predecessor_project_head_id": destination_predecessor.project_head_id,
            "recovery_id": recovery_id,
            "recovery_source_authority_sha256": science_successor_recovery_source_sha256(
                source
            ),
            "result_sha256": [canonical_digest(item) for item in results],
            "schema_version": "openevo.successor_recovery_seed_revision.v1",
        }
        revision_sha256 = canonical_digest(revision_payload)
        revision = m2.EvolutionRevisionRefV2(
            evolution_revision_id=f"evolution-{revision_sha256}",
            project_id=destination_predecessor.project_id,
            manifest_sha256=revision_sha256,
            artifact_count=3,
        )
        artifact_ids = tuple(item.artifact_id for item in composition)
        request = ContextProjectionResolveRequest(
            task_id=context.task.task_id,
            instruction=source_project.config.task.objective,
            successor_transition_id=recovery_id,
            predecessor_project_head_id=destination_predecessor.project_head_id,
            agent={"harness": "codex", "settings": {"auth_mode": "subscription"}},
            base_model=source_project.config.execution.codex_model,
            policy_version=_record.evidence.policy_version,
            rollout_step=0,
            metadata={
                "task_tags": [_successor_context_task_tag(context)],
                "evolution": {
                    "context_artifact_ids": artifact_ids,
                    "context_artifact_owner_transition_ids": tuple(
                        item.owner_successor_transition_id
                        for item in composition
                    ),
                },
            },
            execution_profile=execution_profile_for_release_mode(
                source_project.config.execution.mode
            ),
            destination_roots=RuntimeDestinationRoots(
                target_data="/openevo/session/evolution",
                harness_skills="/openevo/session/evolution/skills",
                harness_instruction=MANAGED_WORKSPACE,
            ),
        )
        materialized = _materialized_context_from_wire(
            client.create_materialized_context(request.model_dump(mode="json"))
        )
        if (
            materialized.registry_digest != binding.registry_digest
            or materialized.successor_transition_id != recovery_id
            or materialized.predecessor_project_head_id
            != destination_predecessor.project_head_id
            or materialized.selection.artifact_ids != artifact_ids
        ):
            raise ScienceSuccessorPreparationV2Error(
                "recovery seed materialized context differs from its authority",
                failure_code="recovery_seed_materialized_context_mismatch",
            )
        materialized_sha256 = canonical_digest(materialized)
        runtime_payload = {
            "destination_project_id": destination_predecessor.project_id,
            "evolution_revision": revision.model_dump(mode="json"),
            "materialized_context_id": materialized.context_id,
            "materialized_context_manifest_sha256": materialized_sha256,
            "recovery_id": recovery_id,
            "registry_sha256": binding.registry_digest,
            "runtime_contract_sha256": (
                destination_predecessor.runtime_context_snapshot.runtime_contract_sha256
            ),
        }
        runtime_sha256 = canonical_digest(runtime_payload)
        runtime = m2.RuntimeContextSnapshotRefV2(
            runtime_context_snapshot_id=f"runtime-context-{runtime_sha256}",
            project_id=destination_predecessor.project_id,
            evolution_revision_id=revision.evolution_revision_id,
            evolution_revision_manifest_sha256=revision.manifest_sha256,
            registry_sha256=binding.registry_digest,
            runtime_contract_sha256=(
                destination_predecessor.runtime_context_snapshot.runtime_contract_sha256
            ),
            manifest_sha256=runtime_sha256,
        )
        return (
            revision,
            SuccessorMaterializationV2(
                project_id=destination_predecessor.project_id,
                successor_transition_id=recovery_id,
                predecessor_project_head_id=destination_predecessor.project_head_id,
                runtime_context_source="materialized_new",
                materialized_context_id=materialized.context_id,
                materialized_context_manifest_sha256=materialized_sha256,
                runtime_context_snapshot=runtime,
            ),
            composition,
        )

    @staticmethod
    def _recovery_leakage_basis(
        context: ScienceSuccessorPreparationContextV2,
        dataset: SealedTranscriptDatasetV2,
        client: EvolutionClientProtocol,
        *,
        rollout_task_id: str,
    ) -> dict[str, object]:
        """Derive a private output-denylist from verified task-local feedback.

        The values remain Core-owned job configuration.  Public admission and
        recovery receipts expose only hashes and finding counts, never the
        protected literals themselves.
        """

        binding = dataset.training_feedback
        if binding is None:
            raise ScienceSuccessorPreparationV2Error(
                "recovery agent-system evolution lacks training feedback authority",
                failure_code="recovery_training_feedback_authority_missing",
            )
        attachments = tuple(
            TrainingFeedbackAttachment.model_validate(
                client.get_training_feedback_attachment(attachment_id)
            )
            for attachment_id in binding.attachment_ids
        )
        observed_ids = tuple(item.attachment_id for item in attachments)
        observed_hashes = tuple(item.content_sha256 for item in attachments)
        if (
            binding.task_scope_id != context.task.task_id
            or observed_ids != binding.attachment_ids
            or observed_hashes != binding.attachment_sha256
            or any(
                item.task_id != rollout_task_id
                or item.task_scope_id != binding.task_scope_id
                or item.dataset_id != dataset.dataset_id
                or item.dataset_revision != binding.completed_dataset_revision
                or item.status != "sealed"
                or item.authority.value != "evaluator_only"
                for item in attachments
            )
        ):
            raise ScienceSuccessorPreparationV2Error(
                "recovery leakage basis differs from verified feedback authority",
                failure_code="recovery_training_feedback_authority_mismatch",
            )
        task_specific_feedback: list[dict[str, object]] = []
        benchmark_task_ids: set[str] = set()
        for item in attachments:
            raw_benchmark_id = item.task_local_feedback.get(
                "benchmark_task_scope_id"
            )
            if isinstance(raw_benchmark_id, str) and re.fullmatch(
                r"[A-Za-z][A-Za-z0-9-]{1,48}_[0-9]{3,}",
                raw_benchmark_id,
            ):
                benchmark_task_ids.add(raw_benchmark_id)
            extracted = (
                ProductionScienceSuccessorPreparerV2._task_specific_feedback_literals(
                    item.task_local_feedback
                )
            )
            if extracted:
                task_specific_feedback.append(extracted)
        return {
            "task_ids": sorted(
                {
                    context.task.task_id,
                    dataset.task_id,
                    *(item.task_id for item in attachments),
                    *benchmark_task_ids,
                }
            ),
            "task_scope_ids": sorted(
                {binding.task_scope_id, *(item.task_scope_id for item in attachments)}
            ),
            "task_specific_feedback": task_specific_feedback,
        }

    @staticmethod
    def _task_specific_feedback_literals(value: object) -> dict[str, object]:
        """Select only typed answer/source values from task-local feedback."""

        if not isinstance(value, dict):
            return {}
        selected: dict[str, object] = {}
        for raw_key, nested in value.items():
            key = str(raw_key).strip().lower().replace("-", "_")
            if key in {"report_title", "document_title"}:
                titles = task_specific_report_titles(
                    nested,
                    explicit_title=True,
                )
                if titles:
                    selected["source_titles"] = titles
                continue
            if key in {"report_heading", "report_headings"}:
                titles = task_specific_report_titles(nested)
                if titles:
                    selected["source_titles"] = titles
                continue
            if key in TASK_SPECIFIC_LITERAL_KEYS:
                selected[key] = nested
                continue
            child = ProductionScienceSuccessorPreparerV2._task_specific_feedback_literals(
                nested
            )
            if child:
                selected[key] = child
        return selected

    def recovery_codex_model(
        self,
        context: ScienceSuccessorPreparationContextV2,
    ) -> str:
        """Return the project-owned Codex model for a verified recovery source."""

        _record, _result, project = self._authority(context)
        model = project.config.execution.codex_model
        if not isinstance(model, str) or not model:
            raise ScienceSuccessorPreparationV2Error(
                "recovery source does not own a subscription Codex model"
            )
        return model

    def validate_recovery_source_dataset(
        self,
        context: ScienceSuccessorPreparationContextV2,
        expected: SealedTranscriptDatasetV2,
        *,
        client: EvolutionClientProtocol,
    ) -> SealedTranscriptDatasetV2:
        """Rebuild the immutable source dataset receipt on a current binding."""

        record, result, _project = self._authority(context)
        assert record.evidence is not None
        dataset = DatasetCreateResponse.model_validate(
            client.get_dataset(expected.dataset_id)
        )
        artifact = ArtifactResponse.model_validate(
            client.get_artifact(dataset.artifact_id)
        )
        base = self._sealed_dataset_receipt(
            context,
            dataset=dataset,
            artifact=artifact,
            expected_name=(
                f"{context.task.project_id}:{context.task.task_id}:"
                f"{context.accepted_attempt.attempt_id}"
            ),
            expected_idempotency_key=_successor_dataset_idempotency_key(
                context,
                record,
            ),
            expected_record=record,
            expected_record_count=record.evidence.transcript_record_count,
            captured_trace_count=len(result.trajectory.traces),
        )
        if base != expected.model_copy(update={"training_feedback": None}):
            raise ScienceSuccessorPreparationV2Error(
                "recovery source dataset changed after its failed successor"
            )
        return expected

    def recovery_source_dataset(
        self,
        context: ScienceSuccessorPreparationContextV2,
        *,
        client: EvolutionClientProtocol,
    ) -> SealedTranscriptDatasetV2:
        """Reconstruct the base sealed dataset from a failed Core transition."""

        record, result, _project = self._authority(context)
        assert record.evidence is not None
        dataset_id = context.transition_attempt.dataset_id
        if not isinstance(dataset_id, str) or not dataset_id:
            raise ScienceSuccessorPreparationV2Error(
                "recovery source transition lacks a sealed dataset"
            )
        dataset = DatasetCreateResponse.model_validate(client.get_dataset(dataset_id))
        artifact = ArtifactResponse.model_validate(
            client.get_artifact(dataset.artifact_id)
        )
        return self._sealed_dataset_receipt(
            context,
            dataset=dataset,
            artifact=artifact,
            expected_name=(
                f"{context.task.project_id}:{context.task.task_id}:"
                f"{context.accepted_attempt.attempt_id}"
            ),
            expected_idempotency_key=_successor_dataset_idempotency_key(
                context,
                record,
            ),
            expected_record=record,
            expected_record_count=record.evidence.transcript_record_count,
            captured_trace_count=len(result.trajectory.traces),
        )

    def validate_outputs(
        self,
        context: ScienceSuccessorPreparationContextV2,
        dataset: SealedTranscriptDatasetV2,
        outputs: tuple[ScienceMethodOutputV2, ...],
    ) -> ValidatedScienceOutputsV2:
        self._require_running()
        record, _result, project = self._authority(context)
        expected = tuple(
            (item.target_id, item.method_id, item.output_artifact_type)
            for item in context.plan.enabled_methods
        )
        actual = tuple((item.target_id, item.method_id, item.artifact_type) for item in outputs)
        if actual != expected:
            raise ScienceSuccessorPreparationV2Error(
                "method outputs do not exactly cover the successor plan"
            )
        with self._evolution(
            context,
            record,
            project,
        ) as (_binding, client):
            (
                _prior_context,
                inherited,
                _prior_owner_by_target,
            ) = self._prior_context_artifacts(context, client)
        composition_by_target = {
            item.target_id: item
            for item in inherited
        }
        transition_id = (
            context.transition.transition.successor_transition_id
        )
        for output in outputs:
            if (
                output.admission_action is None
                or output.evolution_job_id is None
                or output.admission_decision_id is None
                or output.admission_decision_sha256 is None
                or output.content_admission_sha256 is None
                or output.content_admission is None
                or not output.proposal_artifact_ids
                or output.owner_successor_transition_id is None
            ):
                raise ScienceSuccessorPreparationV2Error(
                    "method output lacks native admission authority"
                )
            if output.origin == "produced":
                if output.owner_successor_transition_id != transition_id:
                    raise ScienceSuccessorPreparationV2Error(
                        "produced method output has a foreign transition owner"
                    )
                composition_by_target[output.target_id] = (
                    SuccessorArtifactContributionV2(
                        target_id=output.target_id,
                        artifact_id=output.artifact_id,
                        artifact_type=output.artifact_type,
                        owner_successor_transition_id=transition_id,
                        origin="produced",
                        evolution_job_id=output.evolution_job_id,
                        admission_action=output.admission_action,
                        admission_decision_id=output.admission_decision_id,
                        admission_decision_sha256=(
                            output.admission_decision_sha256
                        ),
                        content_admission=output.content_admission,
                        proposal_artifact_ids=output.proposal_artifact_ids,
                    )
                )
            else:
                inherited_output = composition_by_target.get(output.target_id)
                if (
                    inherited_output is None
                    or inherited_output.artifact_id != output.artifact_id
                    or inherited_output.artifact_type != output.artifact_type
                    or inherited_output.owner_successor_transition_id
                    != output.owner_successor_transition_id
                ):
                    raise ScienceSuccessorPreparationV2Error(
                        "keep/reject output differs from inherited registry authority"
                    )
                composition_by_target[output.target_id] = (
                    SuccessorArtifactContributionV2(
                        target_id=output.target_id,
                        artifact_id=output.artifact_id,
                        artifact_type=output.artifact_type,
                        owner_successor_transition_id=(
                            output.owner_successor_transition_id
                        ),
                        origin="inherited",
                        evolution_job_id=output.evolution_job_id,
                        admission_action=output.admission_action,
                        admission_decision_id=output.admission_decision_id,
                        admission_decision_sha256=(
                            output.admission_decision_sha256
                        ),
                        content_admission=output.content_admission,
                        proposal_artifact_ids=output.proposal_artifact_ids,
                    )
                )
        composition = tuple(
            composition_by_target[target_id]
            for target_id in sorted(composition_by_target)
        )
        if not context.plan.enabled_methods:
            predecessor_revision = (
                context.task.admission.predecessor_project_head.evolution_revision
            )
            return ValidatedScienceOutputsV2(
                project_id=context.task.project_id,
                successor_transition_id=transition_id,
                predecessor_project_head_id=(
                    context.task.admission.predecessor_project_head.project_head_id
                ),
                dataset=dataset,
                outputs=outputs,
                composition=composition,
                evolution_revision=predecessor_revision,
            )
        if all(item.origin == "inherited" for item in outputs):
            revision = (
                context.task.admission.predecessor_project_head.evolution_revision
            )
            return ValidatedScienceOutputsV2(
                project_id=context.task.project_id,
                successor_transition_id=transition_id,
                predecessor_project_head_id=(
                    context.task.admission.predecessor_project_head.project_head_id
                ),
                dataset=dataset,
                outputs=outputs,
                composition=composition,
                evolution_revision=revision,
            )
        manifest = {
            "artifacts": [
                item.model_dump(mode="json")
                for item in composition
            ],
            "dataset": dataset.model_dump(mode="json"),
            "evolution_revision_contract_version": "2",
            "method_outputs": [
                item.model_dump(mode="json") for item in outputs
            ],
            "predecessor_evolution_revision": (
                context.task.admission.predecessor_project_head.evolution_revision.model_dump(
                    mode="json"
                )
            ),
            "predecessor_project_head_id": (
                context.task.admission.predecessor_project_head.project_head_id
            ),
            "project_id": context.task.project_id,
            "successor_transition_id": (context.transition.transition.successor_transition_id),
        }
        digest = canonical_digest(manifest)
        revision = m2.EvolutionRevisionRefV2(
            evolution_revision_id=f"evolution-{digest}",
            project_id=context.task.project_id,
            manifest_sha256=digest,
            artifact_count=len(composition),
        )
        return ValidatedScienceOutputsV2(
            project_id=context.task.project_id,
            successor_transition_id=(context.transition.transition.successor_transition_id),
            predecessor_project_head_id=(
                context.task.admission.predecessor_project_head.project_head_id
            ),
            dataset=dataset,
            outputs=outputs,
            composition=composition,
            evolution_revision=revision,
        )

    def materialize_context(
        self,
        context: ScienceSuccessorPreparationContextV2,
        validated: ValidatedScienceOutputsV2,
    ) -> SuccessorMaterializationV2:
        self._require_running()
        if not context.plan.enabled_methods:
            return self._inherited_materialization(
                context,
                validated,
            )
        record, _result, project = self._authority(context)
        assert record.evidence is not None
        output_ids = tuple(
            item.artifact_id for item in validated.composition
        )
        owner_transition_ids = tuple(
            item.owner_successor_transition_id
            for item in validated.composition
        )
        replayed_materialization = False
        with self._evolution(context, record, project) as (binding, client):
            request = ContextProjectionResolveRequest(
                task_id=context.task.task_id,
                instruction=project.config.task.objective,
                successor_transition_id=(context.transition.transition.successor_transition_id),
                predecessor_project_head_id=(
                    context.task.admission.predecessor_project_head.project_head_id
                ),
                agent={
                    "harness": "codex",
                    "settings": {"auth_mode": "subscription"},
                },
                base_model=project.config.execution.codex_model,
                policy_version=record.evidence.policy_version,
                rollout_step=0,
                metadata={
                    "task_tags": [_successor_context_task_tag(context)],
                    "evolution": {
                        "context_artifact_ids": output_ids,
                        "context_artifact_owner_transition_ids": (
                            owner_transition_ids
                        ),
                    },
                },
                execution_profile=execution_profile_for_release_mode(
                    project.config.execution.mode
                ),
                destination_roots=RuntimeDestinationRoots(
                    target_data="/openevo/session/evolution",
                    harness_skills="/openevo/session/evolution/skills",
                    harness_instruction=MANAGED_WORKSPACE,
                ),
            )
            request_payload = request.model_dump(mode="json")
            if context.transition_attempt.reconciliation_only is True:
                request_digest = canonical_digest(request)
                try:
                    raw_materialized = (
                        client.get_internal_successor_materialized_context(
                            context.transition.transition.successor_transition_id,
                            request_digest,
                        )
                    )
                    replayed_materialization = True
                except EvolutionHttpStatusError as lookup_error:
                    if lookup_error.status_code != 404:
                        raise
                    try:
                        raw_materialized = client.create_materialized_context(
                            request_payload
                        )
                    except (
                        EvolutionHttpStatusError,
                        ValueError,
                        httpx.TransportError,
                    ) as publication_error:
                        try:
                            raw_materialized = (
                                client.get_internal_successor_materialized_context(
                                    context.transition.transition.successor_transition_id,
                                    request_digest,
                                )
                            )
                            replayed_materialization = True
                        except (
                            EvolutionHttpStatusError,
                            ValueError,
                            httpx.TransportError,
                        ):
                            raise publication_error
            else:
                raw_materialized = client.create_materialized_context(
                    request_payload
                )
            materialized = MaterializedContext.model_validate_json(
                json.dumps(
                    raw_materialized,
                    ensure_ascii=True,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        self._require_running()
        allowed_registry_digests = {
            context.task.admission.registry_sha256,
        }
        if context.transition_attempt.reconciliation_only is True:
            allowed_registry_digests.add(binding.registry_digest)
            if replayed_materialization:
                # The Core-only readback endpoint already proved a unique,
                # canonical request digest for this exact transition. Its
                # registry is durable publication authority even if another
                # repair release is now active.
                allowed_registry_digests.add(materialized.registry_digest)
        if (
            materialized.registry_digest not in allowed_registry_digests
            or materialized.successor_transition_id
            != context.transition.transition.successor_transition_id
            or materialized.predecessor_project_head_id
            != context.task.admission.predecessor_project_head.project_head_id
            or materialized.selection.artifact_ids != output_ids
        ):
            raise ScienceSuccessorPreparationV2Error(
                "materialized context differs from validated successor outputs"
            )
        materialized_sha256 = canonical_digest(materialized)
        predecessor_runtime = (
            context.task.admission.predecessor_project_head.runtime_context_snapshot
        )
        runtime_manifest = {
            "evolution_revision": validated.evolution_revision.model_dump(mode="json"),
            "materialized_context_id": materialized.context_id,
            "materialized_context_manifest_sha256": materialized_sha256,
            "project_id": context.task.project_id,
            "registry_sha256": materialized.registry_digest,
            "runtime_context_contract_version": "2",
            "runtime_contract_sha256": predecessor_runtime.runtime_contract_sha256,
            "selected_artifact_ids": list(output_ids),
            "successor_transition_id": (context.transition.transition.successor_transition_id),
        }
        runtime_sha256 = canonical_digest(runtime_manifest)
        runtime = m2.RuntimeContextSnapshotRefV2(
            runtime_context_snapshot_id=f"runtime-context-{runtime_sha256}",
            project_id=context.task.project_id,
            evolution_revision_id=validated.evolution_revision.evolution_revision_id,
            evolution_revision_manifest_sha256=(validated.evolution_revision.manifest_sha256),
            registry_sha256=materialized.registry_digest,
            runtime_contract_sha256=predecessor_runtime.runtime_contract_sha256,
            manifest_sha256=runtime_sha256,
        )
        return SuccessorMaterializationV2(
            project_id=context.task.project_id,
            successor_transition_id=(context.transition.transition.successor_transition_id),
            predecessor_project_head_id=(
                context.task.admission.predecessor_project_head.project_head_id
            ),
            materialized_context_id=materialized.context_id,
            materialized_context_manifest_sha256=materialized_sha256,
            runtime_context_snapshot=runtime,
        )

    def _inherited_materialization(
        self,
        context: ScienceSuccessorPreparationContextV2,
        validated: ValidatedScienceOutputsV2,
    ) -> SuccessorMaterializationV2:
        predecessor = (
            context.task.admission.predecessor_project_head
        )
        artifact_ids = tuple(
            item.artifact_id for item in validated.composition
        )
        if (
            validated.outputs
            or any(
                item.origin != "inherited"
                for item in validated.composition
            )
            or validated.evolution_revision
            != predecessor.evolution_revision
            or len(artifact_ids)
            != predecessor.evolution_revision.artifact_count
        ):
            raise ScienceSuccessorPreparationV2Error(
                "no-evolution successor changed predecessor evolution authority"
            )
        commit = self._ledger.successor_commit_for_project_head(
            predecessor.project_head_id
        )
        runtime_source: str
        source_transition_id: str | None
        source_predecessor_id: str | None
        materialized_context_id: str | None
        materialized_manifest_sha256: str | None
        if commit is None:
            if (
                predecessor.generation != 0
                or predecessor.evolution_revision.artifact_count != 0
                or artifact_ids
            ):
                raise ScienceSuccessorPreparationV2Error(
                    "no-evolution successor lacks exact predecessor receipt"
                )
            runtime_source = "empty_inherited"
            source_transition_id = None
            source_predecessor_id = None
            materialized_context_id = None
            materialized_manifest_sha256 = None
        else:
            manifest = commit.manifest
            if (
                manifest.method_artifact_ids != artifact_ids
                or (
                    manifest.artifacts
                    and tuple(
                        (
                            item.target_id,
                            item.artifact_id,
                            item.artifact_type,
                            item.owner_successor_transition_id,
                        )
                        for item in manifest.artifacts
                    )
                    != tuple(
                        (
                            item.target_id,
                            item.artifact_id,
                            item.artifact_type,
                            item.owner_successor_transition_id,
                        )
                        for item in validated.composition
                    )
                )
            ):
                raise ScienceSuccessorPreparationV2Error(
                    "no-evolution successor artifact receipt changed"
                )
            if type(manifest) is AtomicSuccessorManifestV2:
                if (
                    manifest.runtime_context_source
                    == "materialized_new"
                ):
                    runtime_source = "materialized_inherited"
                    source_transition_id = (
                        manifest.successor_transition_id
                    )
                    source_predecessor_id = (
                        manifest.predecessor_project_head_id
                    )
                    materialized_context_id = (
                        manifest.materialized_context_id
                    )
                    materialized_manifest_sha256 = (
                        manifest.materialized_context_manifest_sha256
                    )
                else:
                    runtime_source = manifest.runtime_context_source
                    source_transition_id = (
                        manifest.materialized_source_successor_transition_id
                    )
                    source_predecessor_id = (
                        manifest.materialized_source_predecessor_project_head_id
                    )
                    materialized_context_id = (
                        manifest.materialized_context_id
                    )
                    materialized_manifest_sha256 = (
                        manifest.materialized_context_manifest_sha256
                    )
            elif type(manifest) is AtomicSuccessorRecoverySeedManifestV1:
                runtime_source = "materialized_inherited"
                source_transition_id = manifest.recovery_id
                source_predecessor_id = manifest.predecessor_project_head_id
                materialized_context_id = manifest.materialized_context_id
                materialized_manifest_sha256 = (
                    manifest.materialized_context_manifest_sha256
                )
            elif type(manifest) in {
                AtomicEvolutionAbandonManifestV2,
                AtomicHistoricalRestoreManifestV2,
            }:
                runtime_source = manifest.runtime_context_source
                source_transition_id = (
                    manifest.materialized_source_successor_transition_id
                )
                source_predecessor_id = (
                    manifest.materialized_source_predecessor_project_head_id
                )
                materialized_context_id = (
                    manifest.materialized_context_id
                )
                materialized_manifest_sha256 = (
                    manifest.materialized_context_manifest_sha256
                )
            else:  # pragma: no cover - closed receipt union
                raise ScienceSuccessorPreparationV2Error(
                    "no-evolution successor receipt type is unsupported"
                )
        return SuccessorMaterializationV2(
            project_id=context.task.project_id,
            successor_transition_id=(
                context.transition.transition.successor_transition_id
            ),
            predecessor_project_head_id=(
                predecessor.project_head_id
            ),
            runtime_context_source=runtime_source,
            materialized_source_successor_transition_id=(
                source_transition_id
            ),
            materialized_source_predecessor_project_head_id=(
                source_predecessor_id
            ),
            materialized_context_id=materialized_context_id,
            materialized_context_manifest_sha256=(
                materialized_manifest_sha256
            ),
            runtime_context_snapshot=(
                predecessor.runtime_context_snapshot
            ),
        )

    def capture_workspace_result(
        self,
        context: ScienceSuccessorPreparationContextV2,
    ) -> AcceptedWorkspaceResultV2:
        self._require_running()
        record, result, project = self._authority(context)
        if result.workspace_result is None or record.evidence is None:
            raise ScienceSuccessorPreparationV2Error("captured Attempt has no workspace result")
        receipt = WorkspaceResultReceiptV2.model_validate(
            result.workspace_result.model_dump(mode="python")
        )
        authoritative = self._handoffs.get_result(receipt.handoff_id)
        if (
            authoritative != receipt
            or receipt.result_manifest_sha256 != record.evidence.workspace_result_manifest_sha256
            or receipt.output_archive.content_sha256 != record.evidence.workspace_archive_sha256
        ):
            raise ScienceSuccessorPreparationV2Error(
                "workspace result differs from captured execution evidence"
            )
        predecessor = context.task.admission.predecessor_project_head
        archive = receipt.output_archive
        chunk_size = min(m2.MAX_WORKSPACE_CHUNK_BYTES, archive.byte_size)
        chunk_count = (archive.byte_size + chunk_size - 1) // chunk_size
        key_seed = receipt.result_manifest_sha256[:32]
        session, _ = self._workspaces.create_upload(
            context.task.project_id,
            m2.WorkspaceUploadCreateV2(
                expected_project_head_id=predecessor.project_head_id,
                expected_project_head_manifest_sha256=predecessor.manifest_sha256,
                expected_project_config_sha256=project.project_config_sha256,
                archive=archive,
                chunk_byte_size=chunk_size,
                chunk_count=chunk_count,
            ),
            idempotency_key=f"successor-workspace-{key_seed}",
            now=self._clock(),
        )
        session = self._workspaces.get_upload(
            context.task.project_id,
            session.upload_id,
        )
        if session.state != "finalized":
            with self._handoffs.open_result(receipt) as stream:
                stream.seek(session.accepted_byte_size)
                while session.next_chunk_index < session.chunk_count:
                    self._require_running()
                    expected_size = min(
                        session.chunk_byte_size,
                        archive.byte_size - session.accepted_byte_size,
                    )
                    chunk = stream.read(expected_size)
                    if len(chunk) != expected_size:
                        raise ScienceSuccessorPreparationV2Error(
                            "workspace result ended before its declared byte size"
                        )
                    session, _ = self._workspaces.put_chunk(
                        context.task.project_id,
                        session.upload_id,
                        chunk_index=session.next_chunk_index,
                        chunk=chunk,
                        chunk_sha256=hashlib.sha256(chunk).hexdigest(),
                        chunk_byte_size=len(chunk),
                        if_match=session.etag,
                        idempotency_key=(
                            f"successor-workspace-{key_seed}-chunk-{session.next_chunk_index}"
                        ),
                        now=self._clock(),
                    )
                if stream.read(1):
                    raise ScienceSuccessorPreparationV2Error(
                        "workspace result exceeds its declared byte size"
                    )
            session, _ = self._workspaces.finalize_upload(
                context.task.project_id,
                session.upload_id,
                m2.WorkspaceUploadFinalizeV2(expected_content_sha256=archive.content_sha256),
                if_match=session.etag,
                idempotency_key=f"successor-workspace-{key_seed}-finalize",
                now=self._clock(),
            )
        snapshot = session.workspace_snapshot
        if (
            session.state != "finalized"
            or snapshot is None
            or snapshot.project_id != context.task.project_id
            or snapshot.entry_count != archive.entry_count
            or snapshot.byte_size != archive.extracted_byte_size
        ):
            raise ScienceSuccessorPreparationV2Error(
                "workspace snapshot differs from the accepted result archive"
            )
        self._handoffs.mark_consumed(receipt)
        return AcceptedWorkspaceResultV2(
            project_id=context.task.project_id,
            task_id=context.task.task_id,
            accepted_attempt_id=context.accepted_attempt.attempt_id,
            workspace_snapshot=snapshot,
        )

    def discard_transition_outputs(
        self,
        context: ScienceSuccessorCleanupContextV2,
    ) -> ScienceSuccessorCleanupReceiptV2:
        """Discard non-active outputs after the Core has committed abandon."""

        self._require_running()
        record, _result, project = self._cleanup_authority(
            context
        )
        transition_id = (
            context.transition.transition.successor_transition_id
        )
        with self._evolution(context, record, project) as (_binding, client):
            raw = client.discard_successor_transition_outputs(
                transition_id
            )
        self._require_running()
        discarded = (
            raw.get("discarded_artifact_ids")
            if type(raw) is dict
            else None
        )
        discarded_contexts = (
            raw.get("discarded_materialized_context_ids")
            if type(raw) is dict
            else None
        )
        if (
            type(raw) is not dict
            or set(raw) != {
                "successor_transition_id",
                "discarded_artifact_ids",
                "discarded_materialized_context_ids",
            }
            or raw.get("successor_transition_id") != transition_id
            or not isinstance(discarded, list)
            or not isinstance(discarded_contexts, list)
            or any(
                not isinstance(artifact_id, str) or not artifact_id
                for artifact_id in discarded
            )
            or any(
                not isinstance(context_id, str) or not context_id
                for context_id in discarded_contexts
            )
            or len(discarded) != len(set(discarded))
            or len(discarded_contexts)
            != len(set(discarded_contexts))
        ):
            raise ScienceSuccessorPreparationV2Error(
                "discard receipt differs from the requested transition"
            )
        try:
            return ScienceSuccessorCleanupReceiptV2(
                successor_transition_id=transition_id,
                discarded_artifact_ids=tuple(discarded),
                discarded_materialized_context_ids=tuple(
                    discarded_contexts
                ),
            )
        except ValueError as exc:
            raise ScienceSuccessorPreparationV2Error(
                "discard receipt differs from the requested transition"
            ) from exc

    def _authority(
        self,
        context: ScienceSuccessorPreparationContextV2,
    ) -> tuple[ScienceAttemptExecutionRecordV2, Any, ProjectRecordV2]:
        if type(context) is not ScienceSuccessorPreparationContextV2:
            raise TypeError("v2 successor preparation requires its exact context")
        return self._validated_authority(context)

    def _cleanup_authority(
        self,
        context: ScienceSuccessorCleanupContextV2,
    ) -> tuple[ScienceAttemptExecutionRecordV2, Any, ProjectRecordV2]:
        if type(context) is not ScienceSuccessorCleanupContextV2:
            raise TypeError(
                "v2 successor cleanup requires its exact context"
            )
        return self._validated_authority(context)

    def _validated_authority(
        self,
        context: ScienceSuccessorPreparationContextV2
        | ScienceSuccessorCleanupContextV2,
    ) -> tuple[ScienceAttemptExecutionRecordV2, Any, ProjectRecordV2]:
        record = self._ledger.get_attempt_execution(
            context.task.task_id,
            context.accepted_attempt.attempt_id,
        )
        result = self._ledger.get_captured_session_result(
            context.task.task_id,
            context.accepted_attempt.attempt_id,
        )
        project = self._catalog.get_project(context.task.project_id)
        if (
            type(project) is not ProjectRecordV2
            or record.state != "captured"
            or record.receipt is None
            or record.evidence is None
            or record.successor_plan != context.plan
            or project.project_config_sha256 != context.task.admission.project_config_sha256
            or m2.project_config_sha256_for(project.config) != project.project_config_sha256
        ):
            raise ScienceSuccessorPreparationV2Error(
                "successor preparation authority is incomplete or changed"
            )
        return record, result, project

    @contextmanager
    def _evolution(
        self,
        context: ScienceSuccessorPreparationContextV2
        | ScienceSuccessorCleanupContextV2,
        record: ScienceAttemptExecutionRecordV2,
        project: ProjectRecordV2,
    ) -> Iterator[tuple[ServiceRunBinding, EvolutionClientProtocol]]:
        self._require_running()
        assert record.receipt is not None
        snapshot, lease = self._services.ensure_run_binding(
            ServiceExecutionMode.CODEX_SUBSCRIPTION_TRANSCRIPT,
            codex_model=project.config.execution.codex_model,
            runtime_image=MANAGED_RUNTIME_IMAGES["managed_science"],
        )
        binding = getattr(lease, "binding", None)
        reconciliation_only = (
            type(context) is ScienceSuccessorPreparationContextV2
            and context.transition_attempt.reconciliation_only is True
        )
        if (
            lease is None
            or type(binding) is not ServiceRunBinding
            or getattr(snapshot, "run_ready", False) is not True
            or context.task.admission.registry_sha256
            != record.receipt.registry_sha256
            or binding.runtime_identity_digest != record.receipt.runtime_identity_sha256
            or (
                not reconciliation_only
                and (
                    binding.registry_digest
                    != context.task.admission.registry_sha256
                    or binding.framework_lock_digest
                    != record.receipt.framework_lock_sha256
                )
            )
        ):
            if lease is not None:
                close = getattr(lease, "close", None)
                if callable(close):
                    close()
            raise ScienceSuccessorPreparationV2Error(
                "successor service authority changed after Attempt execution"
            )
        self._require_running()
        if self._evolution_factory is None:
            assert record.evidence is not None
            client = EvolutionHttpClient(
                binding.evolution_backend_url,
                headers=binding.request_headers(),
                planned_job_evidence_root=(
                    self._planned_job_http_evidence_root
                ),
                planned_job_evidence_context={
                    "source_project_id": context.task.project_id,
                    "source_task_id": context.task.task_id,
                    "source_attempt_id": context.accepted_attempt.attempt_id,
                    "source_rollout_task_id": record.evidence.rollout_task_id,
                    "source_codex_session_id": record.evidence.session_id,
                    "source_project_head_id": (
                        context.task.admission.predecessor_project_head.project_head_id
                    ),
                    "source_project_head_manifest_sha256": (
                        context.task.admission.predecessor_project_head.manifest_sha256
                    ),
                    "source_generation": (
                        context.task.admission.predecessor_project_head.generation
                    ),
                    "successor_transition_id": (
                        context.transition.transition.successor_transition_id
                    ),
                    "successor_transition_attempt_id": (
                        context.transition_attempt.transition_attempt_id
                    ),
                    "successor_transition_attempt_ordinal": (
                        context.transition_attempt.ordinal
                    ),
                    "service_generation_sha256": binding.generation_digest,
                    "service_registry_sha256": binding.registry_digest,
                    "framework_lock_sha256": binding.framework_lock_digest,
                    "runtime_identity_sha256": binding.runtime_identity_digest,
                },
            )
        else:
            client = self._evolution_factory(binding)
        try:
            yield binding, client
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
            lease.close()

    def _compile_methods(
        self,
        context: ScienceSuccessorPreparationContextV2,
        *,
        project: ProjectRecordV2,
        binding: ServiceRunBinding,
        record: ScienceAttemptExecutionRecordV2,
    ) -> tuple[CompiledExperiment, tuple[CompiledEvolutionMethodSpec, ...]]:
        assert record.evidence is not None
        prior_dataset_ids = self._ledger.prior_dataset_artifact_ids_for_head(
            context.task.admission.predecessor_project_head.project_head_id
        )
        compiled = compile_science_evolution_experiment_v2(
            task=context.task,
            attempt=context.accepted_attempt,
            project=project,
            binding=binding,
            registry=self._registry,
            prior_dataset_artifact_ids=prior_dataset_ids,
        )
        methods = tuple(
            sorted(
                compiled.evolution_methods_for_round(
                    0,
                    prior_dataset_artifact_ids=prior_dataset_ids,
                    task_id=context.task.task_id,
                ),
                key=lambda item: item.target_id,
            )
        )
        plan = compiled.evolution_plan_for_round(
            0,
            prior_dataset_artifact_ids=prior_dataset_ids,
            task_id=context.task.task_id,
        )
        expected_methods = tuple(
            (item.target_id, item.method, item.artifact_type) for item in methods
        )
        saved_methods = tuple(
            (item.target_id, item.method_id, item.output_artifact_type)
            for item in context.plan.enabled_methods
        )
        if (
            expected_methods != saved_methods
            or compiled.tasks[0].policy_version_for_round(0) != record.evidence.policy_version
            or (
                plan.registry_snapshot_digest != methods[0].registry_snapshot_digest
                if methods
                else plan.selections != ()
            )
        ):
            raise ScienceSuccessorPreparationV2Error(
                "successor method compilation changed after terminal capture",
                failure_code="recovery_successor_method_compilation_changed",
            )
        return compiled, methods

    def _prior_context_artifacts(
        self,
        context: ScienceSuccessorPreparationContextV2,
        client: EvolutionClientProtocol,
    ) -> tuple[
        dict[str, list[str]],
        tuple[SuccessorArtifactContributionV2, ...],
        dict[str, str],
    ]:
        result = {key: [] for key in _CONTEXT_ARTIFACT_TYPES}
        predecessor_id = context.task.admission.predecessor_project_head.project_head_id
        result["dataset"] = list(self._ledger.prior_dataset_artifact_ids_for_head(predecessor_id))
        commit = self._ledger.successor_commit_for_project_head(predecessor_id)
        if commit is None:
            return result, (), {}
        manifest = commit.manifest
        if type(manifest) not in {
            AtomicSuccessorManifestV2,
            AtomicEvolutionAbandonManifestV2,
            AtomicHistoricalRestoreManifestV2,
            AtomicSuccessorRecoverySeedManifestV1,
        }:  # pragma: no cover - closed receipt union
            raise ScienceSuccessorPreparationV2Error(
                "predecessor context has an unsupported commit receipt",
                failure_code="recovery_prior_context_commit_unsupported",
            )
        stored_contributions = manifest.artifacts
        if stored_contributions and tuple(
            item.artifact_id for item in stored_contributions
        ) != manifest.method_artifact_ids:
            raise ScienceSuccessorPreparationV2Error(
                "predecessor artifact composition differs from its receipt",
                failure_code="recovery_prior_context_composition_mismatch",
            )
        if type(manifest) is AtomicSuccessorManifestV2:
            legacy_owner = manifest.successor_transition_id
        elif type(manifest) is AtomicSuccessorRecoverySeedManifestV1:
            legacy_owner = manifest.recovery_id
        else:
            legacy_owner = (
                manifest.materialized_source_successor_transition_id
            )
        contributions: list[SuccessorArtifactContributionV2] = []
        owner_by_target: dict[str, str] = {}
        for index, artifact_id in enumerate(
            manifest.method_artifact_ids
        ):
            stored = (
                None
                if not stored_contributions
                else stored_contributions[index]
            )
            owner_transition_id = (
                legacy_owner
                if stored is None
                else stored.owner_successor_transition_id
            )
            if owner_transition_id is None:
                raise ScienceSuccessorPreparationV2Error(
                    "predecessor context has no artifact owner transition",
                    failure_code="recovery_prior_context_owner_missing",
                )
            artifact = ArtifactResponse.model_validate(
                client.get_internal_successor_artifact(
                    owner_transition_id,
                    artifact_id,
                )
            )
            artifact_type = str(artifact.type)
            if stored is None:
                candidate_targets = tuple(
                    target.id
                    for target in self._registry.snapshot.targets.values()
                    if target.artifact_type == artifact_type
                )
                if len(candidate_targets) != 1:
                    raise ScienceSuccessorPreparationV2Error(
                        "legacy predecessor artifact has ambiguous target ownership",
                        failure_code="recovery_prior_context_target_ambiguous",
                    )
                target_id = candidate_targets[0]
            else:
                target_id = stored.target_id
                target = self._registry.snapshot.targets.get(
                    target_id
                )
                if (
                    stored.artifact_id != artifact_id
                    or stored.artifact_type != artifact_type
                    or target is None
                    or target.artifact_type != artifact_type
                ):
                    raise ScienceSuccessorPreparationV2Error(
                        "predecessor artifact differs from its typed contribution",
                        failure_code="recovery_prior_context_contribution_invalid",
                    )
            if (
                artifact.artifact_id != artifact_id
                or artifact.state
                not in {
                    ArtifactState.ACTIVE,
                    ArtifactState.SEALED,
                }
                or artifact.promoted is not True
                or artifact_type not in result
                or artifact_type == "dataset"
                or target_id in owner_by_target
            ):
                raise ScienceSuccessorPreparationV2Error(
                    "predecessor context contains a non-target artifact",
                    failure_code="recovery_prior_context_registry_invalid",
                )
            result[artifact_type].append(artifact.artifact_id)
            owner_by_target[target_id] = owner_transition_id
            contributions.append(
                SuccessorArtifactContributionV2(
                    target_id=target_id,
                    artifact_id=artifact_id,
                    artifact_type=artifact_type,
                    owner_successor_transition_id=(
                        owner_transition_id
                    ),
                    origin="inherited",
                )
            )
        for values in result.values():
            values.sort()
        ordered = tuple(
            sorted(
                contributions,
                key=lambda item: item.target_id,
            )
        )
        if (
            len(ordered)
            != context.task.admission.predecessor_project_head.evolution_revision.artifact_count
        ):
            raise ScienceSuccessorPreparationV2Error(
                "predecessor artifact composition is incomplete",
                failure_code="recovery_prior_context_composition_incomplete",
            )
        return result, ordered, owner_by_target

    def _adopt_recovery_job_output(
        self,
        client: EvolutionClientProtocol,
        *,
        spec: CompiledEvolutionMethodSpec,
        adopted_job: ScienceSuccessorRecoveryAdoptedJobAuthorityV1,
    ) -> _NativeMethodJobOutput:
        """Read one succeeded paid job without creating or retrying a job."""

        terminal = client.get_internal_job_result(adopted_job.job_id)
        raw_outputs = terminal.get("outputs") if isinstance(terminal, Mapping) else None
        artifact_ids = (
            terminal.get("artifact_ids") if isinstance(terminal, Mapping) else None
        )
        selected = (
            []
            if not isinstance(raw_outputs, list)
            else [
                item
                for item in raw_outputs
                if isinstance(item, dict) and item.get("type") == spec.artifact_type
            ]
        )
        if (
            not isinstance(terminal, Mapping)
            or terminal.get("job_id") != adopted_job.job_id
            or terminal.get("state") != JobState.SUCCEEDED.value
            or terminal.get("error") is not None
            or terminal.get("successor_transition_id")
            != adopted_job.job_owner_successor_transition_id
            or science_successor_recovery_adoptable_job_result_sha256(terminal)
            != adopted_job.job_result_sha256
            or not isinstance(artifact_ids, list)
            or not isinstance(raw_outputs, list)
            or artifact_ids
            != [
                item.get("artifact_id")
                for item in raw_outputs
                if isinstance(item, dict)
            ]
            or len(selected) != 1
        ):
            raise ScienceSuccessorPreparationV2Error(
                "adopted recovery job terminal authority changed",
                failure_code="recovery_adopted_job_terminal_mismatch",
            )
        raw_output = selected[0]
        artifact = ArtifactResponse.model_validate(
            client.get_internal_successor_artifact(
                adopted_job.job_owner_successor_transition_id,
                adopted_job.proposal_artifact_id,
            )
        )
        reservation = ReflectorInferenceReservationReceipt.model_validate(
            client.get_internal_reflector_inference_reservation(adopted_job.job_id)
        )
        if (
            raw_output.get("artifact_id") != adopted_job.proposal_artifact_id
            or raw_output.get("payload_manifest_digest")
            != adopted_job.proposal_payload_manifest_sha256
            or raw_output.get("payload_byte_size")
            != adopted_job.proposal_payload_byte_size
            or artifact.artifact_id != adopted_job.proposal_artifact_id
            or artifact.type.value != adopted_job.artifact_type
            or artifact.state is not ArtifactState.SEALED
            or canonical_digest(artifact.manifest)
            != adopted_job.proposal_registry_manifest_sha256
            or artifact.manifest != raw_output.get("manifest")
            or reservation.job_id != adopted_job.job_id
            or reservation.state != "completed"
            or reservation.model_calls_started != 1
            or reservation.content_sha256
            != adopted_job.inference_reservation_sha256
        ):
            raise ScienceSuccessorPreparationV2Error(
                "adopted recovery proposal authority changed",
                failure_code="recovery_adopted_job_proposal_mismatch",
            )
        return _NativeMethodJobOutput(
            job_id=adopted_job.job_id,
            output=ScienceMethodOutputV2(
                target_id=spec.target_id,
                method_id=spec.method,
                artifact_id=adopted_job.proposal_artifact_id,
                artifact_type=spec.artifact_type,
                manifest_sha256=adopted_job.proposal_payload_manifest_sha256,
                byte_size=adopted_job.proposal_payload_byte_size,
                execution_boundary="outside_inference",
            ),
        )

    def _run_one_method(
        self,
        client: EvolutionClientProtocol,
        *,
        spec: CompiledEvolutionMethodSpec,
        legacy_payload: dict[str, Any],
        transition_attempt_id: str,
        transition_attempt_ordinal: int,
        successor_transition_id: str,
        predecessor_successor_transition_id: str | None,
        retry_terminal_job: bool,
        parent_artifact_id: str | None = None,
        content_admission_basis: ArtifactContentAdmissionBasis | None = None,
        max_reflector_model_calls: int | None = None,
        apply_admission: bool = True,
    ) -> _NativeMethodJobOutput:
        self._require_running()
        request = _plan_bound_request(
            spec,
            legacy_payload,
            successor_transition_id=successor_transition_id,
            predecessor_successor_transition_id=(
                predecessor_successor_transition_id
            ),
            max_reflector_model_calls=max_reflector_model_calls,
            content_admission_basis=content_admission_basis,
            promoted=False,
        )
        created = JobCreateResponse.model_validate(
            client.create_plan_bound_job(request.model_dump(mode="json"))
        )
        if created.state in {
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.EXPIRED,
        }:
            observed = client.get_internal_job_result(created.job_id)
            if (
                not isinstance(observed, Mapping)
                or observed.get("job_id") != created.job_id
                or observed.get("state") != created.state.value
                or type(observed.get("retryable")) is not bool
            ):
                raise ScienceSuccessorPreparationV2Error(
                    "initial managed evolution terminal authority is invalid",
                    failure_code="managed_evolution_initial_terminal_authority_invalid",
                )
            if observed["retryable"] is not True:
                raise ScienceSuccessorPreparationV2Error(
                    "initial managed evolution job failed deterministically",
                    retryable=False,
                    failure_code="managed_evolution_initial_job_failed",
                )
            if not retry_terminal_job or transition_attempt_ordinal < 2:
                raise ScienceSuccessorPreparationV2Error(
                    "initial managed evolution job was already terminal",
                    retryable=True,
                    failure_code="managed_evolution_initial_job_terminal_retryable",
                )
            created = JobCreateResponse.model_validate(
                client.retry_plan_bound_job(
                    created.job_id,
                    {
                        "retry_request_id": transition_attempt_id,
                        "plan_id": request.plan.plan_id,
                        "target_id": request.target_id,
                    },
                )
            )
            if created.state in {
                JobState.FAILED,
                JobState.CANCELLED,
                JobState.EXPIRED,
            }:
                raise ScienceSuccessorPreparationV2Error(
                    "managed evolution job retry did not requeue",
                    retryable=True,
                    failure_code="managed_evolution_retry_not_requeued",
                )
        terminal: Mapping[str, Any] | None = None
        for poll_index in range(self._max_poll_attempts):
            if poll_index and self._stop.wait(self._poll_interval):
                self._require_running()
            self._require_running()
            observed = client.get_internal_job_result(created.job_id)
            if not isinstance(observed, Mapping):
                raise ScienceSuccessorPreparationV2Error(
                    "managed evolution job returned a non-object result",
                    failure_code="managed_evolution_result_not_object",
                )
            state = observed.get("state")
            if state == JobState.SUCCEEDED.value:
                terminal = observed
                break
            if state in {
                JobState.FAILED.value,
                JobState.CANCELLED.value,
                JobState.EXPIRED.value,
            }:
                retryable = observed.get("retryable")
                if type(retryable) is not bool:
                    raise ScienceSuccessorPreparationV2Error(
                        "managed evolution failure classification is invalid",
                        failure_code="managed_evolution_failure_classification_invalid",
                    )
                raise ScienceSuccessorPreparationV2Error(
                    "managed evolution method did not succeed",
                    retryable=retryable,
                    failure_code="managed_evolution_method_failed",
                )
        if terminal is None:
            raise ScienceSuccessorPreparationV2Error(
                "managed evolution method did not reach a terminal result",
                retryable=True,
                failure_code="managed_evolution_terminal_timeout",
            )
        if (
            terminal.get("job_id") != created.job_id
            or terminal.get("error") is not None
            or terminal.get("successor_transition_id")
            != successor_transition_id
        ):
            raise ScienceSuccessorPreparationV2Error(
                "managed evolution terminal identity is invalid",
                failure_code="managed_evolution_terminal_identity_invalid",
            )
        artifact_ids = terminal.get("artifact_ids")
        raw_outputs = terminal.get("outputs")
        if (
            not isinstance(artifact_ids, list)
            or not isinstance(raw_outputs, list)
            or artifact_ids
            != [item.get("artifact_id") for item in raw_outputs if isinstance(item, dict)]
            or len(artifact_ids) != len(set(artifact_ids))
        ):
            raise ScienceSuccessorPreparationV2Error(
                "managed evolution output inventory is inconsistent",
                failure_code="managed_evolution_output_inventory_invalid",
            )
        allowed_types = set(self._registry.snapshot.methods[spec.method].output_artifact_types)
        selected = [
            item
            for item in raw_outputs
            if isinstance(item, dict) and item.get("type") == spec.artifact_type
        ]
        if not selected or len(selected) > 128 or any(
            not isinstance(item, dict) or item.get("type") not in allowed_types
            for item in raw_outputs
        ):
            raise ScienceSuccessorPreparationV2Error(
                "managed evolution outputs differ from the verified descriptor"
            )
        if any(item.get("promoted") is not False for item in raw_outputs):
            raise ScienceSuccessorPreparationV2Error(
                "managed evolution proposal was promoted before Core admission"
            )
        proposal_by_id: dict[str, tuple[dict[str, Any], ArtifactResponse]] = {}
        for output in selected:
            digest = output.get("payload_manifest_digest")
            byte_size = output.get("payload_byte_size")
            artifact_id = output.get("artifact_id")
            if (
                not isinstance(artifact_id, str)
                or artifact_id in proposal_by_id
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or not isinstance(byte_size, int)
                or isinstance(byte_size, bool)
                or not 0 <= byte_size <= m2.MAX_SNAPSHOT_BYTES
            ):
                raise ScienceSuccessorPreparationV2Error(
                    "managed evolution target output evidence is invalid"
                )
            artifact = ArtifactResponse.model_validate(
                client.get_internal_successor_artifact(
                    successor_transition_id,
                    artifact_id,
                )
            )
            if (
                artifact.type.value != spec.artifact_type
                or artifact.state is not ArtifactState.SEALED
                or artifact.promoted is not False
                or artifact.manifest != output.get("manifest")
                or artifact.scores != output.get("scores")
            ):
                raise ScienceSuccessorPreparationV2Error(
                    "managed evolution proposal registry authority is invalid"
                )
            proposal_by_id[artifact_id] = (output, artifact)
        if not apply_admission:
            if len(selected) != 1:
                raise ScienceSuccessorPreparationV2Error(
                    "single-target recovery produced multiple native proposals"
                )
            output = selected[0]
            return _NativeMethodJobOutput(
                job_id=created.job_id,
                output=ScienceMethodOutputV2(
                    target_id=spec.target_id,
                    method_id=spec.method,
                    artifact_id=str(output["artifact_id"]),
                    artifact_type=spec.artifact_type,
                    manifest_sha256=str(output["payload_manifest_digest"]),
                    byte_size=int(output["payload_byte_size"]),
                    execution_boundary="outside_inference",
                ),
            )
        if content_admission_basis is None or self._artifact_admission_root is None:
            raise ScienceSuccessorPreparationV2Error(
                "native evolution proposal lacks Core admission authority"
            )
        proposal_set = NativeArtifactProposalSet(
            job_id=created.job_id,
            target_id=spec.target_id,
            artifact_type=ArtifactType(spec.artifact_type),
            parent_artifact_id=parent_artifact_id,
            proposals=tuple(item[1] for item in proposal_by_id.values()),
        )
        selection = self._artifact_admission_policy.decide(proposal_set)
        if (
            (
                selection.action is ProposalAction.UPDATE
                and selection.selected_artifact_id not in proposal_by_id
            )
            or (
                selection.action in {ProposalAction.KEEP, ProposalAction.REJECT}
                and parent_artifact_id is None
            )
        ):
            raise ScienceSuccessorPreparationV2Error(
                "Core admission policy returned an invalid successor selection"
            )
        decision_id = "native-admission-" + canonical_digest(
            {
                "job_id": created.job_id,
                "proposal_artifact_ids": sorted(proposal_by_id),
                "successor_transition_id": successor_transition_id,
                "target_id": spec.target_id,
            }
        )[:40]
        admission_report_sha256 = canonical_digest(
            {
                "basis_sha256": content_admission_basis.content_sha256,
                "job_id": created.job_id,
                "proposal_outputs": [
                    {
                        "artifact_id": item[0]["artifact_id"],
                        "payload_byte_size": item[0]["payload_byte_size"],
                        "payload_manifest_digest": item[0]["payload_manifest_digest"],
                    }
                    for item in proposal_by_id.values()
                ],
                "selection": selection.model_dump(mode="json"),
                "target_id": spec.target_id,
            }
        )
        admission_service = NativeArtifactAdmissionService(
            registry=client,
            root=self._artifact_admission_root,
        )
        admission_authority = admission_service.issue_admission_authority(
            producer="core-production-science-successor-v2"
        )
        admission_decision = admission_service.decide(
            authority=admission_authority,
            request=ArtifactProposalDecisionRequest(
                decision_id=decision_id,
                job_id=created.job_id,
                artifact_type=ArtifactType(spec.artifact_type),
                action=selection.action,
                parent_artifact_id=parent_artifact_id,
                genesis=(
                    selection.action is ProposalAction.UPDATE
                    and parent_artifact_id is None
                ),
                selected_artifact_id=selection.selected_artifact_id,
                reason=selection.reason,
                admission=ArtifactAdmissionEvidence(
                    validator_id="core-native-artifact-admission-v1",
                    schema_version="1",
                    passed=selection.action is not ProposalAction.REJECT,
                    report_sha256=admission_report_sha256,
                ),
                content_admission_basis=content_admission_basis,
            ),
        )
        expected_source_artifact_ids = tuple(
            sorted(
                {
                    artifact_id
                    for binding in request.input_bindings
                    if binding.binding_id in {"current_dataset", "dataset_inputs"}
                    for artifact_id in binding.artifact_ids
                }
            )
        )
        if selection.action is ProposalAction.UPDATE:
            assert selection.selected_artifact_id is not None
            active_owner = successor_transition_id
            active_authority = SuccessorArtifactAuthorityResponse.model_validate(
                client.get_internal_successor_artifact_authority(
                    successor_transition_id,
                    selection.selected_artifact_id,
                )
            )
            origin = "produced"
        else:
            if predecessor_successor_transition_id is None or parent_artifact_id is None:
                raise ScienceSuccessorPreparationV2Error(
                    "keep/reject admission lacks parent transition authority"
                )
            active_owner = predecessor_successor_transition_id
            active_authority = SuccessorArtifactAuthorityResponse.model_validate(
                client.get_internal_successor_artifact_authority(
                    predecessor_successor_transition_id,
                    parent_artifact_id,
                )
            )
            origin = "inherited"
        if (
            admission_decision.active_artifact_id
            != active_authority.artifact.artifact_id
            or admission_decision.proposal_artifact_ids
            != tuple(sorted(proposal_by_id))
            or admission_decision.content_admission.proposal_artifact_ids
            != admission_decision.proposal_artifact_ids
            or admission_decision.content_admission.basis_sha256
            != content_admission_basis.content_sha256
            or admission_decision.content_admission.source_artifact_ids
            != expected_source_artifact_ids
            or (
                bool(expected_source_artifact_ids)
                != (
                    admission_decision.content_admission.source_payload_sha256
                    is not None
                )
            )
            or (
                selection.action is not ProposalAction.REJECT
                and not admission_decision.content_admission.passed
            )
            or (
                selection.action is ProposalAction.UPDATE
                and (
                    active_authority.admission_decision_id
                    != admission_decision.decision_id
                    or active_authority.admission_decision_sha256
                    != admission_decision.content_sha256
                    or active_authority.content_admission
                    != admission_decision.content_admission
                )
            )
        ):
            raise ScienceSuccessorPreparationV2Error(
                "native evolution admission authority is inconsistent"
            )
        return _NativeMethodJobOutput(
            job_id=created.job_id,
            output=ScienceMethodOutputV2(
                target_id=spec.target_id,
                method_id=spec.method,
                artifact_id=active_authority.artifact.artifact_id,
                artifact_type=spec.artifact_type,
                manifest_sha256=active_authority.payload_manifest_sha256,
                byte_size=active_authority.payload_byte_size,
                execution_boundary="outside_inference",
                evolution_job_id=created.job_id,
                admission_action=selection.action.value,
                admission_decision_id=admission_decision.decision_id,
                admission_decision_sha256=admission_decision.content_sha256,
                content_admission_sha256=(
                    admission_decision.content_admission.content_sha256
                ),
                content_admission=admission_decision.content_admission,
                proposal_artifact_ids=admission_decision.proposal_artifact_ids,
                origin=origin,
                owner_successor_transition_id=active_owner,
            ),
        )


def _project_requires_training_feedback(project: ProjectRecordV2) -> bool:
    """Return the verified, project-scoped post-run feedback gate.

    The flag lives in an enabled reflector method's closed configuration so it
    is covered by the project config digest and executable-registry schema.  A
    Daemon-wide environment or launch flag would be invisible to project
    identity and would incorrectly affect unrelated projects.
    """

    if type(project) is not ProjectRecordV2:
        raise TypeError("training feedback gate requires an exact project record")
    return any(
        selection.enabled
        and selection.config.get("training_feedback_required") is True
        for selection in project.config.evolution.targets.values()
    )


def _sorted_literals(values: set[str]) -> tuple[str, ...]:
    return tuple(sorted(values, key=lambda item: item.casefold()))


def _plan_bound_request(
    spec: CompiledEvolutionMethodSpec,
    legacy_payload: Mapping[str, Any],
    *,
    successor_transition_id: str,
    predecessor_successor_transition_id: str | None,
    max_reflector_model_calls: int | None = None,
    content_admission_basis: ArtifactContentAdmissionBasis | None = None,
    promoted: bool = True,
) -> PlanBoundJobCreateRequest:
    config = legacy_payload.get("config")
    bindings = legacy_payload.get("input_bindings")
    if not isinstance(config, dict) or not isinstance(bindings, list):
        raise ScienceSuccessorPreparationV2Error("compiled evolution job payload is incomplete")
    user_config = spec.selection.config()
    if any(config.get(key) != value for key, value in user_config.items()):
        raise ScienceSuccessorPreparationV2Error(
            "compiled evolution method config changed after normalization"
        )
    core_config = {key: value for key, value in config.items() if key not in user_config}
    if max_reflector_model_calls is not None:
        if not 1 <= max_reflector_model_calls <= 1024:
            raise ScienceSuccessorPreparationV2Error(
                "recovery reflector inference call budget is invalid"
            )
        core_config["max_reflector_model_calls"] = max_reflector_model_calls
        if spec.method == "agent_system_gepa_reflector":
            raw_audit = core_config.get("agent_system_audit")
            audit = dict(raw_audit) if isinstance(raw_audit, dict) else {}
            audit["max_repair_attempts"] = 0
            core_config["agent_system_audit"] = audit
    if content_admission_basis is not None:
        audit = core_config.get("agent_system_audit")
        audit = dict(audit) if isinstance(audit, dict) else {}
        existing = audit.get("leakage_basis")
        if existing not in (None, {}):
            raise ScienceSuccessorPreparationV2Error(
                "compiled evolution job already contains a leakage basis"
            )
        audit["leakage_basis"] = content_admission_basis.model_dump(mode="json")
        audit["declared_leakage_basis_sha256"] = (
            content_admission_basis.content_sha256
        )
        core_config["agent_system_audit"] = audit
        core_config["content_admission_basis_sha256"] = (
            content_admission_basis.content_sha256
        )
    core_config["promoted"] = promoted
    return PlanBoundJobCreateRequest(
        plan=spec.plan,
        target_id=spec.target_id,
        job_type=spec.method,
        input_bindings=tuple(bindings),
        successor_transition_id=successor_transition_id,
        predecessor_successor_transition_id=(
            predecessor_successor_transition_id
        ),
        core_config=core_config,
        priority=100,
    )


__all__ = [
    "ProductionScienceSuccessorPreparerV2",
    "ScienceSuccessorPreparationV2Error",
    "TrainingFeedbackProviderV2",
]
