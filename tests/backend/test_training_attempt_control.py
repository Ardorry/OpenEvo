from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from openevo.backend.contracts.v2.app import create_core_control_v2_contract_app
from openevo.backend.contracts.v2.models import (
    ProjectHeadRefV2,
    SuccessorTransitionRefV2,
    SuccessorTransitionV2,
)
from openevo.backend.training_attempt_control import (
    ProductionProjectFreezeArtifactReader,
    TrainingAttemptExecutionStatusV1,
    TrainingSuccessorArtifactAuthorityV2,
    install_core_training_attempt_endpoint,
)
from openevo.evolution.models import ArtifactContentAdmissionReceipt
from openevo.evolution.revisions import (
    AtomicSuccessorCommitV2,
    AtomicSuccessorManifestV2,
    SuccessorArtifactContributionV2,
    atomic_successor_manifest_sha256,
)


def _sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _content_admission(proposal_id: str) -> ArtifactContentAdmissionReceipt:
    payload = {
        "schema_version": "openevo.artifact_content_admission.v1",
        "basis_sha256": "1" * 64,
        "proposal_artifact_ids": [proposal_id],
        "source_artifact_ids": ["dataset-sealed"],
        "source_payload_sha256": "2" * 64,
        "scanned_file_count": 1,
        "scanned_byte_count": 32,
        "finding_count": 0,
        "finding_categories": [],
        "passed": True,
    }
    return ArtifactContentAdmissionReceipt.model_validate(
        {**payload, "content_sha256": _sha256(payload)}
    )


def _project_head(*, generation: int, artifact_count: int) -> ProjectHeadRefV2:
    project_id = "project-training-attempt-control"
    return ProjectHeadRefV2.model_validate(
        {
            "project_head_id": f"project-head-{generation}",
            "project_id": project_id,
            "generation": generation,
            "predecessor_project_head_id": (
                None if generation == 0 else f"project-head-{generation - 1}"
            ),
            "workspace_snapshot": {
                "workspace_snapshot_id": f"workspace-{generation}",
                "project_id": project_id,
                "manifest_sha256": "3" * 64,
                "entry_count": 1,
                "byte_size": 1,
            },
            "evolution_revision": {
                "evolution_revision_id": f"evolution-{generation}",
                "project_id": project_id,
                "manifest_sha256": "4" * 64,
                "artifact_count": artifact_count,
            },
            "runtime_context_snapshot": {
                "runtime_context_snapshot_id": f"runtime-context-{generation}",
                "project_id": project_id,
                "evolution_revision_id": f"evolution-{generation}",
                "evolution_revision_manifest_sha256": "4" * 64,
                "registry_sha256": "5" * 64,
                "runtime_contract_sha256": "6" * 64,
                "manifest_sha256": "7" * 64,
            },
            "effective_execution_snapshot": {
                "effective_execution_snapshot_id": f"execution-{generation}",
                "project_id": project_id,
                "execution_mode": "codex_subscription_transcript",
                "capture_mode": "transcript",
                "token_level_metrics_available": False,
                "producer_id": "managed-science",
                "snapshot_sha256": "8" * 64,
            },
            "registry_sha256": "5" * 64,
            "manifest_sha256": "9" * 64,
        }
    )


def _commit() -> AtomicSuccessorCommitV2:
    artifact_id = "artifact-agent-system-1"
    content = _content_admission(artifact_id)
    contribution = SuccessorArtifactContributionV2(
        target_id="agent_system",
        artifact_id=artifact_id,
        artifact_type="agent_system",
        owner_successor_transition_id="successor-1",
        origin="produced",
        evolution_job_id="job-agent-system-1",
        admission_action="update",
        admission_decision_id="decision-agent-system-1",
        admission_decision_sha256="a" * 64,
        content_admission=content,
        proposal_artifact_ids=(artifact_id,),
    )
    manifest = AtomicSuccessorManifestV2(
        project_id="project-training-attempt-control",
        successor_transition_id="successor-1",
        task_id="task-1",
        task_admission_id="admission-1",
        admission_sha256="b" * 64,
        accepted_attempt_id="attempt-1",
        predecessor_project_head_id="project-head-0",
        predecessor_generation=0,
        predecessor_manifest_sha256="c" * 64,
        successor_project_head_id="project-head-1",
        successor_generation=1,
        successor_manifest_sha256="d" * 64,
        workspace_snapshot_id="workspace-1",
        workspace_manifest_sha256="e" * 64,
        evolution_revision_id="evolution-1",
        evolution_revision_manifest_sha256="f" * 64,
        runtime_context_snapshot_id="runtime-context-1",
        runtime_context_manifest_sha256="0" * 64,
        effective_execution_snapshot_id="execution-1",
        effective_execution_snapshot_sha256="1" * 64,
        registry_sha256="2" * 64,
        normalized_evolution_intent_sha256="3" * 64,
        dataset_id="dataset-1",
        dataset_artifact_id="artifact-dataset-1",
        dataset_manifest_sha256="4" * 64,
        materialized_context_id="materialized-context-1",
        materialized_context_manifest_sha256="5" * 64,
        method_artifact_ids=(artifact_id,),
        artifacts=(contribution,),
    )
    return AtomicSuccessorCommitV2(
        manifest_sha256=atomic_successor_manifest_sha256(manifest),
        manifest=manifest,
    )


