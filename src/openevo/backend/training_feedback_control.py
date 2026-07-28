"""Private Core-control bridge for trusted post-run evaluator feedback.

The route is deliberately outside the frozen public v2 OpenAPI document, but
it is still covered by the v2 bearer middleware.  It never mints evaluator
authority in the caller: Core forwards a closed request to Evolution using its
generation-bound internal service identity, and Evolution owns the durable
authority capability and immutable store.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from fastapi import FastAPI, HTTPException

from openevo.evolution.training_feedback import (
    EvolutionDatasetViewResolveRequest,
    ResolvedEvolutionDatasetView,
    TrainingFeedbackAttachment,
    TrainingFeedbackAttachmentCreateRequest,
    TrainingFeedbackAttachmentList,
)
from openevo.experiments.clients import EvolutionHttpClient


_PATH = "/v2/internal/training-feedback/attachments"


class TrainingFeedbackServiceBinding(Protocol):
    evolution_backend_url: str

    def request_headers(self) -> dict[str, str]: ...


class TrainingFeedbackServiceControl(Protocol):
    def run_binding(self) -> TrainingFeedbackServiceBinding: ...


class TrainingFeedbackEvolutionClient(Protocol):
    def create_training_feedback_attachment(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]: ...

    def get_training_feedback_attachment(
        self, attachment_id: str
    ) -> dict[str, Any]: ...

    def list_training_feedback_attachments_for_session(
        self, session_id: str
    ) -> dict[str, Any]: ...

    def resolve_evolution_dataset_view(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]: ...

    def close(self) -> None: ...


def install_core_training_feedback_endpoint(
    app: FastAPI,
    service_control: TrainingFeedbackServiceControl,
    *,
    evolution_factory: Callable[
        [TrainingFeedbackServiceBinding], TrainingFeedbackEvolutionClient
    ]
    | None = None,
) -> None:
    """Install the bearer-authenticated evaluator handoff on Core Control."""

    if not hasattr(app.state, "core_control_v2_provider"):
        raise RuntimeError("training feedback requires the authenticated v2 provider")
    if getattr(app.state, "core_training_feedback_installed", False):
        raise RuntimeError("Core training feedback endpoint is already installed")
    app.state.core_training_feedback_installed = True
    factory = evolution_factory or (
        lambda binding: EvolutionHttpClient(
            binding.evolution_backend_url,
            headers=binding.request_headers(),
        )
    )

    def invoke(call):
        try:
            binding = service_control.run_binding()
            client = factory(binding)
            try:
                return call(client)
            finally:
                client.close()
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            status = 409 if "conflict" in str(exc) else 422
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        except (OSError, RuntimeError) as exc:
            raise HTTPException(
                status_code=503,
                detail="trusted evaluator feedback transport is unavailable",
            ) from exc

    @app.post(
        _PATH,
        response_model=TrainingFeedbackAttachment,
        include_in_schema=False,
    )
    async def create_training_feedback_attachment(
        payload: TrainingFeedbackAttachmentCreateRequest,
    ) -> TrainingFeedbackAttachment:
        attachment = TrainingFeedbackAttachment.model_validate(
            invoke(
                lambda client: client.create_training_feedback_attachment(
                    payload.model_dump(mode="json")
                )
            )
        )
        if (
            attachment.session_id != payload.session_id
            or attachment.task_id != payload.task_id
            or attachment.task_scope_id != payload.task_scope_id
            or attachment.dataset_id != payload.completed_dataset_id
            or attachment.dataset_revision != payload.completed_dataset_revision
            or attachment.producer != payload.producer
            or attachment.feedback_class is not payload.feedback_class
            or attachment.global_feedback != payload.global_feedback
            or attachment.task_local_feedback != payload.task_local_feedback
            or attachment.status != "sealed"
        ):
            raise HTTPException(
                status_code=502,
                detail="Evolution returned mismatched feedback authority",
            )
        return attachment

    @app.get(
        _PATH + "/{attachment_id}",
        response_model=TrainingFeedbackAttachment,
        include_in_schema=False,
    )
    async def get_training_feedback_attachment(
        attachment_id: str,
    ) -> TrainingFeedbackAttachment:
        return TrainingFeedbackAttachment.model_validate(
            invoke(
                lambda client: client.get_training_feedback_attachment(attachment_id)
            )
        )

    @app.get(
        "/v2/internal/training-feedback/sessions/{session_id}/attachments",
        response_model=TrainingFeedbackAttachmentList,
        include_in_schema=False,
    )
    async def list_training_feedback_attachments_for_session(
        session_id: str,
    ) -> TrainingFeedbackAttachmentList:
        return TrainingFeedbackAttachmentList.model_validate(
            invoke(
                lambda client: client.list_training_feedback_attachments_for_session(
                    session_id
                )
            )
        )

    @app.post(
        "/v2/internal/training-feedback/resolve",
        response_model=ResolvedEvolutionDatasetView,
        include_in_schema=False,
    )
    async def resolve_evolution_dataset_view(
        payload: EvolutionDatasetViewResolveRequest,
    ) -> ResolvedEvolutionDatasetView:
        return ResolvedEvolutionDatasetView.model_validate(
            invoke(
                lambda client: client.resolve_evolution_dataset_view(
                    payload.model_dump(mode="json")
                )
            )
        )


__all__ = [
    "TrainingFeedbackEvolutionClient",
    "TrainingFeedbackServiceBinding",
    "TrainingFeedbackServiceControl",
    "install_core_training_feedback_endpoint",
]
