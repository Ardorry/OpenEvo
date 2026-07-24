"""Closed safe-feedback boundary for taskwise online ChemBench4K evolution.

The private evaluator may know the target, but this module exports only a
fixed taxonomy.  There is no free-text field, score field, target field, or
answer-bearing extension map that could cross into the Core evolution job.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from openevo_chembench.chembench4k_evaluation import (
    ChemBench4KParseStatus,
    PrivateChemBench4KEvaluation,
)


SAFE_SIGNAL_SCHEMA_V1 = "taskwise_safe_evolution_signal_v1"


class TaskwiseSafeSignalCodeV1(str, Enum):
    """Complete allowlist for evaluator-derived online feedback."""

    CORRECT = "correct"
    INCORRECT = "incorrect"
    PARSE_FAILURE = "parse_failure"
    FORMAT_VIOLATION = "format_violation"
    CONFIDENCE_ISSUE = "confidence_issue"
    UNIT_CONSISTENCY_ISSUE = "unit_consistency_issue"
    NUMERICAL_REASONING_ISSUE = "numerical_reasoning_issue"
    STRUCTURE_INTERPRETATION_ISSUE = "structure_interpretation_issue"
    REACTION_DIRECTION_ISSUE = "reaction_direction_issue"
    OPTION_ELIMINATION_ISSUE = "option_elimination_issue"


_FORBIDDEN_KEYS = frozenset(
    {
        "answer",
        "answer_mapping",
        "correct_answer",
        "correct_option",
        "correct_option_text",
        "option_text",
        "question",
        "rationale",
        "score",
        "target",
        "target_letter",
        "target_scores",
        "verifier_rationale",
    }
)


def _canonical_bytes(payload: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class TaskwiseSafeEvolutionSignalV1:
    """The only private-evaluation projection accepted by online evolution."""

    codes: tuple[TaskwiseSafeSignalCodeV1, ...]
    schema_version: str = SAFE_SIGNAL_SCHEMA_V1

    def __post_init__(self) -> None:
        if self.schema_version != SAFE_SIGNAL_SCHEMA_V1:
            raise ValueError("unexpected taskwise safe-feedback schema")
        if (
            not isinstance(self.codes, tuple)
            or not self.codes
            or any(type(code) is not TaskwiseSafeSignalCodeV1 for code in self.codes)
            or len(self.codes) != len(set(self.codes))
        ):
            raise ValueError("safe-feedback codes must be a unique non-empty tuple")
        outcomes = {
            code
            for code in self.codes
            if code
            in {
                TaskwiseSafeSignalCodeV1.CORRECT,
                TaskwiseSafeSignalCodeV1.INCORRECT,
            }
        }
        if len(outcomes) != 1:
            raise ValueError("safe feedback must contain exactly one correctness outcome")
        if (
            TaskwiseSafeSignalCodeV1.PARSE_FAILURE in self.codes
            and TaskwiseSafeSignalCodeV1.INCORRECT not in self.codes
        ):
            raise ValueError("parse failure cannot be paired with a correct outcome")

    def to_evolution_payload(self) -> dict[str, object]:
        """Serialize the exact closed payload accepted by Core event builders."""

        return {
            "schema_version": self.schema_version,
            "signals": [code.value for code in self.codes],
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical_bytes(self.to_evolution_payload())).hexdigest()


def safe_signal_from_private_evaluation(
    evaluation: PrivateChemBench4KEvaluation,
    *,
    diagnostic_codes: tuple[TaskwiseSafeSignalCodeV1, ...] = (),
) -> TaskwiseSafeEvolutionSignalV1:
    """Project one private result without copying its target or raw completion."""

    if type(evaluation) is not PrivateChemBench4KEvaluation:
        raise TypeError("evaluation must be exact PrivateChemBench4KEvaluation")
    if (
        not isinstance(diagnostic_codes, tuple)
        or any(type(code) is not TaskwiseSafeSignalCodeV1 for code in diagnostic_codes)
        or any(
            code
            in {
                TaskwiseSafeSignalCodeV1.CORRECT,
                TaskwiseSafeSignalCodeV1.INCORRECT,
                TaskwiseSafeSignalCodeV1.PARSE_FAILURE,
                TaskwiseSafeSignalCodeV1.FORMAT_VIOLATION,
            }
            for code in diagnostic_codes
        )
    ):
        raise ValueError("diagnostic codes must use only optional closed taxonomy values")

    codes: list[TaskwiseSafeSignalCodeV1] = [
        (
            TaskwiseSafeSignalCodeV1.CORRECT
            if evaluation.correct
            else TaskwiseSafeSignalCodeV1.INCORRECT
        )
    ]
    if evaluation.official.status is ChemBench4KParseStatus.NO_UPPERCASE:
        codes.append(TaskwiseSafeSignalCodeV1.PARSE_FAILURE)
    if evaluation.strict.status is ChemBench4KParseStatus.STRICT_FORMAT_MISMATCH:
        codes.append(TaskwiseSafeSignalCodeV1.FORMAT_VIOLATION)
    codes.extend(diagnostic_codes)
    return TaskwiseSafeEvolutionSignalV1(codes=tuple(codes))


def taskwise_safe_signal_from_payload(
    payload: Mapping[str, object],
) -> TaskwiseSafeEvolutionSignalV1:
    """Parse untrusted data through an exact recursive/structural allowlist."""

    if not isinstance(payload, Mapping):
        raise TypeError("safe-feedback payload must be a mapping")
    _reject_forbidden_recursive(payload)
    if set(payload) != {"schema_version", "signals"}:
        raise ValueError("safe-feedback payload contains non-allowlisted fields")
    signals = payload["signals"]
    if not isinstance(signals, list) or not signals:
        raise ValueError("safe-feedback signals must be a non-empty list")
    try:
        codes = tuple(TaskwiseSafeSignalCodeV1(value) for value in signals)
    except (TypeError, ValueError) as exc:
        raise ValueError("safe-feedback signal is outside the closed taxonomy") from exc
    return TaskwiseSafeEvolutionSignalV1(
        schema_version=payload["schema_version"],
        codes=codes,
    )


def _reject_forbidden_recursive(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("safe-feedback keys must be strings")
            normalized = key.casefold().replace("-", "_")
            if normalized in _FORBIDDEN_KEYS:
                raise ValueError("private answer-bearing field reached safe feedback")
            _reject_forbidden_recursive(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _reject_forbidden_recursive(item)
        return
    if value is not None and type(value) not in {str, int, float, bool}:
        raise TypeError("safe-feedback payload contains unsupported value type")


__all__ = [
    "SAFE_SIGNAL_SCHEMA_V1",
    "TaskwiseSafeEvolutionSignalV1",
    "TaskwiseSafeSignalCodeV1",
    "safe_signal_from_private_evaluation",
    "taskwise_safe_signal_from_payload",
]
