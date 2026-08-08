from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from openevo_researchclawbench.config import FROZEN_TASKS
from openevo_researchclawbench.owned_resource_registry import OwnedResourceRegistry
from openevo_researchclawbench.training_state_store import (
    TrainingStateStore,
    canonical_sha256,
)
from openevo_researchclawbench.training_supervisor import (
    CandidateAuthorityUnavailable,
    CommunityTrainingSupervisor,
    SupervisorIdentity,
    TrainingBudgetPolicy,
    TrainingPaused,
    official_frozen_run_plan,
)

IDENTITY = SupervisorIdentity(
    protocol_sha256="1" * 64,
    core_identity_sha256="2" * 64,
    adapter_identity_sha256="3" * 64,
)


class SyntheticOperations:
    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        self.results: dict[str, dict] = {}

    def _once(self, key: str, value: dict) -> dict:
        if key in self.results:
            assert self.results[key] == value
            return value
        self.calls[key] = self.calls.get(key, 0) + 1
        self.results[key] = value
        return value

    def candidate_readiness(self, request):
        result = {
            "service_reachable": True,
            "bearer_present": True,
            "bearer_valid": True,
            "generation_matches": True,
            "candidate_port_authorized": True,
            "secret_recorded": False,
            "environment_fallback_used": False,
            "service_identity_id": "synthetic-core-service",
            "generation": "1" * 32,
            "release_identity": "2" * 64,
        }
        if request.get("attempt_index", 0) > 0 and (
            request.get("preseeded_workspace_authority") is not None
            or request.get("successor_workspace_authority") is not None
        ):
            snapshot = {
                "workspace_snapshot_id": (
                    f"workspace-{request['task_id']}-{request['attempt_index']}"
                ),
                "project_id": "project-synthetic",
                "manifest_sha256": "9" * 64,
                "entry_count": 8,
                "byte_size": 128,
            }
            result["candidate_workspace_binding"] = {
                "schema_version": (
                    "openevo.researchclawbench.candidate_workspace_binding.v1"
                ),
                "workspace_snapshot_match": True,
                "expected_workspace_snapshot": snapshot,
                "actual_workspace_snapshot": snapshot,
                "content_sha256": "8" * 64,
            }
        return result

    def evolution_readiness(self, request):
        del request
        return {
            "ready": True,
            "identity_matches": True,
            "authority_issued": True,
            "docker_mount_created": True,
            "container_path_visible": True,
            "container_user_can_read": True,
            "generation_matches": True,
            "release_identity_matches": True,
            "adoption_receipt_valid": True,
            "cleanup_verified": True,
            "secret_recorded": False,
            "codex_cli_started": False,
            "model_started": False,
        }

    def ensure_candidate(self, request, idempotency_key):
        attempt = request["attempt_index"]
        project_id = request.get("core_project_id") or "project-synthetic"
        injected = [
            f"artifact-{request['task_id']}-0-{kind}"
            for kind in ("system", "memory", "skills")
        ]
        artifact_types = ("agent_system", "text_memory", "skill_bundle")
        runtime_injection = {
            "artifact_count": 0 if attempt == 0 else 3,
            "artifact_ids": [] if attempt == 0 else injected,
            "runtime_injection_receipt": (
                None
                if attempt == 0
                else {
                    "artifacts": [
                        {
                            "artifact_type": artifact_type,
                            "artifact_id": artifact_id,
                            "content_sha256": str(index + 1) * 64,
                        }
                        for index, (artifact_type, artifact_id) in enumerate(
                            zip(artifact_types, injected, strict=True)
                        )
                    ]
                }
            ),
        }
        workspace_authority = {
            "schema_version": (
                "openevo.researchclawbench.successor_workspace_authority.v2"
            ),
            "task_id": request["task_id"],
            "source_attempt_index": attempt,
            "source_run_id": request["run_id"],
            "project_id": "project-synthetic",
            "successor_transition_id": (
                f"successor-{request['task_id']}-{attempt}"
            ),
            "workspace_root": f"/synthetic/{request['task_id']}/{attempt}",
        }
        workspace_authority["content_sha256"] = canonical_sha256(
            workspace_authority
        )
        return self._once(
            idempotency_key,
            {
                "session_id": f"session-{request['task_id']}-{attempt}",
                "dataset_id": f"dataset-{request['task_id']}-{attempt}",
                "dataset_revision": f"dataset-artifact-{request['task_id']}-{attempt}.v1",
                "completed": True,
                "run_id": request["run_id"],
                "core_project_id": project_id,
                "core_task_id": f"task-{request['task_id']}-{attempt}",
                "core_attempt_id": f"attempt-{request['task_id']}-{attempt}",
                "workspace_binding_id": f"workspace-{request['task_id']}-{attempt}",
                "task_request_id": f"request-{request['task_id']}-{attempt}",
                "session_result_id": f"result-{request['task_id']}-{attempt}",
                "candidate_output_root": (
                    f"/synthetic/{request['task_id']}/candidate-{attempt}"
                ),
                "input_project_head_id": (
                    f"head-{request['task_id']}-{attempt}"
                ),
                "runtime_injection": runtime_injection,
                "transcript_receipt": {"sha256": "f" * 64},
                "runtime_seconds": 10 + attempt,
                "cost_total_usd": None,
                "composite_size_bytes": 100 + attempt,
                "successor_transition_id": workspace_authority[
                    "successor_transition_id"
                ],
                "successor_workspace_authority": workspace_authority,
            },
        )

    def validate_artifact(self, request, idempotency_key):
        return self._once(
            idempotency_key,
            {
                "artifact_valid": True,
                "completeness": 17,
                "artifact_root_sha256": "a" * 64,
            },
        )

    def evaluate(self, request, idempotency_key):
        attempt = int(request["candidate"]["run_id"].split("_a", 1)[1].split("_", 1)[0])
        return self._once(
            idempotency_key,
            {"total_score": (0.2, 0.5, 0.4)[attempt]},
        )

    def attach_feedback(self, request, idempotency_key):
        attempt = request["attempt_index"]
        result = {
            "attachment_id": f"attachment-{request['task_id']}-{attempt}",
            "resolved_view_sha256": f"{attempt + 4}" * 64,
            "task_local_overlay_id": f"overlay-{request['task_id']}",
        }
        if request.get("feedback_source") == "current_task_gt":
            gt = request["gt_supervision"]
            result.update(
                {
                    "feedback_class": "HARD_GT",
                    "feedback_source": "current_task_gt",
                    "judge_calls": 0,
                    "ground_truth_sha256": gt["ground_truth_sha256"],
                    "judge_feedback_included": False,
                }
            )
        return self._once(
            idempotency_key,
            result,
        )

    def current_task_gt_supervision(self, task_id):
        return {
            "task_id": task_id,
            "feedback_class": "HARD_GT",
            "ground_truth_sha256": "7" * 64,
            "judge_feedback_included": False,
        }

    def evolve_artifacts(self, request, idempotency_key):
        attempt = request["attempt_index"]
        return self._once(
            idempotency_key,
            {
                "jobs": [
                    {
                        "artifact_type": artifact_type,
                        "job_id": f"job-{request['task_id']}-{attempt}-{artifact_type}",
                    }
                    for artifact_type in ("agent_system", "text_memory", "skill_bundle")
                ]
            },
        )

    def admit_composite(self, request, idempotency_key):
        attempt = request["attempt_index"]
        return self._once(
            idempotency_key,
            {
                "composite_id": f"composite-{request['task_id']}-{attempt + 1}",
                "registry_artifact_ids": [
                    f"artifact-{request['task_id']}-{attempt}-{kind}"
                    for kind in ("system", "memory", "skills")
                ],
            },
        )

    def prepare_evolved_workspace(self, request, idempotency_key):
        admitted = request["admitted_composite"]
        return self._once(
            idempotency_key,
            {
                "task_id": request["task_id"],
                "same_task_only": True,
                "fresh_generation_zero_destination": True,
                "core_project_id": f"project-{request['task_id']}-evolved",
                "composite_id": f"{admitted['composite_id']}-clean",
                "artifact_ids": list(admitted["registry_artifact_ids"]),
                "preseeded_workspace_authority": {"synthetic": True},
                "candidate_started": False,
                "task_created": False,
                "raw_gt_carried": False,
                "judge_feedback_carried": False,
                "cross_task_inheritance": False,
                "recovery_command_used": False,
                "recovery_path_used": False,
            },
        )

    def destroy_task_local(self, request, idempotency_key):
        return self._once(idempotency_key, {"destroyed": True})

    def sanitize_composite(self, request, idempotency_key):
        return self._once(
            idempotency_key,
            {"passed": True, "composite_id": request["composite_id"]},
        )

    def freeze_final(self, request, idempotency_key):
        return self._once(
            idempotency_key,
            {
                "agent_system": {"registry_id": "system-final", "sha256": "a" * 64},
                "text_memory": {"registry_id": "memory-final", "sha256": "b" * 64},
                "skill_bundle": {"registry_id": "skills-final", "sha256": "c" * 64},
                "composite_sha256": "d" * 64,
            },
        )


