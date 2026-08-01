from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from openevo.backend.contracts.v2.models import (
    EffectiveExecutionSnapshotRefV2,
    TaskSubmitRequestV2,
    project_config_sha256_for,
)
from openevo.backend.contracts.v2.store import ProjectRecordV2
from openevo.backend.run_admission import (
    EffectiveExecutionSettings,
    resolve_genesis_execution_snapshot,
)
from openevo.backend.science_execution_v2 import (
    ScienceAttemptExecutorV2,
    science_session_result_sha256,
)
from openevo.backend.science_run_owner import (
    CoreScienceTaskOwnerV2,
    _validate_successor_materialization_receipt,
)
from openevo.backend.science_run_store import ScienceProjectAdmissionAuthorityV2
from openevo.backend.science_successor import (
    ScienceSuccessorPreparationContextV2,
    SealedTranscriptDatasetV2,
)
from openevo.backend.science_successor_preparer_v2 import (
    ProductionScienceSuccessorPreparerV2,
    ScienceSuccessorPreparationV2Error,
    _materialized_context_from_wire,
    _project_requires_training_feedback,
)
from openevo.backend.workspace_handoff_v2 import WorkspaceHandoffStoreV2
from openevo.backend.workspace_store_v2 import WorkspaceStoreV2
from openevo.evolution.admission import (
    ArtifactProposalDecisionRequest,
    ProposalAction,
    artifact_content_admission_receipt,
)
from openevo.evolution.context_materialization import MaterializedContext
from openevo.evolution.context_projection import ContextProjectionResolveRequest
from openevo.evolution.framework import canonical_digest
from openevo.evolution.models import (
    ArtifactResponse,
    DatasetCreateRequest,
)
from openevo.evolution.planned_jobs import PlanBoundJobCreateRequest
from openevo.evolution.training_feedback import (
    EvolutionDatasetViewReceipt,
    FeedbackAuthority,
    FeedbackClass,
    ResolvedEvolutionDatasetView,
    TrainingFeedbackAttachment,
    TrainingFeedbackAttachmentCreateRequest,
)
from openevo.experiments.clients import EvolutionHttpStatusError
from tests.backend.test_science_execution_v2 import (
    _Catalog,
    _Clock,
    _head,
    _project_config,
    _Rollout,
    _service_binding,
    _Services,
    _wait_task_state,
)
from tests.framework_testkit import verified_builtin_registry


def test_project_scoped_method_config_requires_durable_feedback() -> None:
    payload = _project_config().model_dump(mode="json")
    payload["evolution"]["targets"] = {
        "text_memory": {
            "enabled": True,
            "method": "text_memory_expel_reflector",
            "config": {"training_feedback_required": True},
        }
    }
    config = type(_project_config()).model_validate(payload)
    project = ProjectRecordV2(
        project_id="project-feedback-gate",
        display_name="Feedback gate",
        config=config,
        project_config_sha256=project_config_sha256_for(config),
        created_at="2026-07-28T00:00:00.000000Z",
        updated_at="2026-07-28T00:00:00.000000Z",
        resource_version=1,
    )
    assert _project_requires_training_feedback(project) is True

    base = _project_config()
    ungated = ProjectRecordV2(
        project_id=project.project_id,
        display_name=project.display_name,
        config=base,
        project_config_sha256=project_config_sha256_for(base),
        created_at=project.created_at,
        updated_at=project.updated_at,
        resource_version=project.resource_version,
    )
    assert _project_requires_training_feedback(ungated) is False


