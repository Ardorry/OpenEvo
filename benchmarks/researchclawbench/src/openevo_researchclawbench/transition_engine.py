"""Closed transition graph for durable Community training orchestration."""

from __future__ import annotations

from enum import StrEnum


class TrainingStage(StrEnum):
    INITIALIZED = "INITIALIZED"
    RECOVERY_SEEDED_CONTINUATION = "RECOVERY_SEEDED_CONTINUATION"
    CANDIDATE_SEALED_RECONCILED = "CANDIDATE_SEALED_RECONCILED"
    TASK_ATTEMPT_READY = "TASK_ATTEMPT_READY"
    CANDIDATE_RUNNING = "CANDIDATE_RUNNING"
    CANDIDATE_SETUP_BLOCKED = "CANDIDATE_SETUP_BLOCKED"
    CANDIDATE_SEALED = "CANDIDATE_SEALED"
    VALIDATOR_FEEDBACK_PENDING = "VALIDATOR_FEEDBACK_PENDING"
    ARTIFACT_VALIDATED = "ARTIFACT_VALIDATED"
    EVALUATION_PENDING = "EVALUATION_PENDING"
    EVALUATED = "EVALUATED"
    ATTACHMENT_SEALED = "ATTACHMENT_SEALED"
    EVOLUTION_PENDING = "EVOLUTION_PENDING"
    EVOLUTION_RUNNING = "EVOLUTION_RUNNING"
    EVOLUTION_COMPLETED = "EVOLUTION_COMPLETED"
    COMPOSITE_ADMITTED = "COMPOSITE_ADMITTED"
    EVOLVED_WORKSPACE_PREPARED = "EVOLVED_WORKSPACE_PREPARED"
    NEXT_ATTEMPT_READY = "NEXT_ATTEMPT_READY"
    TASK_SELECTION_PENDING = "TASK_SELECTION_PENDING"
    TASK_BEST_SELECTED = "TASK_BEST_SELECTED"
    TASK_NO_VALID_ATTEMPT = "TASK_NO_VALID_ATTEMPT"
    TASK_LOCAL_DESTROYED = "TASK_LOCAL_DESTROYED"
    CROSS_TASK_SANITIZED = "CROSS_TASK_SANITIZED"
    NEXT_TASK_READY = "NEXT_TASK_READY"
    COMMUNITY_TRAINING_COMPLETE = "COMMUNITY_TRAINING_COMPLETE"
    FINAL_FREEZE_PENDING = "FINAL_FREEZE_PENDING"
    FINAL_FROZEN = "FINAL_FROZEN"
    ITEM_CLOSED = "ITEM_CLOSED"
    ITEM_RESET = "ITEM_RESET"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


