"""Private Core-control read API for one captured Science Attempt.

This endpoint exposes only immutable, already-sealed execution authority.  It
does not submit or retry work and therefore cannot be used to fabricate a
SessionResult after the fact.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Literal, Protocol

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from openevo.backend.science_execution_v2 import (
    ScienceAttemptExecutionEvidenceV2,
    ScienceAttemptFailureAuthorityV1,
    ScienceAttemptExecutionReceiptV2,
)
from openevo.backend.contracts.v2.models import ProjectHeadRefV2, SuccessorTransitionV2
from openevo.backend.science_successor import ScienceSuccessorTransitionAttemptV2
from openevo.backend.project_freeze_control import FrozenProjectArtifactV1
from openevo.evolution.revisions import AtomicSuccessorCommitV2
from openevo.evolution.models import (
    ArtifactContentAdmissionReceipt,
    ArtifactResponse,
    SuccessorArtifactAuthorityResponse,
)
from openevo.experiments.clients import EvolutionHttpClient
from openevo.rollout.models import SessionResult


class CapturedTrainingAttemptAuthorityV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "openevo.captured_training_attempt_authority.v1"
    execution_receipt: ScienceAttemptExecutionReceiptV2
    execution_evidence: ScienceAttemptExecutionEvidenceV2
    session_result: SessionResult


class TrainingAttemptExecutionStatusV1(BaseModel):
    """Read-only lifecycle authority for captured and failed Attempts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "openevo.training_attempt_execution_status.v1"
    task_id: str
    attempt_id: str
    state: str
    terminal: bool
    captured: bool
    error_code: str | None = None
    retryable: bool | None = None
    model_started: bool | None = None
    benchmark_started: bool | None = None
    failure_authority: ScienceAttemptFailureAuthorityV1 | None = None


