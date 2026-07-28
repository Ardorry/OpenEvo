from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from openevo_researchclawbench.config import FROZEN_TASKS
from openevo_researchclawbench.owned_resource_registry import OwnedResourceRegistry
from openevo_researchclawbench.training_state_store import TrainingStateStore
from openevo_researchclawbench.training_supervisor import (
    CommunityTrainingSupervisor,
    SupervisorIdentity,
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

    def ensure_candidate(self, request, idempotency_key):
        attempt = request["attempt_index"]
        return self._once(
            idempotency_key,
            {
                "session_id": f"session-{request['task_id']}-{attempt}",
                "dataset_id": f"dataset-{request['task_id']}-{attempt}",
                "completed": True,
                "run_id": f"run-{request['task_id']}-{attempt}",
                "runtime_seconds": 10 + attempt,
                "cost_total_usd": None,
                "composite_size_bytes": 100 + attempt,
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
        attempt = int(request["candidate"]["run_id"].rsplit("-", 1)[1])
        return self._once(
            idempotency_key,
            {"total_score": (0.2, 0.5, 0.4)[attempt]},
        )

    def attach_feedback(self, request, idempotency_key):
        attempt = request["attempt_index"]
        return self._once(
            idempotency_key,
            {
                "attachment_id": f"attachment-{request['task_id']}-{attempt}",
                "resolved_view_sha256": f"{attempt + 4}" * 64,
                "task_local_overlay_id": f"overlay-{request['task_id']}",
            },
        )

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


def test_completed_side_effect_before_transition_is_not_repeated(tmp_path: Path) -> None:
    operations = SyntheticOperations()
    supervisor = _supervisor(tmp_path, operations)
    supervisor.run_next()  # attempt ready
    supervisor.run_next()  # candidate running and side effect planned
    state = supervisor.status()
    request = {
        "task_id": "Life_005",
        "attempt_index": 0,
        "input_composite_id": "c000",
        "task_local_overlay_id": None,
        "fresh_workspace": True,
        "resume_in_place": False,
    }
    key = "synthetic-training:Life_005:a0:candidate"
    receipt = operations.ensure_candidate(request, key)
    supervisor.store.complete_side_effect(idempotency_key=key, receipt=receipt)
    restarted = _supervisor(tmp_path, operations)
    restarted.run_next()
    assert restarted.status()["stage"] == "CANDIDATE_SEALED"
    assert operations.calls[key] == 1
    assert state["stage"] == "CANDIDATE_RUNNING"


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
        "session_id": state["active_candidate_receipt"]["session_id"],
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