_ALLOWED: dict[TrainingStage, frozenset[TrainingStage]] = {
    TrainingStage.INITIALIZED: frozenset(
        {
            TrainingStage.TASK_ATTEMPT_READY,
            TrainingStage.NEXT_ATTEMPT_READY,
            TrainingStage.CANDIDATE_SEALED_RECONCILED,
            TrainingStage.VALIDATOR_FEEDBACK_PENDING,
            TrainingStage.RECOVERY_SEEDED_CONTINUATION,
        }
    ),
    TrainingStage.RECOVERY_SEEDED_CONTINUATION: frozenset(
        {TrainingStage.NEXT_ATTEMPT_READY}
    ),
    TrainingStage.CANDIDATE_SEALED_RECONCILED: frozenset(
        {TrainingStage.CANDIDATE_SEALED}
    ),
    TrainingStage.TASK_ATTEMPT_READY: frozenset(
        {
            TrainingStage.CANDIDATE_RUNNING,
            TrainingStage.BUDGET_EXHAUSTED,
            TrainingStage.BLOCKED,
            TrainingStage.FAILED,
        }
    ),
    TrainingStage.CANDIDATE_RUNNING: frozenset(
        {
            TrainingStage.CANDIDATE_SEALED,
            TrainingStage.CANDIDATE_SETUP_BLOCKED,
            TrainingStage.BLOCKED,
            TrainingStage.FAILED,
        }
    ),
    TrainingStage.CANDIDATE_SETUP_BLOCKED: frozenset(),
    TrainingStage.CANDIDATE_SEALED: frozenset(
        {
            TrainingStage.ARTIFACT_VALIDATED,
            TrainingStage.VALIDATOR_FEEDBACK_PENDING,
            TrainingStage.TASK_SELECTION_PENDING,
            TrainingStage.FAILED,
        }
    ),
    TrainingStage.VALIDATOR_FEEDBACK_PENDING: frozenset(
        {TrainingStage.ATTACHMENT_SEALED}
    ),
    TrainingStage.ARTIFACT_VALIDATED: frozenset(
        {
            TrainingStage.EVALUATION_PENDING,
            TrainingStage.ATTACHMENT_SEALED,
            TrainingStage.FAILED,
        }
    ),
    TrainingStage.EVALUATION_PENDING: frozenset(
        {
            TrainingStage.EVALUATED,
            TrainingStage.BUDGET_EXHAUSTED,
            TrainingStage.BLOCKED,
            TrainingStage.FAILED,
        }
    ),
    TrainingStage.EVALUATED: frozenset(
        {
            TrainingStage.ATTACHMENT_SEALED,
            TrainingStage.TASK_SELECTION_PENDING,
            TrainingStage.ITEM_CLOSED,
        }
    ),
    TrainingStage.ATTACHMENT_SEALED: frozenset(
        {TrainingStage.EVOLUTION_PENDING, TrainingStage.TASK_SELECTION_PENDING}
    ),
    TrainingStage.EVOLUTION_PENDING: frozenset(
        {TrainingStage.EVOLUTION_RUNNING, TrainingStage.BUDGET_EXHAUSTED}
    ),
    TrainingStage.EVOLUTION_RUNNING: frozenset(
        {TrainingStage.EVOLUTION_COMPLETED, TrainingStage.BLOCKED, TrainingStage.FAILED}
    ),
    TrainingStage.EVOLUTION_COMPLETED: frozenset({TrainingStage.COMPOSITE_ADMITTED}),
    TrainingStage.COMPOSITE_ADMITTED: frozenset(
        {
            TrainingStage.NEXT_ATTEMPT_READY,
            TrainingStage.EVOLVED_WORKSPACE_PREPARED,
        }
    ),
    TrainingStage.EVOLVED_WORKSPACE_PREPARED: frozenset(
        {TrainingStage.TASK_ATTEMPT_READY}
    ),
    TrainingStage.NEXT_ATTEMPT_READY: frozenset({TrainingStage.TASK_ATTEMPT_READY}),
    TrainingStage.TASK_SELECTION_PENDING: frozenset(
        {TrainingStage.TASK_BEST_SELECTED, TrainingStage.TASK_NO_VALID_ATTEMPT}
    ),
    TrainingStage.TASK_BEST_SELECTED: frozenset({TrainingStage.TASK_LOCAL_DESTROYED}),
    TrainingStage.TASK_NO_VALID_ATTEMPT: frozenset(),
    TrainingStage.TASK_LOCAL_DESTROYED: frozenset({TrainingStage.CROSS_TASK_SANITIZED}),
    TrainingStage.CROSS_TASK_SANITIZED: frozenset(
        {TrainingStage.NEXT_TASK_READY, TrainingStage.COMMUNITY_TRAINING_COMPLETE}
    ),
    TrainingStage.NEXT_TASK_READY: frozenset({TrainingStage.TASK_ATTEMPT_READY}),
    TrainingStage.COMMUNITY_TRAINING_COMPLETE: frozenset(
        {TrainingStage.FINAL_FREEZE_PENDING}
    ),
    TrainingStage.FINAL_FREEZE_PENDING: frozenset(
        {TrainingStage.FINAL_FROZEN, TrainingStage.BLOCKED, TrainingStage.FAILED}
    ),
    TrainingStage.FINAL_FROZEN: frozenset(),
    TrainingStage.ITEM_CLOSED: frozenset({TrainingStage.ITEM_RESET}),
    TrainingStage.ITEM_RESET: frozenset(),
    TrainingStage.BUDGET_EXHAUSTED: frozenset(),
    TrainingStage.BLOCKED: frozenset(),
    TrainingStage.FAILED: frozenset(),
}


def require_transition(source: TrainingStage, target: TrainingStage) -> None:
    source = TrainingStage(source)
    target = TrainingStage(target)
    if target not in _ALLOWED[source]:
        raise ValueError(f"invalid training transition: {source.value}->{target.value}")


def evolution_allowed(stage: TrainingStage) -> bool:
    return TrainingStage(stage) not in {
        TrainingStage.COMMUNITY_TRAINING_COMPLETE,
        TrainingStage.FINAL_FREEZE_PENDING,
        TrainingStage.FINAL_FROZEN,
        TrainingStage.ITEM_CLOSED,
        TrainingStage.ITEM_RESET,
        TrainingStage.BUDGET_EXHAUSTED,
        TrainingStage.CANDIDATE_SETUP_BLOCKED,
        TrainingStage.BLOCKED,
        TrainingStage.FAILED,
        TrainingStage.TASK_NO_VALID_ATTEMPT,
    }


__all__ = ["TrainingStage", "evolution_allowed", "require_transition"]
