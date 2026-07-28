"""Durable, resumable owner for the fixed Community training protocol.

The supervisor owns sequencing and receipts only.  Candidate, evaluator and
evolution side effects are delegated to typed operations backed by OpenEvo
Core.  Every call is fenced by a stable idempotency key before it can run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .config import ARTIFACT_TYPES, FROZEN_TASKS
from .training_state_store import TrainingStateStore, canonical_sha256
from .transition_engine import TrainingStage, evolution_allowed


class TrainingPaused(RuntimeError):
    """External gate is absent; durable state remains resumable in place."""


class TrainingOperations(Protocol):
    def ensure_candidate(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...

    def validate_artifact(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...

    def evaluate(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...

    def attach_feedback(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...

    def evolve_artifacts(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...

    def admit_composite(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...

    def destroy_task_local(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...

    def sanitize_composite(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...

    def freeze_final(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class SupervisorIdentity:
    protocol_sha256: str
    core_identity_sha256: str
    adapter_identity_sha256: str


class CommunityTrainingSupervisor:
    def __init__(
        self,
        *,
        store: TrainingStateStore,
        experiment_id: str,
        identity: SupervisorIdentity,
        operations: TrainingOperations,
        task_ids: tuple[str, ...] = FROZEN_TASKS,
    ) -> None:
        if not task_ids or any(task not in FROZEN_TASKS for task in task_ids):
            raise ValueError("supervisor task scope is outside the frozen Community inventory")
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("supervisor task scope contains duplicates")
        self.store = store
        self.experiment_id = experiment_id
        self.identity = identity
        self.operations = operations
        self.task_ids = task_ids

    def initialize(self) -> dict[str, Any]:
        self.store.initialize_experiment(
            experiment_id=self.experiment_id,
            protocol_sha256=self.identity.protocol_sha256,
            core_identity_sha256=self.identity.core_identity_sha256,
            adapter_identity_sha256=self.identity.adapter_identity_sha256,
            initial_state={
                "schema_version": "openevo.researchclawbench.training_state.v1",
                "task_ids": list(self.task_ids),
                "current_task_index": 0,
                "current_attempt": 0,
                "current_composite_id": "c000",
                "current_task_local_overlay_id": None,
                "session_ids": [],
                "dataset_ids": [],
                "attachment_ids": [],
                "evolution_job_ids": [],
                "registry_artifact_ids": [],
                "composite_ids": ["c000"],
                "task_best": {},
                "retry_count": 0,
                "failure_reason": None,
                "final_freeze_receipt": None,
            },
        )
        return self.status()

    def status(self) -> dict[str, Any]:
        state = self.store.load(self.experiment_id)
        self.store.verify_identity(
            state,
            protocol_sha256=self.identity.protocol_sha256,
            core_identity_sha256=self.identity.core_identity_sha256,
            adapter_identity_sha256=self.identity.adapter_identity_sha256,
        )
        return state

    def verify(self) -> dict[str, Any]:
        state = self.status()
        attempts = sum(
            len(self.store.attempts_for_task(self.experiment_id, task))
            for task in self.task_ids
        )
        resources = self.store.active_resources(self.experiment_id)
        if state["current_task_index"] >= len(self.task_ids) and state["stage"] not in {
            TrainingStage.COMMUNITY_TRAINING_COMPLETE.value,
            TrainingStage.FINAL_FREEZE_PENDING.value,
            TrainingStage.FINAL_FROZEN.value,
        }:
            raise ValueError("training state task cursor is invalid")
        return {
            "status": "PASS",
            "stage": state["stage"],
            "state_sha256": state["_state_sha256"],
            "state_revision": state["_revision"],
            "attempt_receipts": attempts,
            "active_owned_resources": resources,
            "identity_verified": True,
        }

    def resume(self) -> dict[str, Any]:
        # All uncertain side effects are recovered through ensure_* calls using
        # their original idempotency key.  No log inference is used.
        return self.run_next()

    def _transition(
        self,
        source: TrainingStage,
        target: TrainingStage,
        suffix: str,
        *,
        updates: dict[str, Any] | None = None,
        receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self.status()
        task = self.task_ids[state["current_task_index"]] if state["current_task_index"] < len(self.task_ids) else "complete"
        key = f"{self.experiment_id}:{task}:a{state['current_attempt']}:{suffix}"
        return self.store.transition(
            experiment_id=self.experiment_id,
            idempotency_key=key,
            source=source,
            target=target,
            updates=updates or {},
            receipt=receipt or {"task_id": task, "attempt_index": state["current_attempt"]},
        )

    def _effect(
        self,
        *,
        kind: str,
        request: dict[str, Any],
        execute,
    ) -> dict[str, Any]:
        state = self.status()
        task = (
            self.task_ids[state["current_task_index"]]
            if state["current_task_index"] < len(self.task_ids)
            else "complete"
        )
        key = f"{self.experiment_id}:{task}:a{state['current_attempt']}:{kind}"
        observed = self.store.plan_side_effect(
            experiment_id=self.experiment_id,
            idempotency_key=key,
            kind=kind,
            request=request,
        )
        if observed["status"] == "completed":
            return observed["receipt"]
        receipt = execute(request, key)
        if not isinstance(receipt, dict):
            raise ValueError(f"{kind} did not return a closed receipt")
        return self.store.complete_side_effect(idempotency_key=key, receipt=receipt)

    def run_next(self) -> dict[str, Any]:
        state = self.status()
        stage = TrainingStage(state["stage"])
        task = self.task_ids[state["current_task_index"]] if state["current_task_index"] < len(self.task_ids) else None
        attempt = state["current_attempt"]
        if stage in {TrainingStage.BLOCKED, TrainingStage.FAILED, TrainingStage.FINAL_FROZEN}:
            raise TrainingPaused(f"supervisor is terminal at {stage.value}")
        if stage is TrainingStage.INITIALIZED:
            return self._transition(stage, TrainingStage.TASK_ATTEMPT_READY, "initialize")
        if stage is TrainingStage.TASK_ATTEMPT_READY:
            request = {
                "task_id": task,
                "attempt_index": attempt,
                "input_composite_id": state["current_composite_id"],
                "task_local_overlay_id": state["current_task_local_overlay_id"],
                "fresh_workspace": True,
                "resume_in_place": False,
            }
            self.store.plan_side_effect(
                experiment_id=self.experiment_id,
                idempotency_key=f"{self.experiment_id}:{task}:a{attempt}:candidate",
                kind="candidate",
                request=request,
            )
            return self._transition(stage, TrainingStage.CANDIDATE_RUNNING, "candidate-start")
        if stage is TrainingStage.CANDIDATE_RUNNING:
            request = {
                "task_id": task,
                "attempt_index": attempt,
                "input_composite_id": state["current_composite_id"],
                "task_local_overlay_id": state["current_task_local_overlay_id"],
                "fresh_workspace": True,
                "resume_in_place": False,
            }
            candidate = self._effect(
                kind="candidate",
                request=request,
                execute=self.operations.ensure_candidate,
            )
            required = {"session_id", "dataset_id", "completed", "run_id", "runtime_seconds"}
            if not required.issubset(candidate):
                raise ValueError("candidate receipt is incomplete")
            return self._transition(
                stage,
                TrainingStage.CANDIDATE_SEALED,
                "candidate-sealed",
                updates={
                    "session_ids": [*state["session_ids"], candidate["session_id"]],
                    "dataset_ids": [*state["dataset_ids"], candidate["dataset_id"]],
                    "active_candidate_receipt": candidate,
                },
                receipt=candidate,
            )
        if stage is TrainingStage.CANDIDATE_SEALED:
            validation = self._effect(
                kind="validator",
                request={"candidate": state["active_candidate_receipt"]},
                execute=self.operations.validate_artifact,
            )
            if validation.get("artifact_valid") is not True:
                return self._transition(
                    stage,
                    TrainingStage.FAILED,
                    "validator-failed",
                    updates={"failure_reason": "ARTIFACT_VALIDATOR_FAILED"},
                    receipt=validation,
                )
            return self._transition(
                stage,
                TrainingStage.ARTIFACT_VALIDATED,
                "artifact-validated",
                updates={"active_validation_receipt": validation},
                receipt=validation,
            )
        if stage is TrainingStage.ARTIFACT_VALIDATED:
            return self._transition(stage, TrainingStage.EVALUATION_PENDING, "evaluation-pending")
        if stage is TrainingStage.EVALUATION_PENDING:
            evaluation = self._effect(
                kind="evaluation",
                request={
                    "candidate": state["active_candidate_receipt"],
                    "validation": state["active_validation_receipt"],
                    "evaluator_only": True,
                },
                execute=self.operations.evaluate,
            )
            if type(evaluation.get("total_score")) not in {int, float}:
                raise TrainingPaused("community score is pending trusted Judge credentials")
            attempt_receipt = {
                "task_id": task,
                "attempt_index": attempt,
                "input_composite_id": state["current_composite_id"],
                "session_id": state["active_candidate_receipt"]["session_id"],
                "dataset_id": state["active_candidate_receipt"]["dataset_id"],
                "completed": state["active_candidate_receipt"]["completed"],
                "artifact_valid": state["active_validation_receipt"]["artifact_valid"],
                "validator_completeness": state["active_validation_receipt"].get("completeness", 0),
                "score": float(evaluation["total_score"]),
                "runtime_seconds": state["active_candidate_receipt"]["runtime_seconds"],
                "cost_total_usd": state["active_candidate_receipt"].get("cost_total_usd"),
                "composite_size_bytes": state["active_candidate_receipt"].get("composite_size_bytes", 0),
                "output_artifact_hash": state["active_validation_receipt"].get("artifact_root_sha256"),
            }
            self.store.record_attempt(self.experiment_id, attempt_receipt)
            return self._transition(
                stage,
                TrainingStage.EVALUATED,
                "evaluated",
                updates={"active_evaluation_receipt": evaluation},
                receipt=evaluation,
            )
        if stage is TrainingStage.EVALUATED:
            if attempt == 2:
                return self._transition(
                    stage, TrainingStage.TASK_SELECTION_PENDING, "selection-pending"
                )
            attachment = self._effect(
                kind="feedback-attachment",
                request={
                    "task_id": task,
                    "attempt_index": attempt,
                    "dataset_id": state["active_candidate_receipt"]["dataset_id"],
                    "session_id": state["active_candidate_receipt"]["session_id"],
                    "evaluation": state["active_evaluation_receipt"],
                    "authority": "evaluator_only",
                },
                execute=self.operations.attach_feedback,
            )
            if not attachment.get("attachment_id") or not attachment.get("resolved_view_sha256"):
                raise ValueError("feedback attachment receipt is incomplete")
            return self._transition(
                stage,
                TrainingStage.ATTACHMENT_SEALED,
                "attachment-sealed",
                updates={
                    "attachment_ids": [*state["attachment_ids"], attachment["attachment_id"]],
                    "active_attachment_receipt": attachment,
                    "current_task_local_overlay_id": attachment.get("task_local_overlay_id"),
                },
                receipt=attachment,
            )
        if stage is TrainingStage.ATTACHMENT_SEALED:
            return self._transition(stage, TrainingStage.EVOLUTION_PENDING, "evolution-pending")
        if stage is TrainingStage.EVOLUTION_PENDING:
            if not evolution_allowed(stage):
                raise ValueError("evolution is disabled after final freeze")
            self.store.plan_side_effect(
                experiment_id=self.experiment_id,
                idempotency_key=f"{self.experiment_id}:{task}:a{attempt}:evolution",
                kind="evolution",
                request={
                    "task_id": task,
                    "attempt_index": attempt,
                    "attachment": state["active_attachment_receipt"],
                    "parent_composite_id": state["current_composite_id"],
                    "artifact_types": list(ARTIFACT_TYPES),
                },
            )
            return self._transition(stage, TrainingStage.EVOLUTION_RUNNING, "evolution-start")
        if stage is TrainingStage.EVOLUTION_RUNNING:
            request = {
                "task_id": task,
                "attempt_index": attempt,
                "attachment": state["active_attachment_receipt"],
                "parent_composite_id": state["current_composite_id"],
                "artifact_types": list(ARTIFACT_TYPES),
            }
            evolved = self._effect(
                kind="evolution",
                request=request,
                execute=self.operations.evolve_artifacts,
            )
            jobs = evolved.get("jobs")
            if (
                not isinstance(jobs, list)
                or len(jobs) != 3
                or {item.get("artifact_type") for item in jobs if isinstance(item, dict)}
                != set(ARTIFACT_TYPES)
            ):
                raise ValueError("evolution receipt does not contain three native jobs")
            return self._transition(
                stage,
                TrainingStage.EVOLUTION_COMPLETED,
                "evolution-completed",
                updates={
                    "evolution_job_ids": [
                        *state["evolution_job_ids"],
                        *[item["job_id"] for item in jobs],
                    ],
                    "active_evolution_receipt": evolved,
                },
                receipt=evolved,
            )
        if stage is TrainingStage.EVOLUTION_COMPLETED:
            admitted = self._effect(
                kind="composite-admission",
                request={
                    "task_id": task,
                    "attempt_index": attempt,
                    "parent_composite_id": state["current_composite_id"],
                    "evolution": state["active_evolution_receipt"],
                },
                execute=self.operations.admit_composite,
            )
            if not admitted.get("composite_id") or len(admitted.get("registry_artifact_ids", [])) != 3:
                raise ValueError("admitted composite receipt is incomplete")
            return self._transition(
                stage,
                TrainingStage.COMPOSITE_ADMITTED,
                "composite-admitted",
                updates={
                    "registry_artifact_ids": [
                        *state["registry_artifact_ids"],
                        *admitted["registry_artifact_ids"],
                    ],
                    "composite_ids": [*state["composite_ids"], admitted["composite_id"]],
                    "active_admission_receipt": admitted,
                },
                receipt=admitted,
            )
        if stage is TrainingStage.COMPOSITE_ADMITTED:
            return self._transition(
                stage,
                TrainingStage.NEXT_ATTEMPT_READY,
                "next-attempt-prepared",
                updates={"current_composite_id": state["active_admission_receipt"]["composite_id"]},
            )
        if stage is TrainingStage.NEXT_ATTEMPT_READY:
            return self._transition(
                stage,
                TrainingStage.TASK_ATTEMPT_READY,
                "next-attempt",
                updates={"current_attempt": attempt + 1},
            )
        if stage is TrainingStage.TASK_SELECTION_PENDING:
            records = self.store.attempts_for_task(self.experiment_id, task)
            best = _select_best(records)
            return self._transition(
                stage,
                TrainingStage.TASK_BEST_SELECTED,
                "task-best",
                updates={
                    "current_composite_id": best["input_composite_id"],
                    "task_best": {**state["task_best"], task: best},
                },
                receipt=best,
            )
        if stage is TrainingStage.TASK_BEST_SELECTED:
            destroyed = self._effect(
                kind="task-local-destroy",
                request={"task_id": task, "overlay_id": state["current_task_local_overlay_id"]},
                execute=self.operations.destroy_task_local,
            )
            if destroyed.get("destroyed") is not True:
                raise ValueError("task-local overlay destruction is unproven")
            return self._transition(
                stage,
                TrainingStage.TASK_LOCAL_DESTROYED,
                "task-local-destroyed",
                updates={"current_task_local_overlay_id": None},
                receipt=destroyed,
            )
        if stage is TrainingStage.TASK_LOCAL_DESTROYED:
            sanitized = self._effect(
                kind="cross-task-sanitize",
                request={"task_id": task, "composite_id": state["current_composite_id"]},
                execute=self.operations.sanitize_composite,
            )
            if sanitized.get("passed") is not True:
                raise ValueError("cross-task sanitizer failed closed")
            return self._transition(
                stage,
                TrainingStage.CROSS_TASK_SANITIZED,
                "cross-task-sanitized",
                updates={"current_composite_id": sanitized["composite_id"]},
                receipt=sanitized,
            )
        if stage is TrainingStage.CROSS_TASK_SANITIZED:
            if state["current_task_index"] + 1 == len(self.task_ids):
                return self._transition(
                    stage,
                    TrainingStage.COMMUNITY_TRAINING_COMPLETE,
                    "community-complete",
                    updates={"current_task_index": len(self.task_ids)},
                )
            return self._transition(
                stage,
                TrainingStage.NEXT_TASK_READY,
                "next-task-prepared",
                updates={
                    "current_task_index": state["current_task_index"] + 1,
                    "current_attempt": 0,
                },
            )
        if stage is TrainingStage.NEXT_TASK_READY:
            return self._transition(stage, TrainingStage.TASK_ATTEMPT_READY, "next-task")
        if stage is TrainingStage.COMMUNITY_TRAINING_COMPLETE:
            return self._transition(stage, TrainingStage.FINAL_FREEZE_PENDING, "freeze-pending")
        if stage is TrainingStage.FINAL_FREEZE_PENDING:
            expected_attempts = len(self.task_ids) * 3
            expected_cycles = len(self.task_ids) * 2
            request = {
                "task_ids": list(self.task_ids),
                "expected_candidate_attempts": expected_attempts,
                "expected_reflector_cycles": expected_cycles,
                "expected_evolution_jobs": expected_cycles * 3,
                "selected_composite_id": state["current_composite_id"],
                "task_best": state["task_best"],
                "active_task_local_overlay": state["current_task_local_overlay_id"],
            }
            if (
                sum(len(self.store.attempts_for_task(self.experiment_id, item)) for item in self.task_ids)
                != expected_attempts
                or len(state["evolution_job_ids"]) != expected_cycles * 3
                or state["current_task_local_overlay_id"] is not None
            ):
                raise ValueError("final freeze inventory is incomplete")
            frozen = self._effect(
                kind="final-freeze",
                request=request,
                execute=self.operations.freeze_final,
            )
            required = {"agent_system", "text_memory", "skill_bundle", "composite_sha256"}
            if not required.issubset(frozen):
                raise ValueError("final freeze receipt is incomplete")
            return self._transition(
                stage,
                TrainingStage.FINAL_FROZEN,
                "final-frozen",
                updates={"final_freeze_receipt": frozen},
                receipt=frozen,
            )
        raise ValueError(f"unhandled training stage: {stage.value}")

    def run_until_pause(self, *, max_transitions: int = 10_000) -> dict[str, Any]:
        for _ in range(max_transitions):
            state = self.status()
            if state["stage"] == TrainingStage.FINAL_FROZEN.value:
                return state
            self.run_next()
        raise ValueError("training transition budget was exhausted")


def _select_best(records: list[dict[str, Any]]) -> dict[str, Any]:
    if len(records) != 3:
        raise ValueError("best-of-three requires exactly three sealed attempts")
    candidates = [
        item
        for item in records
        if type(item.get("score")) in {int, float}
        and item.get("artifact_valid") is True
        and item.get("completed") is True
    ]
    if not candidates:
        raise ValueError("best composite cannot be determined")
    return max(
        candidates,
        key=lambda item: (
            float(item["score"]),
            int(item.get("validator_completeness", 0)),
            -float(item.get("runtime_seconds", float("inf"))),
            -float(item.get("cost_total_usd") or 0.0),
            -int(item.get("composite_size_bytes", 0)),
            -int(item["attempt_index"]),
        ),
    )


def supervisor_capability_audit(package_root: str | Path) -> dict[str, Any]:
    root = Path(package_root)
    required = {
        "training_supervisor.py",
        "training_state_store.py",
        "transition_engine.py",
        "owned_resource_registry.py",
    }
    present = {item.name for item in root.iterdir() if item.is_file()}
    components_present = required.issubset(present)
    production_driver = root / "production_training_operations.py"
    production_operations_bound = production_driver.is_file()
    return {
        # Formal availability includes the real Core/evaluator driver.  A
        # durable state machine by itself must not make static readiness pass.
        "available": components_present and production_operations_bound,
        "durable_state_machine_ready": components_present,
        "durable_sqlite_state": "training-supervisor.sqlite3"
        in (root / "training_state_store.py").read_text(),
        "idempotent_side_effects": "plan_side_effect"
        in (root / "training_state_store.py").read_text(),
        "owned_resource_registry": "OwnedResourceRegistry"
        in (root / "owned_resource_registry.py").read_text(),
        "production_operations_bound": production_operations_bound,
        "formal_command_available": production_operations_bound,
        "blocker": (
            None
            if production_operations_bound
            else "PRODUCTION_TRAINING_OPERATIONS_DRIVER_MISSING"
        ),
    }


def official_frozen_run_plan(
    *,
    final_state: dict[str, Any],
    official_task_ids: tuple[str, ...],
) -> dict[str, Any]:
    if final_state.get("stage") != TrainingStage.FINAL_FROZEN.value:
        raise ValueError("official frozen run requires a FINAL_FROZEN receipt")
    freeze = final_state.get("final_freeze_receipt")
    if not isinstance(freeze, dict) or not freeze.get("composite_sha256"):
        raise ValueError("official frozen run lacks a final composite authority")
    if len(official_task_ids) != 40 or len(set(official_task_ids)) != 40:
        raise ValueError("official frozen mode requires exactly 40 distinct tasks")
    return {
        "mode": "official_frozen_run",
        "task_ids": list(official_task_ids),
        "frozen_composite_sha256": freeze["composite_sha256"],
        "evolution_enabled": False,
        "reflector_enabled": False,
        "training_feedback_attachment_enabled": False,
        "teacher_enabled": False,
        "task_local_overlay_enabled": False,
        "cross_task_state_updates": False,
        "feedback_released": False,
        "scoring_gate": "ALL_40_CANDIDATE_RUNS_SEALED",
    }


__all__ = [
    "CommunityTrainingSupervisor",
    "SupervisorIdentity",
    "TrainingOperations",
    "TrainingPaused",
    "official_frozen_run_plan",
    "supervisor_capability_audit",
]
