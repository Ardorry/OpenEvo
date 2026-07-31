"""Pure state machine for sequential attempt-level reflector evolution."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Iterable

from .config import (
    ARTIFACT_TYPES,
    ATTEMPTS_PER_TASK,
    EXPECTED_ARTIFACT_EVOLUTION_REQUESTS,
    EXPECTED_CANDIDATE_RUNS,
    EXPECTED_REFLECTOR_CYCLES,
    FROZEN_TASKS,
    OFFICIAL_TASK_COUNT,
)


class EvolutionState(str, Enum):
    READY = "READY"
    ATTEMPT_COMPLETE = "ATTEMPT_COMPLETE"
    REFLECTION_PENDING = "REFLECTION_PENDING"
    ADMISSION_PENDING = "ADMISSION_PENDING"
    TASK_SELECTION_PENDING = "TASK_SELECTION_PENDING"
    SANITIZATION_PENDING = "SANITIZATION_PENDING"
    COMPLETE = "COMPLETE"
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class AttemptRecord:
    task_id: str
    attempt_index: int
    input_composite_revision: str
    agent_system_revision: str
    text_memory_revision: str
    skill_bundle_revision: str
    completed: bool
    artifact_valid: bool
    score: float | None
    runtime_seconds: int
    cost_total_usd: float
    output_artifact_hash: str
    reflector_round: int | None
    proposed_composite: str | None
    admission_status: str
    selected_as_task_best: bool = False
    rollback_reason: str | None = None
    validator_completeness: int = 0
    composite_size_bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReflectionProposal:
    source_task_id: str
    source_attempt_id: str
    parent_composite_revision: str
    observed_score: float
    best_score_on_current_task: float
    agent_system: dict[str, Any]
    text_memory: dict[str, Any]
    skill_bundle: dict[str, Any]
    expected_generalization: str
    task_specific_content_removed: tuple[str, ...]
    admission_required: bool

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ReflectionProposal":
        expected = {
            "source_task_id", "source_attempt_id", "parent_composite_revision",
            "observed_score", "best_score_on_current_task", "agent_system",
            "text_memory", "skill_bundle", "expected_generalization",
            "task_specific_content_removed", "admission_required",
        }
        if set(payload) != expected:
            raise ValueError("reflection proposal fields are not closed")
        for key in ("agent_system", "text_memory"):
            part = payload[key]
            if not isinstance(part, dict) or part.get("action") not in {"update", "keep", "reject"}:
                raise ValueError(f"invalid {key} proposal")
            if not isinstance(part.get("reason"), str) or not part.get("reason", "").strip():
                raise ValueError(f"{key} proposal lacks reason")
        skills = payload["skill_bundle"]
        if not isinstance(skills, dict) or skills.get("action") not in {"update", "keep", "reject"}:
            raise ValueError("invalid skill_bundle proposal")
        for key in ("add", "modify", "remove"):
            if not isinstance(skills.get(key), list):
                raise ValueError(f"skill_bundle.{key} must be a list")
        if payload["admission_required"] is not True:
            raise ValueError("every proposal requires admission")
        return cls(
            source_task_id=str(payload["source_task_id"]),
            source_attempt_id=str(payload["source_attempt_id"]),
            parent_composite_revision=str(payload["parent_composite_revision"]),
            observed_score=float(payload["observed_score"]),
            best_score_on_current_task=float(payload["best_score_on_current_task"]),
            agent_system=dict(payload["agent_system"]),
            text_memory=dict(payload["text_memory"]),
            skill_bundle=dict(payload["skill_bundle"]),
            expected_generalization=str(payload["expected_generalization"]),
            task_specific_content_removed=tuple(payload["task_specific_content_removed"]),
            admission_required=True,
        )


def select_best_attempt(records: Iterable[AttemptRecord]) -> AttemptRecord:
    candidates = [record for record in records if record.completed and record.artifact_valid and record.score is not None]
    if not candidates:
        raise ValueError("BEST_COMPOSITE_UNDETERMINED")
    # Higher score/completeness first; then lower runtime/cost/size; finally earlier attempt.
    return max(
        candidates,
        key=lambda item: (
            float(item.score),
            item.validator_completeness,
            -item.runtime_seconds,
            -item.cost_total_usd,
            -item.composite_size_bytes,
            -item.attempt_index,
        ),
    )


class SequentialEvolutionStateMachine:
    def __init__(self) -> None:
        self.tasks = FROZEN_TASKS
        self.task_index = 0
        self.attempt_index = 0
        self.state = EvolutionState.READY
        self.records: list[AttemptRecord] = []
        self.consecutive_admission_rejections = 0

    @property
    def current_task(self) -> str:
        return self.tasks[self.task_index]

    def record_attempt(self, record: AttemptRecord) -> None:
        if self.state is not EvolutionState.READY:
            raise RuntimeError("attempt cannot be recorded in current state")
        if record.task_id != self.current_task or record.attempt_index != self.attempt_index:
            raise ValueError("attempt identity does not match state machine")
        self.records.append(record)
        self.state = EvolutionState.ATTEMPT_COMPLETE
        if self.attempt_index < 2:
            self.state = EvolutionState.REFLECTION_PENDING
        else:
            self.state = EvolutionState.TASK_SELECTION_PENDING

    def record_reflection(self, proposal: ReflectionProposal) -> None:
        if self.state is not EvolutionState.REFLECTION_PENDING:
            raise RuntimeError("reflection is not pending")
        if proposal.source_task_id != self.current_task:
            raise ValueError("reflection task mismatch")
        self.state = EvolutionState.ADMISSION_PENDING

    def record_admission(self, accepted: bool) -> None:
        if self.state is not EvolutionState.ADMISSION_PENDING:
            raise RuntimeError("admission is not pending")
        self.consecutive_admission_rejections = 0 if accepted else self.consecutive_admission_rejections + 1
        if self.consecutive_admission_rejections >= 2:
            self.state = EvolutionState.STOPPED
            return
        self.attempt_index += 1
        self.state = EvolutionState.READY

    def select_task_best(self) -> AttemptRecord:
        if self.state is not EvolutionState.TASK_SELECTION_PENDING:
            raise RuntimeError("task selection is not pending")
        current = [record for record in self.records if record.task_id == self.current_task]
        best = select_best_attempt(current)
        self.state = EvolutionState.SANITIZATION_PENDING
        return best

    def record_sanitization(self, accepted: bool) -> None:
        if self.state is not EvolutionState.SANITIZATION_PENDING:
            raise RuntimeError("sanitization is not pending")
        if not accepted:
            self.state = EvolutionState.STOPPED
            return
        if self.task_index + 1 == len(self.tasks):
            self.state = EvolutionState.COMPLETE
            return
        self.task_index += 1
        self.attempt_index = 0
        self.state = EvolutionState.READY


@dataclass(frozen=True)
class ScheduledAttempt:
    task_id: str
    task_index: int
    attempt_index: int
    reflector_after: bool


@dataclass(frozen=True)
class ScheduledEvolutionRequest:
    task_id: str
    task_index: int
    reflector_round: int
    artifact_type: str


@dataclass(frozen=True)
class FrozenTrainingSchedule:
    attempts: tuple[ScheduledAttempt, ...]
    evolution_requests: tuple[ScheduledEvolutionRequest, ...]

    @classmethod
    def build(cls) -> "FrozenTrainingSchedule":
        attempts: list[ScheduledAttempt] = []
        requests: list[ScheduledEvolutionRequest] = []
        for task_index, task_id in enumerate(FROZEN_TASKS):
            for attempt_index in range(ATTEMPTS_PER_TASK):
                attempts.append(
                    ScheduledAttempt(
                        task_id=task_id,
                        task_index=task_index,
                        attempt_index=attempt_index,
                        reflector_after=attempt_index < ATTEMPTS_PER_TASK - 1,
                    )
                )
                if attempt_index < ATTEMPTS_PER_TASK - 1:
                    for artifact_type in ARTIFACT_TYPES:
                        requests.append(
                            ScheduledEvolutionRequest(
                                task_id=task_id,
                                task_index=task_index,
                                reflector_round=attempt_index,
                                artifact_type=artifact_type,
                            )
                        )
        schedule = cls(tuple(attempts), tuple(requests))
        schedule.validate()
        return schedule

    def validate(self) -> None:
        if len(self.attempts) != EXPECTED_CANDIDATE_RUNS:
            raise ValueError("frozen schedule does not contain 51 candidate attempts")
        cycles = {(item.task_id, item.reflector_round) for item in self.evolution_requests}
        if len(cycles) != EXPECTED_REFLECTOR_CYCLES:
            raise ValueError("frozen schedule does not contain 34 reflector cycles")
        if len(self.evolution_requests) != EXPECTED_ARTIFACT_EVOLUTION_REQUESTS:
            raise ValueError("frozen schedule does not contain 102 native evolution requests")
        for cycle in cycles:
            types = {
                item.artifact_type
                for item in self.evolution_requests
                if (item.task_id, item.reflector_round) == cycle
            }
            if types != set(ARTIFACT_TYPES):
                raise ValueError("reflector cycle does not review all three artifact types")


def official_freeze_configuration(frozen_composite_id: str) -> dict[str, Any]:
    if not frozen_composite_id:
        raise ValueError("official freeze requires a composite identity")
    return {
        "official_task_count": OFFICIAL_TASK_COUNT,
        "frozen_composite_id": frozen_composite_id,
        "candidate_runs_per_task": 1,
        "evolution_enabled": False,
        "reflector_enabled": False,
        "task_local_overlay_enabled": False,
        "teacher_enabled": False,
        "cross_task_state_updates": False,
        "feedback_released": False,
        "score_release_gate": "ALL_40_CANDIDATE_RUNS_SEALED",
    }


def reject_official_training_transition(config: dict[str, Any], transition: str) -> None:
    forbidden = {
        "evolution": "evolution_enabled",
        "reflector": "reflector_enabled",
        "teacher": "teacher_enabled",
        "feedback": "feedback_released",
        "cross_task_update": "cross_task_state_updates",
    }
    if transition not in forbidden:
        raise ValueError("unknown official-test transition")
    if config.get(forbidden[transition]) is not False:
        raise ValueError("official freeze configuration is not fail-closed")
    raise RuntimeError(f"OFFICIAL_FROZEN_TRANSITION_REJECTED:{transition}")