def _supervisor(tmp_path: Path, operations: SyntheticOperations, tasks=("Life_005",)):
    store = TrainingStateStore(tmp_path / "state")
    supervisor = CommunityTrainingSupervisor(
        store=store,
        experiment_id="synthetic-training",
        identity=IDENTITY,
        operations=operations,
        task_ids=tasks,
    )
    supervisor.initialize()
    return supervisor


def _per_item_supervisor(
    tmp_path: Path, operations: SyntheticOperations, task: str = "Life_005"
):
    store = TrainingStateStore(tmp_path / "per-item-state")
    supervisor = CommunityTrainingSupervisor(
        store=store,
        experiment_id="synthetic-per-item",
        identity=IDENTITY,
        operations=operations,
        task_ids=(task,),
        current_task_gt_supervision=True,
        per_item_reset=True,
        budget_policy=TrainingBudgetPolicy(
            max_candidate_model_calls=2,
            max_reflector_model_calls=5,
            max_judge_operations=2,
            cumulative_runtime_seconds=3600,
            reflector_calls_per_cycle=5,
        ),
    )
    supervisor.initialize()
    return supervisor


def test_per_item_reset_closes_exact_pair_and_clears_active_state(
    tmp_path: Path,
) -> None:
    class RecordingOperations(SyntheticOperations):
        def __init__(self) -> None:
            super().__init__()
            self.candidate_requests: list[dict] = []
            self.evaluation_requests: list[dict] = []
            self.attachment_requests: list[dict] = []
            self.evolution_requests: list[dict] = []
            self.evolved_workspace_requests: list[dict] = []

        def ensure_candidate(self, request, idempotency_key):
            self.candidate_requests.append(request)
            return super().ensure_candidate(request, idempotency_key)

        def evaluate(self, request, idempotency_key):
            self.evaluation_requests.append(request)
            return super().evaluate(request, idempotency_key)

        def attach_feedback(self, request, idempotency_key):
            self.attachment_requests.append(request)
            return super().attach_feedback(request, idempotency_key)

        def evolve_artifacts(self, request, idempotency_key):
            self.evolution_requests.append(request)
            return super().evolve_artifacts(request, idempotency_key)

        def prepare_evolved_workspace(self, request, idempotency_key):
            self.evolved_workspace_requests.append(request)
            return super().prepare_evolved_workspace(request, idempotency_key)

    operations = RecordingOperations()
    supervisor = _per_item_supervisor(tmp_path, operations)
    _drive_to(supervisor, "ITEM_RESET")

    state = supervisor.status()
    verified = supervisor.verify()
    pair = state["paired_result"]
    assert state["namespace_type"] == "per_item_reset"
    assert pair["baseline_score"] == 0.2
    assert pair["evolved_score"] == 0.5
    assert pair["delta"] == pytest.approx(0.3)
    assert all(pair["candidate_identity_distinct"].values())
    assert pair["artifact_consumption"]["artifact_read_requested"] is True
    assert len(pair["artifact_consumption"]["artifact_ids"]) == 3
    assert set(pair["artifact_consumption"]["artifact_sha256"]) == {
        "agent_system",
        "text_memory",
        "skill_bundle",
    }
    assert state["active_artifact_ids"] == []
    assert len(state["archived_artifact_ids"]) == 3
    assert state["current_composite_id"] is None
    assert state["core_project_id"] is None
    assert state["preseeded_workspace_authority"] is None
    assert state["current_task_local_overlay_id"] is None
    assert state["current_task_local_overlay_scope_id"] is None
    assert state["item_reset_receipt"]["cross_project_fork_to_next_task"] is False
    assert verified["execution_status"] == "COMPLETED"
    assert operations.calls[
        "synthetic-per-item:Life_005:a0:evaluation"
    ] == 1
    assert operations.calls[
        "synthetic-per-item:Life_005:a1:evaluation"
    ] == 1
    assert operations.calls[
        "synthetic-per-item:Life_005:a0:evolution"
    ] == 1
    assert "synthetic-per-item:Life_005:a1:evolution" not in operations.calls
    assert len(operations.candidate_requests) == 2
    baseline_request, evolved_request = operations.candidate_requests
    assert baseline_request["attempt_index"] == 0
    assert baseline_request["core_project_id"] is None
    assert baseline_request["input_composite_id"] == "c000"
    assert baseline_request["task_local_overlay_id"] is None
    assert baseline_request["preseeded_workspace_authority"] is None
    assert evolved_request["attempt_index"] == 1
    assert evolved_request["task_local_overlay_id"] is None
    assert evolved_request["preseeded_workspace_authority"] is not None
    for candidate_request in operations.candidate_requests:
        assert "ground_truth" not in candidate_request
        assert "judge" not in candidate_request
        assert candidate_request["fresh_workspace"] is True
        assert candidate_request["resume_in_place"] is False
    assert len(operations.evaluation_requests) == 2
    assert len(operations.attachment_requests) == 1
    attachment = operations.attachment_requests[0]
    assert attachment["feedback_source"] == "current_task_gt"
    assert attachment["judge_feedback"] is None
    assert attachment["gt_supervision"]["ground_truth_sha256"] == "7" * 64
    assert len(operations.evolution_requests) == 1
    assert operations.evolution_requests[0]["attachment"][
        "judge_feedback_included"
    ] is False
    assert len(operations.evolved_workspace_requests) == 1
    assert operations.evolved_workspace_requests[0]["judge_feedback"] is None