class _Provider:
    def authenticate(self, values: tuple[bytes, ...]) -> bool:
        return values == (b"Bearer training-attempt-control-test",)


class _Binding:
    evolution_backend_url = "http://127.0.0.1:1"

    def request_headers(self) -> dict[str, str]:
        return {}


class _ServiceControl:
    def run_binding(self) -> _Binding:
        return _Binding()


class _Owner:
    def __init__(self, commit: AtomicSuccessorCommitV2) -> None:
        self.commit = commit
        self.cancelled = False
        self.reconciliations: list[dict[str, str]] = []
        predecessor = _project_head(generation=0, artifact_count=0)
        successor = _project_head(generation=1, artifact_count=1)
        transition = SuccessorTransitionRefV2(
            successor_transition_id="successor-1",
            project_id=predecessor.project_id,
            kind="settings",
            predecessor_project_head=predecessor,
            expected_successor_generation=1,
            plan_sha256="6" * 64,
            task_admission=None,
            accepted_attempt=None,
            successor_project_head=successor,
        )
        self.transition = SuccessorTransitionV2(
            transition=transition,
            state="committed",
            progress_completed=1,
            progress_total=1,
            error=None,
            created_at="2026-07-29T00:00:00.000000Z",
            updated_at="2026-07-29T00:00:01.000000Z",
        )

    def get_successor_transition(self, successor_transition_id: str):
        assert successor_transition_id == "successor-1"
        return self.transition

    def successor_transition_attempts(self, successor_transition_id: str):
        assert successor_transition_id == "successor-1"
        return []

    def successor_commit(self, successor_transition_id: str):
        assert successor_transition_id == "successor-1"
        return self.commit

    def reconcile_completed_successor_methods(
        self,
        successor_transition_id: str,
        *,
        expected_project_head_id: str,
        expected_terminal_attempt_id: str,
        expected_terminal_authority_sha256: str,
        reconciliation_request_id: str,
    ):
        assert successor_transition_id == "successor-1"
        self.reconciliations.append(
            {
                "expected_project_head_id": expected_project_head_id,
                "expected_terminal_attempt_id": expected_terminal_attempt_id,
                "expected_terminal_authority_sha256": (
                    expected_terminal_authority_sha256
                ),
                "reconciliation_request_id": reconciliation_request_id,
            }
        )
        return self.transition

    def get_training_attempt_execution_status(self, task_id: str, attempt_id: str):
        return TrainingAttemptExecutionStatusV1(
            task_id=task_id,
            attempt_id=attempt_id,
            state="cancelled" if self.cancelled else "captured",
            terminal=True,
            captured=not self.cancelled,
        )

    def cancel_attempt(self, task_id: str, attempt_id: str):
        assert task_id == "task-1"
        assert attempt_id == "attempt-1"
        self.cancelled = True
        return object()


