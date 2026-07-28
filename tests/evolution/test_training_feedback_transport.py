from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

from fastapi.testclient import TestClient

from openevo.evolution.server import create_app
from openevo.internal_auth import InternalServiceIdentity


def _identities() -> tuple[InternalServiceIdentity, InternalServiceIdentity]:
    credential = "training-feedback-transport-test-credential"
    server = InternalServiceIdentity(
        service_id="evolution-backend",
        generation_digest="1" * 64,
        registry_digest="2" * 64,
        framework_lock_digest="3" * 64,
        credential=credential,
    )
    core = InternalServiceIdentity(
        service_id="core-control",
        generation_digest=server.generation_digest,
        registry_digest=server.registry_digest,
        framework_lock_digest=server.framework_lock_digest,
        credential=credential,
    )
    return server, core


def _sealed_dataset(
    client: TestClient,
    headers: dict[str, str],
) -> tuple[dict, str]:
    event = client.post(
        "/v1/events",
        json={
            "source": "openevo",
            "event_type": "openevo.session_completed",
            "source_event_id": "session:feedback-session",
            "task_id": "feedback_task",
            "session_id": "feedback_session",
            "status": "COMPLETED",
            "policy_version": "policy_1",
            "payload": {
                "session_result": {
                    "session_id": "feedback_session",
                    "task_id": "feedback_task",
                    "trajectory": {
                        "traces": [
                            {
                                "observation": "public input",
                                "action": "public output",
                                "reward": 0.0,
                            }
                        ]
                    },
                }
            },
        },
        headers=headers,
    )
    assert event.status_code == 200, event.text
    dataset = client.post(
        "/v1/datasets",
        json={
            "idempotency_key": "feedback-dataset-create",
            "name": "feedback_dataset",
            "purpose": "openevo_science_successor_v2",
            "query": {
                "source": "openevo",
                "event_types": ["openevo.session_completed"],
                "status": ["COMPLETED"],
                "policy_version": "policy_1",
                "source_event_id": "session:feedback-session",
                "task_id": "feedback_task",
                "session_id": "feedback_session",
            },
            "limits": {"max_events": 1, "max_traces": 1},
        },
        headers=headers,
    )
    assert dataset.status_code == 200, dataset.text
    body = dataset.json()
    artifact = client.get(
        f"/v1/artifacts/{body['artifact_id']}", headers=headers
    )
    assert artifact.status_code == 200
    revision = f"{body['artifact_id']}.v{artifact.json()['version']}"
    return body, revision


def _request(dataset_id: str, revision: str) -> dict:
    return {
        "idempotency_key": "feedback-evaluator-attempt-0",
        "session_id": "feedback_session",
        "task_id": "feedback_task",
        "task_scope_id": "feedback_task",
        "completed_dataset_id": dataset_id,
        "completed_dataset_revision": revision,
        "producer": "trusted_evaluator",
        "feedback_class": "MIXED",
        "global_feedback": {"completed": True, "total_score": 0.75},
        "task_local_feedback": {"correct_answer": "task-local-only"},
    }


def _create_attachment_in_child(root: str, queue) -> None:
    path = Path(root)
    server, core = _identities()
    app = create_app(
        db_path=path / "evolution.db",
        artifact_root=path / "artifacts",
        internal_identity=server,
    )
    try:
        with TestClient(app) as client:
            dataset, revision = _sealed_dataset(client, core.request_headers())
            response = client.post(
                "/v1/internal/training-feedback/attachments",
                json=_request(dataset["dataset_id"], revision),
                headers=core.request_headers(),
            )
            response.raise_for_status()
            queue.put(
                {
                    "dataset": dataset,
                    "revision": revision,
                    "attachment": response.json(),
                }
            )
    except BaseException as exc:
        queue.put({"error": f"{type(exc).__name__}:{exc}"})