def test_next_per_item_namespace_starts_clean_after_prior_item_reset(
    tmp_path: Path,
) -> None:
    life_operations = SyntheticOperations()
    life = _per_item_supervisor(tmp_path / "life", life_operations)
    _drive_to(life, "ITEM_RESET")
    life_state = life.status()

    class ChemistryRecordingOperations(SyntheticOperations):
        def __init__(self) -> None:
            super().__init__()
            self.candidate_requests: list[dict] = []

        def ensure_candidate(self, request, idempotency_key):
            self.candidate_requests.append(request)
            return super().ensure_candidate(request, idempotency_key)

    chemistry_operations = ChemistryRecordingOperations()
    chemistry = _per_item_supervisor(
        tmp_path / "chemistry",
        chemistry_operations,
        task="Chemistry_004",
    )
    initial = chemistry.status()
    assert initial["current_composite_id"] == "c000"
    assert initial["core_project_id"] is None
    assert initial["active_artifact_ids"] == []
    assert initial["archived_artifact_ids"] == []
    assert initial["current_task_local_overlay_id"] is None
    assert initial["preseeded_workspace_authority"] is None
    _drive_to(chemistry, "CANDIDATE_SEALED")

    request = chemistry_operations.candidate_requests[0]
    assert request["task_id"] == "Chemistry_004"
    assert request["attempt_index"] == 0
    assert request["core_project_id"] is None
    assert request["input_composite_id"] == "c000"
    assert request["task_local_overlay_id"] is None
    assert request["preseeded_workspace_authority"] is None
    assert request["successor_workspace_authority"] is None
    assert "Life_005" not in repr(request)
    chemistry_session = chemistry.status()["session_ids"][0]
    assert chemistry_session not in life_state["session_ids"]
    assert life_state["active_artifact_ids"] == []
    assert life_state["core_project_id"] is None


def _drive_to(supervisor: CommunityTrainingSupervisor, stage: str) -> None:
    for _ in range(100):
        if supervisor.status()["stage"] == stage:
            return
        supervisor.run_next()
    raise AssertionError(f"supervisor did not reach {stage}")


def test_single_task_three_attempt_closed_loop_survives_restart_every_transition(
    tmp_path: Path,
) -> None:
    operations = SyntheticOperations()
    supervisor = _supervisor(tmp_path, operations)
    for _ in range(200):
        state = supervisor.status()
        if state["stage"] == "FINAL_FROZEN":
            break
        supervisor.run_next()
        supervisor = _supervisor(tmp_path, operations)
    state = supervisor.status()
    assert state["stage"] == "FINAL_FROZEN"
    assert len(supervisor.store.attempts_for_task("synthetic-training", "Life_005")) == 3
    assert len(state["evolution_job_ids"]) == 6
    assert state["task_best"]["Life_005"]["attempt_index"] == 1
    assert state["current_task_local_overlay_id"] is None
    assert state["final_freeze_receipt"]["composite_sha256"] == "d" * 64
    assert all(count == 1 for count in operations.calls.values())