class _Evolution:
    def __init__(self, commit: AtomicSuccessorCommitV2) -> None:
        contribution = commit.manifest.artifacts[0]
        self.authority = {
            "successor_transition_id": "successor-1",
            "job_id": contribution.evolution_job_id,
            "input_artifact_ids": [],
            "artifact": {
                "artifact_id": contribution.artifact_id,
                "type": contribution.artifact_type,
                "name": "promoted agent system",
                "version": 1,
                "state": "sealed",
                "uri": "file:///sealed/agent-system.json",
                "manifest": {},
                "compatibility": {},
                "scores": {},
                "tags": [],
                "promoted": True,
            },
            "payload_manifest_sha256": "7" * 64,
            "payload_byte_size": 32,
            "proposal_artifact_ids": list(contribution.proposal_artifact_ids),
            "admission_decision_id": contribution.admission_decision_id,
            "admission_decision_sha256": contribution.admission_decision_sha256,
            "content_admission": contribution.content_admission.model_dump(mode="json"),
            "promotion_status": "promoted",
        }
        self.closed = False

    def get_internal_successor_artifact_authority(
        self, successor_transition_id: str, artifact_id: str
    ) -> dict:
        assert successor_transition_id == "successor-1"
        assert artifact_id == "artifact-agent-system-1"
        return self.authority

    def get_artifact(self, artifact_id: str) -> dict:
        raise AssertionError(f"unexpected inherited artifact lookup: {artifact_id}")

    def get_internal_successor_artifact_text_snapshot(
        self, successor_transition_id: str, artifact_id: str
    ) -> dict:
        assert successor_transition_id == "successor-1"
        assert artifact_id == "artifact-agent-system-1"
        text = "Reconstruct the candidate-specific analysis strategy."
        return {
            "schema_version": "openevo.internal.artifact_text_snapshot.v1",
            "artifact_id": artifact_id,
            "artifact_type": "agent_system",
            "payload_manifest_sha256": "7" * 64,
            "documents": [
                {
                    "relative_path": "AGENTS.md",
                    "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "utf8_byte_size": len(text.encode()),
                    "text": text,
                }
            ],
            "total_utf8_bytes": len(text.encode()),
        }

    def close(self) -> None:
        self.closed = True


def _client(commit: AtomicSuccessorCommitV2, evolution: _Evolution) -> TestClient:
    app = create_core_control_v2_contract_app(_Provider())
    install_core_training_attempt_endpoint(
        app,
        _Owner(commit),
        object(),
        _ServiceControl(),
        evolution_factory=lambda _binding: evolution,
    )
    return TestClient(app)


def test_training_successor_endpoint_returns_exact_native_admission_authority() -> None:
    commit = _commit()
    evolution = _Evolution(commit)
    with _client(commit, evolution) as client:
        denied = client.get("/v2/internal/training-successors/successor-1")
        accepted = client.get(
            "/v2/internal/training-successors/successor-1",
            headers={"Authorization": "Bearer training-attempt-control-test"},
        )
    assert denied.status_code == 401
    assert accepted.status_code == 200
    authority = accepted.json()["artifacts"][0]
    contribution = commit.manifest.artifacts[0]
    assert authority["job_id"] == contribution.evolution_job_id
    assert authority["admission_action"] == "update"
    assert authority["admission_decision_id"] == contribution.admission_decision_id
    assert authority["admission_decision_sha256"] == (
        contribution.admission_decision_sha256
    )
    assert authority["content_admission"] == contribution.content_admission.model_dump(
        mode="json"
    )
    assert evolution.closed is True


def test_successor_artifact_text_snapshot_is_bearer_protected_and_authority_bound() -> None:
    commit = _commit()
    evolution = _Evolution(commit)
    path = (
        "/v2/internal/training-successors/successor-1/"
        "artifacts/artifact-agent-system-1/text-snapshot"
    )
    with _client(commit, evolution) as client:
        denied = client.get(path)
        accepted = client.get(
            path,
            headers={"Authorization": "Bearer training-attempt-control-test"},
        )
    assert denied.status_code == 401
    assert accepted.status_code == 200
    body = accepted.json()
    assert body["artifact_id"] == "artifact-agent-system-1"
    assert body["artifact_type"] == "agent_system"
    assert body["documents"][0]["relative_path"] == "AGENTS.md"
    assert body["documents"][0]["text"] == (
        "Reconstruct the candidate-specific analysis strategy."
    )