class TrainingAttemptCancellationRequestV1(BaseModel):
    """Typed, idempotent request to stop exactly one owned Attempt.

    Authentication and generation binding are enforced by the enclosing Core
    control service.  The request never accepts a process name, PID, container
    selector, or other untyped resource target.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "openevo.training_attempt_cancellation_request.v1"
    operation_id: str
    reason_code: str

    @model_validator(mode="after")
    def _closed_identity(self) -> "TrainingAttemptCancellationRequestV1":
        if not self.operation_id.strip() or not self.reason_code.strip():
            raise ValueError("training Attempt cancellation identity is empty")
        return self


class CapturedTrainingAttemptOwner(Protocol):
    def get_captured_training_attempt_authority(
        self, task_id: str, attempt_id: str
    ) -> CapturedTrainingAttemptAuthorityV2: ...

    def get_training_attempt_execution_status(
        self, task_id: str, attempt_id: str
    ) -> TrainingAttemptExecutionStatusV1: ...

    def cancel_attempt(self, task_id: str, attempt_id: str) -> Any: ...

    def get_successor_transition(self, successor_transition_id: str) -> SuccessorTransitionV2: ...

    def successor_transition_attempts(
        self, successor_transition_id: str
    ) -> list[ScienceSuccessorTransitionAttemptV2]: ...

    def successor_commit(
        self, successor_transition_id: str
    ) -> AtomicSuccessorCommitV2 | None: ...

    def reconcile_completed_successor_methods(
        self,
        successor_transition_id: str,
        *,
        expected_project_head_id: str,
        expected_terminal_attempt_id: str,
        expected_terminal_authority_sha256: str,
        reconciliation_request_id: str,
    ) -> SuccessorTransitionV2: ...

    def successor_commit_for_project_head(
        self, project_head_id: str
    ) -> AtomicSuccessorCommitV2 | None: ...

    def active_project_head(self, project_id: str) -> ProjectHeadRefV2: ...

    def get_project_head(self, project_head_id: str) -> ProjectHeadRefV2: ...

    def restore_historical_project_head(
        self,
        *,
        project_id: str,
        expected_project_head_id: str,
        source_project_head_id: str,
        restore_request_id: str,
        cross_project: bool = False,
    ) -> tuple[ProjectHeadRefV2, AtomicSuccessorCommitV2]: ...

    def historical_restore_commit(
        self, restore_request_id: str
    ) -> AtomicSuccessorCommitV2: ...


class TrainingSuccessorArtifactAuthorityV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str
    artifact_type: str
    origin: str
    job_id: str | None = None
    input_artifact_ids: tuple[str, ...] = ()
    proposal_artifact_ids: tuple[str, ...] = ()
    admission_action: str | None = None
    admission_decision_id: str | None = None
    admission_decision_sha256: str | None = None
    content_admission: ArtifactContentAdmissionReceipt | None = None
    payload_manifest_sha256: str | None = None
    registry_record_sha256: str
    payload_byte_size: int | None = None
    state: str
    promoted: bool

    @model_validator(mode="after")
    def _closed_admission_authority(self) -> "TrainingSuccessorArtifactAuthorityV2":
        fields = (
            self.admission_action,
            self.admission_decision_id,
            self.admission_decision_sha256,
            self.content_admission,
        )
        if any(value is not None for value in fields):
            if (
                any(value is None for value in fields)
                or self.job_id is None
                or self.admission_action not in {"update", "keep", "reject"}
                or not self.proposal_artifact_ids
                or self.content_admission is None
                or self.content_admission.proposal_artifact_ids
                != self.proposal_artifact_ids
                or (
                    self.admission_action == "update"
                    and (
                        self.origin != "produced"
                        or self.artifact_id not in self.proposal_artifact_ids
                    )
                )
                or (
                    self.admission_action in {"keep", "reject"}
                    and (
                        self.origin != "inherited"
                        or self.artifact_id in self.proposal_artifact_ids
                    )
                )
            ):
                raise ValueError("training successor admission authority is invalid")
        return self


class TrainingSuccessorAuthorityV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "openevo.training_successor_authority.v1"
    transition: SuccessorTransitionV2
    attempts: tuple[ScienceSuccessorTransitionAttemptV2, ...]
    commit: AtomicSuccessorCommitV2 | None
    artifacts: tuple[TrainingSuccessorArtifactAuthorityV2, ...] = ()


class CompletedMethodsSuccessorReconciliationRequestV2(BaseModel):
    """Closed no-model authority for one successor commit-tail recovery."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[
        "openevo.completed_methods_successor_reconciliation_request.v1"
    ] = "openevo.completed_methods_successor_reconciliation_request.v1"
    expected_project_head_id: str = Field(min_length=1, max_length=128)
    expected_terminal_attempt_id: str = Field(min_length=1, max_length=128)
    expected_terminal_authority_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=128)
    model_execution_allowed: Literal[False]


class HistoricalProjectHeadRestoreRequestV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "openevo.historical_project_head_restore_request.v1"
    expected_project_head_id: str
    expected_project_head_manifest_sha256: str
    source_project_head_id: str
    source_project_head_manifest_sha256: str
    idempotency_key: str
    mode: Literal["historical_restore", "cross_project_fork"] = (
        "historical_restore"
    )
    source_project_id: str | None = None

    @model_validator(mode="after")
    def _closed_restore_mode(self) -> "HistoricalProjectHeadRestoreRequestV2":
        if self.mode == "historical_restore" and self.source_project_id is not None:
            raise ValueError("historical restore cannot name another source project")
        if self.mode == "cross_project_fork" and not self.source_project_id:
            raise ValueError("cross-project restore requires its source project")
        return self


class HistoricalProjectHeadRestoreAuthorityV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "openevo.historical_project_head_restore_authority.v1"
    restore_request_id: str
    source_project_head_id: str
    predecessor_project_head_id: str
    successor_project_head: ProjectHeadRefV2
    commit: AtomicSuccessorCommitV2


class TrainingAttemptServiceBinding(Protocol):
    evolution_backend_url: str

    def request_headers(self) -> dict[str, str]: ...


class TrainingAttemptServiceControl(Protocol):
    def run_binding(self) -> TrainingAttemptServiceBinding: ...