def test_current_task_gt_path_skips_judge_and_stops_after_native_evolution(
    tmp_path: Path,
) -> None:
    class CurrentTaskGtOperations(SyntheticOperations):
        def current_task_gt_supervision(self, task_id):
            assert task_id == "Life_005"
            return {
                "schema_version": "openevo.researchclawbench.current_task_gt.v1",
                "task_id": task_id,
                "ground_truth_sha256": "7" * 64,
                "feedback_class": "HARD_GT",
                "global_feedback": {},
                "task_local_feedback": {
                    "feedback_source": "current_task_gt",
                    "ground_truth_sha256": "7" * 64,
                    "ground_truth_entries": [{"content": "expected"}],
                    "judge_feedback_included": False,
                },
                "judge_feedback_included": False,
            }

        def evaluate(self, request, idempotency_key):  # pragma: no cover
            raise AssertionError("Judge must not run in current-task GT mode")

        def attach_feedback(self, request, idempotency_key):
            assert request["feedback_source"] == "current_task_gt"
            assert request["gt_supervision"]["judge_feedback_included"] is False
            return self._once(
                idempotency_key,
                {
                    "attachment_id": "attachment-Life_005-0",
                    "resolved_view_sha256": "4" * 64,
                    "task_local_overlay_id": "overlay-Life_005",
                    "task_local_overlay_scope_id": "task-Life_005-0",
                    "feedback_class": "HARD_GT",
                    "feedback_source": "current_task_gt",
                    "judge_calls": 0,
                    "ground_truth_sha256": "7" * 64,
                    "judge_feedback_included": False,
                },
            )

    operations = CurrentTaskGtOperations()
    supervisor = CommunityTrainingSupervisor(
        store=TrainingStateStore(tmp_path / "gt-state"),
        experiment_id="synthetic-current-task-gt",
        identity=IDENTITY,
        operations=operations,
        task_ids=("Life_005",),
        current_task_gt_supervision=True,
    )
    supervisor.initialize()
    _drive_to(supervisor, "EVOLUTION_COMPLETED")

    state = supervisor.status()
    assert state["current_attempt"] == 0
    assert state["supervision_mode"] == "current_task_gt"
    assert state["judge_feedback_included"] is False
    assert len(state["session_ids"]) == 1
    assert len(state["attachment_ids"]) == 1
    assert len(state["evolution_job_ids"]) == 3
    assert not any(":evaluation" in key for key in operations.calls)


def test_completed_side_effect_before_transition_is_not_repeated(tmp_path: Path) -> None:
    operations = SyntheticOperations()
    supervisor = _supervisor(tmp_path, operations)
    supervisor.run_next()  # attempt ready
    supervisor.run_next()  # candidate running and side effect planned
    request = {
        "experiment_id": "synthetic-training",
        "task_id": "Life_005",
        "attempt_index": 0,
        "run_id": "Life_005_a0_v2",
        "protocol_sha256": "1" * 64,
        "core_identity_sha256": "2" * 64,
        "adapter_identity_sha256": "3" * 64,
        "core_project_id": None,
        "input_composite_id": "c000",
        "task_local_overlay_id": None,
        "fresh_workspace": True,
        "resume_in_place": False,
        "core_control_generation": "1" * 32,
        "core_control_release_identity": "2" * 64,
        "core_control_service_identity_id": "synthetic-core-service",
    }
    key = "synthetic-training:Life_005:a0:candidate"
    receipt = operations.ensure_candidate(request, key)
    supervisor.store.complete_side_effect(idempotency_key=key, receipt=receipt)
    restarted = _supervisor(tmp_path, operations)
    restarted.run_next()
    assert restarted.status()["stage"] == "CANDIDATE_SEALED"
    assert operations.calls[key] == 1


def test_append_only_recovery_continuation_consumes_a0_without_candidate_replay(
    tmp_path: Path,
) -> None:
    operations = SyntheticOperations()
    store = TrainingStateStore(tmp_path / "continuation")
    supervisor = CommunityTrainingSupervisor(
        store=store,
        experiment_id="synthetic-recovery-continuation",
        identity=IDENTITY,
        operations=operations,
        task_ids=FROZEN_TASKS,
        validator_failure_policy={"enabled": True, "run_id_suffix": "v12"},
    )
    candidate = {
        "session_id": "session-source-a0",
        "dataset_id": "dataset-source-a0",
        "dataset_revision": "dataset-source-a0.v1",
        "completed": True,
        "run_id": "Astronomy_004_a0_v12",
        "runtime_seconds": 10.0,
        "core_project_id": "project-source",
        "core_task_id": "task-source",
        "core_attempt_id": "attempt-source",
        "successor_transition_id": "transition-source",
    }
    validation = {
        "artifact_valid": False,
        "completeness": 3,
        "validator_errors": ["REPORT_REQUIRED_SECTIONS_MISSING"],
        "artifact_root_sha256": "a" * 64,
    }
    attachment = {
        "attachment_id": "attachment-source-a0",
        "resolved_view_sha256": "b" * 64,
        "task_local_overlay_id": "attachment-source-a0",
        "task_local_overlay_scope_id": "task-source",
    }
    jobs = [
        {"artifact_type": kind, "job_id": f"recovery-job-{kind}"}
        for kind in ("agent_system", "skill_bundle", "text_memory")
    ]
    evolution = {"jobs": jobs, "recovery_id": "recovery-source"}
    admission = {
        "composite_id": "composite-recovery-c1",
        "core_project_id": "project-destination",
        "registry_artifact_ids": [
            "registry-agent-system",
            "registry-skill-bundle",
            "registry-text-memory",
        ],
    }
    preseeded_workspace_authority = {
        "schema_version": (
            "openevo.researchclawbench.preseeded_workspace_authority.v1"
        ),
        "project_id": "project-destination",
        "project_head_id": "project-head-destination",
        "project_head_manifest_sha256": "f" * 64,
        "workspace_snapshot_id": "workspace-destination",
        "workspace_manifest_sha256": "0" * 64,
        "archive_content_sha256": "1" * 64,
        "archive_byte_size": 1024,
        "archive_entry_count": 0,
        "archive_extracted_byte_size": 0,
        "seed_request_id": "recovery-seed-source",
        "seed_sha256": "e" * 64,
    }
    state = supervisor.initialize_successor_recovery_continuation(
        source_reference={
            "source_namespace": "source-v12",
            "source_state_sha256": "c" * 64,
            "source_transition_id": "transition-source",
            "source_transition_attempt_id": "transition-attempt-source",
            "recovery_id": "recovery-source",
            "recovery_record_sha256": "d" * 64,
            "recovery_seed_request_id": "recovery-seed-source",
            "recovery_seed_sha256": "e" * 64,
            "destination_project_id": "project-destination",
            "task_id": "Astronomy_004",
            "attempt_index": 0,
            "candidate_reexecuted": False,
            "additional_candidate_model_calls": 0,
            "preseeded_workspace_authority": preseeded_workspace_authority,
        },
        candidate=candidate,
        validation=validation,
        attachment=attachment,
        evolution=evolution,
        admission=admission,
    )
    assert state["stage"] == "RECOVERY_SEEDED_CONTINUATION"
    assert store.attempts_for_task(
        "synthetic-recovery-continuation", "Astronomy_004"
    )[0]["attempt_status"] == "CONSUMED_INVALID_ARTIFACT"
    assert operations.calls == {}
    assert supervisor.verify()["completed_reflector_cycles"] == 1

    assert supervisor.run_next()["stage"] == "NEXT_ATTEMPT_READY"
    ready = supervisor.run_next()
    assert ready["stage"] == "TASK_ATTEMPT_READY"
    assert ready["current_attempt"] == 1
    assert ready["current_composite_id"] == "composite-recovery-c1"
    assert operations.calls == {}

    running = supervisor.run_next()
    assert running["stage"] == "CANDIDATE_RUNNING"
    assert (
        running["active_candidate_intent"]["preseeded_workspace_authority"]
        == preseeded_workspace_authority
    )


