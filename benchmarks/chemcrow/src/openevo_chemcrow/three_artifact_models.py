from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from .models import (
    ArtifactKind,
    EvaluatorFeedback,
    PairwiseEvaluation,
    RuntimeFeedback,
    Trajectory,
)

THREE_ARTIFACT_ORDER = (
    ArtifactKind.TEXT_MEMORY,
    ArtifactKind.SKILL_BUNDLE,
    ArtifactKind.AGENT_SYSTEM,
)

LEGACY_THREE_ARTIFACT_BUNDLE_PROTOCOL = "chemcrow_three_isolated_artifacts_v1"
CORE_NATIVE_THREE_ARTIFACT_BUNDLE_PROTOCOL = (
    "chemcrow_three_isolated_core_native_artifacts_v2"
)
LEGACY_THREE_ARTIFACT_PROTOCOL_LABEL = "chemcrow-three-isolated-artifacts-v1"
CORE_NATIVE_THREE_ARTIFACT_PROTOCOL_LABEL = (
    "chemcrow-three-isolated-core-native-artifacts-v2"
)


def three_artifact_protocol_label(bundle_protocol: str) -> str:
    if bundle_protocol == LEGACY_THREE_ARTIFACT_BUNDLE_PROTOCOL:
        return LEGACY_THREE_ARTIFACT_PROTOCOL_LABEL
    if bundle_protocol == CORE_NATIVE_THREE_ARTIFACT_BUNDLE_PROTOCOL:
        return CORE_NATIVE_THREE_ARTIFACT_PROTOCOL_LABEL
    raise ValueError(f"unsupported three-artifact bundle protocol: {bundle_protocol}")


