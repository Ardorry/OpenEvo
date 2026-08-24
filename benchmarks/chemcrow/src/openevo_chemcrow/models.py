from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ArtifactKind(StrEnum):
    TEXT_MEMORY = "text_memory"
    SKILL_BUNDLE = "skill_bundle"
    AGENT_SYSTEM = "agent_system"


class FeedbackMode(StrEnum):
    F0 = "F0"
    F1 = "F1"
    F2 = "F2"


class ObservationSource(StrEnum):
    LIVE = "live"
    CACHE_REPLAY = "cache_replay"
    FIXTURE = "fixture"
    MOCK = "mock"


class TaskItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    prompt: str
    broad_category: str
    allowed_tool_metadata: dict[str, Any] = Field(default_factory=dict)
    safety_metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, str]
    sanitized_item_sha256: str


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    started_at: datetime | None = None


class ToolObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: str
    tool_name: str
    canonical_arguments_sha256: str
    result: Any | None = None
    error: str | None = None
    source: ObservationSource
    elapsed_seconds: float = Field(default=0.0, ge=0.0)


class Trajectory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    task_id: str
    role: Literal["baseline", "evolved"]
    status: Literal["COMPLETED", "ERROR", "TIMEOUT"]
    answer: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    observations: list[ToolObservation] = Field(default_factory=list)
    visible_reasoning_summaries: list[str] = Field(default_factory=list)
    retries: int = Field(default=0, ge=0)
    wall_time_seconds: float = Field(default=0.0, ge=0.0)
    token_metadata: dict[str, Any] = Field(default_factory=dict)
    cost_metadata: dict[str, Any] = Field(default_factory=dict)
    candidate_config_sha256: str
    artifact_ids: list[str] = Field(default_factory=list)


class RuntimeFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    tool_errors: list[str] = Field(default_factory=list)
    invalid_calls: list[str] = Field(default_factory=list)
    empty_observations: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    completion_failures: list[str] = Field(default_factory=list)


class RubricScores(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chemical_correctness: float = Field(ge=0.0, le=4.0)
    reasoning_quality: float = Field(ge=0.0, le=4.0)
    task_completion: float = Field(ge=0.0, le=4.0)


class EvaluatorFeedback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evaluator_role: Literal["evolution_evaluator", "final_evaluator"]
    evaluator_run_id: str
    scores: RubricScores
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    actionable_critique: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ArtifactReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    parent_baseline_run: str
    reflector_run_id: str
    reflector_input_hash: str
    artifact_type: ArtifactKind
    artifact_id: str
    content_hash: str
    size_bytes: int = Field(ge=0)
    generation_time_seconds: float = Field(ge=0.0)
    consumed_by_evolved_run_id: str | None = None


class PairwiseEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline_scores: RubricScores
    evolved_scores: RubricScores
    delta_chemical_correctness: float
    delta_reasoning_quality: float
    delta_task_completion: float
    winner: Literal["baseline", "evolved", "tie"]
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    presentation_order: list[Literal["A", "B"]]
    mapping_seal_sha256: str
    provisional_llm_judged: bool = True


class PairResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    task_category: str
    s0_hash: str
    baseline: Trajectory
    runtime_feedback: RuntimeFeedback
    evolution_feedback: EvaluatorFeedback | None
    feedback_hash: str
    artifact: ArtifactReceipt
    evolved: Trajectory
    final_evaluation: PairwiseEvaluation
    event_order: list[str]
    reset_receipt_sha256: str

    @model_validator(mode="after")
    def _pair_invariants(self) -> PairResult:
        if self.baseline.artifact_ids:
            raise ValueError("baseline must not consume evolved artifacts")
        if self.evolved.artifact_ids != [self.artifact.artifact_id]:
            raise ValueError("evolved run must consume exactly the same-item artifact")
        if self.artifact.consumed_by_evolved_run_id != self.evolved.run_id:
            raise ValueError("artifact consumption receipt does not name evolved run")
        if self.baseline.candidate_config_sha256 != self.evolved.candidate_config_sha256:
            raise ValueError("candidate pair configuration differs")
        if self.event_order.count("reflector") != 1:
            raise ValueError("exactly one Reflector step is required")
        expected = [
            "s0_asserted",
            "baseline",
            "runtime_feedback",
            "evolution_evaluator",
            "reflector",
            "evolved",
            "final_evaluator",
            "sealed",
            "reset",
        ]
        if self.event_order != expected:
            raise ValueError("task-local phase order is invalid")
        return self