def test_next_attempt_intent_binds_sealed_successor_workspace_authority(
    tmp_path: Path,
) -> None:
    operations = SyntheticOperations()
    supervisor = _supervisor(tmp_path, operations)
    for _ in range(100):
        state = supervisor.status()
        if (
            state["stage"] == "CANDIDATE_RUNNING"
            and state["current_attempt"] == 1
        ):
            break
        supervisor.run_next()
    else:
        raise AssertionError("supervisor did not bind the attempt-one workspace")

    intent = supervisor.status()["active_candidate_intent"]
    assert intent["successor_workspace_authority"]["source_attempt_index"] == 0
    assert intent["candidate_workspace_binding"]["workspace_snapshot_match"] is True
    assert (
        intent["expected_workspace_snapshot"]["workspace_snapshot_id"]
        == "workspace-Life_005-1"
    )


def test_completed_successor_failure_reconciles_once_without_candidate_replay(
    tmp_path: Path,
) -> None:
    terminal_sha256 = "a" * 64
    checkpoint_sha256 = "b" * 64

    class ReconciledOperations(SyntheticOperations):
        def evolve_artifacts(self, request, idempotency_key):
            result = super().evolve_artifacts(request, idempotency_key)
            result.update(
                {
                    "source_terminal_receipt_sha256": terminal_sha256,
                    "successor_recovery_checkpoint_sha256": checkpoint_sha256,
                    "model_calls_started": 0,
                    "successor_commit_id": "commit-reconciled",
                    "completed_methods_reconciliation": {
                        "reconciliation_only": True,
                        "model_execution_allowed": False,
                        "model_calls_started": 0,
                        "successor_transition_id": request["attachment"].get(
                            "successor_transition_id"
                        ),
                    },
                }
            )
            return result

    operations = ReconciledOperations()
    supervisor = _supervisor(tmp_path, operations)
    _drive_to(supervisor, "EVOLUTION_RUNNING")
    source_state = supervisor.status()
    executor = SupervisorIdentity(
        protocol_sha256="4" * 64,
        core_identity_sha256="5" * 64,
        adapter_identity_sha256="6" * 64,
    )

    reconciled = supervisor.reconcile_completed_successor_failure(
        operation_id="energy-a0",
        source_identity=IDENTITY,
        source_state_sha256=source_state["_state_sha256"],
        expected_source_terminal_receipt_sha256=terminal_sha256,
        expected_recovery_checkpoint_sha256=checkpoint_sha256,
        executor_identity=executor,
        executor_active_identity_sha256="7" * 64,
        executor_readiness_sha256="8" * 64,
    )
    assert reconciled["stage"] == "EVOLUTION_COMPLETED"
    assert len(reconciled["evolution_job_ids"]) == 3
    assert operations.calls[
        "synthetic-training:Life_005:a0:evolution"
    ] == 1

    assert supervisor.run_next()["stage"] == "COMPOSITE_ADMITTED"
    closed = supervisor.run_next()
    assert closed["stage"] == "NEXT_ATTEMPT_READY"
    replayed = supervisor.reconcile_completed_successor_failure(
        operation_id="energy-a0",
        source_identity=IDENTITY,
        source_state_sha256=source_state["_state_sha256"],
        expected_source_terminal_receipt_sha256=terminal_sha256,
        expected_recovery_checkpoint_sha256=checkpoint_sha256,
        executor_identity=executor,
        executor_active_identity_sha256="7" * 64,
        executor_readiness_sha256="8" * 64,
    )
    assert replayed == closed
    assert operations.calls[
        "synthetic-training:Life_005:a0:evolution"
    ] == 1