class TrainingAttemptEvolutionClient(Protocol):
    def get_artifact(self, artifact_id: str) -> dict[str, Any]: ...

    def get_internal_successor_artifact_authority(
        self, successor_transition_id: str, artifact_id: str
    ) -> dict[str, Any]: ...

    def close(self) -> None: ...


class CapturedWorkspaceResultStore(Protocol):
    def open_result(self, receipt): ...


class ProductionProjectFreezeArtifactReader:
    """Resolve the exact promoted artifact authority for one Core project head."""

    def __init__(
        self,
        *,
        owner: CapturedTrainingAttemptOwner,
        service_control: TrainingAttemptServiceControl,
        evolution_factory: Callable[
            [TrainingAttemptServiceBinding], TrainingAttemptEvolutionClient
        ]
        | None = None,
    ) -> None:
        self._owner = owner
        self._services = service_control
        self._factory = evolution_factory or (
            lambda binding: EvolutionHttpClient(
                binding.evolution_backend_url,
                headers=binding.request_headers(),
            )
        )

    def verified_artifacts_for_project_head(
        self,
        project_id: str,
        project_head_id: str,
    ) -> tuple[FrozenProjectArtifactV1, ...]:
        head = self._owner.get_project_head(project_head_id)
        if head.project_id != project_id:
            raise ValueError("project freeze head belongs to another project")
        commit = self._owner.successor_commit_for_project_head(project_head_id)
        if commit is None or not hasattr(commit.manifest, "artifacts"):
            raise RuntimeError("project freeze head lacks successor artifact authority")
        binding = self._services.run_binding()
        client = self._factory(binding)
        result: list[FrozenProjectArtifactV1] = []
        try:
            transition_id = commit.manifest.successor_transition_id
            for contribution in commit.manifest.artifacts:
                if contribution.origin == "produced":
                    authority = SuccessorArtifactAuthorityResponse.model_validate(
                        client.get_internal_successor_artifact_authority(
                            transition_id,
                            contribution.artifact_id,
                        )
                    )
                    artifact = authority.artifact
                    payload_sha256 = authority.payload_manifest_sha256
                else:
                    # Historical restore and frozen-fork manifests retain the
                    # transition that originally promoted each inherited
                    # artifact.  Resolve that immutable promotion authority so
                    # the digest keeps the same payload-manifest semantics as
                    # the composite that is being frozen.  Hashing the mutable
                    # registry response here changes the digest domain after a
                    # restore even though the artifact identity is unchanged.
                    authority = SuccessorArtifactAuthorityResponse.model_validate(
                        client.get_internal_successor_artifact_authority(
                            contribution.owner_successor_transition_id,
                            contribution.artifact_id,
                        )
                    )
                    artifact = authority.artifact
                    payload_sha256 = authority.payload_manifest_sha256
                artifact_payload = artifact.model_dump(mode="json")
                registry_sha256 = hashlib.sha256(
                    json.dumps(
                        artifact_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest()
                if (
                    artifact.artifact_id != contribution.artifact_id
                    or artifact.type.value != contribution.artifact_type
                    or artifact.promoted is not True
                    or artifact.state.value not in {"sealed", "active"}
                ):
                    raise RuntimeError(
                        "project freeze artifact lacks promoted registry authority"
                    )
                result.append(
                    FrozenProjectArtifactV1(
                        artifact_type=contribution.artifact_type,
                        registry_id=artifact.artifact_id,
                        sha256=payload_sha256 or registry_sha256,
                    )
                )
        finally:
            client.close()
        frozen = tuple(sorted(result, key=lambda item: item.artifact_type))
        if tuple(item.artifact_type for item in frozen) != (
            "agent_system",
            "skill_bundle",
            "text_memory",
        ):
            raise RuntimeError("project freeze lacks the exact triple-artifact authority")
        return frozen


def install_core_training_attempt_endpoint(
    app: FastAPI,
    owner: CapturedTrainingAttemptOwner,
    workspace_results: CapturedWorkspaceResultStore,
    service_control: TrainingAttemptServiceControl,
    *,
    evolution_factory: Callable[
        [TrainingAttemptServiceBinding], TrainingAttemptEvolutionClient
    ]
    | None = None,
) -> None:
    if not hasattr(app.state, "core_control_v2_provider"):
        raise RuntimeError("captured training Attempt requires authenticated Core v2")
    if getattr(app.state, "core_training_attempt_installed", False):
        raise RuntimeError("captured training Attempt endpoint is already installed")
    app.state.core_training_attempt_installed = True
    factory = evolution_factory or (
        lambda binding: EvolutionHttpClient(
            binding.evolution_backend_url,
            headers=binding.request_headers(),
        )
    )

    def artifact_authorities(
        commit: AtomicSuccessorCommitV2,
    ) -> tuple[TrainingSuccessorArtifactAuthorityV2, ...]:
        binding = service_control.run_binding()
        client = factory(binding)
        result: list[TrainingSuccessorArtifactAuthorityV2] = []
        try:
            transition_id = commit.manifest.successor_transition_id
            for contribution in commit.manifest.artifacts:
                if contribution.origin == "produced":
                    authority = SuccessorArtifactAuthorityResponse.model_validate(
                        client.get_internal_successor_artifact_authority(
                            transition_id,
                            contribution.artifact_id,
                        )
                    )
                    artifact = authority.artifact
                    if (
                        contribution.evolution_job_id is not None
                        and (
                            authority.job_id != contribution.evolution_job_id
                            or authority.proposal_artifact_ids
                            != contribution.proposal_artifact_ids
                            or authority.admission_decision_id
                            != contribution.admission_decision_id
                            or authority.admission_decision_sha256
                            != contribution.admission_decision_sha256
                            or authority.content_admission
                            != contribution.content_admission
                        )
                    ):
                        raise RuntimeError(
                            "successor admission differs from registry authority"
                        )
                    inputs = authority.input_artifact_ids
                    job_id = contribution.evolution_job_id or authority.job_id
                    proposals = (
                        contribution.proposal_artifact_ids
                        or authority.proposal_artifact_ids
                    )
                    admission_action = (
                        contribution.admission_action
                        or ("update" if authority.admission_decision_id else None)
                    )
                    admission_decision_id = (
                        contribution.admission_decision_id
                        or authority.admission_decision_id
                    )
                    admission_decision_sha256 = (
                        contribution.admission_decision_sha256
                        or authority.admission_decision_sha256
                    )
                    content_admission = (
                        contribution.content_admission
                        or authority.content_admission
                    )
                    payload_sha256 = authority.payload_manifest_sha256
                    payload_byte_size = authority.payload_byte_size
                else:
                    artifact = ArtifactResponse.model_validate(
                        client.get_artifact(contribution.artifact_id)
                    )
                    inputs = ()
                    job_id = contribution.evolution_job_id
                    proposals = contribution.proposal_artifact_ids
                    admission_action = contribution.admission_action
                    admission_decision_id = contribution.admission_decision_id
                    admission_decision_sha256 = (
                        contribution.admission_decision_sha256
                    )
                    content_admission = contribution.content_admission
                    payload_sha256 = None
                    payload_byte_size = None
                artifact_payload = artifact.model_dump(mode="json")
                record_sha256 = hashlib.sha256(
                    json.dumps(
                        artifact_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest()
                result.append(
                    TrainingSuccessorArtifactAuthorityV2(
                        artifact_id=artifact.artifact_id,
                        artifact_type=artifact.type.value,
                        origin=contribution.origin,
                        job_id=job_id,
                        input_artifact_ids=inputs,
                        proposal_artifact_ids=proposals,
                        admission_action=admission_action,
                        admission_decision_id=admission_decision_id,
                        admission_decision_sha256=admission_decision_sha256,
                        content_admission=content_admission,
                        payload_manifest_sha256=payload_sha256,
                        registry_record_sha256=record_sha256,
                        payload_byte_size=payload_byte_size,
                        state=artifact.state.value,
                        promoted=artifact.promoted,
                    )
                )
        finally:
            client.close()
        return tuple(result)

    @app.get(
        "/v2/internal/training-attempts/{task_id}/{attempt_id}/execution-status",
        response_model=TrainingAttemptExecutionStatusV1,
        include_in_schema=False,
    )
    async def get_training_attempt_execution_status(
        task_id: str,
        attempt_id: str,
    ) -> TrainingAttemptExecutionStatusV1:
        try:
            return owner.get_training_attempt_execution_status(task_id, attempt_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/v2/internal/training-attempts/{task_id}/{attempt_id}/cancel",
        response_model=TrainingAttemptExecutionStatusV1,
        include_in_schema=False,
    )
    async def cancel_training_attempt(
        task_id: str,
        attempt_id: str,
        request: TrainingAttemptCancellationRequestV1,
    ) -> TrainingAttemptExecutionStatusV1:
        """Cancel only the exact Task/Attempt owned by authenticated Core v2.

        The science run owner already journals cancellation durably and treats
        repeated cancellation as the same terminal state.  ``operation_id``
        is included in the request contract so callers can persist and replay
        one intent without widening the cancellation target.
        """

        try:
            owner.cancel_attempt(task_id, attempt_id)
            status = owner.get_training_attempt_execution_status(task_id, attempt_id)
            if not status.terminal or status.state not in {"cancelled", "failed"}:
                raise RuntimeError("training Attempt cancellation did not converge")
            return status
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get(
        "/v2/internal/training-attempts/{task_id}/{attempt_id}",
        response_model=CapturedTrainingAttemptAuthorityV2,
        include_in_schema=False,
    )
    async def get_captured_training_attempt(
        task_id: str,
        attempt_id: str,
    ) -> CapturedTrainingAttemptAuthorityV2:
        try:
            return owner.get_captured_training_attempt_authority(task_id, attempt_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get(
        "/v2/internal/training-attempts/{task_id}/{attempt_id}/workspace-result",
        include_in_schema=False,
    )
    async def get_captured_workspace_result(task_id: str, attempt_id: str):
        authority = owner.get_captured_training_attempt_authority(task_id, attempt_id)
        receipt = authority.session_result.workspace_result
        if receipt is None:
            raise HTTPException(status_code=409, detail="captured Attempt has no workspace result")

        def stream():
            with workspace_results.open_result(receipt) as source:
                while block := source.read(1024 * 1024):
                    yield block

        return StreamingResponse(
            stream(),
            media_type="application/vnd.openevo.workspace-tar",
            headers={
                "X-Content-SHA256": receipt.output_archive.content_sha256,
                "X-Result-Manifest-SHA256": receipt.result_manifest_sha256,
            },
        )

    @app.get(
        "/v2/internal/training-successors/{successor_transition_id}",
        response_model=TrainingSuccessorAuthorityV2,
        include_in_schema=False,
    )
    async def get_training_successor_authority(
        successor_transition_id: str,
    ) -> TrainingSuccessorAuthorityV2:
        transition = owner.get_successor_transition(successor_transition_id)
        attempts = tuple(owner.successor_transition_attempts(successor_transition_id))
        commit = owner.successor_commit(successor_transition_id)
        if transition.state == "committed" and commit is None:
            raise HTTPException(status_code=409, detail="committed successor lacks commit authority")
        try:
            artifacts = () if commit is None else artifact_authorities(commit)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(
                status_code=409,
                detail="successor artifact authority is inconsistent",
            ) from exc
        return TrainingSuccessorAuthorityV2(
            transition=transition,
            attempts=attempts,
            commit=commit,
            artifacts=artifacts,
        )

    @app.post(
        "/v2/internal/training-successors/{successor_transition_id}"
        "/completed-methods-reconcile",
        response_model=TrainingSuccessorAuthorityV2,
        include_in_schema=False,
    )
    async def reconcile_completed_training_successor_methods(
        successor_transition_id: str,
        request: CompletedMethodsSuccessorReconciliationRequestV2,
    ) -> TrainingSuccessorAuthorityV2:
        try:
            owner.reconcile_completed_successor_methods(
                successor_transition_id,
                expected_project_head_id=request.expected_project_head_id,
                expected_terminal_attempt_id=(
                    request.expected_terminal_attempt_id
                ),
                expected_terminal_authority_sha256=(
                    request.expected_terminal_authority_sha256
                ),
                reconciliation_request_id=request.idempotency_key,
            )
            transition = owner.get_successor_transition(
                successor_transition_id
            )
            attempts = tuple(
                owner.successor_transition_attempts(successor_transition_id)
            )
            commit = owner.successor_commit(successor_transition_id)
            artifacts = () if commit is None else artifact_authorities(commit)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return TrainingSuccessorAuthorityV2(
            transition=transition,
            attempts=attempts,
            commit=commit,
            artifacts=artifacts,
        )

    @app.post(
        "/v2/internal/projects/{project_id}/historical-restores",
        response_model=HistoricalProjectHeadRestoreAuthorityV2,
        include_in_schema=False,
    )
    async def restore_historical_project_head(
        project_id: str,
        request: HistoricalProjectHeadRestoreRequestV2,
    ) -> HistoricalProjectHeadRestoreAuthorityV2:
        active = owner.active_project_head(project_id)
        source = owner.get_project_head(request.source_project_head_id)
        cross_project = request.mode == "cross_project_fork"
        if cross_project:
            if (
                source.project_id != request.source_project_id
                or source.project_id == project_id
            ):
                raise HTTPException(
                    status_code=409,
                    detail="cross-project restore source authority changed",
                )
        elif source.project_id != project_id:
            raise HTTPException(
                status_code=409,
                detail="historical restore source belongs to another project",
            )
        # A replay may observe the already-published successor as active.  In
        # that case the owner resolves the immutable idempotency receipt.  A
        # fresh mutation still requires exact predecessor and source hashes.
        if active.project_head_id == request.expected_project_head_id and (
            active.manifest_sha256
            != request.expected_project_head_manifest_sha256
            or source.manifest_sha256
            != request.source_project_head_manifest_sha256
        ):
            raise HTTPException(
                status_code=409,
                detail="historical restore project-head manifest changed",
            )
        successor, commit = owner.restore_historical_project_head(
            project_id=project_id,
            expected_project_head_id=request.expected_project_head_id,
            source_project_head_id=request.source_project_head_id,
            restore_request_id=request.idempotency_key,
            cross_project=cross_project,
        )
        manifest = commit.manifest
        if manifest.source_manifest_sha256 != request.source_project_head_manifest_sha256:
            raise HTTPException(
                status_code=409,
                detail="historical restore source manifest changed",
            )
        return HistoricalProjectHeadRestoreAuthorityV2(
            restore_request_id=manifest.successor_transition_id,
            source_project_head_id=request.source_project_head_id,
            predecessor_project_head_id=request.expected_project_head_id,
            successor_project_head=successor,
            commit=commit,
        )

    @app.get(
        "/v2/internal/historical-restores/{restore_request_id}",
        response_model=HistoricalProjectHeadRestoreAuthorityV2,
        include_in_schema=False,
    )
    async def get_historical_project_head_restore(
        restore_request_id: str,
    ) -> HistoricalProjectHeadRestoreAuthorityV2:
        commit = owner.historical_restore_commit(restore_request_id)
        manifest = commit.manifest
        successor = owner.get_project_head(manifest.successor_project_head_id)
        return HistoricalProjectHeadRestoreAuthorityV2(
            restore_request_id=manifest.successor_transition_id,
            source_project_head_id=manifest.source_project_head_id,
            predecessor_project_head_id=manifest.predecessor_project_head_id,
            successor_project_head=successor,
            commit=commit,
        )


__all__ = [
    "CapturedTrainingAttemptAuthorityV2",
    "TrainingAttemptExecutionStatusV1",
    "CapturedTrainingAttemptOwner",
    "CapturedWorkspaceResultStore",
    "HistoricalProjectHeadRestoreAuthorityV2",
    "HistoricalProjectHeadRestoreRequestV2",
    "ProductionProjectFreezeArtifactReader",
    "TrainingSuccessorAuthorityV2",
    "TrainingSuccessorArtifactAuthorityV2",
    "install_core_training_attempt_endpoint",
]