def test_production_preparer_resolves_trusted_feedback_for_successor_jobs(
    tmp_path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    dataset = SealedTranscriptDatasetV2(
        dataset_id="dataset-1",
        artifact_id="artifact-dataset-1",
        manifest_sha256="a" * 64,
        record_count=1,
        task_id="task-1",
        task_admission_id="admission-1",
        accepted_attempt_id="attempt-1",
        capture_mode="transcript",
        token_level_metrics_available=False,
        sealed=True,
    )
    source_artifact = ArtifactResponse(
        artifact_id=dataset.artifact_id,
        type="dataset",
        name="source",
        version=1,
        state="active",
        uri=(tmp_path / "manifest.json").as_uri(),
        manifest={},
        promoted=True,
    )

    class Provider:
        def create_attachment_request(self, *, context, dataset, completed_dataset_revision):
            return TrainingFeedbackAttachmentCreateRequest(
                idempotency_key="successor-feedback-1",
                session_id="session-1",
                task_id="rollout-task-1",
                task_scope_id=context.task.task_id,
                completed_dataset_id=dataset.dataset_id,
                completed_dataset_revision=completed_dataset_revision,
                feedback_class=FeedbackClass.SOFT_JUDGE,
                global_feedback={"completed": True, "total_score": 0.5},
                task_local_feedback={},
            )

    request = Provider().create_attachment_request(
        context=SimpleNamespace(task=SimpleNamespace(task_id="task-1")),
        dataset=dataset,
        completed_dataset_revision="artifact-dataset-1.v1",
    )
    body = {
        "schema_version": "openevo.training_feedback_attachment.v2",
        "attachment_id": "attachment-1",
        "revision": 1,
        "session_id": "session-1",
        "task_id": "rollout-task-1",
        "task_scope_id": "task-1",
        "dataset_id": "dataset-1",
        "dataset_revision": "artifact-dataset-1.v1",
        "producer": "trusted_evaluator",
        "authority_id": "authority-1",
        "authority": FeedbackAuthority.EVALUATOR_ONLY.value,
        "feedback_class": FeedbackClass.SOFT_JUDGE.value,
        "global_feedback": request.global_feedback,
        "task_local_feedback": {},
        "created_at": "2026-07-28T00:00:00+00:00",
        "source_session_result_sha256": "b" * 64,
        "source_dataset_manifest_sha256": "a" * 64,
        "status": "sealed",
    }
    body["content_sha256"] = canonical_digest(body)
    attachment = TrainingFeedbackAttachment.model_validate(body)
    view = EvolutionDatasetViewReceipt(
        resolution_id="view-1",
        source_dataset_id="dataset-1",
        source_dataset_manifest_sha256="a" * 64,
        source_session_result_sha256="b" * 64,
        attachment_ids=(attachment.attachment_id,),
        attachment_sha256=(attachment.content_sha256,),
        records_sha256="c" * 64,
        manifest_sha256="d" * 64,
    )
    resolved_artifact = ArtifactResponse(
        artifact_id="artifact-resolved-1",
        type="dataset",
        name="resolved",
        version=1,
        state="active",
        uri=(tmp_path / "resolved.json").as_uri(),
        manifest={},
        promoted=False,
    )
    resolved_body = {
        "completed_dataset_id": "dataset-1",
        "completed_dataset_revision": "artifact-dataset-1.v1",
        "attachment_ids": [attachment.attachment_id],
        "attachment_sha256": [attachment.content_sha256],
        "dataset_view": view.model_dump(mode="json"),
        "dataset_artifact_id": resolved_artifact.artifact_id,
    }
    resolved = ResolvedEvolutionDatasetView(
        completed_dataset_id="dataset-1",
        completed_dataset_revision="artifact-dataset-1.v1",
        attachments=(attachment,),
        dataset_view=view,
        dataset_artifact=resolved_artifact,
        resolved_view_sha256=canonical_digest(resolved_body),
    )

    class Client:
        def get_artifact(self, artifact_id):
            assert artifact_id == source_artifact.artifact_id
            return source_artifact.model_dump(mode="json")

        def create_training_feedback_attachment(self, payload):
            assert TrainingFeedbackAttachmentCreateRequest.model_validate(payload) == request
            return attachment.model_dump(mode="json")

        def resolve_evolution_dataset_view(self, payload):
            assert payload["attachment_ids"] == ["attachment-1"]
            return resolved.model_dump(mode="json")

        def close(self):
            return None

    preparer = ProductionScienceSuccessorPreparerV2(
        catalog=object(),
        ledger=object(),
        workspaces=object(),
        workspace_handoffs=object(),
        services=object(),
        executable_registry=registry,
        training_feedback_provider=Provider(),
        training_feedback_required=True,
    )
    client = Client()

    @contextmanager
    def evolution(_context, _record, _project):
        yield object(), client

    preparer._evolution = evolution
    context = SimpleNamespace(task=SimpleNamespace(task_id="task-1"))
    record = SimpleNamespace(
        receipt=SimpleNamespace(
            session_id="session-1",
            rollout_task_id="rollout-task-1",
        )
    )
    project = SimpleNamespace(
        config=SimpleNamespace(
            evolution=SimpleNamespace(
                training_feedback=SimpleNamespace(mode="disabled")
            )
        )
    )
    preparer._authority = lambda _context: (record, object(), project)
    enriched = preparer.resolve_training_feedback(
        context,
        dataset,
    )
    assert enriched.training_feedback is not None
    assert enriched.training_feedback.attachment_ids == ("attachment-1",)
    assert enriched.training_feedback.resolved_dataset_artifact_id == (
        "artifact-resolved-1"
    )


def test_official_successor_mode_rejects_feedback_provider(tmp_path) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    with pytest.raises(ValueError, match="official frozen"):
        ProductionScienceSuccessorPreparerV2(
            catalog=object(),
            ledger=object(),
            workspaces=object(),
            workspace_handoffs=object(),
            services=object(),
            executable_registry=registry,
            training_feedback_provider=object(),
            official_frozen_mode=True,
        )


class _Evolution:
    def __init__(self, registry_sha256: str) -> None:
        self.registry_sha256 = registry_sha256
        self.artifacts: dict[str, dict] = {}
        self.datasets: dict[str, dict] = {}
        self.dataset_requests: list[DatasetCreateRequest] = []
        self.jobs: dict[str, dict] = {}
        self.job_requests: dict[str, PlanBoundJobCreateRequest] = {}
        self.admission_receipts: dict[str, dict] = {}
        self.artifact_owners: dict[str, str] = {}
        self.target_run_counts: dict[str, int] = {}
        self.succeeded_authority_overrides: dict[str, dict] = {}
        self.materialized: MaterializedContext | None = None
        self.materialized_count = 0
        self.materialization_response_loss_once = False
        self.discard_response: dict | None = None
        self.discarded_transition_ids: list[str] = []
        self.closed_count = 0
        self.rollout = None

    def close(self) -> None:
        self.closed_count += 1

    def create_dataset(self, payload: dict) -> dict:
        request = DatasetCreateRequest.model_validate(payload)
        assert request.idempotency_key is not None
        prior = next(
            (
                dataset
                for prior_request, dataset in zip(
                    self.dataset_requests,
                    self.datasets.values(),
                    strict=True,
                )
                if prior_request.idempotency_key == request.idempotency_key
            ),
            None,
        )
        if prior is not None:
            assert request == next(
                item
                for item in self.dataset_requests
                if item.idempotency_key == request.idempotency_key
            )
            return prior
        assert self.rollout is not None and self.rollout.result is not None
        result = self.rollout.result
        assert request.query.source == "openevo"
        assert request.query.event_types == ["openevo.session_completed"]
        assert request.query.status == ["COMPLETED"]
        assert request.query.source_event_id == f"session:{result.session_id}"
        assert request.query.task_id == result.task_id
        assert request.query.session_id == result.session_id
        ordinal = len(self.dataset_requests) + 1
        dataset_id = f"dataset-successor-{ordinal}"
        artifact_id = f"artifact-dataset-successor-{ordinal}"
        event_id = f"event-dataset-successor-{ordinal}"
        normalized = request.model_dump(mode="json", exclude_none=True)
        manifest = {
            "dataset_id": dataset_id,
            "name": request.name,
            "purpose": request.purpose,
            "query": normalized["query"],
            "limits": normalized["limits"],
            "event_ids": [event_id],
            "event_count": 1,
            "trace_count": 1,
            "records_path": "records.jsonl",
            "records_uri": f"file:///opaque/{dataset_id}/records.jsonl",
            "records_byte_size": 1,
            "records_sha256": "a" * 64,
            "create_identity": request.idempotency_key,
            "source_event_evidence": {
                "event_id": event_id,
                "source": "openevo",
                "event_type": "openevo.session_completed",
                "source_event_id": f"session:{result.session_id}",
                "task_id": result.task_id,
                "session_id": result.session_id,
                "session_result_sha256": science_session_result_sha256(result),
            },
        }
        self.artifacts[artifact_id] = ArtifactResponse(
            artifact_id=artifact_id,
            type="dataset",
            name=request.name,
            version=1,
            state="active",
            uri="file:///opaque/dataset.json",
            manifest=manifest,
            compatibility={"purpose": "openevo_science_successor_v2"},
            scores={},
            tags=[],
            promoted=True,
        ).model_dump(mode="json")
        response = {
            "dataset_id": dataset_id,
            "artifact_id": artifact_id,
            "event_count": 1,
            "trace_count": 1,
        }
        self.dataset_requests.append(request)
        self.datasets[dataset_id] = response
        return response

    def get_dataset(self, dataset_id: str) -> dict:
        return self.datasets[dataset_id]

    def create_plan_bound_job(self, payload: dict) -> dict:
        request = PlanBoundJobCreateRequest.model_validate(payload)
        assert request.core_config["promoted"] is False
        assert request.successor_transition_id is not None
        selection = request.selection()
        ordinal = self.target_run_counts.get(request.target_id, 0) + 1
        self.target_run_counts[request.target_id] = ordinal
        suffix = "" if ordinal == 1 else f"-{ordinal}"
        artifact_id = (
            f"artifact-{request.target_id}-successor{suffix}"
        )
        manifest = {
            "content_path": "memory.md",
            "target_id": request.target_id,
        }
        artifact = ArtifactResponse(
            artifact_id=artifact_id,
            type=request.target_id,
            name=f"Verified successor {request.target_id}",
            version=1,
            state="sealed",
            uri=f"file:///opaque/{request.target_id}",
            manifest=manifest,
            compatibility={"agent_harness": ["codex"]},
            scores={"quality": 1.0},
            tags=[],
            promoted=False,
        ).model_dump(mode="json")
        self.artifacts[artifact_id] = artifact
        self.artifact_owners[artifact_id] = (
            request.successor_transition_id
        )
        job_id = f"job-{request.target_id}-successor{suffix}"
        output = {
            "artifact_id": artifact_id,
            "type": request.target_id,
            "name": artifact["name"],
            "manifest": manifest,
            "lineage": {"plan_id": request.plan.plan_id},
            "compatibility": artifact["compatibility"],
            "scores": artifact["scores"],
            "promoted": False,
            "created_at": "2026-07-23T03:00:00Z",
            "payload_manifest_digest": canonical_digest({"memory.md": "Use the accepted result."}),
            "payload_byte_size": len("Use the accepted result.".encode()),
            "payload_file_count": 1,
        }
        outputs = [output]
        artifact_ids = [artifact_id]
        if selection.method_id == "agent_system_gepa_reflector":
            artifact["scores"] = {"static_guardrail_score": 0.5}
            output["scores"] = artifact["scores"]
            for candidate_index, score in ((2, 0.9), (3, 0.7)):
                candidate_id = f"{artifact_id}-candidate-{candidate_index}"
                candidate = ArtifactResponse(
                    artifact_id=candidate_id,
                    type=request.target_id,
                    name=f"GEPA candidate {candidate_index}",
                    version=1,
                    state="sealed",
                    uri=f"file:///opaque/{request.target_id}/{candidate_index}",
                    manifest={
                        "content_path": "AGENTS.md",
                        "candidate_index": candidate_index,
                    },
                    compatibility={"agent_harness": ["codex"]},
                    scores={"static_guardrail_score": score},
                    tags=[],
                    promoted=False,
                ).model_dump(mode="json")
                self.artifacts[candidate_id] = candidate
                self.artifact_owners[candidate_id] = request.successor_transition_id
                artifact_ids.append(candidate_id)
                outputs.append(
                    {
                        "artifact_id": candidate_id,
                        "type": request.target_id,
                        "name": candidate["name"],
                        "manifest": candidate["manifest"],
                        "lineage": {"plan_id": request.plan.plan_id},
                        "compatibility": candidate["compatibility"],
                        "scores": candidate["scores"],
                        "promoted": False,
                        "created_at": "2026-07-23T03:00:00Z",
                        "payload_manifest_digest": canonical_digest(
                            {"AGENTS.md": f"Generic candidate {candidate_index}."}
                        ),
                        "payload_byte_size": len(
                            f"Generic candidate {candidate_index}.".encode()
                        ),
                        "payload_file_count": 1,
                    }
                )
            report_id = f"{artifact_id}-report"
            report = ArtifactResponse(
                artifact_id=report_id,
                type="report",
                name="GEPA candidate archive",
                version=1,
                state="sealed",
                uri="file:///opaque/gepa-report",
                manifest={"content_path": "candidate_archive.json"},
                promoted=False,
            ).model_dump(mode="json")
            self.artifacts[report_id] = report
            self.artifact_owners[report_id] = request.successor_transition_id
            artifact_ids.append(report_id)
            outputs.append(
                {
                    "artifact_id": report_id,
                    "type": "report",
                    "name": report["name"],
                    "manifest": report["manifest"],
                    "lineage": {"plan_id": request.plan.plan_id},
                    "compatibility": {},
                    "scores": {},
                    "promoted": False,
                    "created_at": "2026-07-23T03:00:00Z",
                    "payload_manifest_digest": canonical_digest(
                        {"candidate_archive.json": "{}"}
                    ),
                    "payload_byte_size": 2,
                    "payload_file_count": 1,
                }
            )
        self.jobs[job_id] = {
            "artifact_ids": artifact_ids,
            "error": None,
            "job_id": job_id,
            "retryable": None,
            "state": "succeeded",
            "successor_transition_id": request.successor_transition_id,
            "outputs": outputs,
        }
        self.job_requests[job_id] = request
        expected_methods = {
            "agent_system": {
                "agent_system_reflector",
                "agent_system_history_reflector",
                "agent_system_gepa_reflector",
            },
            "skill_bundle": {"skill_bundle_reflector"},
            "text_memory": {
                "text_memory_reflector",
                "text_memory_expel_reflector",
            },
        }
        assert selection.method_id in expected_methods[request.target_id]
        return {"job_id": job_id, "state": "pending"}

    def get_internal_job_result(self, job_id: str) -> dict:
        return self.jobs[job_id]

    def get_internal_successor_transition_job_inventory(
        self,
        successor_transition_id: str,
    ) -> dict:
        payload = {
            "successor_transition_id": successor_transition_id,
            "job_ids": sorted(
                job_id
                for job_id, job in self.jobs.items()
                if job["successor_transition_id"]
                == successor_transition_id
            ),
        }
        payload["content_sha256"] = canonical_digest(payload)
        return payload

    def get_internal_succeeded_plan_bound_job_authority(
        self,
        successor_transition_id: str,
        target_id: str,
    ) -> dict:
        matches = [
            (job_id, request)
            for job_id, request in self.job_requests.items()
            if request.successor_transition_id == successor_transition_id
            and request.target_id == target_id
        ]
        assert len(matches) == 1
        job_id, request = matches[0]
        terminal = self.jobs[job_id]
        output_types = {
            "agent_system_gepa_reflector": (
                "agent_system",
                "report",
            ),
            "skill_bundle_reflector": ("skill_bundle",),
            "text_memory_expel_reflector": ("text_memory",),
        }[request.selection().method_id]
        authority = {
            "job_id": job_id,
            "state": "succeeded",
            "successor_transition_id": successor_transition_id,
            "plan_id": request.plan.plan_id,
            "registry_snapshot_digest": (
                request.plan.registry_snapshot_digest
            ),
            "plan_digest": canonical_digest(request.plan),
            "target_id": target_id,
            "method_id": request.selection().method_id,
            "method_identity_digest": (
                request.selection().method_identity_digest
            ),
            "execution_envelope_digest": canonical_digest(
                {"request": request.model_dump(mode="json")}
            ),
            "job_type": request.job_type,
            "predecessor_successor_transition_id": (
                request.predecessor_successor_transition_id
            ),
            "core_config_sha256": canonical_digest(
                request.core_config
            ),
            "input_bindings_sha256": canonical_digest(
                {
                    "input_bindings": [
                        binding.model_dump(mode="json")
                        for binding in request.input_bindings
                    ]
                }
            ),
            "declared_output_artifact_types": list(output_types),
            "output_artifact_ids": terminal["artifact_ids"],
            "priority": request.priority,
            "attempt_count": 1,
            "job_result_sha256": canonical_digest(terminal),
        }
        authority.update(
            self.succeeded_authority_overrides.get(target_id, {})
        )
        return authority

    def get_artifact(self, artifact_id: str) -> dict:
        artifact = self.artifacts[artifact_id]
        assert artifact["state"] != "sealed"
        return artifact

    def get_internal_successor_artifact(
        self,
        successor_transition_id: str,
        artifact_id: str,
    ) -> dict:
        assert self.artifact_owners[artifact_id] == (
            successor_transition_id
        )
        return self.artifacts[artifact_id]

    def apply_internal_artifact_admission(self, payload: dict) -> dict:
        request = ArtifactProposalDecisionRequest.model_validate(payload)
        job = self.jobs[request.job_id]
        proposal_ids = tuple(
            sorted(
                artifact_id
                for artifact_id in job["artifact_ids"]
                if self.artifacts[artifact_id]["type"]
                == request.artifact_type.value
            )
        )
        assert proposal_ids
        if request.action is ProposalAction.UPDATE:
            assert request.selected_artifact_id in proposal_ids
            active_artifact_id = request.selected_artifact_id
        else:
            assert request.selected_artifact_id is None
            assert request.parent_artifact_id is not None
            assert self.artifacts[request.parent_artifact_id]["promoted"] is True
            active_artifact_id = request.parent_artifact_id
        for proposal_id in proposal_ids:
            self.artifacts[proposal_id]["promoted"] = (
                proposal_id == active_artifact_id
            )
            next(
                output
                for output in job["outputs"]
                if output["artifact_id"] == proposal_id
            )["promoted"] = proposal_id == active_artifact_id
        if request.parent_artifact_id is not None:
            self.artifacts[request.parent_artifact_id]["promoted"] = (
                True
            )
        content_admission = artifact_content_admission_receipt(
            basis=request.content_admission_basis,
            payloads={
                proposal_id: {"memory.md": "Use a generic validation workflow."}
                for proposal_id in proposal_ids
            },
            source_payloads={
                artifact_id: {
                    "records.jsonl": '{"response":"A generic completed trace."}'
                }
                for binding in self.job_requests[request.job_id].input_bindings
                if binding.binding_id in {"current_dataset", "dataset_inputs"}
                for artifact_id in binding.artifact_ids
            },
        )
        body = {
            "schema_version": "openevo.artifact_proposal_decision.v1",
            "decision_id": request.decision_id,
            "job_id": request.job_id,
            "artifact_type": request.artifact_type.value,
            "action": request.action.value,
            "parent_artifact_id": request.parent_artifact_id,
            "genesis": request.genesis,
            "proposal_artifact_ids": list(proposal_ids),
            "selected_artifact_id": request.selected_artifact_id,
            "rejected_artifact_ids": [
                item for item in proposal_ids if item != request.selected_artifact_id
            ],
            "active_artifact_id": active_artifact_id,
            "reason": request.reason,
            "admission": request.admission.model_dump(mode="json"),
            "content_admission": content_admission.model_dump(mode="json"),
            "created_at": "2026-07-23T03:00:00Z",
        }
        body["content_sha256"] = canonical_digest(body)
        self.admission_receipts[request.job_id] = body
        return body

    def get_internal_successor_artifact_authority(
        self,
        successor_transition_id: str,
        artifact_id: str,
    ) -> dict:
        assert self.artifact_owners[artifact_id] == successor_transition_id
        job_id = next(
            job_id
            for job_id, job in self.jobs.items()
            if artifact_id in job["artifact_ids"]
        )
        request = self.job_requests[job_id]
        receipt = self.admission_receipts[job_id]
        output = next(
            item
            for item in self.jobs[job_id]["outputs"]
            if item["artifact_id"] == artifact_id
        )
        return {
            "successor_transition_id": successor_transition_id,
            "job_id": job_id,
            "input_artifact_ids": [
                artifact_id
                for binding in request.input_bindings
                for artifact_id in binding.artifact_ids
            ],
            "artifact": self.artifacts[artifact_id],
            "payload_manifest_sha256": output["payload_manifest_digest"],
            "payload_byte_size": output["payload_byte_size"],
            "proposal_artifact_ids": receipt["proposal_artifact_ids"],
            "admission_decision_id": receipt["decision_id"],
            "admission_decision_sha256": receipt["content_sha256"],
            "content_admission": receipt["content_admission"],
            "promotion_status": "promoted",
        }

    def discard_successor_transition_outputs(
        self,
        successor_transition_id: str,
    ) -> dict:
        self.discarded_transition_ids.append(successor_transition_id)
        assert self.discard_response is not None
        return self.discard_response

    def create_materialized_context(self, payload: dict) -> dict:
        request = ContextProjectionResolveRequest.model_validate(payload)
        assert request.metadata.evolution is not None
        artifact_ids = request.metadata.evolution.context_artifact_ids
        owner_transition_ids = (
            request.metadata.evolution.context_artifact_owner_transition_ids
        )
        assert artifact_ids is not None
        assert owner_transition_ids is not None
        assert tuple(
            self.artifact_owners[artifact_id]
            for artifact_id in artifact_ids
        ) == owner_transition_ids
        self.materialized_count += 1
        context_suffix = (
            ""
            if self.materialized_count == 1
            else f"-{self.materialized_count}"
        )
        self.materialized = MaterializedContext(
            context_id=f"ctx-successor-v2{context_suffix}",
            request_digest=canonical_digest(request),
            registry_digest=self.registry_sha256,
            successor_transition_id=request.successor_transition_id,
            predecessor_project_head_id=request.predecessor_project_head_id,
            base_model=request.base_model,
            projections=(),
            selection={
                "artifact_ids": artifact_ids,
                "skipped_artifacts": (),
                "reasons": ("explicit_artifact_ids",),
            },
            blobs=(),
            environment=(),
            instruction=(
                "Use the following long-term memory for this task:\nUse the accepted result."
            ),
            adapter_merge_spec={
                "base_model": request.base_model,
                "merge_mode": "reference_only",
                "adapters": (),
            },
        )
        if self.materialization_response_loss_once:
            self.materialization_response_loss_once = False
            raise httpx.ReadError("simulated materialization response loss")
        return self.materialized.model_dump(mode="json")

    def get_internal_successor_materialized_context(
        self,
        successor_transition_id: str,
        request_digest: str,
    ) -> dict:
        if (
            self.materialized is None
            or self.materialized.successor_transition_id
            != successor_transition_id
            or self.materialized.request_digest != request_digest
        ):
            raise EvolutionHttpStatusError(
                status_code=404,
                detail_code="not_found",
            )
        return self.materialized.model_dump(mode="json")


def test_production_preparer_discards_only_the_exact_transition_outputs(
    tmp_path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    evolution = _Evolution(registry.snapshot.registry_digest)
    preparer = ProductionScienceSuccessorPreparerV2(
        catalog=object(),
        ledger=object(),
        workspaces=object(),
        workspace_handoffs=object(),
        services=object(),
        executable_registry=registry,
    )
    context = SimpleNamespace(
        transition=SimpleNamespace(
            transition=SimpleNamespace(
                successor_transition_id="successor-transition-exact"
            )
        )
    )
    record = object()
    project = object()

    @contextmanager
    def _evolution(_context, _record, _project):
        assert _context is context
        assert _record is record
        assert _project is project
        yield object(), evolution

    preparer._cleanup_authority = lambda _context: (
        record,
        object(),
        project,
    )
    preparer._evolution = _evolution

    evolution.discard_response = {
        "successor_transition_id": "successor-transition-exact",
        "discarded_artifact_ids": ["artifact-sealed-1"],
        "discarded_materialized_context_ids": [
            "context-materialized-1"
        ],
    }
    receipt = preparer.discard_transition_outputs(context)
    assert receipt.successor_transition_id == (
        "successor-transition-exact"
    )
    assert receipt.discarded_artifact_ids == (
        "artifact-sealed-1",
    )
    assert receipt.discarded_materialized_context_ids == (
        "context-materialized-1",
    )
    assert evolution.discarded_transition_ids == [
        "successor-transition-exact"
    ]

    evolution.discard_response = {
        "successor_transition_id": "successor-transition-other",
        "discarded_artifact_ids": [],
        "discarded_materialized_context_ids": [],
    }
    with pytest.raises(
        ScienceSuccessorPreparationV2Error,
        match="discard receipt differs from the requested transition",
    ):
        preparer.discard_transition_outputs(context)


def test_production_preparer_fails_closed_after_shutdown_is_requested(
    tmp_path,
) -> None:
    registry = verified_builtin_registry(tmp_path / "registry")
    preparer = ProductionScienceSuccessorPreparerV2(
        catalog=object(),
        ledger=object(),
        workspaces=object(),
        workspace_handoffs=object(),
        services=object(),
        executable_registry=registry,
    )

    preparer.request_stop()

    with pytest.raises(
        ScienceSuccessorPreparationV2Error,
        match="successor preparation is stopping",
    ):
        preparer.seal_dataset(object())


def test_production_preparer_commits_complete_workspace_and_context_successor(
    tmp_path,
) -> None:
    clock = _Clock()
    registry = verified_builtin_registry(tmp_path / "registry")
    base_config = _project_config()
    config_json = base_config.model_dump(mode="json")
    config_json["evolution"]["targets"] = {
        "agent_system": {
            "enabled": True,
            "method": "agent_system_gepa_reflector",
            "config": {},
        },
        "skill_bundle": {
            "enabled": True,
            "method": "skill_bundle_reflector",
            "config": {},
        },
        "text_memory": {
            "enabled": True,
            "method": "text_memory_expel_reflector",
            "config": {},
        },
    }
    config = type(base_config).model_validate(config_json)
    binding = _service_binding(registry.snapshot.registry_digest)
    project_id = "project-execution"
    workspaces = WorkspaceStoreV2(tmp_path / "workspaces")
    input_workspace = workspaces.ensure_empty_snapshot(project_id)
    verified = resolve_genesis_execution_snapshot(
        settings=EffectiveExecutionSettings(
            execution_mode=config.execution.mode,
            capture_mode=config.execution.capture_mode,
            harness_id=config.execution.harness_id,
            model_ref=config.execution.codex_model,
            token_limit=config.execution.token_limit,
            task_network_allow_internet=config.execution.task_network_allow_internet,
        ),
        service_binding=binding,
    )
    execution_sha256 = canonical_digest(verified.snapshot)
    head = _head(
        project_id,
        registry_sha256=registry.snapshot.registry_digest,
        workspace=input_workspace,
        effective_execution=EffectiveExecutionSnapshotRefV2(
            effective_execution_snapshot_id=f"exec-{execution_sha256}",
            project_id=project_id,
            execution_mode=config.execution.mode,
            capture_mode=config.execution.capture_mode,
            token_level_metrics_available=False,
            producer_id=verified.producer_id,
            snapshot_sha256=execution_sha256,
        ),
    )
    authority = ScienceProjectAdmissionAuthorityV2(
        project_id=project_id,
        active_project_head=head,
        project_config_sha256=project_config_sha256_for(config),
        workspace_snapshot=input_workspace,
        normalized_evolution_intent_sha256=canonical_digest(config.evolution),
    )
    project = ProjectRecordV2(
        project_id=project_id,
        display_name="Production successor project",
        config=config,
        project_config_sha256=project_config_sha256_for(config),
        created_at="2026-07-23T02:00:00.000000Z",
        updated_at="2026-07-23T02:00:00.000000Z",
        resource_version=1,
    )
    catalog = _Catalog(project)
    handoffs = WorkspaceHandoffStoreV2(tmp_path / "workspace-handoffs")
    services = _Services(binding)
    evolution = _Evolution(registry.snapshot.registry_digest)
    rollout = _Rollout(handoffs, binding, tmp_path / "gateway-sessions")
    evolution.rollout = rollout
    (tmp_path / "gateway-sessions").mkdir(mode=0o700)

    def executor_factory(ledger):
        return ScienceAttemptExecutorV2(
            catalog=catalog,
            workspaces=workspaces,
            workspace_handoffs=handoffs,
            ledger=ledger,
            services=services,
            executable_registry=registry,
            rollout_factory=lambda _binding: rollout,
            prior_dataset_artifact_ids=lambda project_head: (
                ledger.prior_dataset_artifact_ids_for_head(project_head.project_head_id)
            ),
            clock=clock,
            poll_interval_seconds=0,
            max_poll_attempts=2,
        )

    def successor_factory(ledger):
        return ProductionScienceSuccessorPreparerV2(
            catalog=catalog,
            ledger=ledger,
            workspaces=workspaces,
            workspace_handoffs=handoffs,
            services=services,
            executable_registry=registry,
            evolution_factory=lambda _binding: evolution,
            artifact_admission_root=tmp_path / "artifact-admission",
            clock=clock,
            poll_interval_seconds=0,
            max_poll_attempts=2,
        )

    owner = CoreScienceTaskOwnerV2(
        state_root=tmp_path / "owner",
        clock=clock,
        attempt_executor_factory=executor_factory,
        successor_preparer_factory=successor_factory,
    )
    try:
        owner.publish_project_admission_authority(authority)
        task = owner.invoke(
            "submitCoreTaskV2",
            {
                "request": TaskSubmitRequestV2(
                    project_id=project_id,
                    expected_project_admission_etag=authority.project_etag,
                    expected_project_head_id=head.project_head_id,
                    expected_project_head_manifest_sha256=head.manifest_sha256,
                    expected_project_config_sha256=project.project_config_sha256,
                ),
                "idempotency_key": "production-successor",
            },
        )
        try:
            _wait_task_state(owner, task.task_id, "completed")
        except AssertionError as exc:
            current = owner.invoke("getCoreTaskV2", {"task_id": task.task_id})
            transitions = owner.list_successor_transitions(project_id)
            raise AssertionError(f"{exc}; current={current}; transitions={transitions}") from exc

        successor = owner.active_project_head(project_id)
        assert successor.generation == 1
        assert successor.project_head_id == f"project-head-{successor.manifest_sha256}"
        assert successor.predecessor_project_head_id == head.project_head_id
        assert successor.evolution_revision.artifact_count == 3
        assert successor.runtime_context_snapshot.evolution_revision_id == (
            successor.evolution_revision.evolution_revision_id
        )
        assert evolution.materialized is not None
        assert successor.workspace_snapshot != input_workspace
        result_root = workspaces.snapshot_path(successor.workspace_snapshot)
        assert (result_root / "answer.txt").read_text(encoding="utf-8") == ("accepted\n")
        transition = owner.get_successor_transition_for_task(task.task_id)
        commit = owner.successor_commit(transition.transition.successor_transition_id)
        assert commit is not None
        assert commit.manifest.dataset_artifact_id == (
            "artifact-dataset-successor-1"
        )
        assert commit.manifest.method_artifact_ids == (
            "artifact-agent_system-successor-candidate-2",
            "artifact-skill_bundle-successor",
            "artifact-text_memory-successor",
        )
        agent_admission = evolution.admission_receipts[
            "job-agent_system-successor"
        ]
        assert len(agent_admission["proposal_artifact_ids"]) == 3
        assert agent_admission["active_artifact_id"] == (
            "artifact-agent_system-successor-candidate-2"
        )
        assert sum(
            bool(evolution.artifacts[item]["promoted"])
            for item in agent_admission["proposal_artifact_ids"]
        ) == 1
        assert commit.manifest.materialized_context_id == "ctx-successor-v2"

        closed_task = owner.invoke(
            "getCoreTaskV2",
            {"task_id": task.task_id},
        )
        accepted_attempt = next(
            attempt
            for attempt in closed_task.attempts
            if attempt.attempt_id == commit.manifest.accepted_attempt_id
        )
        attempt_record = owner._ledger.get_attempt_execution(
            closed_task.task_id,
            accepted_attempt.attempt_id,
        )
        assert attempt_record.successor_plan is not None
        committed_attempt = owner.successor_transition_attempts(
            transition.transition.successor_transition_id
        )[-1]
        reconciliation_context = ScienceSuccessorPreparationContextV2(
            task=closed_task,
            accepted_attempt=accepted_attempt,
            transition=transition.model_copy(
                update={
                    "state": "running_methods",
                    "progress_completed": 2,
                    "error": None,
                }
            ),
            transition_attempt=committed_attempt.model_copy(
                update={
                    "state": "running",
                    "error": None,
                    "commit_manifest_sha256": None,
                    "reconciliation_only": True,
                    "reconciliation_source_attempt_id": (
                        committed_attempt.transition_attempt_id
                    ),
                    "reconciliation_source_authority_sha256": "1" * 64,
                }
            ),
            plan=attempt_record.successor_plan,
        )
        dataset_response = evolution.datasets[commit.manifest.dataset_id]
        reconciliation_dataset = SealedTranscriptDatasetV2(
            dataset_id=commit.manifest.dataset_id,
            artifact_id=commit.manifest.dataset_artifact_id,
            manifest_sha256=commit.manifest.dataset_manifest_sha256,
            record_count=dataset_response["trace_count"],
            task_id=closed_task.task_id,
            task_admission_id=closed_task.admission.task_admission_id,
            accepted_attempt_id=accepted_attempt.attempt_id,
            capture_mode="transcript",
            token_level_metrics_available=False,
            sealed=True,
        )
        job_inventory_before = dict(evolution.jobs)
        target_run_counts_before = dict(evolution.target_run_counts)
        reconciled_outputs = owner._successor_preparer.reconcile_completed_methods(
            reconciliation_context,
            reconciliation_dataset,
        )
        assert tuple(item.artifact_id for item in reconciled_outputs) == (
            commit.manifest.method_artifact_ids
        )
        assert evolution.jobs == job_inventory_before
        assert evolution.target_run_counts == target_run_counts_before

        later_binding = _service_binding("9" * 64)
        later_binding = replace(
            later_binding,
            framework_lock_digest="8" * 64,
            _identity=replace(
                later_binding._identity,
                framework_lock_digest="8" * 64,
            ),
        )
        services.binding = later_binding
        evolution.registry_sha256 = later_binding.registry_digest
        cross_generation_outputs = (
            owner._successor_preparer.reconcile_completed_methods(
                reconciliation_context,
                reconciliation_dataset,
            )
        )
        assert cross_generation_outputs == reconciled_outputs
        assert evolution.jobs == job_inventory_before
        assert evolution.target_run_counts == target_run_counts_before

        evolution.materialized = None
        evolution.materialization_response_loss_once = True
        materialized_before = evolution.materialized_count
        validated = owner._successor_preparer.validate_outputs(
            reconciliation_context,
            reconciliation_dataset,
            cross_generation_outputs,
        )
        first_materialized = owner._successor_preparer.materialize_context(
            reconciliation_context,
            validated,
        )
        replayed_materialized = owner._successor_preparer.materialize_context(
            reconciliation_context,
            validated,
        )
        assert replayed_materialized == first_materialized
        assert evolution.materialized_count == materialized_before + 1
        assert (
            first_materialized.runtime_context_snapshot.registry_sha256
            == later_binding.registry_digest
        )
        assert (
            _validate_successor_materialization_receipt(
                first_materialized,
                context=reconciliation_context,
                validated=validated,
            )
            == first_materialized
        )
        strict_context = reconciliation_context.model_copy(
            update={
                "transition_attempt": (
                    reconciliation_context.transition_attempt.model_copy(
                        update={
                            "reconciliation_only": False,
                            "reconciliation_source_attempt_id": None,
                            "reconciliation_source_authority_sha256": None,
                        }
                    )
                )
            }
        )
        with pytest.raises(
            ValueError,
            match="successor materialization does not bind validated outputs",
        ):
            _validate_successor_materialization_receipt(
                first_materialized,
                context=strict_context,
                validated=validated,
            )

        services.binding = replace(
            later_binding,
            runtime_identity_digest="0" * 64,
        )
        with pytest.raises(
            ScienceSuccessorPreparationV2Error,
            match="service authority changed",
        ):
            owner._successor_preparer.reconcile_completed_methods(
                reconciliation_context,
                reconciliation_dataset,
            )
        services.binding = later_binding

        evolution.succeeded_authority_overrides["skill_bundle"] = {
            "job_type": "different-historical-method",
        }
        with pytest.raises(
            ScienceSuccessorPreparationV2Error,
            match="job authority is inconsistent",
        ):
            owner._successor_preparer.reconcile_completed_methods(
                reconciliation_context,
                reconciliation_dataset,
            )
        evolution.succeeded_authority_overrides.clear()
        assert evolution.jobs == job_inventory_before
        assert evolution.target_run_counts == target_run_counts_before
        services.binding = binding
        evolution.registry_sha256 = binding.registry_digest

        next_authority = owner.project_admission_authority(project_id)
        partial_config_json = config.model_dump(mode="json")
        partial_config_json["evolution"]["targets"] = {
            "agent_system": {
                "enabled": False,
                "method": "auto",
                "config": {},
            },
            "skill_bundle": {
                "enabled": False,
                "method": "skill_bundle_reflector",
                "config": {},
            },
            "text_memory": {
                "enabled": True,
                "method": "text_memory_expel_reflector",
                "config": {},
            },
        }
        partial_config = type(config).model_validate(
            partial_config_json
        )
        partial_project = ProjectRecordV2(
            project_id=project_id,
            display_name=project.display_name,
            config=partial_config,
            project_config_sha256=project_config_sha256_for(
                partial_config
            ),
            created_at=project.created_at,
            updated_at="2026-07-23T02:00:01.000000Z",
            resource_version=2,
        )
        catalog.project = partial_project
        desired_next_authority = ScienceProjectAdmissionAuthorityV2(
            project_id=project_id,
            active_project_head=successor,
            project_config_sha256=(
                partial_project.project_config_sha256
            ),
            workspace_snapshot=next_authority.workspace_snapshot,
            normalized_evolution_intent_sha256=canonical_digest(
                partial_config.evolution
            ),
        )
        owner.begin_project_admission_authority_rebind(next_authority)
        owner.finish_project_admission_authority_rebind(
            desired_next_authority,
        )
        next_authority = owner.release_project_admission_authority_rebind(
            desired_next_authority,
        )
        second = owner.invoke(
            "submitCoreTaskV2",
            {
                "request": TaskSubmitRequestV2(
                    project_id=project_id,
                    expected_project_admission_etag=next_authority.project_etag,
                    expected_project_head_id=successor.project_head_id,
                    expected_project_head_manifest_sha256=successor.manifest_sha256,
                    expected_project_config_sha256=(
                        partial_project.project_config_sha256
                    ),
                ),
                "idempotency_key": "production-successor-session-2",
            },
        )
        _wait_task_state(owner, second.task_id, "completed")
        assert len(rollout.requests) == 2
        assert rollout.requests[0].runtime_context_binding.source == "empty_genesis"
        second_context = rollout.requests[1].runtime_context_binding
        assert second_context.source == "materialized_successor"
        assert second_context.project_head == successor
        assert second_context.materialized_context_id == (commit.manifest.materialized_context_id)
        assert second_context.selected_artifact_ids == (commit.manifest.method_artifact_ids)
        assert rollout.input_answer_before_run == [None, "accepted\n"]
        second_successor = owner.active_project_head(project_id)
        assert second_successor.generation == 2
        assert second_successor.evolution_revision.artifact_count == 3
        second_transition = owner.get_successor_transition_for_task(
            second.task_id
        )
        second_commit = owner.successor_commit(
            second_transition.transition.successor_transition_id
        )
        assert second_commit is not None
        assert second_commit.manifest.method_artifact_ids == (
            "artifact-agent_system-successor-candidate-2",
            "artifact-skill_bundle-successor",
            "artifact-text_memory-successor-2",
        )
        assert evolution.materialized is not None
        assert evolution.materialized.selection.artifact_ids == (
            second_commit.manifest.method_artifact_ids
        )
        assert len(evolution.jobs) == 4

        no_evolution_json = partial_config.model_dump(mode="json")
        for target in no_evolution_json["evolution"]["targets"].values():
            target["enabled"] = False
        no_evolution_config = type(config).model_validate(
            no_evolution_json
        )
        no_evolution_project = ProjectRecordV2(
            project_id=project_id,
            display_name=project.display_name,
            config=no_evolution_config,
            project_config_sha256=project_config_sha256_for(
                no_evolution_config
            ),
            created_at=project.created_at,
            updated_at="2026-07-23T02:00:02.000000Z",
            resource_version=3,
        )
        catalog.project = no_evolution_project
        current_authority = owner.project_admission_authority(project_id)
        desired_third_authority = ScienceProjectAdmissionAuthorityV2(
            project_id=project_id,
            active_project_head=second_successor,
            project_config_sha256=(
                no_evolution_project.project_config_sha256
            ),
            workspace_snapshot=current_authority.workspace_snapshot,
            normalized_evolution_intent_sha256=canonical_digest(
                no_evolution_config.evolution
            ),
        )
        owner.begin_project_admission_authority_rebind(current_authority)
        owner.finish_project_admission_authority_rebind(
            desired_third_authority,
        )
        third_authority = owner.release_project_admission_authority_rebind(
            desired_third_authority,
        )
        prior_materialized_count = evolution.materialized_count
        third = owner.invoke(
            "submitCoreTaskV2",
            {
                "request": TaskSubmitRequestV2(
                    project_id=project_id,
                    expected_project_admission_etag=(
                        third_authority.project_etag
                    ),
                    expected_project_head_id=(
                        second_successor.project_head_id
                    ),
                    expected_project_head_manifest_sha256=(
                        second_successor.manifest_sha256
                    ),
                    expected_project_config_sha256=(
                        no_evolution_project.project_config_sha256
                    ),
                ),
                "idempotency_key": (
                    "production-successor-session-3-no-evolution"
                ),
            },
        )
        _wait_task_state(owner, third.task_id, "completed")
        third_successor = owner.active_project_head(project_id)
        assert third_successor.generation == 3
        assert third_successor.evolution_revision == (
            second_successor.evolution_revision
        )
        assert third_successor.runtime_context_snapshot == (
            second_successor.runtime_context_snapshot
        )
        third_transition = owner.get_successor_transition_for_task(
            third.task_id
        )
        third_commit = owner.successor_commit(
            third_transition.transition.successor_transition_id
        )
        assert third_commit is not None
        assert third_commit.manifest.method_artifact_ids == (
            second_commit.manifest.method_artifact_ids
        )
        assert evolution.materialized_count == prior_materialized_count
        assert len(evolution.jobs) == 4
        assert len(rollout.requests) == 3
        assert len(evolution.dataset_requests) == 3
        assert len(
            {
                request.idempotency_key
                for request in evolution.dataset_requests
            }
        ) == 3
    finally:
        owner.close()
        handoffs.close()
        workspaces.close()


def test_internal_materialized_context_transport_has_no_host_path(tmp_path) -> None:
    # This assertion guards the private successor receipt shape independently of
    # the HTTP transport tests in the Evolution suite.
    materialized = MaterializedContext(
        context_id="ctx-transport-v2",
        request_digest="1" * 64,
        registry_digest="2" * 64,
        projections=(),
        selection={"artifact_ids": (), "reasons": ("no_candidates",)},
        blobs=(),
        environment=(),
        instruction="",
        adapter_merge_spec={"merge_mode": "reference_only", "adapters": ()},
    )
    encoded = str(materialized.model_dump(mode="json"))
    assert str(tmp_path) not in encoded
    assert "file://" not in encoded


def test_recovery_seed_decodes_materialized_context_from_json_wire() -> None:
    materialized = MaterializedContext(
        context_id="ctx-recovery-seed-wire",
        request_digest="1" * 64,
        registry_digest="2" * 64,
        successor_transition_id="recovery-seed-wire",
        predecessor_project_head_id="project-head-genesis",
        projections=(),
        selection={
            "artifact_ids": ("artifact-agent", "artifact-memory"),
            "skipped_artifacts": (),
            "reasons": ("explicit_artifact_ids",),
        },
        blobs=(),
        environment=(),
        instruction="Use the admitted recovery artifacts.",
        adapter_merge_spec={"merge_mode": "reference_only", "adapters": ()},
    )
    wire = materialized.model_dump(mode="json")
    assert isinstance(wire["selection"]["artifact_ids"], list)
    assert isinstance(wire["selection"]["skipped_artifacts"], list)
    assert isinstance(wire["selection"]["reasons"], list)

    restored = _materialized_context_from_wire(wire)

    assert restored == materialized
    assert isinstance(restored.selection.artifact_ids, tuple)
    assert isinstance(restored.selection.skipped_artifacts, tuple)
    assert isinstance(restored.selection.reasons, tuple)