def test_completed_prefix_continuation_import_is_idempotent_and_model_free(
    tmp_path: Path,
) -> None:
    tasks = FROZEN_TASKS[:5]
    source_operations = SyntheticOperations()
    source_store = TrainingStateStore(tmp_path / "source")
    source = CommunityTrainingSupervisor(
        store=source_store,
        experiment_id="source-prefix",
        identity=IDENTITY,
        operations=source_operations,
        task_ids=tasks,
    )
    source.initialize()
    for _ in range(1000):
        state = source.status()
        if (
            state["stage"] == "NEXT_ATTEMPT_READY"
            and state["current_task_index"] == 4
            and state["current_attempt"] == 0
        ):
            break
        source.run_next()
    else:
        raise AssertionError("source prefix did not reach the Energy a0 boundary")

    source_state = source.status()
    attempts = {
        task: source.store.attempts_for_task("source-prefix", task)
        for task in tasks
    }
    effects = source.store.side_effects_for_experiment("source-prefix")
    budget = source.store.budget_usage("source-prefix")
    source_workspace = source_state["active_candidate_receipt"][
        "successor_workspace_authority"
    ]
    rebased_workspace = {
        **source_workspace,
        "workspace_root": "/synthetic-destination/Energy_004/0",
    }
    rebased_workspace.pop("content_sha256")
    rebased_workspace["content_sha256"] = canonical_sha256(rebased_workspace)
    reference = {
        "source_namespace": "rcb_oe_v0_source_prefix_v40",
        "source_state_sha256": source_state["_state_sha256"],
        "source_database_sha256": "4" * 64,
        "source_protocol_sha256": source_state["_protocol_sha256"],
        "source_core_identity_sha256": source_state["_core_identity_sha256"],
        "source_adapter_identity_sha256": source_state[
            "_adapter_identity_sha256"
        ],
        "source_reconciliation_receipt_sha256": "5" * 64,
        "source_successor_transition_id": source_workspace[
            "successor_transition_id"
        ],
        "source_successor_commit_sha256": "6" * 64,
        "source_stage": "NEXT_ATTEMPT_READY",
        "current_task_index": 4,
        "current_attempt": 0,
        "source_attempts_consumed": 13,
        "source_reflector_cycles_consumed": 9,
        "source_evolution_jobs_consumed": 27,
        "source_candidate_model_calls": 13,
        "source_judge_operations": 13,
        "source_reflector_model_calls": 45,
        "source_active_resources": 0,
        "source_pending_side_effects": 0,
        "source_failed_side_effects": 0,
        "candidate_reexecuted": False,
        "model_execution_allowed": False,
    }
    destination_operations = SyntheticOperations()
    destination = CommunityTrainingSupervisor(
        store=TrainingStateStore(tmp_path / "destination"),
        experiment_id="destination-prefix",
        identity=IDENTITY,
        operations=destination_operations,
        task_ids=tasks,
    )

    imported = destination.initialize_completed_prefix_continuation(
        source_reference=reference,
        source_state=source_state,
        attempts_by_task=attempts,
        completed_side_effects=effects,
        source_budget_usage=budget,
        rebased_successor_workspace_authority=rebased_workspace,
    )
    assert imported["stage"] == "NEXT_ATTEMPT_READY"
    assert imported["current_task_index"] == 4
    assert imported["current_attempt"] == 0
    assert imported["completed_prefix_import_complete"] is True
    verification = destination.verify()
    assert verification["attempt_receipts"] == 13
    assert verification["completed_reflector_cycles"] == 9
    assert verification["evolution_job_ids"] == 27
    assert verification["budget_usage"] == {
        "candidate_model_calls": 13,
        "reflector_model_calls": 45,
        "judge_operations": 13,
    }
    assert destination_operations.calls == {}

    replayed = destination.initialize_completed_prefix_continuation(
        source_reference=reference,
        source_state=source_state,
        attempts_by_task=attempts,
        completed_side_effects=effects,
        source_budget_usage=budget,
        rebased_successor_workspace_authority=rebased_workspace,
    )
    assert replayed == imported
    assert destination_operations.calls == {}
    ready = destination.run_next()
    assert ready["stage"] == "TASK_ATTEMPT_READY"
    assert ready["current_attempt"] == 1


def test_invalid_candidate_artifact_reaches_fail_closed_terminal_state(
    tmp_path: Path,
) -> None:
    class InvalidArtifact(SyntheticOperations):
        def validate_artifact(self, request, idempotency_key):
            return self._once(
                idempotency_key,
                {
                    "artifact_valid": False,
                    "completeness": 15,
                    "artifact_root_sha256": None,
                    "validator_errors": ["REPORT_REQUIRED_SECTIONS_MISSING"],
                },
            )

    operations = InvalidArtifact()
    supervisor = _supervisor(tmp_path, operations)
    _drive_to(supervisor, "CANDIDATE_SEALED")
    failed = supervisor.run_next()

    assert failed["stage"] == "FAILED"
    assert failed["failure_reason"] == "ARTIFACT_VALIDATOR_FAILED"
    assert operations.calls["synthetic-training:Life_005:a0:validator"] == 1
    assert supervisor.verify()["status"] == "PASS"


def test_core_auth_preflight_blocks_before_candidate_intent(tmp_path: Path) -> None:
    class MissingAuthority(SyntheticOperations):
        def candidate_readiness(self, request):
            del request
            raise RuntimeError("synthetic authority absent")

    operations = MissingAuthority()
    supervisor = _supervisor(tmp_path, operations)
    supervisor.run_next()
    blocked = supervisor.run_next()
    assert blocked["stage"] == "BLOCKED"
    assert blocked["failure_reason"] == "CORE_CONTROL_PREFLIGHT_FAILED"
    assert "active_candidate_intent" not in blocked
    assert (
        supervisor.store.side_effect(
            "synthetic-training:Life_005:a0:candidate"
        )
        is None
    )


def test_reflector_mount_preflight_blocks_before_evolution_intent(
    tmp_path: Path,
) -> None:
    class MissingReflectorMount(SyntheticOperations):
        def evolution_readiness(self, request):
            del request
            raise RuntimeError("synthetic mount authority absent")

    operations = MissingReflectorMount()
    supervisor = _supervisor(tmp_path, operations)
    _drive_to(supervisor, "EVOLUTION_PENDING")

    with pytest.raises(TrainingPaused, match="REFLECTOR_CREDENTIAL_MOUNT_NOT_READY"):
        supervisor.run_next()

    assert supervisor.status()["stage"] == "EVOLUTION_PENDING"
    assert (
        supervisor.store.side_effect(
            "synthetic-training:Life_005:a0:evolution"
        )
        is None
    )


def test_candidate_authority_race_moves_running_intent_to_blocked(tmp_path: Path) -> None:
    class GenerationChanged(SyntheticOperations):
        def ensure_candidate(self, request, idempotency_key):
            del request, idempotency_key
            raise CandidateAuthorityUnavailable("synthetic generation changed")

    operations = GenerationChanged()
    supervisor = _supervisor(tmp_path, operations)
    supervisor.run_next()
    running = supervisor.run_next()
    assert running["stage"] == "CANDIDATE_RUNNING"
    blocked = supervisor.run_next()
    assert blocked["stage"] == "BLOCKED"
    assert blocked["failure_reason"] == "CANDIDATE_OPERATION_FAILED_BEFORE_SEAL"
    planned = supervisor.store.side_effect(
        "synthetic-training:Life_005:a0:candidate"
    )
    assert planned is not None and planned["status"] == "planned"