def test_cross_process_restart_idempotency_and_resolved_view(tmp_path: Path) -> None:
    server, core = _identities()
    db_path = tmp_path / "evolution.db"
    artifact_root = tmp_path / "artifacts"
    app = create_app(
        db_path=db_path,
        artifact_root=artifact_root,
        internal_identity=server,
    )
    with TestClient(app) as client:
        dataset, revision = _sealed_dataset(client, core.request_headers())
        request = _request(dataset["dataset_id"], revision)
        first = client.post(
            "/v1/internal/training-feedback/attachments",
            json=request,
            headers=core.request_headers(),
        )
        duplicate = client.post(
            "/v1/internal/training-feedback/attachments",
            json=request,
            headers=core.request_headers(),
        )
        assert first.status_code == duplicate.status_code == 200
        assert first.json() == duplicate.json()
        attachment = first.json()
        resolved = client.post(
            "/v1/internal/training-feedback/resolve",
            json={
                "completed_dataset_id": dataset["dataset_id"],
                "completed_dataset_revision": revision,
                "task_id": "feedback_task",
                "task_scope_id": "feedback_task",
                "attachment_ids": [attachment["attachment_id"]],
            },
            headers=core.request_headers(),
        )
        assert resolved.status_code == 200, resolved.text
        resolved_body = resolved.json()
        records_uri = resolved_body["dataset_artifact"]["manifest"]["records_uri"]
        records = Path(records_uri.removeprefix("file://")).read_text()
        assert "task-local-only" not in records
        source_manifest = Path(
            client.get(
                f"/v1/artifacts/{dataset['artifact_id']}",
                headers=core.request_headers(),
            ).json()["uri"].removeprefix(
                "file://"
            )
        )
        source_before = source_manifest.read_bytes()

    restarted = create_app(
        db_path=db_path,
        artifact_root=artifact_root,
        internal_identity=server,
    )
    with TestClient(restarted) as client:
        fetched = client.get(
            f"/v1/internal/training-feedback/attachments/{attachment['attachment_id']}",
            headers=core.request_headers(),
        )
        listed = client.get(
            "/v1/internal/training-feedback/sessions/feedback_session/attachments",
            headers=core.request_headers(),
        )
        resolved_again = client.post(
            "/v1/internal/training-feedback/resolve",
            json={
                "completed_dataset_id": dataset["dataset_id"],
                "completed_dataset_revision": revision,
                "task_id": "feedback_task",
                "task_scope_id": "feedback_task",
                "attachment_ids": [attachment["attachment_id"]],
            },
            headers=core.request_headers(),
        )
        assert fetched.status_code == listed.status_code == resolved_again.status_code == 200
        assert fetched.json() == attachment
        assert listed.json()["attachments"] == [attachment]
        assert resolved_again.json() == resolved_body
        assert source_manifest.read_bytes() == source_before


def test_attachment_created_in_another_process_survives_service_restart(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    process = context.Process(
        target=_create_attachment_in_child,
        args=(str(tmp_path), queue),
    )
    process.start()
    process.join(timeout=30)
    assert process.exitcode == 0
    child = queue.get(timeout=5)
    assert "error" not in child

    server, core = _identities()
    restarted = create_app(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
        internal_identity=server,
    )
    with TestClient(restarted) as client:
        response = client.get(
            "/v1/internal/training-feedback/attachments/"
            + child["attachment"]["attachment_id"],
            headers=core.request_headers(),
        )
        assert response.status_code == 200
        assert response.json() == child["attachment"]


def test_transport_rejects_wrong_authority_conflict_revision_and_official_mode(
    tmp_path: Path,
) -> None:
    server, core = _identities()
    app = create_app(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
        internal_identity=server,
    )
    with TestClient(app) as client:
        dataset, revision = _sealed_dataset(client, core.request_headers())
        request = _request(dataset["dataset_id"], revision)
        wrong = client.post(
            "/v1/internal/training-feedback/attachments",
            json=request,
            headers=server.request_headers(),
        )
        bad_revision = client.post(
            "/v1/internal/training-feedback/attachments",
            json={**request, "completed_dataset_revision": "wrong.v1"},
            headers=core.request_headers(),
        )
        accepted = client.post(
            "/v1/internal/training-feedback/attachments",
            json=request,
            headers=core.request_headers(),
        )
        conflict = client.post(
            "/v1/internal/training-feedback/attachments",
            json={**request, "global_feedback": {"completed": True, "total_score": 0.5}},
            headers=core.request_headers(),
        )
    assert wrong.status_code == 403
    assert bad_revision.status_code == 422
    assert accepted.status_code == 200
    assert conflict.status_code == 409

    official = create_app(
        db_path=tmp_path / "official.db",
        artifact_root=tmp_path / "official-artifacts",
        internal_identity=server,
        training_feedback_official_mode=True,
    )
    with TestClient(official) as client:
        refused = client.post(
            "/v1/internal/training-feedback/attachments",
            json=request,
            headers=core.request_headers(),
        )
    assert refused.status_code == 409


def test_sqlite_and_immutable_mirror_tamper_fail_closed(tmp_path: Path) -> None:
    server, core = _identities()
    app = create_app(
        db_path=tmp_path / "evolution.db",
        artifact_root=tmp_path / "artifacts",
        internal_identity=server,
    )
    with TestClient(app) as client:
        dataset, revision = _sealed_dataset(client, core.request_headers())
        created = client.post(
            "/v1/internal/training-feedback/attachments",
            json=_request(dataset["dataset_id"], revision),
            headers=core.request_headers(),
        ).json()
        mirror = (
            tmp_path
            / "artifacts/core-control/training-feedback/attachments"
            / f"{created['attachment_id']}.json"
        )
        payload = json.loads(mirror.read_text())
        payload["global_feedback"]["total_score"] = 0.1
        mirror.write_text(json.dumps(payload, sort_keys=True))
        mirror.chmod(0o600)
        fetched = client.get(
            f"/v1/internal/training-feedback/attachments/{created['attachment_id']}",
            headers=core.request_headers(),
        )
    assert fetched.status_code == 404