def test_completed_method_reconciliation_endpoint_is_bearer_protected_and_typed() -> None:
    commit = _commit()
    evolution = _Evolution(commit)
    app = create_core_control_v2_contract_app(_Provider())
    owner = _Owner(commit)
    install_core_training_attempt_endpoint(
        app,
        owner,
        object(),
        _ServiceControl(),
        evolution_factory=lambda _binding: evolution,
    )
    path = (
        "/v2/internal/training-successors/successor-1/"
        "completed-methods-reconcile"
    )
    payload = {
        "schema_version": (
            "openevo.completed_methods_successor_reconciliation_request.v1"
        ),
        "expected_project_head_id": "project-head-0",
        "expected_terminal_attempt_id": "successor-attempt-1",
        "expected_terminal_authority_sha256": "a" * 64,
        "idempotency_key": "successor-reconcile-request-1",
        "model_execution_allowed": False,
    }

    with TestClient(app) as client:
        denied = client.post(path, json=payload)
        invalid = client.post(
            path,
            json={**payload, "model_execution_allowed": True},
            headers={"Authorization": "Bearer training-attempt-control-test"},
        )
        accepted = client.post(
            path,
            json=payload,
            headers={"Authorization": "Bearer training-attempt-control-test"},
        )

    assert denied.status_code == 401
    assert invalid.status_code == 422
    assert accepted.status_code == 200
    assert accepted.json()["transition"]["state"] == "committed"
    assert owner.reconciliations == [
        {
            "expected_project_head_id": "project-head-0",
            "expected_terminal_attempt_id": "successor-attempt-1",
            "expected_terminal_authority_sha256": "a" * 64,
            "reconciliation_request_id": "successor-reconcile-request-1",
        }
    ]


def test_project_freeze_reader_preserves_inherited_promotion_digests() -> None:
    owner_transition_id = "successor-materialized-1"
    artifact_types = ("agent_system", "skill_bundle", "text_memory")
    contributions = tuple(
        SuccessorArtifactContributionV2(
            target_id=artifact_type,
            artifact_id=f"artifact-{artifact_type}-1",
            artifact_type=artifact_type,
            owner_successor_transition_id=owner_transition_id,
            origin="inherited",
        )
        for artifact_type in artifact_types
    )
    commit = SimpleNamespace(
        manifest=SimpleNamespace(
            successor_transition_id="historical-restore-1",
            artifacts=contributions,
        )
    )

    class FreezeOwner:
        def get_project_head(self, project_head_id: str) -> ProjectHeadRefV2:
            assert project_head_id == "project-head-2"
            return _project_head(generation=2, artifact_count=3)

        def successor_commit_for_project_head(self, project_head_id: str):
            assert project_head_id == "project-head-2"
            return commit

    class InheritedEvolution:
        def __init__(self) -> None:
            self.lookups: list[tuple[str, str]] = []
            self.closed = False

        def get_internal_successor_artifact_authority(
            self,
            successor_transition_id: str,
            artifact_id: str,
        ) -> dict:
            assert successor_transition_id == owner_transition_id
            self.lookups.append((successor_transition_id, artifact_id))
            artifact_type = artifact_id.removeprefix("artifact-").removesuffix("-1")
            content = _content_admission(artifact_id)
            return {
                "successor_transition_id": owner_transition_id,
                "job_id": f"job-{artifact_type}-1",
                "input_artifact_ids": [],
                "artifact": {
                    "artifact_id": artifact_id,
                    "type": artifact_type,
                    "name": f"promoted {artifact_type}",
                    "version": 1,
                    "state": "sealed",
                    "uri": f"file:///sealed/{artifact_type}.json",
                    "manifest": {},
                    "compatibility": {},
                    "scores": {},
                    "tags": [],
                    "promoted": True,
                },
                "payload_manifest_sha256": {
                    "agent_system": "7" * 64,
                    "skill_bundle": "8" * 64,
                    "text_memory": "9" * 64,
                }[artifact_type],
                "payload_byte_size": 32,
                "proposal_artifact_ids": [artifact_id],
                "admission_decision_id": f"decision-{artifact_type}-1",
                "admission_decision_sha256": "a" * 64,
                "content_admission": content.model_dump(mode="json"),
                "promotion_status": "promoted",
            }

        def get_artifact(self, artifact_id: str) -> dict:
            raise AssertionError(f"unexpected registry-only lookup: {artifact_id}")

        def close(self) -> None:
            self.closed = True

    evolution = InheritedEvolution()
    reader = ProductionProjectFreezeArtifactReader(
        owner=FreezeOwner(),
        service_control=_ServiceControl(),
        evolution_factory=lambda _binding: evolution,
    )

    frozen = reader.verified_artifacts_for_project_head(
        "project-training-attempt-control",
        "project-head-2",
    )

    assert {item.artifact_type: item.sha256 for item in frozen} == {
        "agent_system": "7" * 64,
        "skill_bundle": "8" * 64,
        "text_memory": "9" * 64,
    }
    assert evolution.lookups == [
        (owner_transition_id, contribution.artifact_id)
        for contribution in contributions
    ]
    assert evolution.closed is True