def test_terminal_candidate_setup_failure_closes_effect_and_supervisor(
    tmp_path: Path,
) -> None:
    class SetupBlocked(SyntheticOperations):
        def ensure_candidate(self, request, idempotency_key):
            del request, idempotency_key
            raise CandidateAuthorityUnavailable(
                "synthetic setup isolation failed",
                reason_code="candidate_subscription_isolation_not_ready",
                terminal_proven=True,
                failure_receipt={
                    "schema_version": "openevo.candidate_terminal_failure.v1",
                    "core_task_id": "task-Life_005-0",
                    "core_attempt_id": "attempt-Life_005-0",
                    "underlying_session_status": "ERROR",
                    "model_started": False,
                    "benchmark_started": False,
                    "retryable": False,
                    "core_failure_code": (
                        "candidate_subscription_isolation_not_ready"
                    ),
                    "terminal_proven": True,
                },
            )

    supervisor = _supervisor(tmp_path, SetupBlocked())
    supervisor.run_next()
    assert supervisor.run_next()["stage"] == "CANDIDATE_RUNNING"
    blocked = supervisor.run_next()
    assert blocked["stage"] == "CANDIDATE_SETUP_BLOCKED"
    effect = supervisor.store.side_effect(
        "synthetic-training:Life_005:a0:candidate"
    )
    assert effect is not None and effect["status"] == "failed"
    assert effect["receipt"]["terminal_proven"] is True
    assert supervisor.store.budget_usage("synthetic-training") == {
        "candidate_model_calls": 1,
        "reflector_model_calls": 0,
        "judge_operations": 0,
    }
    verify = supervisor.verify()
    assert verify["integrity_status"] == "PASS"
    assert verify["execution_status"] == "CANDIDATE_SETUP_BLOCKED"
    assert verify["pending_side_effects"] == 0
    assert verify["pending_side_effect_status"] == "failed"
    assert verify["underlying_session_status"] == "ERROR"
    assert verify["model_started"] is False
    assert verify["benchmark_started"] is False
    with pytest.raises(TrainingPaused, match="CANDIDATE_SETUP_BLOCKED"):
        supervisor.resume()


def test_protocol_core_and_adapter_identity_drift_fail_closed(tmp_path: Path) -> None:
    operations = SyntheticOperations()
    _supervisor(tmp_path, operations)
    for changed in (
        SupervisorIdentity("9" * 64, "2" * 64, "3" * 64),
        SupervisorIdentity("1" * 64, "9" * 64, "3" * 64),
        SupervisorIdentity("1" * 64, "2" * 64, "9" * 64),
    ):
        supervisor = CommunityTrainingSupervisor(
            store=TrainingStateStore(tmp_path / "state"),
            experiment_id="synthetic-training",
            identity=changed,
            operations=operations,
            task_ids=("Life_005",),
        )
        with pytest.raises(ValueError, match="identity drifted"):
            supervisor.initialize()


def test_fixed_full_schedule_counts_are_51_34_102() -> None:
    assert len(FROZEN_TASKS) * 3 == 51
    assert len(FROZEN_TASKS) * 2 == 34
    assert len(FROZEN_TASKS) * 2 * 3 == 102


def test_budget_reservation_is_idempotent_and_conflicts_fail_closed(
    tmp_path: Path,
) -> None:
    store = TrainingStateStore(tmp_path / "budget-state")
    store.initialize_experiment(
        experiment_id="budget",
        protocol_sha256="1" * 64,
        core_identity_sha256="2" * 64,
        adapter_identity_sha256="3" * 64,
        initial_state={},
    )
    first = store.reserve_budget(
        experiment_id="budget",
        idempotency_key="candidate-a0",
        category="candidate_model_calls",
        units=1,
        limit_units=1,
    )
    replay = store.reserve_budget(
        experiment_id="budget",
        idempotency_key="candidate-a0",
        category="candidate_model_calls",
        units=1,
        limit_units=1,
    )
    assert first["used_units"] == replay["used_units"] == 1
    assert first["recovered"] is False
    assert replay["recovered"] is True
    with pytest.raises(ValueError, match="budget exhausted"):
        store.reserve_budget(
            experiment_id="budget",
            idempotency_key="candidate-a1",
            category="candidate_model_calls",
            units=1,
            limit_units=1,
        )
    with pytest.raises(ValueError, match="idempotency key conflicts"):
        store.reserve_budget(
            experiment_id="budget",
            idempotency_key="candidate-a0",
            category="judge_operations",
            units=1,
            limit_units=1,
        )


def test_candidate_budget_exhaustion_is_terminal_before_second_candidate_intent(
    tmp_path: Path,
) -> None:
    operations = SyntheticOperations()
    store = TrainingStateStore(tmp_path / "bounded-state")
    supervisor = CommunityTrainingSupervisor(
        store=store,
        experiment_id="bounded-training",
        identity=IDENTITY,
        operations=operations,
        task_ids=("Life_005",),
        budget_policy=TrainingBudgetPolicy(
            max_candidate_model_calls=1,
            max_reflector_model_calls=10,
            max_judge_operations=3,
            cumulative_runtime_seconds=10_000,
        ),
    )
    supervisor.initialize()
    _drive_to(supervisor, "TASK_ATTEMPT_READY")
    supervisor.run_next()
    _drive_to(supervisor, "NEXT_ATTEMPT_READY")
    supervisor.run_next()
    assert supervisor.status()["current_attempt"] == 1
    exhausted = supervisor.run_next()
    assert exhausted["stage"] == "BUDGET_EXHAUSTED"
    assert exhausted["failure_reason"] == "BUDGET_EXHAUSTED"
    assert (
        store.side_effect("bounded-training:Life_005:a1:candidate") is None
    )
    assert operations.calls["bounded-training:Life_005:a0:candidate"] == 1