class ArtifactSeparationPolicy(BaseModel):
    """Frozen, deterministic guards applied before any artifact is registered."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    normalized_token_ngram_size: int = Field(default=3, ge=1, le=8)
    near_duplicate_jaccard_threshold: float = Field(default=0.9, ge=0.0, le=1.0)
    near_duplicate_sequence_threshold: float = Field(default=0.95, ge=0.0, le=1.0)
    minimum_tokens_for_near_duplicate_check: int = Field(default=12, ge=1)
    baseline_answer_sequence_threshold: float = Field(default=0.98, ge=0.0, le=1.0)
    # Read-only compatibility for historical v1 receipts. Core-native v2 neither
    # emits nor enforces the ChemCrow-specific responsibility-marker policy.
    responsibility_policy_version: Literal["chemcrow_artifact_responsibility_v1"] | None = Field(
        default=None
    )
    minimum_tokens_for_responsibility_check: int | None = Field(default=None, ge=1)

    @model_serializer(mode="wrap")
    def _serialize_without_unused_legacy_fields(self, serializer):
        data = serializer(self)
        if self.responsibility_policy_version is None:
            data.pop("responsibility_policy_version", None)
        if self.minimum_tokens_for_responsibility_check is None:
            data.pop("minimum_tokens_for_responsibility_check", None)
        return data


class ThreeArtifactReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    pair_id: str
    parent_run_id: str
    reflector_job_id: str
    reflector_run_id: str
    model: str
    system_prompt_hash: str
    prompt_hash: str
    input_evidence_hash: str
    artifact_type: ArtifactKind
    artifact_id: str
    artifact_hash: str
    normalized_text_hash: str
    size_bytes: int = Field(ge=0)
    generation_time_seconds: float = Field(ge=0.0)
    registration_receipt_sha256: str
    sibling_outputs_visible: Literal[False] = False
    historical_answers_included: Literal[False] = False
    consumed_by_evolved_run_id: str | None = None


class ThreeArtifactBundleReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: Literal[
        "chemcrow_three_isolated_artifacts_v1",
        "chemcrow_three_isolated_core_native_artifacts_v2",
    ] = CORE_NATIVE_THREE_ARTIFACT_BUNDLE_PROTOCOL
    task_id: str
    pair_id: str
    parent_run_id: str
    input_evidence_hash: str
    separation_policy: ArtifactSeparationPolicy
    artifacts: list[ThreeArtifactReceipt]
    byte_identical_pairs: list[list[str]] = Field(default_factory=list)
    normalized_identical_pairs: list[list[str]] = Field(default_factory=list)
    near_duplicate_pairs: list[dict[str, object]] = Field(default_factory=list)
    baseline_answer_copy: list[dict[str, object]] = Field(default_factory=list)
    # Historical v1 read compatibility only; v2 does not emit this field.
    responsibility_violations: list[dict[str, object]] = Field(
        default_factory=list
    )

    @model_serializer(mode="wrap")
    def _serialize_protocol_fields(self, serializer):
        data = serializer(self)
        if self.protocol == CORE_NATIVE_THREE_ARTIFACT_BUNDLE_PROTOCOL:
            data.pop("responsibility_violations", None)
        return data

    @model_validator(mode="after")
    def _validate_bundle(self) -> ThreeArtifactBundleReceipt:
        if [item.artifact_type for item in self.artifacts] != list(THREE_ARTIFACT_ORDER):
            raise ValueError(
                "artifact bundle must contain memory, skill_bundle, agent_system in order"
            )
        if len({item.artifact_id for item in self.artifacts}) != 3:
            raise ValueError("artifact IDs must be independent")
        if len({item.reflector_job_id for item in self.artifacts}) != 3:
            raise ValueError("Reflector job IDs must be independent")
        if len({item.reflector_run_id for item in self.artifacts}) != 3:
            raise ValueError("Reflector model invocations must be independent")
        if len({item.prompt_hash for item in self.artifacts}) != 3:
            raise ValueError("Reflector prompts must be independently hashed")
        if any(item.task_id != self.task_id for item in self.artifacts):
            raise ValueError("artifact task lineage differs from bundle")
        if any(item.pair_id != self.pair_id for item in self.artifacts):
            raise ValueError("artifact pair lineage differs from bundle")
        if any(item.parent_run_id != self.parent_run_id for item in self.artifacts):
            raise ValueError("artifact parent lineage differs from bundle")
        if any(item.input_evidence_hash != self.input_evidence_hash for item in self.artifacts):
            raise ValueError("Reflectors did not receive the same frozen evidence")
        if (
            self.byte_identical_pairs
            or self.normalized_identical_pairs
            or self.near_duplicate_pairs
            or self.baseline_answer_copy
            or self.responsibility_violations
        ):
            raise ValueError("artifact separation guard detected duplicate content")
        return self

    def artifact_ids(self) -> list[str]:
        return [item.artifact_id for item in self.artifacts]

    def artifact_id_by_type(self) -> dict[str, str]:
        return {item.artifact_type.value: item.artifact_id for item in self.artifacts}


class CoreInjectionReceiptSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    receipt_sha256: str
    artifact_count: Literal[3] = 3
    memory_artifact_id: str
    skill_artifact_id: str
    agent_system_artifact_id: str
    unexpected_artifact_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_fourth_artifact(self) -> CoreInjectionReceiptSummary:
        if self.unexpected_artifact_ids:
            raise ValueError("Core injected an unexpected fourth evolution artifact")
        if (
            len(
                {
                    self.memory_artifact_id,
                    self.skill_artifact_id,
                    self.agent_system_artifact_id,
                }
            )
            != 3
        ):
            raise ValueError("Core injection reused one artifact for multiple types")
        return self


class ThreeArtifactPairResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["chemcrow_three_artifact_pair_v1"] = "chemcrow_three_artifact_pair_v1"
    task_id: str
    task_category: str
    pair_id: str
    s0_hash: str
    task_prompt_hash: str
    baseline: Trajectory
    runtime_feedback: RuntimeFeedback
    baseline_internal_evaluation: EvaluatorFeedback
    feedback_hash: str
    artifact_bundle: ThreeArtifactBundleReceipt
    evolved: Trajectory
    evolved_internal_evaluation: EvaluatorFeedback
    injection_receipt: CoreInjectionReceiptSummary
    final_evaluation: PairwiseEvaluation
    event_order: list[str]
    reset_receipt_sha256: str

    @model_validator(mode="after")
    def _pair_invariants(self) -> ThreeArtifactPairResult:
        artifacts = self.artifact_bundle.artifacts
        artifact_ids = [item.artifact_id for item in artifacts]
        if self.artifact_bundle.task_id != self.task_id:
            raise ValueError("artifact bundle task lineage differs from pair")
        if self.artifact_bundle.pair_id != self.pair_id:
            raise ValueError("artifact bundle pair lineage differs from pair")
        if self.artifact_bundle.parent_run_id != self.baseline.run_id:
            raise ValueError("artifact bundle parent differs from baseline run")
        if self.baseline.task_id != self.task_id or self.evolved.task_id != self.task_id:
            raise ValueError("Candidate trajectory task lineage differs from pair")
        if self.baseline.role != "baseline" or self.evolved.role != "evolved":
            raise ValueError("Candidate trajectory roles differ from paired protocol")
        if self.baseline.run_id == self.evolved.run_id:
            raise ValueError("baseline and evolved Candidate runs must be independent")
        if (
            self.baseline.status != "COMPLETED"
            or self.evolved.status != "COMPLETED"
            or not self.baseline.answer.strip()
            or not self.evolved.answer.strip()
        ):
            raise ValueError("both Candidate runs must complete with final answers")
        if self.baseline.artifact_ids:
            raise ValueError("baseline must start from bare S0")
        if self.evolved.artifact_ids != artifact_ids:
            raise ValueError("evolved run must consume exactly the three same-item artifacts")
        if any(item.consumed_by_evolved_run_id != self.evolved.run_id for item in artifacts):
            raise ValueError("artifact consumption receipt does not name the evolved run")
        expected_mapping = {
            ArtifactKind.TEXT_MEMORY: self.injection_receipt.memory_artifact_id,
            ArtifactKind.SKILL_BUNDLE: self.injection_receipt.skill_artifact_id,
            ArtifactKind.AGENT_SYSTEM: self.injection_receipt.agent_system_artifact_id,
        }
        if any(expected_mapping[item.artifact_type] != item.artifact_id for item in artifacts):
            raise ValueError("Core injection type mapping differs from registered artifacts")
        if self.baseline.candidate_config_sha256 != self.evolved.candidate_config_sha256:
            raise ValueError("candidate pair configuration differs")
        if self.baseline.candidate_config_sha256 != self.s0_hash:
            raise ValueError("Candidate configuration is not bound to pair S0")
        if (
            self.baseline_internal_evaluation.evaluator_run_id
            == self.evolved_internal_evaluation.evaluator_run_id
        ):
            raise ValueError("G1/G2 internal evaluations must be independent calls")
        if self.baseline_internal_evaluation.evaluator_role != "evolution_evaluator":
            raise ValueError("G1 internal evaluation used the wrong role")
        if self.evolved_internal_evaluation.evaluator_role != "evolution_evaluator":
            raise ValueError("G2 internal evaluation used the wrong role")
        expected_order = [
            "s0_asserted",
            "baseline",
            "runtime_feedback",
            "baseline_internal_evaluator",
            "reflector_memory",
            "reflector_skill_bundle",
            "reflector_agent_system",
            "artifacts_registered",
            "evolved",
            "evolved_internal_evaluator",
            "final_evaluator",
            "sealed",
            "reset",
        ]
        if self.event_order != expected_order:
            raise ValueError("three-artifact task-local phase order is invalid")
        return self
