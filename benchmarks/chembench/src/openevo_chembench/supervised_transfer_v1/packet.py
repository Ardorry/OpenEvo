"""Private supervised reflector packet with a closed Train-only projection."""

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
from openevo_chembench.supervised_transfer_v1.config import PROTOCOL_ID

PACKET_SCHEMA = "SupervisedEvolutionPacketV1"
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
        "model_raw_completion",
        "model_official_prediction",
        "model_strict_prediction",
        "correct_answer_letter",
        "correct_option_text",
        "prediction_correct",
        "error_taxonomy",
        "predecessor_memory",
        "predecessor_artifact_id",
        "predecessor_skill",
        "predecessor_skill_artifact_id",
        "predecessor_agent_system",
        "predecessor_agent_system_artifact_id",
        "trajectory_id",
    ],
    "schema_version": PACKET_SCHEMA,
}
PACKET_INPUT_SCHEMA_DIGEST = sha256_bytes(canonical_json_bytes(PACKET_INPUT_SCHEMA))

REFLECTOR_INSTRUCTION = """Supervised category-context update contract:
1. Analyze this concrete chemistry training item and why the correct option holds.
2. Diagnose the gap between the model prediction and the correct answer.
3. Abstract only transferable rules for the packet category.
4. Never write the question, an option, an answer mapping, a UID, an ordinal, or a path.
5. Every rule must contain Rule ID, Status, Category, Trigger, Principle, Action,
   Validation, Evidence count, and Evidence hash.
6. Evidence from only one independent training item stays provisional.
7. Confirm a rule only after at least two independent training items support it.
8. Resolve contradictions explicitly; unsupported advice is provisional or retired.
9. Return one bounded category-specific structured response containing text memory,
   a reusable skill workflow, and agent-system directives. Preserve the Core-required
   memory sections and make skill/system rules independently actionable.
10. Memory, skill, and agent-system outputs must use only Train evidence represented
    by this packet and its approved predecessor context; they must never contain a
    task-to-answer mapping."""
REFLECTOR_PROMPT_DIGEST = sha256_bytes(REFLECTOR_INSTRUCTION.encode("utf-8"))

_CHUNK_PAYLOAD_CHARACTERS = 136
_MAX_CHUNKS = 1024
_MAX_PREDECESSOR_COMPONENT_BYTES = 24_576


class SupervisedErrorCodeV1(str, Enum):
    """Complete deterministic error taxonomy supplied to the reflector."""

    CORRECT = "correct"
    INCORRECT = "incorrect"
    OFFICIAL_PARSE_FAILURE = "official_parse_failure"
    STRICT_FORMAT_VIOLATION = "strict_format_violation"
    WRONG_OPTION = "wrong_option"


