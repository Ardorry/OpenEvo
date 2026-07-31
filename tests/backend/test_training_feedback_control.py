from __future__ import annotations

from fastapi.testclient import TestClient

from openevo.backend.contracts.v2.app import create_core_control_v2_contract_app
from openevo.backend.training_feedback_control import (
    install_core_training_feedback_endpoint,
)
from openevo.evolution.framework import canonical_digest
from openevo.experiments.clients import EvolutionHttpStatusError


class _Provider:
    def authenticate(self, values: tuple[bytes, ...]) -> bool:
        return values == (b"Bearer feedback-control-test",)


class _Binding:
    evolution_backend_url = "http://127.0.0.1:1"

    def request_headers(self) -> dict[str, str]:
        return {
            "x-openevo-internal-service": "core-control",
            "x-openevo-internal-credential": "generation-bound-test",
        }


class _ServiceControl:
    def __init__(self) -> None:
        self.binding = _Binding()

    def run_binding(self) -> _Binding:
        return self.binding


class _Evolution:
    def __init__(self, body: dict) -> None:
        self.body = body
        self.payloads: list[dict] = []
        self.closed = False

    def create_training_feedback_attachment(self, payload: dict) -> dict:
        self.payloads.append(payload)
        return self.body

    def get_completed_dataset_authority(self, dataset_id: str) -> dict:
        assert dataset_id == "dataset-1"
        return {
            "completed_dataset_id": dataset_id,
            "completed_dataset_revision": "artifact-dataset-1.v1",
            "dataset_artifact_id": "artifact-dataset-1",
            "dataset_manifest_sha256": "b" * 64,
            "task_id": "rollout-task-1",
            "session_id": "session-1",
            "source_event_id": "session:session-1",
            "source_session_result_sha256": "a" * 64,
        }

    def get_training_feedback_attachment(self, attachment_id: str) -> dict:
        assert attachment_id == self.body["attachment_id"]
        return self.body

    def list_training_feedback_attachments_for_session(self, session_id: str) -> dict:
        assert session_id == self.body["session_id"]
        return {"session_id": session_id, "attachments": [self.body]}

    def resolve_evolution_dataset_view(self, payload: dict) -> dict:
        attachment = self.body
        view = {
            "schema_version": "openevo.training_feedback_dataset_view.v1",
            "resolution_id": "tfv-control-test",
            "source_dataset_id": attachment["dataset_id"],
            "source_dataset_manifest_sha256": attachment[
                "source_dataset_manifest_sha256"
            ],
            "source_session_result_sha256": attachment[
                "source_session_result_sha256"
            ],
            "attachment_ids": [attachment["attachment_id"]],
            "attachment_sha256": [attachment["content_sha256"]],
            "records_sha256": "c" * 64,
            "manifest_sha256": "d" * 64,
            "task_local_overlay_sha256": "e" * 64,
        }
        artifact = {
            "artifact_id": "artifact-feedback-view",
            "type": "dataset",
            "name": "feedback view",
            "version": 1,
            "state": "active",
            "uri": "file:///private/feedback-view/manifest.json",
            "manifest": {},
            "lineage": {},
            "compatibility": {},
            "tags": [],
            "promoted": False,
        }
        identity = {
            "completed_dataset_id": payload["completed_dataset_id"],
            "completed_dataset_revision": payload["completed_dataset_revision"],
            "attachment_ids": [attachment["attachment_id"]],
            "attachment_sha256": [attachment["content_sha256"]],
            "dataset_view": view,
            "dataset_artifact_id": artifact["artifact_id"],
        }
        return {
            "completed_dataset_id": payload["completed_dataset_id"],
            "completed_dataset_revision": payload["completed_dataset_revision"],
            "attachments": [attachment],
            "dataset_view": view,
            "dataset_artifact": artifact,
            "resolved_view_sha256": canonical_digest(identity),
        }

    def close(self) -> None:
        self.closed = True


class _MissingEvolution(_Evolution):
    def get_training_feedback_attachment(self, attachment_id: str) -> dict:
        raise EvolutionHttpStatusError(status_code=404, detail_code="not_found")


def _request() -> dict:
    return {
        "idempotency_key": "core-feedback-attempt-0",
        "session_id": "session-1",
        "task_id": "rollout-task-1",
        "task_scope_id": "core-task-1",
        "completed_dataset_id": "dataset-1",
        "completed_dataset_revision": "artifact-dataset-1.v1",
        "producer": "trusted_evaluator",
        "feedback_class": "MIXED",
        "global_feedback": {"completed": True, "total_score": 0.5},
        "task_local_feedback": {"expected": "task-local-only"},
    }


