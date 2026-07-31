"""Evidence bridge from a *real* OpenEvo result to native evolution inputs.

This module no longer fabricates ``TaskRequest``, session-completed events, or
artifact registrations.  The Gateway exports the canonical event and the
Evolution Store/Worker owns datasets and artifact registry writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openevo.evolution.training_feedback import (
    FeedbackClass,
    TrainingFeedbackEvolutionService,
)

from .candidate_runner import NativeCandidateResult
from .feedback_filter import validate_feedback


@dataclass(frozen=True)
class NativeCompletedTaskEvidence:
    rollout_task_id: str
    session_id: str
    source_event_id: str
    policy_version: str
    trace_count: int
    workspace_result_manifest_sha256: str


def completed_task_evidence(result: NativeCandidateResult) -> NativeCompletedTaskEvidence:
    session = result.session_result
    policy_version = session.metadata.get("policy_version")
    if not isinstance(policy_version, str) or not policy_version:
        raise ValueError("native result lacks the OpenEvo policy version")
    if session.task_id != result.rollout_task_id:
        raise ValueError("native result task identity changed")
    return NativeCompletedTaskEvidence(
        rollout_task_id=result.rollout_task_id,
        session_id=session.session_id,
        source_event_id=f"session:{session.session_id}",
        policy_version=policy_version,
        trace_count=result.transcript_trace_count,
        workspace_result_manifest_sha256=result.workspace_result_manifest_sha256,
    )


def build_native_dataset_request(
    result: NativeCandidateResult,
    *,
    dataset_name: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Select exactly the Gateway-owned event for one completed attempt."""

    evidence = completed_task_evidence(result)
    if evidence.trace_count < 1:
        raise ValueError("completed transcript dataset would be empty")
    return {
        "idempotency_key": idempotency_key,
        "name": dataset_name,
        "purpose": "researchclawbench_native_completed_attempt",
        "query": {
            "source": "openevo",
            "event_types": ["openevo.session_completed"],
            "status": ["COMPLETED"],
            "policy_version": evidence.policy_version,
            "source_event_id": evidence.source_event_id,
            "task_id": evidence.rollout_task_id,
            "session_id": evidence.session_id,
        },
        "limits": {"max_events": 1, "max_traces": evidence.trace_count},
    }


def attach_training_feedback(
    feedback: dict[str, Any],
    *,
    allowed_tasks: set[str],
    service: TrainingFeedbackEvolutionService,
    evaluator_authority: object,
    dataset_id: str,
    native_session_id: str,
    native_task_id: str,
    benchmark_task_id: str,
    feedback_class: FeedbackClass = FeedbackClass.SOFT_JUDGE,
    task_local_feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ask the trusted Core service to publish a post-run dataset supplement.

    The adapter can validate the public feedback shape, but only the opaque
    evaluator capability held by the supervisor authorizes publication.
    """

    released = validate_feedback(feedback, allowed_tasks=allowed_tasks)
    if released["task_id"] != benchmark_task_id:
        raise ValueError("feedback task does not match the completed native dataset")
    global_feedback = {
        key: value
        for key, value in released.items()
        if key not in {"task_id", "attempt_id"}
    }
    receipt = service.attach_and_register(
        authority=evaluator_authority,
        dataset_id=dataset_id,
        session_id=native_session_id,
        task_id=native_task_id,
        task_scope_id=benchmark_task_id,
        feedback_class=feedback_class,
        global_feedback=global_feedback,
        task_local_feedback=task_local_feedback or {},
    )
    return {
        "schema_version": "openevo.researchclawbench.training_feedback_receipt.v1",
        "native_session_id": native_session_id,
        "source_dataset_id": dataset_id,
        "attachment_id": receipt.attachment.attachment_id,
        "attachment_sha256": receipt.attachment.content_sha256,
        "evolution_dataset_artifact_id": receipt.dataset_artifact.artifact_id,
        "evolution_dataset_manifest_sha256": receipt.dataset_view.manifest_sha256,
        "task_local_overlay_sha256": receipt.dataset_view.task_local_overlay_sha256,
        "attached_to_native_dataset": True,
    }


def reject_synthetic_native_inputs(payload: dict[str, Any]) -> None:
    forbidden = {"source", "event_type", "source_event_id", "uri", "promoted"}
    if set(payload) & forbidden:
        raise ValueError("adapter may not synthesize OpenEvo events or registry artifacts")
