"""Private Train-only supervision packet for three-target transfer v2."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from openevo_chembench.chembench4k_evaluation import PrivateChemBench4KEvaluation
from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_CATEGORIES,
    CHOICE_LABELS,
    PrivateChemBench4KTask,
)
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    require_sha256,
    sha256_bytes,
)
from openevo_chembench.supervised_transfer_v2.config import PROTOCOL_ID

PACKET_SCHEMA = "SupervisedEvolutionPacketV2"
PACKET_INPUT_SCHEMA = {
    "additionalProperties": False,
    "required": [
        "protocol_id",
        "category",
        "task_uid",
        "training_task_ordinal",
        "round_index",
        "public_question",
        "options",
        "round_evidence",
        "correct_answer_letter",
        "correct_option_text",
        "predecessor_memory",
        "predecessor_artifact_id",
        "predecessor_skill",
        "predecessor_skill_artifact_id",
        "predecessor_agent_system",
        "predecessor_agent_system_artifact_id",
        "artifact_lineage",
        "trajectory_id",
    ],
    "schema_version": PACKET_SCHEMA,
}
PACKET_INPUT_SCHEMA_DIGEST = sha256_bytes(canonical_json_bytes(PACKET_INPUT_SCHEMA))

REFLECTOR_INSTRUCTION = """Supervised three-target category update contract:
1. Analyze the current Train chemistry item, all four options, and why the ground-truth option holds.
2. Compare every completed round in round_evidence and diagnose chemistry, reasoning, elimination, and output gaps.
3. Produce one complete replacement text_memory, skill_bundle, and agent_system for the same category.
4. Deduplicate before adding; resolve contradictions; Cycle 2 and Cycle 3 must not mechanically repeat earlier content.
5. A rule supported by only one independent Train task remains provisional; confirmation needs two different Train tasks.
6. Never copy a question, option, answer mapping, UID, ordinal, source index, dataset path, or repository path.
7. Each rule must bind category, target type, status, trigger, actionable principle, validation, evidence count,
   supporting Train evidence digests, first-seen cycle, last-confirmed cycle, and contradiction count. The trusted
   wrapper derives the opaque supporting-task-set hash; never author or guess that hash.
8. Keep chemistry knowledge in text_memory, reusable workflow in skill_bundle, and concise behavioral policy in agent_system.
9. Use only this packet and its approved predecessor context. Test and Reserve data are unavailable and forbidden.
10. Never write a standalone A/B/C/D answer letter in any model-authored rule, skill, or agent-system field; refer
    only to the chemically supported choice.