def _attachment(request: dict) -> dict:
    body = {
        "schema_version": "openevo.training_feedback_attachment.v2",
        "attachment_id": "tfa-control-test",
        "revision": 1,
        "session_id": request["session_id"],
        "task_id": request["task_id"],
        "task_scope_id": request["task_scope_id"],
        "dataset_id": request["completed_dataset_id"],
        "dataset_revision": request["completed_dataset_revision"],
        "producer": request["producer"],
        "authority_id": "evaluator-authority-1",
        "authority": "evaluator_only",
        "feedback_class": request["feedback_class"],
        "global_feedback": request["global_feedback"],
        "task_local_feedback": request["task_local_feedback"],
        "created_at": "2026-07-28T00:00:00+00:00",
        "source_session_result_sha256": "a" * 64,
        "source_dataset_manifest_sha256": "b" * 64,
        "status": "sealed",
    }
    body["content_sha256"] = canonical_digest(body)
    return body


def test_core_control_bearer_forwards_to_generation_bound_evolution_identity() -> None:
    request = _request()
    evolution = _Evolution(_attachment(request))
    app = create_core_control_v2_contract_app(_Provider())
    install_core_training_feedback_endpoint(
        app,
        _ServiceControl(),
        evolution_factory=lambda _binding: evolution,
    )
    with TestClient(app) as client:
        unauthenticated = client.post(
            "/v2/internal/training-feedback/attachments",
            json=request,
        )
        accepted = client.post(
            "/v2/internal/training-feedback/attachments",
            json=request,
            headers={"Authorization": "Bearer feedback-control-test"},
        )
        assert "/v2/internal/training-feedback/attachments" not in app.openapi()["paths"]
    assert unauthenticated.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json()["content_sha256"] == evolution.body["content_sha256"]
    assert evolution.payloads == [
        request
        | {
            "schema_version": "openevo.training_feedback_create.v1",
            "created_at": None,
        }
    ]
    assert evolution.closed is True


def test_core_control_rejects_mismatched_evolution_attachment() -> None:
    request = _request()
    mismatched = _attachment(request)
    mismatched["task_scope_id"] = "another-core-task"
    mismatched["content_sha256"] = canonical_digest(
        {key: value for key, value in mismatched.items() if key != "content_sha256"}
    )
    app = create_core_control_v2_contract_app(_Provider())
    install_core_training_feedback_endpoint(
        app,
        _ServiceControl(),
        evolution_factory=lambda _binding: _Evolution(mismatched),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v2/internal/training-feedback/attachments",
            json=request,
            headers={"Authorization": "Bearer feedback-control-test"},
        )
    assert response.status_code == 502


def test_core_control_get_list_and_resolve_are_private_durable_transports() -> None:
    request = _request()
    evolution = _Evolution(_attachment(request))
    app = create_core_control_v2_contract_app(_Provider())
    install_core_training_feedback_endpoint(
        app,
        _ServiceControl(),
        evolution_factory=lambda _binding: evolution,
    )
    headers = {"Authorization": "Bearer feedback-control-test"}
    with TestClient(app) as client:
        fetched = client.get(
            "/v2/internal/training-feedback/attachments/tfa-control-test",
            headers=headers,
        )
        dataset_authority = client.get(
            "/v2/internal/training-feedback/datasets/dataset-1",
            headers=headers,
        )
        listed = client.get(
            "/v2/internal/training-feedback/sessions/session-1/attachments",
            headers=headers,
        )
        resolved = client.post(
            "/v2/internal/training-feedback/resolve",
            json={
                "completed_dataset_id": request["completed_dataset_id"],
                "completed_dataset_revision": request[
                    "completed_dataset_revision"
                ],
                "task_id": request["task_id"],
                "task_scope_id": request["task_scope_id"],
                "attachment_ids": ["tfa-control-test"],
            },
            headers=headers,
        )
        public_paths = app.openapi()["paths"]
    assert fetched.status_code == listed.status_code == resolved.status_code == dataset_authority.status_code == 200
    assert dataset_authority.json()["completed_dataset_revision"] == "artifact-dataset-1.v1"
    assert listed.json()["attachments"] == [fetched.json()]
    assert resolved.json()["attachments"] == [fetched.json()]
    assert not any("training-feedback" in path for path in public_paths)


def test_core_control_preserves_missing_feedback_authority_semantics() -> None:
    request = _request()
    evolution = _MissingEvolution(_attachment(request))
    app = create_core_control_v2_contract_app(_Provider())
    install_core_training_feedback_endpoint(
        app,
        _ServiceControl(),
        evolution_factory=lambda _binding: evolution,
    )

    with TestClient(app) as client:
        response = client.get(
            "/v2/internal/training-feedback/attachments/tfa-missing",
            headers={"Authorization": "Bearer feedback-control-test"},
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "training feedback authority was not found"}
    assert evolution.closed is True
