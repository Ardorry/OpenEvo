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


class CandidateAuthorityUnavailable(RuntimeError):
    """The planned candidate cannot authenticate its exact Core generation."""

    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "candidate_authority_unavailable",
        terminal_proven: bool = False,
        failure_receipt: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.terminal_proven = terminal_proven
        self.failure_receipt = failure_receipt


class TrainingOperations(Protocol):
    def reconcile_candidate_authority(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]: ...

    def candidate_readiness(self, request: dict[str, Any]) -> dict[str, Any]: ...

    def evolution_readiness(self, request: dict[str, Any]) -> dict[str, Any]: ...

    def ensure_candidate(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]: ...

    def validate_artifact(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]: ...

    def evaluate(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...

    def project_feedback(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]: ...

    def current_task_gt_supervision(self, task_id: str) -> dict[str, Any]: ...

    def attach_feedback(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...

    def evolve_artifacts(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]: ...

    def admit_composite(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...

    def prepare_evolved_workspace(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]: ...

    def destroy_task_local(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]: ...

    def sanitize_composite(
        self, request: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]: ...

    def freeze_final(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class SupervisorIdentity:
    protocol_sha256: str
    core_identity_sha256: str
    adapter_identity_sha256: str


@dataclass(frozen=True)
class TrainingBudgetPolicy:
    max_candidate_model_calls: int
    max_reflector_model_calls: int
    max_judge_operations: int
    cumulative_runtime_seconds: int
    reflector_calls_per_cycle: int = 5

    def __post_init__(self) -> None:
        for value in (
            self.max_candidate_model_calls,
            self.max_reflector_model_calls,
            self.max_judge_operations,
            self.cumulative_runtime_seconds,
            self.reflector_calls_per_cycle,
        ):
            if type(value) is not int or value <= 0:
                raise ValueError("training budgets must be positive integers")


class CommunityTrainingSupervisor:
    def __init__(
        self,
        *,
        store: TrainingStateStore,
        experiment_id: str,
        identity: SupervisorIdentity,
        operations: TrainingOperations,
        task_ids: tuple[str, ...] = FROZEN_TASKS,
        validator_failure_policy: dict[str, Any] | None = None,
        budget_policy: TrainingBudgetPolicy | None = None,
        current_task_gt_supervision: bool = False,
        per_item_reset: bool = False,
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
        self.validator_failure_policy = validator_failure_policy
        if (current_task_gt_supervision or per_item_reset) and len(task_ids) != 1:
            raise ValueError("current-task GT supervision requires exactly one task")
        if per_item_reset and not current_task_gt_supervision:
            raise ValueError("per-item reset requires current-task GT supervision")
        self.current_task_gt_supervision_enabled = current_task_gt_supervision
        self.per_item_reset_enabled = per_item_reset
        self.budget_policy = budget_policy or TrainingBudgetPolicy(
            max_candidate_model_calls=len(task_ids) * 3,
            max_reflector_model_calls=len(task_ids) * 2 * 5,
            max_judge_operations=len(task_ids) * 3,
            cumulative_runtime_seconds=345_600,
        )

    @property
    def validator_failure_learning_enabled(self) -> bool:
        return bool(
            isinstance(self.validator_failure_policy, dict)
            and self.validator_failure_policy.get("enabled") is True
        )

    def _initial_state(self) -> dict[str, Any]:
        return {
            "schema_version": "openevo.researchclawbench.training_state.v1",
            "namespace_type": (
                "per_item_reset" if self.per_item_reset_enabled else "standard_training"
            ),
            "supervision_mode": (
                "sanitized_evaluation_feedback_v1"
                if self.per_item_reset_enabled
                else "current_task_gt"
                if self.current_task_gt_supervision_enabled
                else "community_evaluator"
            ),
            "task_ids": list(self.task_ids),
            "current_task_index": 0,
            "current_attempt": 0,
            "current_composite_id": "c000",
            "core_project_id": None,
            "preseeded_workspace_authority": None,
            "current_task_local_overlay_id": None,
            "current_task_local_overlay_scope_id": None,
            "session_ids": [],
            "dataset_ids": [],
            "attachment_ids": [],
            "evolution_job_ids": [],
            "registry_artifact_ids": [],
            "composite_ids": ["c000"],
            "task_best": {},
            "retry_count": 0,
            "budget_policy": {
                "max_candidate_model_calls": self.budget_policy.max_candidate_model_calls,
                "max_reflector_model_calls": self.budget_policy.max_reflector_model_calls,
                "max_judge_operations": self.budget_policy.max_judge_operations,
                "cumulative_runtime_seconds": self.budget_policy.cumulative_runtime_seconds,
                "reflector_calls_per_cycle": self.budget_policy.reflector_calls_per_cycle,
            },
            "failure_reason": None,
            "final_freeze_receipt": None,
            "active_artifact_ids": [],
            "archived_artifact_ids": [],
            "paired_result": None,
            "item_reset_receipt": None,
            "active_feedback_projection_receipt": None,
        }

    def initialize(self) -> dict[str, Any]:
        self.store.initialize_experiment(
            experiment_id=self.experiment_id,
            protocol_sha256=self.identity.protocol_sha256,
            core_identity_sha256=self.identity.core_identity_sha256,
            adapter_identity_sha256=self.identity.adapter_identity_sha256,
            initial_state=self._initial_state(),
        )
        return self.status()

    def initialize_validator_failure_learning(
        self,
        *,
        source_reference: dict[str, Any],
        candidate: dict[str, Any],
        validation: dict[str, Any],
        source_receipt_sha256: str,
    ) -> dict[str, Any]:
        """Start a Community-only successor from a consumed invalid attempt.

        The candidate and validator authorities are immutable references.  No
        candidate operation is planned or executed for attempt zero.
        """

        if not self.validator_failure_learning_enabled or len(self.task_ids) != 1:
            raise ValueError("validator-failure learning requires one Community task")
        task_id = self.task_ids[0]
        required_candidate = {
            "session_id",
            "dataset_id",
            "dataset_revision",
            "completed",
            "run_id",
            "runtime_seconds",
            "core_project_id",
            "core_task_id",
            "core_attempt_id",
            "successor_transition_id",
        }
        if (
            not required_candidate.issubset(candidate)
            or candidate.get("completed") is not True
            or validation.get("artifact_valid") is not False
            or source_reference.get("task_id") != task_id
            or source_reference.get("attempt_index") != 0
            or source_reference.get("candidate_reexecuted") is not False
            or source_reference.get("additional_candidate_model_calls") != 0
        ):
            raise ValueError("validator-learning source authority is incomplete")
        initial = self._initial_state()
        initial.update(
            {
                "namespace_type": "community_validator_failure_learning",
                "validator_failure_learning_source": source_reference,
                "candidate_origin": "referenced_sealed_core_authority",
                "candidate_reexecuted": False,
                "additional_candidate_model_calls": 0,
            }
        )
        self.store.initialize_experiment(
            experiment_id=self.experiment_id,
            protocol_sha256=self.identity.protocol_sha256,
            core_identity_sha256=self.identity.core_identity_sha256,
            adapter_identity_sha256=self.identity.adapter_identity_sha256,
            initial_state=initial,
        )
        state = self.status()
        if state["stage"] == TrainingStage.VALIDATOR_FEEDBACK_PENDING.value:
            return state
        if state["stage"] != TrainingStage.INITIALIZED.value:
            raise ValueError("validator-learning namespace initialization conflicts")
        self.store.record_attempt(
            self.experiment_id,
            self._invalid_attempt_receipt(
                task_id=task_id,
                attempt_index=0,
                candidate=candidate,
                validation=validation,
                input_composite_id="c000",
            ),
        )
        return self._transition(
            TrainingStage.INITIALIZED,
            TrainingStage.VALIDATOR_FEEDBACK_PENDING,
            "validator-learning-source-admitted",
            updates={
                "session_ids": [candidate["session_id"]],
                "dataset_ids": [candidate["dataset_id"]],
                "active_candidate_receipt": candidate,
                "active_validation_receipt": validation,
                "core_project_id": candidate["core_project_id"],
                "attempt_status": "CONSUMED_INVALID_ARTIFACT",
                "candidate_status": "SEALED",
                "validator_status": "FAILED",
                "evaluator_status": "SKIPPED",
                "score_status": "ABSENT",
                "training_signal_status": "READY_FROM_VALIDATOR",
                "source_reference_receipt_sha256": source_receipt_sha256,
            },
            receipt={
                "operation": "community_validator_failure_source_reference",
                "source": source_reference,
                "candidate_reexecuted": False,
                "additional_candidate_model_calls": 0,
                "judge_calls": 0,
                "validator_receipt": validation,
                "source_reference_receipt_sha256": source_receipt_sha256,
            },
        )

    def initialize_successor_recovery_continuation(
        self,
        *,
        source_reference: dict[str, Any],
        candidate: dict[str, Any],
        validation: dict[str, Any],
        attachment: dict[str, Any],
        evolution: dict[str, Any],
        admission: dict[str, Any],
    ) -> dict[str, Any]:
        """Start a fresh full-plan namespace after an append-only recovery.

        Attempt zero remains an immutable invalid Candidate from the source
        namespace.  The exact completed recovery triple and its atomic Core
        project seed become this namespace's first evolution cycle; no source
        operation is replayed and no Candidate is executed here.
        """

        if not self.validator_failure_learning_enabled or len(self.task_ids) != 17:
            raise ValueError("recovery continuation requires the frozen Community plan")
        task_id = self.task_ids[0]
        jobs = evolution.get("jobs")
        required_candidate = {
            "session_id",
            "dataset_id",
            "dataset_revision",
            "completed",
            "run_id",
            "runtime_seconds",
            "core_project_id",
            "core_task_id",
            "core_attempt_id",
            "successor_transition_id",
        }
        required_source = {
            "source_namespace",
            "source_state_sha256",
            "source_transition_id",
            "source_transition_attempt_id",
            "recovery_id",
            "recovery_record_sha256",
            "recovery_seed_request_id",
            "recovery_seed_sha256",
            "candidate_reexecuted",
            "additional_candidate_model_calls",
            "preseeded_workspace_authority",
        }
        preseeded = source_reference.get("preseeded_workspace_authority")
        if (
            not required_candidate.issubset(candidate)
            or candidate.get("completed") is not True
            or validation.get("artifact_valid") is not False
            or not required_source.issubset(source_reference)
            or source_reference.get("task_id") != task_id
            or source_reference.get("attempt_index") != 0
            or source_reference.get("candidate_reexecuted") is not False
            or source_reference.get("additional_candidate_model_calls") != 0
            or not isinstance(preseeded, dict)
            or preseeded.get("project_id") != source_reference.get("destination_project_id")
            or preseeded.get("seed_request_id") != source_reference.get("recovery_seed_request_id")
            or preseeded.get("seed_sha256") != source_reference.get("recovery_seed_sha256")
            or attachment.get("attachment_id") is None
            or attachment.get("resolved_view_sha256") is None
            or not isinstance(jobs, list)
            or len(jobs) != 3
            or {item.get("artifact_type") for item in jobs if isinstance(item, dict)}
            != set(ARTIFACT_TYPES)
            or admission.get("core_project_id") != source_reference.get("destination_project_id")
            or admission.get("composite_id") is None
            or len(admission.get("registry_artifact_ids", [])) != 3
        ):
            raise ValueError("recovery continuation authority is incomplete")
        initial = self._initial_state()
        initial.update(
            {
                "namespace_type": "append_only_successor_recovery_continuation",
                "successor_recovery_source": source_reference,
                "candidate_origin": "referenced_sealed_source_authority",
                "candidate_reexecuted": False,
                "additional_candidate_model_calls": 0,
            }
        )
        self.store.initialize_experiment(
            experiment_id=self.experiment_id,
            protocol_sha256=self.identity.protocol_sha256,
            core_identity_sha256=self.identity.core_identity_sha256,
            adapter_identity_sha256=self.identity.adapter_identity_sha256,
            initial_state=initial,
        )
        state = self.status()
        if state["stage"] == TrainingStage.RECOVERY_SEEDED_CONTINUATION.value:
            return state
        if state["stage"] != TrainingStage.INITIALIZED.value:
            raise ValueError("recovery continuation initialization conflicts")
        self.store.record_attempt(
            self.experiment_id,
            self._invalid_attempt_receipt(
                task_id=task_id,
                attempt_index=0,
                candidate=candidate,
                validation=validation,
                input_composite_id="c000",
            ),
        )
        effect_key = f"{self.experiment_id}:{task_id}:a0:evolution"
        effect_request = {
            "source": "append_only_successor_recovery",
            "source_namespace": source_reference["source_namespace"],
            "source_transition_id": source_reference["source_transition_id"],
            "recovery_id": source_reference["recovery_id"],
            "parent_composite_id": "c000",
            "artifact_types": list(ARTIFACT_TYPES),
        }
        effect = self.store.plan_side_effect(
            experiment_id=self.experiment_id,
            idempotency_key=effect_key,
            kind="evolution",
            request=effect_request,
        )
        if effect["status"] == "planned":
            completed_receipt = self.store.complete_side_effect(
                idempotency_key=effect_key,
                receipt=evolution,
            )
            effect = {"status": "completed", "receipt": completed_receipt}
        if effect["status"] != "completed" or effect.get("receipt") != evolution:
            raise ValueError("recovery continuation evolution ledger conflicts")
        self.store.reserve_budget(
            experiment_id=self.experiment_id,
            idempotency_key=f"{effect_key}:budget",
            category="reflector_model_calls",
            units=self.budget_policy.reflector_calls_per_cycle,
            limit_units=self.budget_policy.max_reflector_model_calls,
        )
        return self._transition(
            TrainingStage.INITIALIZED,
            TrainingStage.RECOVERY_SEEDED_CONTINUATION,
            "successor-recovery-continuation-admitted",
            updates={
                "session_ids": [candidate["session_id"]],
                "dataset_ids": [candidate["dataset_id"]],
                "attachment_ids": [attachment["attachment_id"]],
                "evolution_job_ids": [item["job_id"] for item in jobs],
                "registry_artifact_ids": list(admission["registry_artifact_ids"]),
                "composite_ids": ["c000", admission["composite_id"]],
                "active_candidate_receipt": candidate,
                "active_validation_receipt": validation,
                "active_attachment_receipt": attachment,
                "active_evolution_receipt": evolution,
                "active_admission_receipt": admission,
                "core_project_id": admission["core_project_id"],
                "preseeded_workspace_authority": preseeded,
                "current_task_local_overlay_id": attachment.get("task_local_overlay_id"),
                "current_task_local_overlay_scope_id": attachment.get(
                    "task_local_overlay_scope_id"
                ),
                "attempt_status": "CONSUMED_INVALID_ARTIFACT",
                "candidate_status": "SEALED_SOURCE_REFERENCED",
                "validator_status": "FAILED_SOURCE_REFERENCED",
                "evaluator_status": "SKIPPED",
                "score_status": "ABSENT",
            },
            receipt={
                "operation": "append_only_successor_recovery_continuation",
                "source": source_reference,
                "recovery_evolution": evolution,
                "recovery_admission": admission,
                "candidate_reexecuted": False,
                "additional_candidate_model_calls": 0,
            },
        )

    def initialize_completed_prefix_continuation(
        self,
        *,
        source_reference: dict[str, Any],
        source_state: dict[str, Any],
        attempts_by_task: dict[str, list[dict[str, Any]]],
        completed_side_effects: list[dict[str, Any]],
        source_budget_usage: dict[str, int],
        rebased_successor_workspace_authority: dict[str, Any],
    ) -> dict[str, Any]:
        """Admit a closed formal prefix without replaying any operation port.

        The caller must separately rebuild and verify the local composite
        journal and the rebased sanitized workspace before invoking this
        method. This method owns only the Supervisor ledger import.
        """

        required_source = {
            "source_namespace",
            "source_state_sha256",
            "source_database_sha256",
            "source_protocol_sha256",
            "source_core_identity_sha256",
            "source_adapter_identity_sha256",
            "source_reconciliation_receipt_sha256",
            "source_successor_transition_id",
            "source_successor_commit_sha256",
            "source_stage",
            "current_task_index",
            "current_attempt",
            "source_attempts_consumed",
            "source_reflector_cycles_consumed",
            "source_evolution_jobs_consumed",
            "source_candidate_model_calls",
            "source_judge_operations",
            "source_reflector_model_calls",
            "source_active_resources",
            "source_pending_side_effects",
            "source_failed_side_effects",
            "candidate_reexecuted",
            "model_execution_allowed",
        }
        if set(source_reference) != required_source:
            raise ValueError("completed-prefix source reference is incomplete")
        task_index = source_reference.get("current_task_index")
        attempt_index = source_reference.get("current_attempt")
        if type(task_index) is not int or type(attempt_index) is not int:
            raise TypeError("completed-prefix cursor must use integers")
        expected_attempts = task_index * 3 + attempt_index + 1
        expected_cycles = task_index * 2 + attempt_index + 1
        expected_budget = {
            "candidate_model_calls": expected_attempts,
            "reflector_model_calls": (
                expected_cycles * self.budget_policy.reflector_calls_per_cycle
            ),
            "judge_operations": expected_attempts,
        }
        source_candidate = source_state.get("active_candidate_receipt")
        source_workspace = (
            source_candidate.get("successor_workspace_authority")
            if isinstance(source_candidate, dict)
            else None
        )
        rebased_body = {
            key: value
            for key, value in rebased_successor_workspace_authority.items()
            if key != "content_sha256"
        }
        workspace_unchanged = (
            {
                key: value
                for key, value in source_workspace.items()
                if key not in {"workspace_root", "content_sha256"}
            }
            if isinstance(source_workspace, dict)
            else None
        )
        rebased_unchanged = {
            key: value
            for key, value in rebased_successor_workspace_authority.items()
            if key not in {"workspace_root", "content_sha256"}
        }
        flattened_attempts = [
            item for task in self.task_ids for item in attempts_by_task.get(task, [])
        ]
        effect_counts: dict[str, int] = {}
        for effect in completed_side_effects:
            kind = effect.get("kind") if isinstance(effect, dict) else None
            if not isinstance(kind, str):
                raise TypeError("completed-prefix side effect kind is invalid")
            effect_counts[kind] = effect_counts.get(kind, 0) + 1
        source_jobs = source_state.get("evolution_job_ids")
        expected_task_best = set(self.task_ids[:task_index])
        source_state_body = {
            key: value for key, value in source_state.items() if not key.startswith("_")
        }
        if (
            source_reference.get("source_stage") != "NEXT_ATTEMPT_READY"
            or source_reference.get("source_state_sha256") != source_state.get("_state_sha256")
            or source_reference.get("source_state_sha256") != canonical_sha256(source_state_body)
            or source_reference.get("source_protocol_sha256")
            != source_state.get("_protocol_sha256")
            or source_reference.get("source_core_identity_sha256")
            != source_state.get("_core_identity_sha256")
            or source_reference.get("source_adapter_identity_sha256")
            != source_state.get("_adapter_identity_sha256")
            or source_state.get("stage") != "NEXT_ATTEMPT_READY"
            or source_state.get("task_ids") != list(self.task_ids)
            or source_state.get("current_task_index") != task_index
            or source_state.get("current_attempt") != attempt_index
            or not 0 <= task_index < len(self.task_ids)
            or attempt_index not in {0, 1}
            or source_reference.get("source_attempts_consumed") != expected_attempts
            or source_reference.get("source_candidate_model_calls") != expected_attempts
            or source_reference.get("source_judge_operations") != expected_attempts
            or source_reference.get("source_reflector_cycles_consumed") != expected_cycles
            or source_reference.get("source_evolution_jobs_consumed") != expected_cycles * 3
            or source_reference.get("source_reflector_model_calls")
            != expected_budget["reflector_model_calls"]
            or source_reference.get("source_active_resources") != 0
            or source_reference.get("source_pending_side_effects") != 0
            or source_reference.get("source_failed_side_effects") != 0
            or source_reference.get("candidate_reexecuted") is not False
            or source_reference.get("model_execution_allowed") is not False
            or source_reference.get("source_namespace") == self.experiment_id
            or set(attempts_by_task) != set(self.task_ids)
            or len(flattened_attempts) != expected_attempts
            or effect_counts.get("evolution") != expected_cycles
            or any(effect.get("status") != "completed" for effect in completed_side_effects)
            or len({effect.get("idempotency_key") for effect in completed_side_effects})
            != len(completed_side_effects)
            or source_budget_usage != expected_budget
            or not isinstance(source_jobs, list)
            or len(source_jobs) != expected_cycles * 3
            or len(source_jobs) != len(set(source_jobs))
            or set(source_state.get("task_best", {})) != expected_task_best
            or not isinstance(source_workspace, dict)
            or source_workspace.get("successor_transition_id")
            != source_reference.get("source_successor_transition_id")
            or rebased_successor_workspace_authority.get("content_sha256")
            != canonical_sha256(rebased_body)
            or workspace_unchanged != rebased_unchanged
        ):
            raise ValueError("completed-prefix source authority is inconsistent")
        for task_position, task in enumerate(self.task_ids):
            records = attempts_by_task[task]
            expected_count = (
                3
                if task_position < task_index
                else (attempt_index + 1 if task_position == task_index else 0)
            )
            if (
                len(records) != expected_count
                or [item.get("attempt_index") for item in records] != list(range(expected_count))
                or any(item.get("task_id") != task for item in records)
            ):
                raise ValueError("completed-prefix attempt inventory is not contiguous")
        evolution_jobs = [
            job.get("job_id")
            for effect in completed_side_effects
            if effect.get("kind") == "evolution"
            for job in (
                effect.get("receipt", {}).get("jobs", [])
                if isinstance(effect.get("receipt"), dict)
                else []
            )
            if isinstance(job, dict)
        ]
        if len(evolution_jobs) != expected_cycles * 3 or set(evolution_jobs) != set(source_jobs):
            raise ValueError("completed-prefix evolution authority is incomplete")

        initial = self._initial_state()
        initial.update(
            {
                "namespace_type": "append_only_completed_prefix_continuation",
                "completed_prefix_source": source_reference,
                "completed_prefix_import_complete": False,
                "candidate_reexecuted": False,
                "additional_candidate_model_calls": 0,
            }
        )
        self.store.initialize_experiment(
            experiment_id=self.experiment_id,
            protocol_sha256=self.identity.protocol_sha256,
            core_identity_sha256=self.identity.core_identity_sha256,
            adapter_identity_sha256=self.identity.adapter_identity_sha256,
            initial_state=initial,
        )
        state = self.status()
        if state["stage"] == TrainingStage.NEXT_ATTEMPT_READY.value:
            if (
                state.get("completed_prefix_import_complete") is not True
                or state.get("completed_prefix_source") != source_reference
            ):
                raise ValueError("completed-prefix continuation replay conflicts")
            return state
        if (
            state["stage"] != TrainingStage.INITIALIZED.value
            or state.get("namespace_type") != "append_only_completed_prefix_continuation"
            or state.get("completed_prefix_source") != source_reference
        ):
            raise ValueError("completed-prefix continuation initialization conflicts")

        for task in self.task_ids:
            for source_attempt in attempts_by_task[task]:
                referenced_attempt = {
                    **source_attempt,
                    "completed_prefix_source": {
                        "source_namespace": source_reference["source_namespace"],
                        "source_receipt_sha256": canonical_sha256(source_attempt),
                    },
                }
                self.store.record_attempt(self.experiment_id, referenced_attempt)
        imported_effects = []
        for effect in sorted(
            completed_side_effects,
            key=lambda item: str(item["idempotency_key"]),
        ):
            receipt = effect.get("receipt")
            if not isinstance(receipt, dict):
                raise TypeError("completed-prefix side effect lacks a receipt")
            source_effect_sha256 = canonical_sha256(effect)
            destination_key = (
                f"{self.experiment_id}:prefix:"
                f"{canonical_sha256({'source_key': effect['idempotency_key']})[:32]}"
            )
            request = {
                "source_namespace": source_reference["source_namespace"],
                "source_idempotency_key": effect["idempotency_key"],
                "source_request_sha256": effect["request_sha256"],
                "source_effect_sha256": source_effect_sha256,
                "model_execution_allowed": False,
            }
            planned = self.store.plan_side_effect(
                experiment_id=self.experiment_id,
                idempotency_key=destination_key,
                kind=str(effect["kind"]),
                request=request,
            )
            referenced_receipt = {
                **receipt,
                "completed_prefix_source": {
                    **request,
                    "source_receipt_sha256": canonical_sha256(receipt),
                },
            }
            if planned["status"] == "planned":
                closed = self.store.complete_side_effect(
                    idempotency_key=destination_key,
                    receipt=referenced_receipt,
                )
            else:
                closed = planned.get("receipt")
            if closed != referenced_receipt:
                raise ValueError("completed-prefix side effect replay conflicts")
            imported_effects.append(
                {
                    "destination_key": destination_key,
                    "source_effect_sha256": source_effect_sha256,
                }
            )
        for category, units in sorted(source_budget_usage.items()):
            self.store.reserve_budget(
                experiment_id=self.experiment_id,
                idempotency_key=f"{self.experiment_id}:prefix-budget:{category}",
                category=category,
                units=units,
                limit_units={
                    "candidate_model_calls": self.budget_policy.max_candidate_model_calls,
                    "reflector_model_calls": self.budget_policy.max_reflector_model_calls,
                    "judge_operations": self.budget_policy.max_judge_operations,
                }[category],
            )

        imported_state = {
            key: value
            for key, value in source_state.items()
            if not key.startswith("_")
            and key
            not in {
                "experiment_id",
                "stage",
                "last_transition",
                "updated_at",
                "schema_version",
                "namespace_type",
            }
        }
        destination_candidate = dict(source_candidate)
        destination_candidate["successor_workspace_authority"] = (
            rebased_successor_workspace_authority
        )
        destination_candidate.pop("content_sha256", None)
        destination_candidate["content_sha256"] = canonical_sha256(destination_candidate)
        imported_state.update(
            {
                "active_candidate_receipt": destination_candidate,
                "completed_prefix_source": source_reference,
                "completed_prefix_import_complete": True,
                "candidate_reexecuted": False,
                "additional_candidate_model_calls": 0,
            }
        )
        return self._transition(
            TrainingStage.INITIALIZED,
            TrainingStage.NEXT_ATTEMPT_READY,
            "completed-prefix-imported",
            updates=imported_state,
            receipt={
                "operation": "append_only_completed_prefix_continuation",
                "source": source_reference,
                "attempt_inventory_sha256": canonical_sha256(attempts_by_task),
                "side_effect_inventory_sha256": canonical_sha256(imported_effects),
                "budget_usage": source_budget_usage,
                "candidate_reexecuted": False,
                "additional_candidate_model_calls": 0,
                "model_execution_allowed": False,
            },
        )

    def reconcile_completed_successor_failure(
        self,
        *,
        operation_id: str,
        source_identity: SupervisorIdentity,
        source_state_sha256: str,
        expected_source_terminal_receipt_sha256: str,
        expected_recovery_checkpoint_sha256: str,
        executor_identity: SupervisorIdentity,
        executor_active_identity_sha256: str,
        executor_readiness_sha256: str,
    ) -> dict[str, Any]:
        """Close one paid-complete successor tail under a distinct executor."""

        state = self.store.load(self.experiment_id)
        self.store.verify_identity(
            state,
            protocol_sha256=source_identity.protocol_sha256,
            core_identity_sha256=source_identity.core_identity_sha256,
            adapter_identity_sha256=source_identity.adapter_identity_sha256,
        )
        suffix = f"completed-successor-reconciled-{operation_id}"
        prior = next(
            (
                item
                for item in self.store.transition_receipts_for_experiment(self.experiment_id)
                if item.get("reconciliation_operation_id") == operation_id
            ),
            None,
        )
        if prior is not None:
            if (
                prior.get("source_terminal_receipt_sha256")
                != expected_source_terminal_receipt_sha256
                or prior.get("successor_recovery_checkpoint_sha256")
                != expected_recovery_checkpoint_sha256
            ):
                raise ValueError("completed-successor reconciliation replay conflicts")
            return state
        task = self.task_ids[state["current_task_index"]]
        attempt = state["current_attempt"]
        if (
            state.get("_state_sha256") != source_state_sha256
            or state.get("stage") != TrainingStage.EVOLUTION_RUNNING.value
            or self.store.active_resources(self.experiment_id)
        ):
            raise ValueError("completed-successor source state changed")
        effect_key = f"{self.experiment_id}:{task}:a{attempt}:evolution"
        effect = self.store.side_effect(effect_key)
        if (
            not isinstance(effect, dict)
            or effect.get("kind") != "evolution"
            or effect.get("status") != "planned"
            or effect.get("receipt") is not None
        ):
            raise ValueError("completed-successor source side effect is not pending")
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
        reconciliation = evolved.get("completed_methods_reconciliation")
        if (
            evolved.get("source_terminal_receipt_sha256")
            != expected_source_terminal_receipt_sha256
            or evolved.get("successor_recovery_checkpoint_sha256")
            != expected_recovery_checkpoint_sha256
            or evolved.get("model_calls_started") != 0
            or not isinstance(reconciliation, dict)
            or reconciliation.get("reconciliation_only") is not True
            or reconciliation.get("model_execution_allowed") is not False
            or reconciliation.get("model_calls_started") != 0
            or reconciliation.get("successor_transition_id")
            != state["active_attachment_receipt"].get("successor_transition_id")
            or not isinstance(jobs, list)
            or len(jobs) != 3
            or {item.get("artifact_type") for item in jobs if isinstance(item, dict)}
            != set(ARTIFACT_TYPES)
        ):
            raise ValueError("completed-successor reconciliation receipt is incomplete")
        return self._transition(
            TrainingStage.EVOLUTION_RUNNING,
            TrainingStage.EVOLUTION_COMPLETED,
            suffix,
            updates={
                "evolution_job_ids": [
                    *state["evolution_job_ids"],
                    *[item["job_id"] for item in jobs],
                ],
                "active_evolution_receipt": evolved,
            },
            receipt={
                **evolved,
                "reconciliation_operation_id": operation_id,
                "source_identity": {
                    "protocol_sha256": source_identity.protocol_sha256,
                    "core_identity_sha256": source_identity.core_identity_sha256,
                    "adapter_identity_sha256": source_identity.adapter_identity_sha256,
                    "state_sha256": source_state_sha256,
                },
                "executor_identity": {
                    "protocol_sha256": executor_identity.protocol_sha256,
                    "core_identity_sha256": executor_identity.core_identity_sha256,
                    "adapter_identity_sha256": executor_identity.adapter_identity_sha256,
                    "active_identity_sha256": executor_active_identity_sha256,
                    "readiness_sha256": executor_readiness_sha256,
                },
                "candidate_reexecuted": False,
                "additional_candidate_model_calls": 0,
                "additional_judge_calls": 0,
                "model_execution_allowed": False,
            },
        )

    @staticmethod
    def _invalid_attempt_receipt(
        *,
        task_id: str,
        attempt_index: int,
        candidate: dict[str, Any],
        validation: dict[str, Any],
        input_composite_id: str,
    ) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "attempt_index": attempt_index,
            "input_composite_id": input_composite_id,
            "session_id": candidate["session_id"],
            "dataset_id": candidate["dataset_id"],
            "completed": candidate.get("completed") is True,
            "artifact_valid": False,
            "validator_completeness": int(validation.get("completeness", 0)),
            "validator_errors": list(validation.get("validator_errors", [])),
            "score": None,
            "score_status": "ABSENT_VALIDATOR_FAILED",
            "evaluator_status": "SKIPPED",
            "runtime_seconds": float(candidate.get("runtime_seconds", 0.0)),
            "cost_total_usd": candidate.get("cost_total_usd"),
            "composite_size_bytes": int(candidate.get("composite_size_bytes", 0)),
            "output_artifact_hash": validation.get("artifact_root_sha256"),
            "attempt_status": "CONSUMED_INVALID_ARTIFACT",
        }

    def reconcile_candidate_sealed(
        self,
        *,
        source: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a new namespace from one immutable sealed Core authority.

        This is not a candidate retry.  The source namespace remains terminal
        and immutable; a dedicated operation validates and materializes its
        already-sealed SessionResult into this successor namespace.
        """

        if not (
            self.experiment_id.endswith("_reconcile")
            or "_reconcile_v" in self.experiment_id
            and self.experiment_id.rsplit("_reconcile_v", 1)[1].isdigit()
        ):
            raise ValueError("candidate reconciliation requires a reconciliation namespace")
        source_namespace = source.get("namespace")
        source_key = source.get("source_key")
        if (
            not isinstance(source_namespace, str)
            or source_namespace == self.experiment_id
            or not isinstance(source_key, str)
            or len(source_key) != 64
        ):
            raise ValueError("candidate reconciliation source identity is invalid")
        initial = self._initial_state()
        initial.update(
            {
                "namespace_type": "reconciliation_successor",
                "reconciliation_source": source,
                "candidate_origin": "imported_sealed_core_authority",
                "candidate_reexecuted": False,
                "additional_candidate_model_calls": 0,
            }
        )
        self.store.initialize_experiment(
            experiment_id=self.experiment_id,
            protocol_sha256=self.identity.protocol_sha256,
            core_identity_sha256=self.identity.core_identity_sha256,
            adapter_identity_sha256=self.identity.adapter_identity_sha256,
            initial_state=initial,
        )
        state = self.status()
        stage = TrainingStage(state["stage"])
        if state.get("reconciliation_source", {}).get("source_key") != source_key:
            raise ValueError("reconciliation namespace source identity drifted")
        if stage is TrainingStage.INITIALIZED:
            reconciled = self._effect(
                kind="candidate-reconciliation",
                request=request,
                execute=self.operations.reconcile_candidate_authority,
            )
            required = {
                "session_id",
                "dataset_id",
                "dataset_revision",
                "completed",
                "run_id",
                "runtime_seconds",
                "core_project_id",
                "core_task_id",
                "core_attempt_id",
                "workspace_binding_id",
                "task_request_id",
                "session_result_id",
                "transcript_receipt",
                "source_namespace",
                "source_state_sha256",
                "seal_origin",
            }
            if (
                not required.issubset(reconciled)
                or reconciled.get("seal_origin") != "reconciliation"
                or reconciled.get("source_namespace") != source_namespace
                or reconciled.get("candidate_reexecuted") is not False
                or reconciled.get("additional_candidate_model_calls") != 0
                or reconciled.get("completed") is not True
            ):
                raise ValueError("candidate reconciliation receipt is incomplete")
            state = self._transition(
                TrainingStage.INITIALIZED,
                TrainingStage.CANDIDATE_SEALED_RECONCILED,
                "candidate-sealed-authority-reconciled",
                updates={
                    "session_ids": [reconciled["session_id"]],
                    "dataset_ids": [reconciled["dataset_id"]],
                    "active_candidate_receipt": reconciled,
                    "core_project_id": reconciled["core_project_id"],
                    "reconciliation_operation_id": reconciled["operation_id"],
                    "reconciliation_receipt_sha256": reconciled["content_sha256"],
                },
                receipt={
                    **reconciled,
                    "candidate_model_call_reused": True,
                    "candidate_model_call_reexecuted": False,
                },
            )
            stage = TrainingStage(state["stage"])
        if stage is TrainingStage.CANDIDATE_SEALED_RECONCILED:
            return self._transition(
                stage,
                TrainingStage.CANDIDATE_SEALED,
                "candidate-sealed-reconciliation-admitted",
                receipt={
                    "seal_origin": "reconciliation",
                    "source_namespace": source_namespace,
                    "source_key": source_key,
                    "candidate_model_call_reused": True,
                    "candidate_model_call_reexecuted": False,
                    "additional_candidate_model_calls": 0,
                },
            )
        if stage is TrainingStage.CANDIDATE_SEALED:
            return state
        raise ValueError("reconciliation namespace is not at a resumable sealed stage")

    def reconcile_current_candidate_sealed(
        self,
        *,
        source: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        """Adopt one already-sealed current Attempt without replaying it.

        Unlike the legacy attempt-zero migration, this operation starts from
        an append-only continuation that already contains the prior attempts,
        composite and task-local overlay.  It may only replace the current
        ``TASK_ATTEMPT_READY`` action with a read-only Core reconciliation.
        """

        if not self.experiment_id.endswith("_reconcile"):
            raise ValueError(
                "candidate continuation reconciliation requires a reconciliation namespace"
            )
        source_namespace = source.get("namespace")
        source_key = source.get("source_key")
        if (
            not isinstance(source_namespace, str)
            or source_namespace == self.experiment_id
            or not isinstance(source_key, str)
            or len(source_key) != 64
        ):
            raise ValueError("candidate continuation source identity is invalid")
        state = self.status()
        stage = TrainingStage(state["stage"])
        task = self.task_ids[state["current_task_index"]]
        attempt = state["current_attempt"]
        if (
            request.get("task_id") != task
            or request.get("source_task_id") != task
            or request.get("source_attempt_index") != attempt
            or request.get("source_input_composite_id") != state.get("current_composite_id")
            or request.get("source_input_project_head_id")
            != state.get("active_admission_receipt", {}).get("core_project_head_id")
            or request.get("source_task_local_overlay_id")
            != state.get("current_task_local_overlay_id")
        ):
            raise ValueError("candidate continuation authority differs from active attempt")
        if stage is TrainingStage.TASK_ATTEMPT_READY:
            state = self._transition(
                stage,
                TrainingStage.CANDIDATE_RUNNING,
                "candidate-reconciliation-start",
                updates={
                    "active_candidate_intent": {
                        "source_namespace": source_namespace,
                        "source_key": source_key,
                        "task_id": task,
                        "attempt_index": attempt,
                        "candidate_reexecuted": False,
                        "additional_candidate_model_calls": 0,
                    },
                    "candidate_origin": "referenced_sealed_core_authority",
                },
                receipt={
                    "source": source,
                    "candidate_intent_persisted": False,
                    "candidate_model_call_reused": True,
                    "candidate_model_call_reexecuted": False,
                    "additional_candidate_model_calls": 0,
                },
            )
            stage = TrainingStage(state["stage"])
        if stage is TrainingStage.CANDIDATE_RUNNING:
            intent = state.get("active_candidate_intent")
            if not isinstance(intent, dict) or intent.get("source_key") != source_key:
                raise ValueError("candidate continuation reconciliation intent drifted")
            reconciled = self._effect(
                kind="candidate-reconciliation",
                request=request,
                execute=self.operations.reconcile_candidate_authority,
            )
            required = {
                "session_id",
                "dataset_id",
                "dataset_revision",
                "completed",
                "run_id",
                "runtime_seconds",
                "core_project_id",
                "core_task_id",
                "core_attempt_id",
                "workspace_binding_id",
                "task_request_id",
                "session_result_id",
                "transcript_receipt",
                "runtime_injection",
            }
            if (
                not required.issubset(reconciled)
                or reconciled.get("seal_origin") != "reconciliation"
                or reconciled.get("source_namespace") != source_namespace
                or reconciled.get("candidate_reexecuted") is not False
                or reconciled.get("additional_candidate_model_calls") != 0
                or reconciled.get("completed") is not True
                or reconciled.get("input_composite_id") != state.get("current_composite_id")
                or reconciled.get("input_project_head_id")
                != request.get("source_input_project_head_id")
                or reconciled.get("task_local_overlay_id")
                != state.get("current_task_local_overlay_id")
            ):
                raise ValueError("candidate continuation reconciliation receipt is incomplete")
            return self._transition(
                stage,
                TrainingStage.CANDIDATE_SEALED,
                "candidate-sealed-authority-reused",
                updates={
                    "session_ids": [*state["session_ids"], reconciled["session_id"]],
                    "dataset_ids": [*state["dataset_ids"], reconciled["dataset_id"]],
                    "active_candidate_receipt": reconciled,
                    "core_project_id": reconciled["core_project_id"],
                    "preseeded_workspace_authority": None,
                    "candidate_status": "SEALED_SOURCE_REFERENCED",
                    "candidate_reexecuted": False,
                    "additional_candidate_model_calls": 0,
                },
                receipt={
                    **reconciled,
                    "candidate_model_call_reused": True,
                    "candidate_model_call_reexecuted": False,
                },
            )
        if stage is TrainingStage.CANDIDATE_SEALED:
            return state
        raise ValueError("candidate continuation is not at a reconcilable stage")

    def reconcile_invalid_validator_terminal(
        self,
        *,
        operation_id: str,
        source_identity: SupervisorIdentity,
        source_state_sha256: str,
        expected_validator_receipt_sha256: str,
        executor_identity: SupervisorIdentity,
        executor_active_identity_sha256: str,
        executor_readiness_sha256: str,
    ) -> dict[str, Any]:
        """Append the missing fail-closed terminal transition after validation.

        This recovery is deliberately narrower than ``run_next``.  It accepts
        only a reconciliation namespace whose validator side effect already
        completed with an invalid artifact before the transition graph
        rejected ``CANDIDATE_SEALED -> FAILED``.  It never executes an
        operation port and records source and repair-executor identities
        independently.
        """

        state = self.store.load(self.experiment_id)
        self.store.verify_identity(
            state,
            protocol_sha256=source_identity.protocol_sha256,
            core_identity_sha256=source_identity.core_identity_sha256,
            adapter_identity_sha256=source_identity.adapter_identity_sha256,
        )
        suffix = f"validator-failed-reconciliation-{operation_id}"
        key = (
            f"{self.experiment_id}:{self.task_ids[state['current_task_index']]}:"
            f"a{state['current_attempt']}:{suffix}"
        )
        if state.get("stage") == TrainingStage.FAILED.value:
            existing = next(
                (
                    item
                    for item in self.store.transition_receipts_for_experiment(self.experiment_id)
                    if item.get("idempotency_key") == key
                ),
                None,
            )
            if existing is None:
                raise ValueError("validator-terminal reconciliation conflicts")
            return state
        if state.get("_state_sha256") != source_state_sha256:
            raise ValueError("validator-terminal source state changed")
        candidate = state.get("active_candidate_receipt")
        if (
            state.get("namespace_type") != "reconciliation_successor"
            or state.get("stage") != TrainingStage.CANDIDATE_SEALED.value
            or state.get("current_task_index") != 0
            or state.get("current_attempt") != 0
            or not isinstance(candidate, dict)
            or candidate.get("seal_origin") != "reconciliation"
            or candidate.get("candidate_reexecuted") is not False
            or candidate.get("additional_candidate_model_calls") != 0
            or self.store.active_resources(self.experiment_id)
            or self.store.attempts_for_task(
                self.experiment_id, self.task_ids[state["current_task_index"]]
            )
        ):
            raise ValueError("validator-terminal reconciliation closure failed")
        effects = self.store.side_effects_for_experiment(self.experiment_id)
        validator = [item for item in effects if item.get("kind") == "validator"]
        forbidden = [
            item
            for item in effects
            if item.get("kind") in {"evaluation", "attachment", "evolution"}
        ]
        if len(effects) != 2 or len(validator) != 1 or forbidden:
            raise ValueError("validator-terminal side-effect ledger is not closed")
        validation = validator[0]
        receipt = validation.get("receipt")
        if (
            validation.get("status") != "completed"
            or not isinstance(receipt, dict)
            or receipt.get("artifact_valid") is not False
            or receipt.get("content_sha256") != expected_validator_receipt_sha256
        ):
            raise ValueError("validator-terminal receipt is not the invalid result")
        terminal_receipt = {
            **receipt,
            "reconciliation_operation_id": operation_id,
            "reconciliation_kind": "completed_validator_terminal_v1",
            "source_identity": {
                "protocol_sha256": source_identity.protocol_sha256,
                "core_identity_sha256": source_identity.core_identity_sha256,
                "adapter_identity_sha256": source_identity.adapter_identity_sha256,
                "state_sha256": source_state_sha256,
            },
            "executor_identity": {
                "protocol_sha256": executor_identity.protocol_sha256,
                "core_identity_sha256": executor_identity.core_identity_sha256,
                "adapter_identity_sha256": executor_identity.adapter_identity_sha256,
                "active_identity_sha256": executor_active_identity_sha256,
                "readiness_sha256": executor_readiness_sha256,
            },
            "candidate_reexecuted": False,
            "additional_candidate_model_calls": 0,
            "judge_calls": 0,
            "source_mutated_before_transition": False,
        }
        return self.store.transition(
            experiment_id=self.experiment_id,
            idempotency_key=key,
            source=TrainingStage.CANDIDATE_SEALED,
            target=TrainingStage.FAILED,
            updates={"failure_reason": "ARTIFACT_VALIDATOR_FAILED"},
            receipt=terminal_receipt,
        )

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
        attempts_by_task = {
            task: self.store.attempts_for_task(self.experiment_id, task) for task in self.task_ids
        }
        attempts = sum(len(items) for items in attempts_by_task.values())
        resources = self.store.active_resources(self.experiment_id)
        effects = self.store.side_effects_for_experiment(self.experiment_id)
        transitions = self.store.transition_receipts_for_experiment(self.experiment_id)
        budget_usage = self.store.budget_usage(self.experiment_id)
        if state["current_task_index"] >= len(self.task_ids) and state["stage"] not in {
            TrainingStage.COMMUNITY_TRAINING_COMPLETE.value,
            TrainingStage.FINAL_FREEZE_PENDING.value,
            TrainingStage.FINAL_FROZEN.value,
            TrainingStage.ITEM_RESET.value,
        }:
            raise ValueError("training state task cursor is invalid")
        for task, records in attempts_by_task.items():
            indices = [int(item["attempt_index"]) for item in records]
            if indices != list(range(len(indices))) or any(
                item.get("task_id") != task for item in records
            ):
                raise ValueError("training attempt inventory is not contiguous")
        session_ids = [
            str(item["session_id"])
            for records in attempts_by_task.values()
            for item in records
            if item.get("session_id")
        ]
        dataset_ids = [
            str(item["dataset_id"])
            for records in attempts_by_task.values()
            for item in records
            if item.get("dataset_id")
        ]
        if len(session_ids) != len(set(session_ids)) or len(dataset_ids) != len(set(dataset_ids)):
            raise ValueError("training attempt authority is duplicated")
        if len(state.get("evolution_job_ids", [])) != len(set(state.get("evolution_job_ids", []))):
            raise ValueError("training evolution job identity is duplicated")
        evolution_effects = [item for item in effects if item["kind"] == "evolution"]
        for item in evolution_effects:
            receipt = item.get("receipt")
            if item["status"] == "completed":
                jobs = receipt.get("jobs") if isinstance(receipt, dict) else None
                if (
                    not isinstance(jobs, list)
                    or len(jobs) != 3
                    or {job.get("artifact_type") for job in jobs} != set(ARTIFACT_TYPES)
                    or len({job.get("job_id") for job in jobs}) != 3
                ):
                    raise ValueError("completed evolution cycle is not a closed triple")
        limits = {
            "candidate_model_calls": self.budget_policy.max_candidate_model_calls,
            "reflector_model_calls": self.budget_policy.max_reflector_model_calls,
            "judge_operations": self.budget_policy.max_judge_operations,
        }
        if any(budget_usage[key] > limit for key, limit in limits.items()):
            raise ValueError("training model-operation budget was exceeded")
        runtime_seconds = self._completed_runtime_seconds(effects)
        if (
            runtime_seconds > self.budget_policy.cumulative_runtime_seconds
            and state["stage"] != TrainingStage.BUDGET_EXHAUSTED.value
        ):
            raise ValueError("training runtime budget exceeded without terminal state")
        pending = [item for item in effects if item["status"] == "planned"]
        failed_effects = [item for item in effects if item["status"] == "failed"]
        if state["stage"] == TrainingStage.FINAL_FROZEN.value:
            if pending or resources:
                raise ValueError("final frozen state has active operations or resources")
            if attempts != len(self.task_ids) * 3:
                raise ValueError("final frozen attempt inventory is incomplete")
            if len(evolution_effects) != len(self.task_ids) * 2:
                raise ValueError("final frozen reflector cycle inventory is incomplete")
            if len(state.get("evolution_job_ids", [])) != len(self.task_ids) * 2 * 3:
                raise ValueError("final frozen evolution job inventory is incomplete")
            if set(state.get("task_best", {})) != set(self.task_ids):
                raise ValueError("final frozen task-best inventory is incomplete")
            if state.get("current_task_local_overlay_id") is not None:
                raise ValueError("final frozen state retains a task-local overlay")
        if state["stage"] == TrainingStage.ITEM_RESET.value and (
            not self.per_item_reset_enabled
            or len(self.task_ids) != 1
            or attempts != 2
            or len(evolution_effects) != 1
            or len(state.get("evolution_job_ids", [])) != 3
            or pending
            or resources
            or not isinstance(state.get("paired_result"), dict)
            or state.get("active_artifact_ids") != []
            or len(state.get("archived_artifact_ids", [])) != 3
            or state.get("current_composite_id") is not None
            or state.get("core_project_id") is not None
            or state.get("preseeded_workspace_authority") is not None
            or state.get("current_task_local_overlay_id") is not None
            or state.get("current_task_local_overlay_scope_id") is not None
            or not isinstance(state.get("item_reset_receipt"), dict)
        ):
            raise ValueError("per-item reset terminal inventory is incomplete")
        terminal_failure = state.get("active_terminal_failure")
        terminal_failure = terminal_failure if isinstance(terminal_failure, dict) else {}
        core_failure = terminal_failure.get("core_failure")
        core_failure = core_failure if isinstance(core_failure, dict) else {}
        if state["stage"] == TrainingStage.CANDIDATE_SETUP_BLOCKED.value:
            execution_status = "CANDIDATE_SETUP_BLOCKED"
        elif state["stage"] in {
            TrainingStage.FINAL_FROZEN.value,
            TrainingStage.ITEM_RESET.value,
        }:
            execution_status = "COMPLETED"
        elif state["stage"] in {
            TrainingStage.BLOCKED.value,
            TrainingStage.FAILED.value,
            TrainingStage.BUDGET_EXHAUSTED.value,
            TrainingStage.TASK_NO_VALID_ATTEMPT.value,
        }:
            execution_status = state["stage"]
        else:
            execution_status = "IN_PROGRESS"
        return {
            "status": "PASS",
            "integrity_status": "PASS",
            "execution_status": execution_status,
            "stage": state["stage"],
            "state_sha256": state["_state_sha256"],
            "state_revision": state["_revision"],
            "attempt_receipts": attempts,
            "transition_receipts": len(transitions),
            "side_effect_rows": len(effects),
            "pending_side_effects": len(pending),
            "failed_side_effects": len(failed_effects),
            "pending_side_effect_status": (
                "planned" if pending else ("failed" if failed_effects else "none")
            ),
            "underlying_session_status": core_failure.get("underlying_session_status"),
            "model_started": core_failure.get("model_started"),
            "benchmark_started": core_failure.get("benchmark_started"),
            "completed_reflector_cycles": sum(
                item["status"] == "completed" for item in evolution_effects
            ),
            "evolution_job_ids": len(state.get("evolution_job_ids", [])),
            "task_best_count": len(state.get("task_best", {})),
            "budget_usage": budget_usage,
            "completed_runtime_seconds": runtime_seconds,
            "active_owned_resources": resources,
            "identity_verified": True,
            "paired_result": state.get("paired_result"),
            "item_reset_receipt": state.get("item_reset_receipt"),
        }

    @staticmethod
    def _completed_runtime_seconds(effects: list[dict[str, Any]]) -> float:
        total = 0.0
        for item in effects:
            if item.get("status") != "completed" or not isinstance(item.get("receipt"), dict):
                continue
            receipt = item["receipt"]
            runtime = receipt.get("runtime_seconds")
            if type(runtime) in {int, float} and runtime >= 0:
                total += float(runtime)
            jobs = receipt.get("jobs")
            if isinstance(jobs, list):
                for job in jobs:
                    job_runtime = job.get("runtime_seconds") if isinstance(job, dict) else None
                    if type(job_runtime) in {int, float} and job_runtime >= 0:
                        total += float(job_runtime)
        return total

    def _runtime_budget_available(self) -> bool:
        effects = self.store.side_effects_for_experiment(self.experiment_id)
        return (
            self._completed_runtime_seconds(effects)
            < self.budget_policy.cumulative_runtime_seconds
        )

    def _reserve_budget(
        self,
        *,
        task: str,
        attempt: int,
        operation: str,
        category: str,
        units: int,
        limit: int,
    ) -> dict[str, Any]:
        return self.store.reserve_budget(
            experiment_id=self.experiment_id,
            idempotency_key=(f"{self.experiment_id}:{task}:a{attempt}:{operation}:budget"),
            category=category,
            units=units,
            limit_units=limit,
        )

    def _budget_exhausted(
        self,
        *,
        stage: TrainingStage,
        task: str,
        attempt: int,
        category: str,
    ) -> dict[str, Any]:
        return self._transition(
            stage,
            TrainingStage.BUDGET_EXHAUSTED,
            f"budget-exhausted-{category}",
            updates={"failure_reason": "BUDGET_EXHAUSTED"},
            receipt={
                "task_id": task,
                "attempt_index": attempt,
                "failure_classification": "BUDGET_EXHAUSTED",
                "budget_category": category,
                "budget_usage": self.store.budget_usage(self.experiment_id),
                "model_started_by_transition": False,
            },
        )

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
        task = (
            self.task_ids[state["current_task_index"]]
            if state["current_task_index"] < len(self.task_ids)
            else "complete"
        )
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
        if observed["status"] == "failed":
            receipt = observed.get("receipt")
            if kind == "candidate" and isinstance(receipt, dict):
                raise CandidateAuthorityUnavailable(
                    "candidate side effect is already terminal",
                    reason_code=str(receipt.get("reason_code") or "candidate_terminal_failure"),
                    terminal_proven=True,
                    failure_receipt=receipt,
                )
            raise RuntimeError(f"{kind} side effect is already terminal")
        try:
            receipt = execute(request, key)
        except CandidateAuthorityUnavailable as exc:
            if exc.terminal_proven:
                failure_receipt = {
                    "schema_version": "openevo.training_side_effect_failure.v1",
                    "terminal_proven": True,
                    "kind": kind,
                    "reason_code": exc.reason_code,
                    "core_failure": exc.failure_receipt or {},
                }
                self.store.fail_side_effect(
                    idempotency_key=key,
                    receipt=failure_receipt,
                )
                exc.failure_receipt = failure_receipt
            raise
        if not isinstance(receipt, dict):
            raise TypeError(f"{kind} did not return a closed receipt")
        typed_resources = receipt.get("owned_resources", [])
        if not isinstance(typed_resources, list):
            raise TypeError(f"{kind} returned an invalid owned-resource inventory")
        for item in typed_resources:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("resource_id"), str)
                or item.get("kind")
                not in {"process", "container", "openevo_session", "runtime", "worker_job"}
                or not isinstance(item.get("identity"), dict)
                or type(item.get("active")) is not bool
            ):
                raise ValueError(f"{kind} returned an invalid typed owned resource")
            self.store.register_resource(
                experiment_id=self.experiment_id,
                resource_id=item["resource_id"],
                kind=item["kind"],
                identity=item["identity"],
            )
            if item["active"] is False:
                self.store.release_resource(item["resource_id"])
        return self.store.complete_side_effect(idempotency_key=key, receipt=receipt)

    def run_next(self) -> dict[str, Any]:
        state = self.status()
        stage = TrainingStage(state["stage"])
        task = (
            self.task_ids[state["current_task_index"]]
            if state["current_task_index"] < len(self.task_ids)
            else None
        )
        attempt = state["current_attempt"]
        run_suffix = (
            str(self.validator_failure_policy["run_id_suffix"])
            if self.validator_failure_learning_enabled
            else "v2"
        )
        run_id = f"{task}_a{attempt}_{run_suffix}" if task is not None else None
        if stage in {
            TrainingStage.CANDIDATE_SETUP_BLOCKED,
            TrainingStage.BLOCKED,
            TrainingStage.FAILED,
            TrainingStage.FINAL_FROZEN,
            TrainingStage.TASK_NO_VALID_ATTEMPT,
            TrainingStage.BUDGET_EXHAUSTED,
            TrainingStage.ITEM_RESET,
        }:
            raise TrainingPaused(f"supervisor is terminal at {stage.value}")
        if (
            stage is TrainingStage.INITIALIZED
            and state.get("namespace_type") == "append_only_completed_prefix_continuation"
            and state.get("completed_prefix_import_complete") is not True
        ):
            raise TrainingPaused("COMPLETED_PREFIX_IMPORT_PENDING")
        if stage is TrainingStage.CANDIDATE_SEALED_RECONCILED:
            source = state.get("reconciliation_source")
            if not isinstance(source, dict):
                raise ValueError("reconciled candidate lacks source authority")
            return self._transition(
                stage,
                TrainingStage.CANDIDATE_SEALED,
                "candidate-sealed-reconciliation-admitted",
                receipt={
                    "seal_origin": "reconciliation",
                    "source_namespace": source.get("namespace"),
                    "source_key": source.get("source_key"),
                    "candidate_model_call_reused": True,
                    "candidate_model_call_reexecuted": False,
                    "additional_candidate_model_calls": 0,
                },
            )
        if stage is TrainingStage.RECOVERY_SEEDED_CONTINUATION:
            return self._transition(
                stage,
                TrainingStage.NEXT_ATTEMPT_READY,
                "recovery-seeded-next-attempt",
                updates={
                    "current_composite_id": state["active_admission_receipt"]["composite_id"]
                },
                receipt={
                    "recovery_id": state["successor_recovery_source"]["recovery_id"],
                    "recovery_seed_request_id": state["successor_recovery_source"][
                        "recovery_seed_request_id"
                    ],
                    "candidate_reexecuted": False,
                },
            )
        if stage is TrainingStage.INITIALIZED:
            if self.per_item_reset_enabled:
                receipt = {
                    "task_id": task,
                    "generation_zero_required": True,
                    "active_artifact_ids": [],
                    "active_successor_head": None,
                    "active_project_head": None,
                    "active_candidate_session": None,
                    "active_workspace": None,
                    "active_gt_supervision": None,
                    "prior_task_context": None,
                    "cross_project_fork_used": False,
                    "provider_calls": 0,
                }
                return self._transition(
                    stage,
                    TrainingStage.TASK_ATTEMPT_READY,
                    "per-item-generation-zero-ready",
                    receipt=receipt,
                )
            return self._transition(stage, TrainingStage.TASK_ATTEMPT_READY, "initialize")
        if stage is TrainingStage.TASK_ATTEMPT_READY:
            preseeded_workspace_authority = state.get("preseeded_workspace_authority")
            successor_workspace_authority = None
            if attempt in {1, 2} and preseeded_workspace_authority is None:
                active_candidate = state.get("active_candidate_receipt")
                if isinstance(active_candidate, dict):
                    successor_workspace_authority = active_candidate.get(
                        "successor_workspace_authority"
                    )
            request = {
                "experiment_id": self.experiment_id,
                "task_id": task,
                "attempt_index": attempt,
                "run_id": run_id,
                "protocol_sha256": self.identity.protocol_sha256,
                "core_identity_sha256": self.identity.core_identity_sha256,
                "adapter_identity_sha256": self.identity.adapter_identity_sha256,
                "core_project_id": state.get("core_project_id"),
                "input_composite_id": state["current_composite_id"],
                "task_local_overlay_id": state["current_task_local_overlay_id"],
                "task_local_overlay_scope_id": state["current_task_local_overlay_scope_id"],
                "preseeded_workspace_authority": preseeded_workspace_authority,
                "successor_workspace_authority": (successor_workspace_authority),
                "fresh_workspace": True,
                "resume_in_place": False,
            }
            if not self._runtime_budget_available():
                return self._budget_exhausted(
                    stage=stage,
                    task=str(task),
                    attempt=attempt,
                    category="cumulative_runtime_seconds",
                )
            try:
                readiness = self.operations.candidate_readiness(request)
            except RuntimeError:
                return self._transition(
                    stage,
                    TrainingStage.BLOCKED,
                    "candidate-preflight-blocked",
                    updates={"failure_reason": "CORE_CONTROL_PREFLIGHT_FAILED"},
                    receipt={
                        "failure_classification": "CORE_CONTROL_PREFLIGHT_FAILED",
                        "candidate_intent_persisted": False,
                        "model_started": False,
                        "secret_recorded": False,
                    },
                )
            required_true = {
                "service_reachable",
                "bearer_present",
                "bearer_valid",
                "generation_matches",
                "candidate_port_authorized",
            }
            if (
                not isinstance(readiness, dict)
                or any(readiness.get(field) is not True for field in required_true)
                or readiness.get("secret_recorded") is not False
                or not isinstance(readiness.get("generation"), str)
                or not isinstance(readiness.get("release_identity"), str)
            ):
                return self._transition(
                    stage,
                    TrainingStage.BLOCKED,
                    "candidate-preflight-blocked",
                    updates={"failure_reason": "CORE_CONTROL_PREFLIGHT_FAILED"},
                    receipt={
                        "failure_classification": "CORE_CONTROL_PREFLIGHT_FAILED",
                        "candidate_intent_persisted": False,
                        "model_started": False,
                        "secret_recorded": False,
                    },
                )
            request = {
                **request,
                "core_control_generation": readiness["generation"],
                "core_control_release_identity": readiness["release_identity"],
                "core_control_service_identity_id": readiness.get("service_identity_id"),
            }
            workspace_binding = readiness.get("candidate_workspace_binding")
            workspace_authority_required = (
                preseeded_workspace_authority is not None
                or successor_workspace_authority is not None
            )
            if workspace_authority_required and (
                not isinstance(workspace_binding, dict)
                or workspace_binding.get("schema_version")
                not in {
                    "openevo.researchclawbench.candidate_workspace_binding.v1",
                    "openevo.researchclawbench.candidate_workspace_binding.v2",
                }
                or workspace_binding.get("workspace_snapshot_match") is not True
                or workspace_binding.get("expected_workspace_snapshot")
                != workspace_binding.get("actual_workspace_snapshot")
                or not isinstance(workspace_binding.get("content_sha256"), str)
            ):
                return self._transition(
                    stage,
                    TrainingStage.BLOCKED,
                    "candidate-workspace-preflight-blocked",
                    updates={"failure_reason": "CANDIDATE_WORKSPACE_PREFLIGHT_FAILED"},
                    receipt={
                        "failure_classification": ("CANDIDATE_WORKSPACE_PREFLIGHT_FAILED"),
                        "candidate_intent_persisted": False,
                        "model_started": False,
                        "secret_recorded": False,
                    },
                )
            if isinstance(workspace_binding, dict):
                request = {
                    **request,
                    "candidate_workspace_binding": workspace_binding,
                    "expected_workspace_snapshot": workspace_binding.get(
                        "expected_workspace_snapshot"
                    ),
                }
            try:
                candidate_budget = self._reserve_budget(
                    task=str(task),
                    attempt=attempt,
                    operation="candidate",
                    category="candidate_model_calls",
                    units=1,
                    limit=self.budget_policy.max_candidate_model_calls,
                )
            except ValueError as exc:
                if "budget exhausted" not in str(exc):
                    raise
                return self._budget_exhausted(
                    stage=stage,
                    task=str(task),
                    attempt=attempt,
                    category="candidate_model_calls",
                )
            self.store.plan_side_effect(
                experiment_id=self.experiment_id,
                idempotency_key=f"{self.experiment_id}:{task}:a{attempt}:candidate",
                kind="candidate",
                request=request,
            )
            return self._transition(
                stage,
                TrainingStage.CANDIDATE_RUNNING,
                "candidate-start",
                updates={
                    "active_candidate_intent": request,
                    "active_core_control_readiness": readiness,
                },
                receipt={
                    "task_id": task,
                    "attempt_index": attempt,
                    "candidate_intent_persisted": True,
                    "core_control": readiness,
                    "secret_recorded": False,
                    "budget_reservation": candidate_budget,
                },
            )
        if stage is TrainingStage.CANDIDATE_RUNNING:
            request = state.get("active_candidate_intent")
            if not isinstance(request, dict):
                return self._transition(
                    stage,
                    TrainingStage.BLOCKED,
                    "candidate-intent-missing",
                    updates={"failure_reason": "CANDIDATE_INTENT_AUTHORITY_MISSING"},
                    receipt={
                        "failure_classification": "CANDIDATE_INTENT_AUTHORITY_MISSING",
                        "model_started": None,
                        "secret_recorded": False,
                    },
                )
            try:
                candidate = self._effect(
                    kind="candidate",
                    request=request,
                    execute=self.operations.ensure_candidate,
                )
            except CandidateAuthorityUnavailable as exc:
                terminal_failure = (
                    exc.failure_receipt if isinstance(exc.failure_receipt, dict) else {}
                )
                core_failure = terminal_failure.get("core_failure")
                core_failure = core_failure if isinstance(core_failure, dict) else {}
                if exc.terminal_proven:
                    return self._transition(
                        stage,
                        TrainingStage.CANDIDATE_SETUP_BLOCKED,
                        "candidate-setup-terminal",
                        updates={
                            "failure_reason": "CANDIDATE_SETUP_BLOCKED",
                            "active_terminal_failure": terminal_failure,
                        },
                        receipt={
                            "failure_classification": "CANDIDATE_SETUP_BLOCKED",
                            "core_failure_code": exc.reason_code,
                            "candidate_intent_persisted": True,
                            "model_started": core_failure.get("model_started"),
                            "benchmark_started": core_failure.get("benchmark_started"),
                            "underlying_session_status": core_failure.get(
                                "underlying_session_status"
                            ),
                            "pending_side_effect_status": "failed",
                            "terminal_proven": True,
                            "secret_recorded": False,
                        },
                    )
                return self._transition(
                    stage,
                    TrainingStage.BLOCKED,
                    "candidate-operation-blocked",
                    updates={"failure_reason": "CANDIDATE_OPERATION_FAILED_BEFORE_SEAL"},
                    receipt={
                        "failure_classification": "CANDIDATE_OPERATION_FAILED_BEFORE_SEAL",
                        "core_failure_code": exc.reason_code,
                        "candidate_intent_persisted": True,
                        "model_started": core_failure.get("model_started"),
                        "benchmark_started": core_failure.get("benchmark_started"),
                        "secret_recorded": False,
                    },
                )
            required = {
                "session_id",
                "dataset_id",
                "dataset_revision",
                "completed",
                "run_id",
                "runtime_seconds",
                "core_project_id",
                "core_task_id",
                "core_attempt_id",
                "workspace_binding_id",
                "task_request_id",
                "session_result_id",
                "transcript_receipt",
                "successor_workspace_authority",
            }
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
                    "core_project_id": candidate["core_project_id"],
                    "preseeded_workspace_authority": None,
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
                if self.validator_failure_learning_enabled:
                    self.store.record_attempt(
                        self.experiment_id,
                        self._invalid_attempt_receipt(
                            task_id=str(task),
                            attempt_index=attempt,
                            candidate=state["active_candidate_receipt"],
                            validation=validation,
                            input_composite_id=state["current_composite_id"],
                        ),
                    )
                    if attempt == 2:
                        return self._transition(
                            stage,
                            TrainingStage.TASK_SELECTION_PENDING,
                            "invalid-final-attempt-selection-pending",
                            updates={
                                "active_validation_receipt": validation,
                                "attempt_status": "CONSUMED_INVALID_ARTIFACT",
                                "validator_status": "FAILED",
                                "evaluator_status": "SKIPPED",
                                "score_status": "ABSENT",
                            },
                            receipt={
                                **validation,
                                "attempt_consumed": True,
                                "judge_skipped": True,
                                "reflector_allowed": False,
                            },
                        )
                    return self._transition(
                        stage,
                        TrainingStage.VALIDATOR_FEEDBACK_PENDING,
                        "invalid-artifact-consumed",
                        updates={
                            "active_validation_receipt": validation,
                            "attempt_status": "CONSUMED_INVALID_ARTIFACT",
                            "validator_status": "FAILED",
                            "evaluator_status": "SKIPPED",
                            "score_status": "ABSENT",
                            "training_signal_status": "READY_FROM_VALIDATOR",
                        },
                        receipt={
                            **validation,
                            "attempt_consumed": True,
                            "same_attempt_retry_allowed": False,
                            "judge_skipped": True,
                            "reflector_allowed": True,
                        },
                    )
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
        if stage is TrainingStage.VALIDATOR_FEEDBACK_PENDING:
            if not self.validator_failure_learning_enabled or attempt >= 2:
                raise ValueError("validator feedback transition is not authorized")
            attachment = self._effect(
                kind="feedback-attachment",
                request={
                    "task_id": task,
                    "attempt_index": attempt,
                    "dataset_id": state["active_candidate_receipt"]["dataset_id"],
                    "dataset_revision": state["active_candidate_receipt"]["dataset_revision"],
                    "session_id": state["active_candidate_receipt"]["session_id"],
                    "core_task_id": state["active_candidate_receipt"]["core_task_id"],
                    "core_attempt_id": state["active_candidate_receipt"]["core_attempt_id"],
                    "successor_transition_id": state["active_candidate_receipt"].get(
                        "successor_transition_id"
                    ),
                    "candidate": state["active_candidate_receipt"],
                    "validation": state["active_validation_receipt"],
                    "feedback_source": "artifact_validator",
                    "authority": "evaluator_only",
                },
                execute=self.operations.attach_feedback,
            )
            if (
                not attachment.get("attachment_id")
                or not attachment.get("resolved_view_sha256")
                or attachment.get("feedback_class") != "MIXED"
                or attachment.get("feedback_source") != "artifact_validator"
            ):
                raise ValueError("validator feedback attachment receipt is incomplete")
            return self._transition(
                stage,
                TrainingStage.ATTACHMENT_SEALED,
                "validator-feedback-attachment-sealed",
                updates={
                    "attachment_ids": [
                        *state["attachment_ids"],
                        attachment["attachment_id"],
                    ],
                    "active_attachment_receipt": attachment,
                    "current_task_local_overlay_id": attachment.get("task_local_overlay_id"),
                    "current_task_local_overlay_scope_id": attachment.get(
                        "task_local_overlay_scope_id"
                    ),
                    "training_signal_status": "ATTACHED_FROM_VALIDATOR",
                },
                receipt=attachment,
            )
        if stage is TrainingStage.ARTIFACT_VALIDATED:
            if self.per_item_reset_enabled:
                return self._transition(
                    stage,
                    TrainingStage.EVALUATION_PENDING,
                    "per-item-independent-evaluation-pending",
                    receipt={
                        "attempt_index": attempt,
                        "candidate_gt_visible": False,
                        "judge_is_evolution_input": False,
                    },
                )
            if self.current_task_gt_supervision_enabled:
                gt = self.operations.current_task_gt_supervision(str(task))
                if (
                    not isinstance(gt, dict)
                    or gt.get("task_id") != task
                    or gt.get("feedback_class") != "HARD_GT"
                    or gt.get("judge_feedback_included") is not False
                    or not isinstance(gt.get("ground_truth_sha256"), str)
                ):
                    raise ValueError("current-task GT supervision authority is invalid")
                attachment = self._effect(
                    kind="feedback-attachment",
                    request={
                        "task_id": task,
                        "attempt_index": attempt,
                        "dataset_id": state["active_candidate_receipt"]["dataset_id"],
                        "dataset_revision": state["active_candidate_receipt"]["dataset_revision"],
                        "session_id": state["active_candidate_receipt"]["session_id"],
                        "core_task_id": state["active_candidate_receipt"]["core_task_id"],
                        "core_attempt_id": state["active_candidate_receipt"]["core_attempt_id"],
                        "successor_transition_id": state["active_candidate_receipt"].get(
                            "successor_transition_id"
                        ),
                        "candidate": state["active_candidate_receipt"],
                        "validation": state["active_validation_receipt"],
                        "feedback_source": "current_task_gt",
                        "gt_supervision": gt,
                        "authority": "evaluator_only",
                    },
                    execute=self.operations.attach_feedback,
                )
                if (
                    not attachment.get("attachment_id")
                    or not attachment.get("resolved_view_sha256")
                    or attachment.get("feedback_class") != "HARD_GT"
                    or attachment.get("feedback_source") != "current_task_gt"
                    or attachment.get("judge_calls") != 0
                    or attachment.get("ground_truth_sha256") != gt["ground_truth_sha256"]
                    or attachment.get("judge_feedback_included") is not False
                ):
                    raise ValueError("current-task GT attachment receipt is incomplete")
                return self._transition(
                    stage,
                    TrainingStage.ATTACHMENT_SEALED,
                    "current-task-gt-attachment-sealed",
                    updates={
                        "attachment_ids": [
                            *state["attachment_ids"],
                            attachment["attachment_id"],
                        ],
                        "active_attachment_receipt": attachment,
                        "current_task_local_overlay_id": attachment.get("task_local_overlay_id"),
                        "current_task_local_overlay_scope_id": attachment.get(
                            "task_local_overlay_scope_id"
                        ),
                        "training_signal_status": "ATTACHED_FROM_CURRENT_TASK_GT",
                        "judge_feedback_included": False,
                    },
                    receipt=attachment,
                )
            return self._transition(stage, TrainingStage.EVALUATION_PENDING, "evaluation-pending")
        if stage is TrainingStage.EVALUATION_PENDING:
            if not self._runtime_budget_available():
                return self._budget_exhausted(
                    stage=stage,
                    task=str(task),
                    attempt=attempt,
                    category="cumulative_runtime_seconds",
                )
            try:
                self._reserve_budget(
                    task=str(task),
                    attempt=attempt,
                    operation="evaluation",
                    category="judge_operations",
                    units=1,
                    limit=self.budget_policy.max_judge_operations,
                )
            except ValueError as exc:
                if "budget exhausted" not in str(exc):
                    raise
                return self._budget_exhausted(
                    stage=stage,
                    task=str(task),
                    attempt=attempt,
                    category="judge_operations",
                )
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
                "validator_completeness": state["active_validation_receipt"].get(
                    "completeness", 0
                ),
                "score": float(evaluation["total_score"]),
                "score_status": "SCORED",
                "evaluator_status": "COMPLETED",
                "runtime_seconds": state["active_candidate_receipt"]["runtime_seconds"],
                "cost_total_usd": state["active_candidate_receipt"].get("cost_total_usd"),
                "composite_size_bytes": state["active_candidate_receipt"].get(
                    "composite_size_bytes", 0
                ),
                "output_artifact_hash": state["active_validation_receipt"].get(
                    "artifact_root_sha256"
                ),
                "core_project_id": state["active_candidate_receipt"].get("core_project_id"),
                "core_task_id": state["active_candidate_receipt"].get("core_task_id"),
                "core_attempt_id": state["active_candidate_receipt"].get("core_attempt_id"),
                "task_request_id": state["active_candidate_receipt"].get("task_request_id"),
                "session_result_id": state["active_candidate_receipt"].get("session_result_id"),
                "candidate_output_root": state["active_candidate_receipt"].get(
                    "candidate_output_root"
                ),
                "input_project_head_id": state["active_candidate_receipt"].get(
                    "input_project_head_id"
                ),
                "runtime_injection": state["active_candidate_receipt"].get("runtime_injection"),
                "evaluation_receipt_id": evaluation.get("evaluation_receipt_id"),
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
            if self.per_item_reset_enabled:
                if attempt == 1:
                    records = self.store.attempts_for_task(self.experiment_id, str(task))
                    if [item.get("attempt_index") for item in records] != [0, 1]:
                        raise ValueError("per-item pair requires exactly baseline and evolved")
                    baseline, evolved = records
                    baseline_runtime = baseline.get("runtime_injection")
                    evolved_runtime = evolved.get("runtime_injection")
                    evolved_receipt = (
                        evolved_runtime.get("runtime_injection_receipt")
                        if isinstance(evolved_runtime, dict)
                        else None
                    )
                    evolved_artifacts = (
                        evolved_receipt.get("artifacts")
                        if isinstance(evolved_receipt, dict)
                        else None
                    )
                    if (
                        not isinstance(baseline_runtime, dict)
                        or baseline_runtime.get("artifact_count") != 0
                        or baseline_runtime.get("artifact_ids") != []
                        or not isinstance(evolved_runtime, dict)
                        or evolved_runtime.get("artifact_count") != 3
                        or set(evolved_runtime.get("artifact_ids", []))
                        != set(state.get("active_artifact_ids", []))
                        or not isinstance(evolved_artifacts, list)
                        or len(evolved_artifacts) != 3
                        or {
                            item.get("artifact_type")
                            for item in evolved_artifacts
                            if isinstance(item, dict)
                        }
                        != set(ARTIFACT_TYPES)
                    ):
                        raise ValueError("per-item Candidate artifact consumption is incomplete")
                    distinct = {
                        "session": baseline.get("session_id") != evolved.get("session_id"),
                        "core_task": baseline.get("core_task_id") != evolved.get("core_task_id"),
                        "core_attempt": baseline.get("core_attempt_id")
                        != evolved.get("core_attempt_id"),
                        "workspace": baseline.get("candidate_output_root")
                        != evolved.get("candidate_output_root"),
                    }
                    if not all(distinct.values()):
                        raise ValueError(
                            "per-item baseline and evolved Candidate identities overlap"
                        )
                    pair = {
                        "task_id": task,
                        "baseline": baseline,
                        "evolved": evolved,
                        "baseline_score": float(baseline["score"]),
                        "evolved_score": float(evolved["score"]),
                        "delta": float(evolved["score"]) - float(baseline["score"]),
                        "candidate_identity_distinct": distinct,
                        "one_evolution_cycle": True,
                        "judge_feedback_in_evolution": False,
                        "judge_reasoning_in_evolution": False,
                        "sanitized_evaluation_feedback_in_evolution": True,
                        "raw_gt_in_evolution_prompt": False,
                        "gt_visible_to_baseline": False,
                        "gt_visible_to_evolved": False,
                        "artifact_consumption": {
                            "artifact_read_requested": True,
                            "artifact_ids": evolved_runtime["artifact_ids"],
                            "artifact_sha256": {
                                item["artifact_type"]: item["content_sha256"]
                                for item in evolved_artifacts
                            },
                        },
                    }
                    return self._transition(
                        stage,
                        TrainingStage.ITEM_CLOSED,
                        "per-item-pair-closed",
                        updates={"paired_result": pair},
                        receipt=pair,
                    )
                if attempt != 0:
                    raise ValueError("per-item protocol permits only two Candidate passes")
                return self._transition(
                    stage,
                    TrainingStage.FEEDBACK_PROJECTION_PENDING,
                    "sanitized-evaluation-feedback-projection-pending",
                    receipt={
                        "task_id": task,
                        "evaluation_receipt_id": state["active_evaluation_receipt"].get(
                            "evaluation_receipt_id"
                        ),
                        "evaluation_frozen": True,
                        "openevo_state_mutations": 0,
                    },
                )
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
                    "dataset_revision": state["active_candidate_receipt"]["dataset_revision"],
                    "session_id": state["active_candidate_receipt"]["session_id"],
                    "core_task_id": state["active_candidate_receipt"]["core_task_id"],
                    "core_attempt_id": state["active_candidate_receipt"]["core_attempt_id"],
                    "successor_transition_id": state["active_candidate_receipt"].get(
                        "successor_transition_id"
                    ),
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
                    "current_task_local_overlay_scope_id": attachment.get(
                        "task_local_overlay_scope_id"
                    ),
                },
                receipt=attachment,
            )
        if stage is TrainingStage.FEEDBACK_PROJECTION_PENDING:
            if not self.per_item_reset_enabled or attempt != 0:
                raise ValueError("feedback projection is outside baseline per-item evaluation")
            gt = self.operations.current_task_gt_supervision(str(task))
            if (
                not isinstance(gt, dict)
                or gt.get("task_id") != task
                or gt.get("feedback_class") != "HARD_GT"
                or gt.get("judge_feedback_included") is not False
                or not isinstance(gt.get("ground_truth_sha256"), str)
            ):
                raise ValueError("current-task GT supervision authority is invalid")
            projection = self._effect(
                kind="feedback-projection",
                request={
                    "task_id": task,
                    "attempt_index": attempt,
                    "candidate": state["active_candidate_receipt"],
                    "validation": state["active_validation_receipt"],
                    "evaluation": state["active_evaluation_receipt"],
                    "gt_supervision": gt,
                    "authority": "evaluator_private_projection_only",
                },
                execute=self.operations.project_feedback,
            )
            admission = projection.get("admission")
            quality = projection.get("quality_contract")
            if (
                projection.get("task_id") != task
                or projection.get("ground_truth_sha256") != gt["ground_truth_sha256"]
                or projection.get("feedback_projector_model_calls") != 0
                or projection.get("openevo_state_mutations") != 0
                or projection.get("evaluation_frozen") is not True
                or projection.get("raw_gt_projected") is not False
                or projection.get("judge_reasoning_projected") is not False
                or projection.get("target_image_projected") is not False
                or not isinstance(admission, dict)
                or admission.get("status") != "ADMITTED"
                or not isinstance(quality, dict)
                or quality.get("generic_only_advice") is not False
                or quality.get("candidate_specific_references_present") is not True
                or quality.get("preserve_strength_guidance_present") is not True
                or quality.get("targeted_improvement_guidance_present") is not True
                or quality.get("gt_leakage") is not False
            ):
                raise ValueError("sanitized evaluation feedback admission is incomplete")
            return self._transition(
                stage,
                TrainingStage.FEEDBACK_ADMITTED,
                "sanitized-evaluation-feedback-admitted",
                updates={"active_feedback_projection_receipt": projection},
                receipt=projection,
            )
        if stage is TrainingStage.FEEDBACK_ADMITTED:
            if not self.per_item_reset_enabled or attempt != 0:
                raise ValueError("admitted feedback is outside baseline per-item evolution")
            gt = self.operations.current_task_gt_supervision(str(task))
            projection = state.get("active_feedback_projection_receipt")
            if (
                not isinstance(gt, dict)
                or not isinstance(projection, dict)
                or projection.get("ground_truth_sha256") != gt.get("ground_truth_sha256")
            ):
                raise ValueError("sanitized feedback and GT authority binding differs")
            attachment = self._effect(
                kind="feedback-attachment",
                request={
                    "task_id": task,
                    "attempt_index": attempt,
                    "dataset_id": state["active_candidate_receipt"]["dataset_id"],
                    "dataset_revision": state["active_candidate_receipt"]["dataset_revision"],
                    "session_id": state["active_candidate_receipt"]["session_id"],
                    "core_task_id": state["active_candidate_receipt"]["core_task_id"],
                    "core_attempt_id": state["active_candidate_receipt"]["core_attempt_id"],
                    "successor_transition_id": state["active_candidate_receipt"].get(
                        "successor_transition_id"
                    ),
                    "candidate": state["active_candidate_receipt"],
                    "validation": state["active_validation_receipt"],
                    "feedback_source": "sanitized_evaluation_feedback_v1",
                    "gt_supervision": gt,
                    "feedback_projection": projection,
                    "authority": "evaluator_sanitized_projection_only",
                    "judge_feedback": None,
                },
                execute=self.operations.attach_feedback,
            )
            if (
                not attachment.get("attachment_id")
                or not attachment.get("resolved_view_sha256")
                or attachment.get("feedback_class") != "HARD_GT"
                or attachment.get("feedback_source") != "sanitized_evaluation_feedback_v1"
                or attachment.get("judge_calls") != 0
                or attachment.get("ground_truth_sha256") != gt["ground_truth_sha256"]
                or attachment.get("sanitized_feedback_sha256")
                != projection.get("sanitized_feedback_sha256")
                or attachment.get("sanitized_feedback_included") is not True
                or attachment.get("judge_feedback_included") is not False
                or attachment.get("raw_gt_projected") is not False
                or attachment.get("judge_reasoning_projected") is not False
                or attachment.get("target_image_projected") is not False
            ):
                raise ValueError("sanitized evaluation feedback attachment is incomplete")
            return self._transition(
                stage,
                TrainingStage.ATTACHMENT_SEALED,
                "sanitized-evaluation-feedback-attachment-sealed",
                updates={
                    "attachment_ids": [
                        *state["attachment_ids"],
                        attachment["attachment_id"],
                    ],
                    "active_attachment_receipt": attachment,
                    "current_task_local_overlay_id": None,
                    "current_task_local_overlay_scope_id": None,
                    "training_signal_status": ("ATTACHED_FROM_SANITIZED_EVALUATION_FEEDBACK"),
                    "judge_feedback_included": False,
                },
                receipt={
                    **attachment,
                    "candidate_overlay_activated": False,
                    "raw_evaluation_consumed_by_core": False,
                },
            )
        if stage is TrainingStage.ATTACHMENT_SEALED:
            return self._transition(stage, TrainingStage.EVOLUTION_PENDING, "evolution-pending")
        if stage is TrainingStage.EVOLUTION_PENDING:
            if not evolution_allowed(stage):
                raise ValueError("evolution is disabled after final freeze")
            if self.per_item_reset_enabled and (
                attempt != 0
                or state.get("judge_feedback_included") is not False
                or state.get("current_task_local_overlay_id") is not None
                or state.get("current_task_local_overlay_scope_id") is not None
                or state.get("active_attachment_receipt", {}).get("judge_feedback_included")
                is not False
                or state.get("active_attachment_receipt", {}).get("feedback_source")
                != "sanitized_evaluation_feedback_v1"
                or state.get("active_attachment_receipt", {}).get("sanitized_feedback_included")
                is not True
                or state.get("active_attachment_receipt", {}).get("raw_gt_projected") is not False
                or state.get("active_attachment_receipt", {}).get("judge_reasoning_projected")
                is not False
            ):
                raise ValueError("per-item evolution input boundary is invalid")
            evolution_request = {
                "task_id": task,
                "attempt_index": attempt,
                "attachment": state["active_attachment_receipt"],
                "parent_composite_id": state["current_composite_id"],
                "artifact_types": list(ARTIFACT_TYPES),
            }
            try:
                reflector_readiness = self.operations.evolution_readiness(evolution_request)
            except RuntimeError as exc:
                raise TrainingPaused("REFLECTOR_CREDENTIAL_MOUNT_NOT_READY") from exc
            required_mount = {
                "authority_issued",
                "docker_mount_created",
                "container_path_visible",
                "container_user_can_read",
                "generation_matches",
                "release_identity_matches",
                "adoption_receipt_valid",
                "cleanup_verified",
            }
            if (
                not isinstance(reflector_readiness, dict)
                or reflector_readiness.get("ready") is not True
                or any(reflector_readiness.get(field) is not True for field in required_mount)
                or reflector_readiness.get("identity_matches") is not True
                or reflector_readiness.get("secret_recorded") is not False
                or reflector_readiness.get("codex_cli_started") is not False
                or reflector_readiness.get("model_started") is not False
            ):
                raise TrainingPaused("REFLECTOR_CREDENTIAL_MOUNT_NOT_READY")
            if not self._runtime_budget_available():
                return self._budget_exhausted(
                    stage=stage,
                    task=str(task),
                    attempt=attempt,
                    category="cumulative_runtime_seconds",
                )
            try:
                reflector_budget = self._reserve_budget(
                    task=str(task),
                    attempt=attempt,
                    operation="evolution",
                    category="reflector_model_calls",
                    units=self.budget_policy.reflector_calls_per_cycle,
                    limit=self.budget_policy.max_reflector_model_calls,
                )
            except ValueError as exc:
                if "budget exhausted" not in str(exc):
                    raise
                return self._budget_exhausted(
                    stage=stage,
                    task=str(task),
                    attempt=attempt,
                    category="reflector_model_calls",
                )
            self.store.plan_side_effect(
                experiment_id=self.experiment_id,
                idempotency_key=f"{self.experiment_id}:{task}:a{attempt}:evolution",
                kind="evolution",
                request=evolution_request,
            )
            return self._transition(
                stage,
                TrainingStage.EVOLUTION_RUNNING,
                "evolution-start",
                updates={"active_reflector_mount_readiness": reflector_readiness},
                receipt={
                    "managed_reflector_credential_mount": reflector_readiness,
                    "secret_recorded": False,
                    "codex_cli_started": False,
                    "model_started": False,
                    "budget_reservation": reflector_budget,
                },
            )
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
            if (
                not admitted.get("composite_id")
                or len(admitted.get("registry_artifact_ids", [])) != 3
            ):
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
            if self.per_item_reset_enabled:
                prepared = self._effect(
                    kind="per-item-evolved-workspace",
                    request={
                        "task_id": task,
                        "attempt_index": attempt,
                        "source_core_project_id": state["core_project_id"],
                        "source_candidate": state["active_candidate_receipt"],
                        "admitted_composite": state["active_admission_receipt"],
                        "ground_truth_sha256": state["active_attachment_receipt"].get(
                            "ground_truth_sha256"
                        ),
                        "judge_feedback": None,
                        "fresh_workspace": True,
                        "same_task_only": True,
                    },
                    execute=self.operations.prepare_evolved_workspace,
                )
                required_true = {
                    "same_task_only",
                    "fresh_generation_zero_destination",
                    "candidate_started",
                    "task_created",
                    "raw_gt_carried",
                    "judge_feedback_carried",
                    "cross_task_inheritance",
                }
                expected = {
                    "same_task_only": True,
                    "fresh_generation_zero_destination": True,
                    "candidate_started": False,
                    "task_created": False,
                    "raw_gt_carried": False,
                    "judge_feedback_carried": False,
                    "cross_task_inheritance": False,
                }
                if (
                    any(prepared.get(key) is not value for key, value in expected.items())
                    or set(expected) != required_true
                    or prepared.get("task_id") != task
                    or not isinstance(prepared.get("core_project_id"), str)
                    or not isinstance(prepared.get("preseeded_workspace_authority"), dict)
                    or len(prepared.get("artifact_ids", [])) != 3
                    or set(prepared.get("artifact_ids", []))
                    != set(state["active_admission_receipt"].get("registry_artifact_ids", []))
                    or prepared.get("recovery_command_used") is not False
                    or prepared.get("recovery_path_used") is not False
                ):
                    raise ValueError("per-item evolved workspace authority is incomplete")
                return self._transition(
                    stage,
                    TrainingStage.EVOLVED_WORKSPACE_PREPARED,
                    "per-item-evolved-workspace-prepared",
                    updates={
                        "current_composite_id": prepared["composite_id"],
                        "core_project_id": prepared["core_project_id"],
                        "preseeded_workspace_authority": prepared["preseeded_workspace_authority"],
                        "active_artifact_ids": prepared["artifact_ids"],
                        "current_task_local_overlay_id": None,
                        "current_task_local_overlay_scope_id": None,
                        "active_evolved_workspace_receipt": prepared,
                    },
                    receipt=prepared,
                )
            return self._transition(
                stage,
                TrainingStage.NEXT_ATTEMPT_READY,
                "next-attempt-prepared",
                updates={
                    "current_composite_id": state["active_admission_receipt"]["composite_id"]
                },
            )
        if stage is TrainingStage.EVOLVED_WORKSPACE_PREPARED:
            if not self.per_item_reset_enabled or attempt != 0:
                raise ValueError("evolved workspace stage is outside per-item baseline")
            return self._transition(
                stage,
                TrainingStage.TASK_ATTEMPT_READY,
                "per-item-fresh-evolved-candidate-ready",
                updates={"current_attempt": 1},
                receipt={
                    "fresh_codex_required": True,
                    "fresh_core_task_required": True,
                    "fresh_workspace_required": True,
                    "raw_gt_visible": False,
                    "artifact_ids": state["active_artifact_ids"],
                },
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
            try:
                best = _select_best(records)
            except ValueError:
                if not self.validator_failure_learning_enabled:
                    raise
                if any(item.get("artifact_valid") is True for item in records):
                    raise
                if len(self.task_ids) > 1:
                    # A quality-invalid attempt is still consumed and its first
                    # two feedback cycles have already evolved the global
                    # composite.  A full Community plan must close this task
                    # explicitly and carry the latest evolved input forward;
                    # the dedicated one-task validator-learning workflow keeps
                    # its historical terminal behavior below.
                    best = _select_latest_invalid_for_continuation(records)
                else:
                    destroyed = self._effect(
                        kind="task-local-destroy",
                        request={
                            "task_id": task,
                            "overlay_id": state["current_task_local_overlay_id"],
                        },
                        execute=self.operations.destroy_task_local,
                    )
                    if destroyed.get("destroyed") is not True:
                        raise ValueError("task-local overlay destruction is unproven")
                    sanitized = self._effect(
                        kind="cross-task-sanitize",
                        request={
                            "task_id": task,
                            "composite_id": state["current_composite_id"],
                            "core_project_id": state["core_project_id"],
                            "final_candidate": state["active_candidate_receipt"],
                        },
                        execute=self.operations.sanitize_composite,
                    )
                    if sanitized.get("passed") is not True:
                        raise ValueError("terminal task sanitizer failed closed")
                    return self._transition(
                        stage,
                        TrainingStage.TASK_NO_VALID_ATTEMPT,
                        "task-no-valid-attempt",
                        updates={
                            "failure_reason": "TASK_NO_VALID_ATTEMPT",
                            "current_task_local_overlay_id": None,
                            "current_task_local_overlay_scope_id": None,
                            "current_composite_id": sanitized["composite_id"],
                        },
                        receipt={
                            "task_id": task,
                            "attempts": records,
                            "selection": "TASK_NO_VALID_ATTEMPT",
                            "task_local_destroy": destroyed,
                            "cross_task_sanitizer": sanitized,
                        },
                    )
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
                updates={
                    "current_task_local_overlay_id": None,
                    "current_task_local_overlay_scope_id": None,
                },
                receipt=destroyed,
            )
        if stage is TrainingStage.TASK_LOCAL_DESTROYED:
            next_task_id = (
                None
                if state["current_task_index"] + 1 == len(self.task_ids)
                else self.task_ids[state["current_task_index"] + 1]
            )
            sanitized = self._effect(
                kind="cross-task-sanitize",
                request={
                    "task_id": task,
                    "next_task_id": next_task_id,
                    "composite_id": state["current_composite_id"],
                    "core_project_id": state["core_project_id"],
                    "final_candidate": state["active_candidate_receipt"],
                },
                execute=self.operations.sanitize_composite,
            )
            if sanitized.get("passed") is not True:
                raise ValueError("cross-task sanitizer failed closed")
            return self._transition(
                stage,
                TrainingStage.CROSS_TASK_SANITIZED,
                "cross-task-sanitized",
                updates={
                    "current_composite_id": sanitized["composite_id"],
                    "core_project_id": sanitized.get("core_project_id", state["core_project_id"]),
                },
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
                "protocol_sha256": self.identity.protocol_sha256,
                "core_identity_sha256": self.identity.core_identity_sha256,
                "adapter_identity_sha256": self.identity.adapter_identity_sha256,
                "active_task_local_overlay": state["current_task_local_overlay_id"],
                "active_task_local_overlay_scope": state["current_task_local_overlay_scope_id"],
            }
            if (
                sum(
                    len(self.store.attempts_for_task(self.experiment_id, item))
                    for item in self.task_ids
                )
                != expected_attempts
                or len(state["evolution_job_ids"]) != expected_cycles * 3
                or state["current_task_local_overlay_id"] is not None
                or state["current_task_local_overlay_scope_id"] is not None
            ):
                raise ValueError("final freeze inventory is incomplete")
            formal_verify = self.verify()
            if (
                formal_verify["pending_side_effects"] != 0
                or formal_verify["completed_reflector_cycles"] != expected_cycles
                or formal_verify["evolution_job_ids"] != expected_cycles * 3
                or formal_verify["task_best_count"] != len(self.task_ids)
            ):
                raise ValueError("final freeze consistency inventory is incomplete")
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
        if stage is TrainingStage.ITEM_CLOSED:
            if not self.per_item_reset_enabled:
                raise ValueError("item close is outside per-item protocol")
            archived = list(state.get("active_artifact_ids", []))
            if len(archived) != 3 or not isinstance(state.get("paired_result"), dict):
                raise ValueError("per-item close lacks pair or three artifacts")
            reset = {
                "task_id": task,
                "archive_preserved": True,
                "archived_artifact_ids": archived,
                "active_artifact_ids": [],
                "active_successor_head": None,
                "active_project_head": None,
                "active_candidate_session": None,
                "active_reflector_session": None,
                "active_workspace": None,
                "active_gt_supervision": None,
                "active_evaluation_feedback": None,
                "cross_project_fork_to_next_task": False,
            }
            return self._transition(
                stage,
                TrainingStage.ITEM_RESET,
                "per-item-active-state-reset",
                updates={
                    "current_task_index": len(self.task_ids),
                    "current_composite_id": None,
                    "core_project_id": None,
                    "preseeded_workspace_authority": None,
                    "current_task_local_overlay_id": None,
                    "current_task_local_overlay_scope_id": None,
                    "active_artifact_ids": [],
                    "archived_artifact_ids": archived,
                    "active_candidate_intent": None,
                    "active_candidate_receipt": None,
                    "active_validation_receipt": None,
                    "active_evaluation_receipt": None,
                    "active_feedback_projection_receipt": None,
                    "active_attachment_receipt": None,
                    "active_evolution_receipt": None,
                    "active_admission_receipt": None,
                    "active_evolved_workspace_receipt": None,
                    "item_reset_receipt": reset,
                },
                receipt=reset,
            )
        raise ValueError(f"unhandled training stage: {stage.value}")

    def run_until_pause(self, *, max_transitions: int = 10_000) -> dict[str, Any]:
        for _ in range(max_transitions):
            state = self.status()
            if state["stage"] in {
                TrainingStage.FINAL_FROZEN.value,
                TrainingStage.ITEM_RESET.value,
            }:
                return state
            self.run_next()
        raise ValueError("training transition budget was exhausted")


def _select_best(records: list[dict[str, Any]]) -> dict[str, Any]:
    if len(records) != 3:
        raise ValueError("best-of-three requires exactly three sealed attempts")
    scored = [
        item
        for item in records
        if type(item.get("score")) in {int, float}
        and item.get("artifact_valid") is True
        and item.get("completed") is True
    ]
    if scored:
        return max(
            scored,
            key=lambda item: (
                float(item["score"]),
                int(item.get("validator_completeness", 0)),
                -float(item.get("runtime_seconds", float("inf"))),
                -float(item.get("cost_total_usd") or 0.0),
                -int(item.get("composite_size_bytes", 0)),
                -int(item["attempt_index"]),
            ),
        )
    unscored = [
        item
        for item in records
        if item.get("score") is None
        and item.get("artifact_valid") is True
        and item.get("completed") is True
    ]
    if not unscored:
        raise ValueError("best composite cannot be determined")
    selected = max(
        unscored,
        key=lambda item: (
            int(item.get("validator_completeness", 0)),
            -float(item.get("runtime_seconds", float("inf"))),
            -float(item.get("cost_total_usd") or 0.0),
            -int(item.get("composite_size_bytes", 0)),
            -int(item["attempt_index"]),
        ),
    )
    return {**selected, "selection_status": "UNSCORED_VALID_ARTIFACT"}


def _select_latest_invalid_for_continuation(
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Close an all-invalid task without discarding its learned composite."""

    if len(records) != 3:
        raise ValueError("all-invalid closeout requires exactly three attempts")
    if any(
        item.get("completed") is not True
        or item.get("artifact_valid") is not False
        or item.get("score") is not None
        for item in records
    ):
        raise ValueError("all-invalid closeout authority is inconsistent")
    selected = max(records, key=lambda item: int(item["attempt_index"]))
    if int(selected["attempt_index"]) != 2:
        raise ValueError("all-invalid closeout lacks the final attempt")
    return {
        **selected,
        "selection_status": "NO_VALID_ARTIFACT_LATEST_EVOLVED",
        "best_of_three_status": "NO_VALID_ARTIFACT",
        "selected_for_training_continuation": True,
        "judge_skipped": True,
    }


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
    production_ports = root / "production_operation_ports.py"
    required_operations = {
        "reconcile_candidate_authority",
        "create_candidate_workspace",
        "start_candidate",
        "recover_candidate",
        "validate_candidate_artifacts",
        "evaluate_candidate",
        "create_feedback_attachment",
        "prepare_successor",
        "collect_artifact_jobs",
        "construct_composite",
        "sanitize_cross_task",
        "freeze_final_artifact",
    }
    production_operations_bound = False
    production_operation_methods: list[str] = []
    if production_driver.is_file() and production_ports.is_file():
        from .production_training_operations import ProductionTrainingOperations

        production_operation_methods = sorted(
            item
            for item in required_operations
            if callable(getattr(ProductionTrainingOperations, item, None))
        )
        port_source = production_ports.read_text(encoding="utf-8")
        production_operations_bound = (
            set(production_operation_methods) == required_operations
            and "CoreV2CandidatePort" in port_source
            and "CoreV2CandidateReconciliationPort" in port_source
            and "DurableCommunityEvaluatorPort" in port_source
            and "CoreFeedbackPort" in port_source
            and "CoreSuccessorPort" in port_source
            and "historical-restores" in port_source
            and "register_restored" in port_source
            and "build_production_ports" in port_source
        )
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
        "production_operation_methods": production_operation_methods,
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
    "TrainingBudgetPolicy",
    "TrainingOperations",
    "TrainingPaused",
    "official_frozen_run_plan",
    "supervisor_capability_audit",
]