11. Return the exact closed structured three-target response required by the isolated wrapper."""
REFLECTOR_PROMPT_DIGEST = sha256_bytes(REFLECTOR_INSTRUCTION.encode("utf-8"))

_CHUNK_PAYLOAD_CHARACTERS = 136
_MAX_CHUNKS = 1024
_MAX_PREDECESSOR_COMPONENT_BYTES = 24_576


class SupervisedErrorCodeV2(str, Enum):
    CORRECT = "correct"
    INCORRECT = "incorrect"
    OFFICIAL_PARSE_FAILURE = "official_parse_failure"
    STRICT_FORMAT_VIOLATION = "strict_format_violation"
    WRONG_OPTION = "wrong_option"


@dataclass(frozen=True, slots=True, repr=False)
class SupervisedRoundEvidenceV2:
    round_index: Literal[0, 1, 2]
    model_raw_completion: str
    model_official_prediction: str | None
    model_strict_prediction: str | None
    prediction_correct: bool
    error_taxonomy: tuple[SupervisedErrorCodeV2, ...]
    trajectory_id: str

    def __post_init__(self) -> None:
        if self.round_index not in (0, 1, 2):
            raise ValueError("only pre-evolution rounds 0..2 may enter a packet")
        if type(self.model_raw_completion) is not str:
            raise TypeError("model_raw_completion must be text")
        for value in (self.model_official_prediction, self.model_strict_prediction):
            if value is not None and value not in CHOICE_LABELS:
                raise ValueError("prediction must be A/B/C/D or None")
        if type(self.prediction_correct) is not bool:
            raise TypeError("prediction_correct must be bool")
        if (
            not self.error_taxonomy
            or any(type(code) is not SupervisedErrorCodeV2 for code in self.error_taxonomy)
            or len(self.error_taxonomy) != len(set(self.error_taxonomy))
        ):
            raise ValueError("error taxonomy must be a unique closed tuple")
        expected = (
            SupervisedErrorCodeV2.CORRECT
            if self.prediction_correct
            else SupervisedErrorCodeV2.INCORRECT
        )
        if expected not in self.error_taxonomy:
            raise ValueError("error taxonomy lacks the correctness outcome")
        require_sha256(self.trajectory_id, "trajectory_id")

    @classmethod
    def from_evaluation(
        cls,
        *,
        round_index: Literal[0, 1, 2],
        evaluation: PrivateChemBench4KEvaluation,
        trajectory_id: str,
    ) -> SupervisedRoundEvidenceV2:
        if type(evaluation) is not PrivateChemBench4KEvaluation:
            raise TypeError("evaluation must be exact PrivateChemBench4KEvaluation")
        codes: list[SupervisedErrorCodeV2] = [
            SupervisedErrorCodeV2.CORRECT
            if evaluation.correct
            else SupervisedErrorCodeV2.INCORRECT
        ]
        if not evaluation.official.prediction:
            codes.append(SupervisedErrorCodeV2.OFFICIAL_PARSE_FAILURE)
        elif not evaluation.correct:
            codes.append(SupervisedErrorCodeV2.WRONG_OPTION)
        if not evaluation.strict.parsed:
            codes.append(SupervisedErrorCodeV2.STRICT_FORMAT_VIOLATION)
        return cls(
            round_index=round_index,
            model_raw_completion=evaluation.raw_completion,
            model_official_prediction=evaluation.official.prediction,
            model_strict_prediction=evaluation.strict.prediction,
            prediction_correct=evaluation.correct,
            error_taxonomy=tuple(codes),
            trajectory_id=trajectory_id,
        )

    def to_private_payload(self) -> dict[str, Any]:
        return {
            "round_index": self.round_index,
            "model_raw_completion": self.model_raw_completion,
            "model_official_prediction": self.model_official_prediction,
            "model_strict_prediction": self.model_strict_prediction,
            "prediction_correct": self.prediction_correct,
            "error_taxonomy": [code.value for code in self.error_taxonomy],
            "trajectory_id": self.trajectory_id,
        }


@dataclass(frozen=True, slots=True, repr=False)
class SupervisedEvolutionPacketV2:
    """One answer-bearing Train packet containing the current round prefix."""

    protocol_id: str
    category: str
    task_uid: str
    training_task_ordinal: int
    round_index: Literal[0, 1, 2]
    public_question: str
    options: tuple[tuple[str, str], ...]
    round_evidence: tuple[SupervisedRoundEvidenceV2, ...]
    correct_answer_letter: Literal["A", "B", "C", "D"]
    correct_option_text: str
    predecessor_memory: str | None
    predecessor_artifact_id: str | None
    predecessor_skill: str | None
    predecessor_skill_artifact_id: str | None
    predecessor_agent_system: str | None
    predecessor_agent_system_artifact_id: str | None
    artifact_lineage: tuple[tuple[str, str], ...]
    trajectory_id: str
    schema_version: str = PACKET_SCHEMA

    def __repr__(self) -> str:
        return "SupervisedEvolutionPacketV2(<private-train-supervision>)"

    __str__ = __repr__

    def __post_init__(self) -> None:
        if self.schema_version != PACKET_SCHEMA or self.protocol_id != PROTOCOL_ID:
            raise ValueError("supervised packet identity mismatch")
        if self.category not in CHEMBENCH4K_CATEGORIES:
            raise ValueError("packet category is unsupported")
        require_sha256(self.task_uid, "task_uid")
        require_sha256(self.trajectory_id, "trajectory_id")
        if (
            isinstance(self.training_task_ordinal, bool)
            or not isinstance(self.training_task_ordinal, int)
            or not 1 <= self.training_task_ordinal <= 50
        ):
            raise ValueError("training_task_ordinal must be in 1..50")
        if self.round_index not in (0, 1, 2):
            raise ValueError("only rounds 0..2 may create an evolution packet")
        if type(self.public_question) is not str or not self.public_question.strip():
            raise ValueError("public_question must be non-empty")
        if tuple(label for label, _text in self.options) != CHOICE_LABELS:
            raise ValueError("packet options must be ordered A/B/C/D")
        if any(type(text) is not str for _label, text in self.options):
            raise TypeError("packet option text must be string")
        if (
            len(self.round_evidence) != self.round_index + 1
            or tuple(item.round_index for item in self.round_evidence)
            != tuple(range(self.round_index + 1))
            or any(type(item) is not SupervisedRoundEvidenceV2 for item in self.round_evidence)
            or self.round_evidence[-1].trajectory_id != self.trajectory_id
        ):
            raise ValueError("round evidence must be the exact ordered current-task prefix")
        if self.correct_answer_letter not in CHOICE_LABELS:
            raise ValueError("correct answer must be A/B/C/D")
        if self.correct_option_text != dict(self.options)[self.correct_answer_letter]:
            raise ValueError("correct option text does not match the answer letter")
        _validate_predecessor_set(
            memory=(self.predecessor_memory, self.predecessor_artifact_id),
            skill=(self.predecessor_skill, self.predecessor_skill_artifact_id),
            agent_system=(
                self.predecessor_agent_system,
                self.predecessor_agent_system_artifact_id,
            ),
        )
        if tuple(sorted(self.artifact_lineage)) != self.artifact_lineage:
            raise ValueError("artifact lineage must be sorted")
        for target_id, artifact_id in self.artifact_lineage:
            if target_id not in {"text_memory", "skill_bundle", "agent_system"}:
                raise ValueError("artifact lineage target is invalid")
            _validate_artifact_id(artifact_id)

    @property
    def model_raw_completion(self) -> str:
        return self.round_evidence[-1].model_raw_completion

    @property
    def model_official_prediction(self) -> str | None:
        return self.round_evidence[-1].model_official_prediction

    @property
    def model_strict_prediction(self) -> str | None:
        return self.round_evidence[-1].model_strict_prediction

    @property
    def prediction_correct(self) -> bool:
        return self.round_evidence[-1].prediction_correct

    @property
    def error_taxonomy(self) -> tuple[SupervisedErrorCodeV2, ...]:
        return self.round_evidence[-1].error_taxonomy

    @classmethod
    def from_evaluations(
        cls,
        *,
        task: PrivateChemBench4KTask,
        training_task_ordinal: int,
        round_index: Literal[0, 1, 2],
        evaluations: tuple[PrivateChemBench4KEvaluation, ...],
        trajectory_ids: tuple[str, ...],
        predecessor_memory: str | None,
        predecessor_artifact_id: str | None,
        predecessor_skill: str | None,
        predecessor_skill_artifact_id: str | None,
        predecessor_agent_system: str | None,
        predecessor_agent_system_artifact_id: str | None,
    ) -> SupervisedEvolutionPacketV2:
        if type(task) is not PrivateChemBench4KTask or task.source_split != "test":
            raise TypeError("supervised packet requires one private frozen test-pool task")
        if len(evaluations) != round_index + 1 or len(trajectory_ids) != len(evaluations):
            raise ValueError("evaluations must be the ordered current-task prefix")
        if any(
            type(item) is not PrivateChemBench4KEvaluation
            or item.task_uid != task.uid
            or item.category != task.category
            for item in evaluations
        ):
            raise ValueError("evaluation and task identity differ")
        evidence = tuple(
            SupervisedRoundEvidenceV2.from_evaluation(
                round_index=index,
                evaluation=evaluation,
                trajectory_id=trajectory_ids[index],
            )
            for index, evaluation in enumerate(evaluations)
        )
        lineage = tuple(
            sorted(
                (target, artifact_id)
                for target, artifact_id in (
                    ("text_memory", predecessor_artifact_id),
                    ("skill_bundle", predecessor_skill_artifact_id),
                    ("agent_system", predecessor_agent_system_artifact_id),
                )
                if artifact_id is not None
            )
        )
        return cls(
            protocol_id=PROTOCOL_ID,
            category=task.category,
            task_uid=task.uid,
            training_task_ordinal=training_task_ordinal,
            round_index=round_index,
            public_question=task.question,
            options=tuple((label, getattr(task, label)) for label in CHOICE_LABELS),
            round_evidence=evidence,
            correct_answer_letter=task.target,
            correct_option_text=getattr(task, task.target),
            predecessor_memory=predecessor_memory,
            predecessor_artifact_id=predecessor_artifact_id,
            predecessor_skill=predecessor_skill,
            predecessor_skill_artifact_id=predecessor_skill_artifact_id,
            predecessor_agent_system=predecessor_agent_system,
            predecessor_agent_system_artifact_id=predecessor_agent_system_artifact_id,
            artifact_lineage=lineage,
            trajectory_id=trajectory_ids[-1],
        )

    def to_private_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "protocol_id": self.protocol_id,
            "category": self.category,
            "task_uid": self.task_uid,
            "training_task_ordinal": self.training_task_ordinal,
            "round_index": self.round_index,
            "public_question": self.public_question,
            "options": dict(self.options),
            "round_evidence": [item.to_private_payload() for item in self.round_evidence],
            "correct_answer_letter": self.correct_answer_letter,
            "correct_option_text": self.correct_option_text,
            "predecessor_memory": self.predecessor_memory,
            "predecessor_artifact_id": self.predecessor_artifact_id,
            "predecessor_skill": self.predecessor_skill,
            "predecessor_skill_artifact_id": self.predecessor_skill_artifact_id,
            "predecessor_agent_system": self.predecessor_agent_system,
            "predecessor_agent_system_artifact_id": self.predecessor_agent_system_artifact_id,
            "artifact_lineage": [list(item) for item in self.artifact_lineage],
            "trajectory_id": self.trajectory_id,
        }

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.to_private_payload()))

    def reflector_records(self) -> tuple[dict[str, Any], ...]:
        projected_fields: list[tuple[str, str]] = [
            ("reflector_instruction", REFLECTOR_INSTRUCTION),
            ("schema_version", self.schema_version),
            ("protocol_id", self.protocol_id),
            ("category", self.category),
            ("task_uid", self.task_uid),
            ("training_task_ordinal", str(self.training_task_ordinal)),
            ("round_index", str(self.round_index)),
            ("evolution_cycle", str(self.round_index + 1)),
            ("public_question", self.public_question),
        ]
        projected_fields.extend((f"option_{label}", text) for label, text in self.options)
        for evidence in self.round_evidence:
            prefix = f"round_{evidence.round_index}"
            projected_fields.extend(
                (
                    (f"{prefix}_model_raw_completion", evidence.model_raw_completion),
                    (
                        f"{prefix}_model_official_prediction",
                        evidence.model_official_prediction or "<none>",
                    ),
                    (
                        f"{prefix}_model_strict_prediction",
                        evidence.model_strict_prediction or "<none>",
                    ),
                    (f"{prefix}_prediction_correct", json.dumps(evidence.prediction_correct)),
                    (
                        f"{prefix}_error_taxonomy",
                        ",".join(code.value for code in evidence.error_taxonomy),
                    ),
                    (f"{prefix}_trajectory_id", evidence.trajectory_id),
                )
            )
        projected_fields.extend(
            (
                ("correct_answer_letter", self.correct_answer_letter),
                ("correct_option_text", self.correct_option_text),
                (
                    "predecessor_memory",
                    "<generation-zero-empty>"
                    if self.predecessor_memory is None
                    else (
                        "<visible-in-Core-existing-text-memory "
                        f"sha256={sha256_bytes(self.predecessor_memory.encode('utf-8'))}>"
                    ),
                ),
                ("predecessor_artifact_id", self.predecessor_artifact_id or "<none>"),
                ("predecessor_skill", self.predecessor_skill or "<generation-zero-empty>"),
                (
                    "predecessor_skill_artifact_id",
                    self.predecessor_skill_artifact_id or "<none>",
                ),
                (
                    "predecessor_agent_system",
                    self.predecessor_agent_system or "<generation-zero-empty>",
                ),
                (
                    "predecessor_agent_system_artifact_id",
                    self.predecessor_agent_system_artifact_id or "<none>",
                ),
                ("artifact_lineage", json.dumps(self.artifact_lineage)),
                ("trajectory_id", self.trajectory_id),
                ("packet_sha256", self.digest),
                ("input_schema_sha256", PACKET_INPUT_SCHEMA_DIGEST),
                ("reflector_prompt_sha256", REFLECTOR_PROMPT_DIGEST),
            )
        )
        parts: list[tuple[str, int, int, str]] = []
        for field_name, value in projected_fields:
            normalized = " ".join(value.split())
            chunks = tuple(
                normalized[offset : offset + _CHUNK_PAYLOAD_CHARACTERS]
                for offset in range(0, max(1, len(normalized)), _CHUNK_PAYLOAD_CHARACTERS)
            ) or ("",)
            parts.extend(
                (field_name, index, len(chunks), chunk)
                for index, chunk in enumerate(chunks, start=1)
            )
        if not parts or len(parts) > _MAX_CHUNKS:
            raise ValueError("supervised packet exceeds the bounded Core projection")
        total = len(parts)
        reward = 1.0 if self.prediction_correct else 0.0
        return tuple(
            {
                "uid": sha256_bytes(
                    f"{self.digest}:{ordinal}:{field_name}:{field_part}".encode()
                ),
                "source_split": "supervised_train",
                "packet_sha256": self.digest,
                "packet_part": ordinal,
                "packet_part_count": total,
                "field": field_name,
                "field_part": field_part,
                "field_part_count": field_total,
                "content": (
                    f"PACKET_PART {ordinal:03d}/{total:03d} field={field_name} "
                    f"field_part={field_part:03d}/{field_total:03d} value={chunk}"
                ),
                "reward": reward,
            }
            for ordinal, (field_name, field_part, field_total, chunk) in enumerate(
                parts, start=1
            )
        )


def _validate_predecessor_set(
    *,
    memory: tuple[str | None, str | None],
    skill: tuple[str | None, str | None],
    agent_system: tuple[str | None, str | None],
) -> None:
    bindings = (memory, skill, agent_system)
    empty = tuple(content is None and artifact_id is None for content, artifact_id in bindings)
    if any(empty) and not all(empty):
        raise ValueError("predecessor targets must be generation-zero or a complete triple")
    if all(empty):
        return
    for field_name, (content, artifact_id) in zip(
        ("memory", "skill", "agent_system"), bindings, strict=True
    ):
        if (
            type(content) is not str
            or not content.strip()
            or len(content.encode("utf-8")) > _MAX_PREDECESSOR_COMPONENT_BYTES
        ):
            raise ValueError(f"predecessor {field_name} content is invalid")
        _validate_artifact_id(artifact_id)


def _validate_artifact_id(value: str | None) -> None:
    if type(value) is not str or not value or "/" in value or "\\" in value:
        raise ValueError("predecessor artifact ID is invalid")


__all__ = [
    "PACKET_INPUT_SCHEMA",
    "PACKET_INPUT_SCHEMA_DIGEST",
    "PACKET_SCHEMA",
    "REFLECTOR_INSTRUCTION",
    "REFLECTOR_PROMPT_DIGEST",
    "SupervisedErrorCodeV2",
    "SupervisedEvolutionPacketV2",
    "SupervisedRoundEvidenceV2",
]