def test_training_attempt_execution_status_is_bearer_protected_and_read_only() -> None:
    commit = _commit()
    evolution = _Evolution(commit)
    path = "/v2/internal/training-attempts/task-1/attempt-1/execution-status"
    with _client(commit, evolution) as client:
        denied = client.get(path)
        accepted = client.get(
            path,
            headers={"Authorization": "Bearer training-attempt-control-test"},
        )
    assert denied.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json() == {
        "schema_version": "openevo.training_attempt_execution_status.v1",
        "task_id": "task-1",
        "attempt_id": "attempt-1",
        "state": "captured",
        "terminal": True,
        "captured": True,
        "error_code": None,
        "retryable": None,
        "model_started": None,
        "benchmark_started": None,
        "failure_authority": None,
    }


def test_training_attempt_cancel_is_bearer_protected_typed_and_idempotent() -> None:
    commit = _commit()
    evolution = _Evolution(commit)
    app = create_core_control_v2_contract_app(_Provider())
    owner = _Owner(commit)
    install_core_training_attempt_endpoint(
        app,
        owner,
        object(),
        _ServiceControl(),
        evolution_factory=lambda _binding: evolution,
    )
    path = "/v2/internal/training-attempts/task-1/attempt-1/cancel"
    payload = {
        "schema_version": "openevo.training_attempt_cancellation_request.v1",
        "operation_id": "cancel-operation-1",
        "reason_code": "OPERATOR_ABORTED_EXACT_OWNED_WAIT",
    }
    with TestClient(app) as client:
        denied = client.post(path, json=payload)
        first = client.post(
            path,
            json=payload,
            headers={"Authorization": "Bearer training-attempt-control-test"},
        )
        replay = client.post(
            path,
            json=payload,
            headers={"Authorization": "Bearer training-attempt-control-test"},
        )
    assert denied.status_code == 401
    assert first.status_code == 200
    assert replay.status_code == 200
    assert first.json() == replay.json()
    assert first.json()["state"] == "cancelled"
    assert first.json()["terminal"] is True


def test_training_successor_endpoint_rejects_registry_admission_drift() -> None:
    commit = _commit()
    evolution = _Evolution(commit)
    evolution.authority["admission_decision_sha256"] = "8" * 64
    with _client(commit, evolution) as client:
        response = client.get(
            "/v2/internal/training-successors/successor-1",
            headers={"Authorization": "Bearer training-attempt-control-test"},
        )
    assert response.status_code == 409
    assert response.json()["detail"] == "successor artifact authority is inconsistent"


def test_native_admission_survives_commit_round_trip_and_rejects_tamper() -> None:
    commit = _commit()
    assert AtomicSuccessorCommitV2.model_validate_json(commit.model_dump_json()) == commit
    payload = commit.model_dump(mode="json")
    payload["manifest"]["artifacts"][0]["content_admission"]["scanned_byte_count"] = 33
    with pytest.raises(ValidationError, match="content admission receipt hash"):
        AtomicSuccessorCommitV2.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    ("action", "origin", "artifact_id"),
    (
        ("update", "produced", "proposal-agent-system"),
        ("keep", "inherited", "active-agent-system"),
        ("reject", "inherited", "active-agent-system"),
    ),
)
def test_training_successor_authority_closes_update_keep_reject(
    action: str,
    origin: str,
    artifact_id: str,
) -> None:
    proposal_id = "proposal-agent-system"
    authority = TrainingSuccessorArtifactAuthorityV2(
        artifact_id=artifact_id,
        artifact_type="agent_system",
        origin=origin,
        job_id="job-agent-system",
        proposal_artifact_ids=(proposal_id,),
        admission_action=action,
        admission_decision_id="decision-agent-system",
        admission_decision_sha256="9" * 64,
        content_admission=_content_admission(proposal_id),
        registry_record_sha256="a" * 64,
        state="sealed",
        promoted=True,
    )
    assert authority.admission_action == action