@dataclass(frozen=True, slots=True, repr=False)
class SupervisedEvolutionPacketV1:
    """One Train-only, answer-bearing packet; never used for Probe/Test."""

    protocol_id: str
    category: str
    task_uid: str
    training_task_ordinal: int
    round_index: Literal[0, 1]
    public_question: str
    options: tuple[tuple[str, str], ...]
    model_raw_completion: str
    model_official_prediction: str | None
    model_strict_prediction: str | None
    correct_answer_letter: Literal["A", "B", "C", "D"]
    correct_option_text: str
    prediction_correct: bool
    error_taxonomy: tuple[SupervisedErrorCodeV1, ...]
    predecessor_memory: str | None
    predecessor_artifact_id: str | None
    trajectory_id: str
    predecessor_skill: str | None = None
    predecessor_skill_artifact_id: str | None = None
    predecessor_agent_system: str | None = None
    predecessor_agent_system_artifact_id: str | None = None
    schema_version: str = PACKET_SCHEMA

    def __repr__(self) -> str:
        return "SupervisedEvolutionPacketV1(<private-train-supervision>)"

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
        if self.round_index not in (0, 1):
            raise ValueError("only rounds 0 and 1 may create an evolution packet")
        if type(self.public_question) is not str or not self.public_question.strip():
            raise ValueError("public_question must be non-empty")
        if tuple(label for label, _text in self.options) != CHOICE_LABELS:
            raise ValueError("packet options must be ordered A/B/C/D")
        if any(type(text) is not str for _label, text in self.options):
            raise TypeError("packet option text must be string")
        if type(self.model_raw_completion) is not str:
            raise TypeError("model_raw_completion must be string")
        for prediction, field_name in (
            (self.model_official_prediction, "model_official_prediction"),
            (self.model_strict_prediction, "model_strict_prediction"),
        ):
            if prediction is not None and type(prediction) is not str:
                raise TypeError(f"{field_name} must be string or None")
        if self.correct_answer_letter not in CHOICE_LABELS:
            raise ValueError("correct answer must be A/B/C/D")
        option_map = dict(self.options)
        if self.correct_option_text != option_map[self.correct_answer_letter]:
            raise ValueError("correct option text does not match the answer letter")
        if self.prediction_correct != (
            self.model_official_prediction == self.correct_answer_letter
        ):
            raise ValueError("prediction_correct does not match official prediction")
        if (
            not isinstance(self.error_taxonomy, tuple)
            or not self.error_taxonomy
            or any(type(code) is not SupervisedErrorCodeV1 for code in self.error_taxonomy)
            or len(self.error_taxonomy) != len(set(self.error_taxonomy))
        ):
            raise ValueError("error taxonomy must be a unique non-empty closed tuple")
        expected_outcome = (
            SupervisedErrorCodeV1.CORRECT
            if self.prediction_correct
            else SupervisedErrorCodeV1.INCORRECT
        )
        if expected_outcome not in self.error_taxonomy:
            raise ValueError("error taxonomy lacks the correctness outcome")
        if self.predecessor_memory is None:
            if any(
                value is not None
                for value in (
                    self.predecessor_artifact_id,
                    self.predecessor_skill,
                    self.predecessor_skill_artifact_id,
                    self.predecessor_agent_system,
                    self.predecessor_agent_system_artifact_id,
                )
            ):
                raise ValueError("generation-zero packet cannot contain predecessor context")
        else:
            for content, artifact_id, field_name in (
                (
                    self.predecessor_memory,
                    self.predecessor_artifact_id,
                    "memory",
                ),
                (
                    self.predecessor_skill,
                    self.predecessor_skill_artifact_id,
                    "skill",
                ),
                (
                    self.predecessor_agent_system,
                    self.predecessor_agent_system_artifact_id,
                    "agent_system",
                ),
            ):
                _validate_predecessor_component(content, artifact_id, field_name)

    @classmethod
    def from_evaluation(
        cls,
        *,
        task: PrivateChemBench4KTask,
        training_task_ordinal: int,
        round_index: Literal[0, 1],
        evaluation: PrivateChemBench4KEvaluation,
        predecessor_memory: str | None,
        predecessor_artifact_id: str | None,
        session_id: str,
        predecessor_skill: str | None = None,
        predecessor_skill_artifact_id: str | None = None,
        predecessor_agent_system: str | None = None,
        predecessor_agent_system_artifact_id: str | None = None,
    ) -> SupervisedEvolutionPacketV1:
        if type(task) is not PrivateChemBench4KTask or task.source_split != "test":
            raise TypeError("supervised packet requires one private frozen test-pool task")
        if type(evaluation) is not PrivateChemBench4KEvaluation:
            raise TypeError("evaluation must be exact PrivateChemBench4KEvaluation")
        if evaluation.task_uid != task.uid or evaluation.category != task.category:
            raise ValueError("evaluation and task identity differ")
        if type(session_id) is not str or not session_id:
            raise ValueError("session_id must be non-empty")
        codes: list[SupervisedErrorCodeV1] = [
            (
                SupervisedErrorCodeV1.CORRECT
                if evaluation.correct
                else SupervisedErrorCodeV1.INCORRECT
            )
        ]
        if not evaluation.official.prediction:
            codes.append(SupervisedErrorCodeV1.OFFICIAL_PARSE_FAILURE)
        elif not evaluation.correct:
            codes.append(SupervisedErrorCodeV1.WRONG_OPTION)
        if not evaluation.strict.parsed:
            codes.append(SupervisedErrorCodeV1.STRICT_FORMAT_VIOLATION)
        trajectory_identity = {
            "schema_version": "supervised_train_trajectory_identity_v1",
            "protocol_id": PROTOCOL_ID,
            "task_uid": task.uid,
            "training_task_ordinal": training_task_ordinal,
            "round_index": round_index,
            "session_id": session_id,
            "raw_completion_sha256": sha256_bytes(evaluation.raw_completion.encode("utf-8")),
            "official_prediction": evaluation.official.prediction,
            "strict_prediction": evaluation.strict.prediction,
            "correct": evaluation.correct,
            "predecessor_artifact_id": predecessor_artifact_id,
            "predecessor_memory_sha256": (
                None
                if predecessor_memory is None
                else sha256_bytes(predecessor_memory.encode("utf-8"))
            ),
            "predecessor_skill_artifact_id": predecessor_skill_artifact_id,
            "predecessor_skill_sha256": (
                None
                if predecessor_skill is None
                else sha256_bytes(predecessor_skill.encode("utf-8"))
            ),
            "predecessor_agent_system_artifact_id": (
                predecessor_agent_system_artifact_id
            ),
            "predecessor_agent_system_sha256": (
                None
                if predecessor_agent_system is None
                else sha256_bytes(predecessor_agent_system.encode("utf-8"))
            ),
        }
        return cls(
            protocol_id=PROTOCOL_ID,
            category=task.category,
            task_uid=task.uid,
            training_task_ordinal=training_task_ordinal,
            round_index=round_index,
            public_question=task.question,
            options=tuple((label, getattr(task, label)) for label in CHOICE_LABELS),
            model_raw_completion=evaluation.raw_completion,
            model_official_prediction=evaluation.official.prediction,
            model_strict_prediction=evaluation.strict.prediction,
            correct_answer_letter=task.target,
            correct_option_text=getattr(task, task.target),
            prediction_correct=evaluation.correct,
            error_taxonomy=tuple(codes),
            predecessor_memory=predecessor_memory,
            predecessor_artifact_id=predecessor_artifact_id,
            trajectory_id=sha256_bytes(canonical_json_bytes(trajectory_identity)),
            predecessor_skill=predecessor_skill,
            predecessor_skill_artifact_id=predecessor_skill_artifact_id,
            predecessor_agent_system=predecessor_agent_system,
            predecessor_agent_system_artifact_id=(
                predecessor_agent_system_artifact_id
            ),
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
            "model_raw_completion": self.model_raw_completion,
            "model_official_prediction": self.model_official_prediction,
            "model_strict_prediction": self.model_strict_prediction,
            "correct_answer_letter": self.correct_answer_letter,
            "correct_option_text": self.correct_option_text,
            "prediction_correct": self.prediction_correct,
            "error_taxonomy": [code.value for code in self.error_taxonomy],
            "predecessor_memory": self.predecessor_memory,
            "predecessor_artifact_id": self.predecessor_artifact_id,
            "predecessor_skill": self.predecessor_skill,
            "predecessor_skill_artifact_id": self.predecessor_skill_artifact_id,
            "predecessor_agent_system": self.predecessor_agent_system,
            "predecessor_agent_system_artifact_id": (
                self.predecessor_agent_system_artifact_id
            ),
            "trajectory_id": self.trajectory_id,
        }

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.to_private_payload()))

    def reflector_records(self) -> tuple[dict[str, Any], ...]:
        """Losslessly expose packet fields through Core's 240-character record view.

        The predecessor body is supplied by the Core prior-target artifact binding and is
        therefore represented here by identity. This avoids duplicating a 24 KiB memory
        while keeping the complete predecessor visible in the composed Core prompt.
        """

        projected_fields: list[tuple[str, str]] = [
            ("reflector_instruction", REFLECTOR_INSTRUCTION),
            ("schema_version", self.schema_version),
            ("protocol_id", self.protocol_id),
            ("category", self.category),
            ("task_uid", self.task_uid),
            ("training_task_ordinal", str(self.training_task_ordinal)),
            ("round_index", str(self.round_index)),
            ("public_question", self.public_question),
        ]
        projected_fields.extend((f"option_{label}", text) for label, text in self.options)
        projected_fields.extend(
            [
                ("model_raw_completion", self.model_raw_completion),
                ("model_official_prediction", self.model_official_prediction or "<none>"),
                ("model_strict_prediction", self.model_strict_prediction or "<none>"),
                ("correct_answer_letter", self.correct_answer_letter),
                ("correct_option_text", self.correct_option_text),
                ("prediction_correct", json.dumps(self.prediction_correct)),
                (
                    "error_taxonomy",
                    ",".join(code.value for code in self.error_taxonomy),
                ),
                (
                    "predecessor_memory",
                    "<generation-zero-empty>"
                    if self.predecessor_memory is None
                    else (
                        "<visible-in-Core-existing-text-memory "
                        f"sha256={sha256_bytes(self.predecessor_memory.encode('utf-8'))}>"
                    ),
                ),
                (
                    "predecessor_artifact_id",
                    self.predecessor_artifact_id or "<none>",
                ),
                (
                    "predecessor_skill",
                    self.predecessor_skill or "<generation-zero-empty>",
                ),
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
                ("trajectory_id", self.trajectory_id),
                ("packet_sha256", self.digest),
                ("input_schema_sha256", PACKET_INPUT_SCHEMA_DIGEST),
                ("reflector_prompt_sha256", REFLECTOR_PROMPT_DIGEST),
            ]
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
        records: list[dict[str, Any]] = []
        total = len(parts)
        reward = 1.0 if self.prediction_correct else 0.0
        for ordinal, (field_name, field_part, field_total, chunk) in enumerate(parts, start=1):
            content = (
                f"PACKET_PART {ordinal:03d}/{total:03d} field={field_name} "
                f"field_part={field_part:03d}/{field_total:03d} value={chunk}"
            )
            if len(" ".join(content.split())) > 236:
                raise AssertionError("Core packet chunk is not visibility-safe")
            records.append(
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
                    "content": content,
                    "reward": reward,
                }
            )
        return tuple(records)


def _validate_predecessor_component(
    content: str | None,
    artifact_id: str | None,
    field_name: str,
) -> None:
    if (
        type(content) is not str
        or not content.strip()
        or len(content.encode("utf-8")) > _MAX_PREDECESSOR_COMPONENT_BYTES
        or type(artifact_id) is not str
        or not artifact_id
        or "/" in artifact_id
        or "\\" in artifact_id
    ):
        raise ValueError(f"predecessor {field_name} binding is invalid")


__all__ = [
    "PACKET_INPUT_SCHEMA",
    "PACKET_INPUT_SCHEMA_DIGEST",
    "PACKET_SCHEMA",
    "REFLECTOR_INSTRUCTION",
    "REFLECTOR_PROMPT_DIGEST",
    "SupervisedErrorCodeV1",
    "SupervisedEvolutionPacketV1",
]