def test_owned_process_stop_is_exact_and_nonowned_is_rejected(tmp_path: Path) -> None:
    store = TrainingStateStore(tmp_path / "state")
    store.initialize_experiment(
        experiment_id="resources",
        protocol_sha256="1" * 64,
        core_identity_sha256="2" * 64,
        adapter_identity_sha256="3" * 64,
        initial_state={},
    )
    registry = OwnedResourceRegistry(store, "resources")
    process = subprocess.Popen(["sleep", "60"], start_new_session=True)
    try:
        resource_id = registry.register_process(
            pid=process.pid,
            process_group=os.getpgid(process.pid),
        )
        with pytest.raises(PermissionError, match="not an active owned"):
            registry.stop_process("process-not-owned")
        assert registry.stop_process(resource_id) is True
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_best_of_three_rolls_back_regressed_latest_revision(tmp_path: Path) -> None:
    operations = SyntheticOperations()
    supervisor = _supervisor(tmp_path, operations)
    supervisor.run_until_pause()
    best = supervisor.status()["task_best"]["Life_005"]
    assert best["attempt_index"] == 1
    assert best["score"] == 0.5
    assert best["input_composite_id"] == "composite-Life_005-1"


def test_attachment_written_before_transition_is_not_repeated(tmp_path: Path) -> None:
    operations = SyntheticOperations()
    supervisor = _supervisor(tmp_path, operations)
    _drive_to(supervisor, "EVALUATED")
    state = supervisor.status()
    request = {
        "task_id": "Life_005",
        "attempt_index": 0,
        "dataset_id": state["active_candidate_receipt"]["dataset_id"],
        "dataset_revision": state["active_candidate_receipt"]["dataset_revision"],
        "session_id": state["active_candidate_receipt"]["session_id"],
        "core_task_id": state["active_candidate_receipt"]["core_task_id"],
        "core_attempt_id": state["active_candidate_receipt"]["core_attempt_id"],
        "successor_transition_id": state["active_candidate_receipt"].get(
            "successor_transition_id"
        ),
        "evaluation": state["active_evaluation_receipt"],
        "authority": "evaluator_only",
    }
    key = "synthetic-training:Life_005:a0:feedback-attachment"
    supervisor.store.plan_side_effect(
        experiment_id="synthetic-training",
        idempotency_key=key,
        kind="feedback-attachment",
        request=request,
    )
    receipt = operations.attach_feedback(request, key)
    supervisor.store.complete_side_effect(idempotency_key=key, receipt=receipt)

    restarted = _supervisor(tmp_path, operations)
    restarted.run_next()
    assert restarted.status()["stage"] == "ATTACHMENT_SEALED"
    assert operations.calls[key] == 1


def test_submitted_evolution_and_admitted_composite_survive_lost_response(
    tmp_path: Path,
) -> None:
    operations = SyntheticOperations()
    supervisor = _supervisor(tmp_path, operations)
    _drive_to(supervisor, "EVOLUTION_RUNNING")
    state = supervisor.status()
    evolution_request = {
        "task_id": "Life_005",
        "attempt_index": 0,
        "attachment": state["active_attachment_receipt"],
        "parent_composite_id": state["current_composite_id"],
        "artifact_types": ["agent_system", "text_memory", "skill_bundle"],
    }
    evolution_key = "synthetic-training:Life_005:a0:evolution"
    evolution = operations.evolve_artifacts(evolution_request, evolution_key)
    supervisor.store.complete_side_effect(
        idempotency_key=evolution_key,
        receipt=evolution,
    )
    restarted = _supervisor(tmp_path, operations)
    restarted.run_next()
    assert restarted.status()["stage"] == "EVOLUTION_COMPLETED"
    assert operations.calls[evolution_key] == 1

    state = restarted.status()
    admission_request = {
        "task_id": "Life_005",
        "attempt_index": 0,
        "parent_composite_id": state["current_composite_id"],
        "evolution": state["active_evolution_receipt"],
    }
    admission_key = "synthetic-training:Life_005:a0:composite-admission"
    restarted.store.plan_side_effect(
        experiment_id="synthetic-training",
        idempotency_key=admission_key,
        kind="composite-admission",
        request=admission_request,
    )
    admission = operations.admit_composite(admission_request, admission_key)
    restarted.store.complete_side_effect(
        idempotency_key=admission_key,
        receipt=admission,
    )
    restarted = _supervisor(tmp_path, operations)
    restarted.run_next()
    assert restarted.status()["stage"] == "COMPOSITE_ADMITTED"
    assert operations.calls[admission_key] == 1


def test_task_best_selection_restart_resumes_at_sanitizer_without_reselection(
    tmp_path: Path,
) -> None:
    operations = SyntheticOperations()
    supervisor = _supervisor(tmp_path, operations)
    _drive_to(supervisor, "TASK_BEST_SELECTED")
    selected = supervisor.status()["task_best"]["Life_005"]

    restarted = _supervisor(tmp_path, operations)
    restarted.run_next()
    assert restarted.status()["stage"] == "TASK_LOCAL_DESTROYED"
    assert restarted.status()["task_best"]["Life_005"] == selected
    restarted.run_next()
    assert restarted.status()["stage"] == "CROSS_TASK_SANITIZED"


def test_official_plan_requires_final_freeze_and_disables_all_training_paths(
    tmp_path: Path,
) -> None:
    operations = SyntheticOperations()
    supervisor = _supervisor(tmp_path, operations)
    with pytest.raises(ValueError, match="FINAL_FROZEN"):
        official_frozen_run_plan(
            final_state=supervisor.status(),
            official_task_ids=tuple(f"Official_{index:03d}" for index in range(40)),
        )
    supervisor.run_until_pause()
    plan = official_frozen_run_plan(
        final_state=supervisor.status(),
        official_task_ids=tuple(f"Official_{index:03d}" for index in range(40)),
    )
    assert plan["scoring_gate"] == "ALL_40_CANDIDATE_RUNS_SEALED"
    assert plan["evolution_enabled"] is False
    assert plan["reflector_enabled"] is False
    assert plan["training_feedback_attachment_enabled"] is False
    assert plan["teacher_enabled"] is False
    assert plan["task_local_overlay_enabled"] is False
