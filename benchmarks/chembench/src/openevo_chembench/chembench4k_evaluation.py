"""Official and strict parsing plus private ChemBench4K evaluation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

from openevo_chembench.chembench4k_models import PrivateChemBench4KTask


_STRICT_SINGLE_LETTER = re.compile(r"^\s*([ABCD])\s*$")


class ChemBench4KParseStatus(str, Enum):
    PARSED = "parsed"
    NO_UPPERCASE = "no_uppercase"
    FIRST_UPPERCASE_NOT_CHOICE = "first_uppercase_not_choice"
    STRICT_FORMAT_MISMATCH = "strict_format_mismatch"


@dataclass(frozen=True, slots=True)
class ChemBench4KParseResult:
    prediction: str | None
    status: ChemBench4KParseStatus

    @property
    def parsed(self) -> bool:
        return bool(self.prediction)


def official_first_capital_parser(raw_output: str) -> ChemBench4KParseResult:
    """Return the first uppercase character exactly as OpenCompass does.

    A non-label uppercase character is still a parsed prediction.  The private
    accuracy evaluator subsequently marks it wrong when it does not equal the
    A/B/C/D target; the parser must not skip ahead to a later answer label.
    """

    if type(raw_output) is not str:
        raise TypeError("raw_output must be a string")
    for character in raw_output:
        if character.isupper():
            return ChemBench4KParseResult(
                prediction=character,
                status=ChemBench4KParseStatus.PARSED,
            )
    return ChemBench4KParseResult(
        prediction="",
        status=ChemBench4KParseStatus.NO_UPPERCASE,
    )


def strict_single_letter_parser(raw_output: str) -> ChemBench4KParseResult:
    """Accept only one uppercase answer label surrounded by whitespace."""

    if type(raw_output) is not str:
        raise TypeError("raw_output must be a string")
    match = _STRICT_SINGLE_LETTER.fullmatch(raw_output)
    if match is None:
        return ChemBench4KParseResult(
            prediction=None,
            status=ChemBench4KParseStatus.STRICT_FORMAT_MISMATCH,
        )
    return ChemBench4KParseResult(
        prediction=match.group(1),
        status=ChemBench4KParseStatus.PARSED,
    )


@dataclass(frozen=True, slots=True)
class PublicChemBench4KEvaluation:
    """Target-free result safe for ordinary result JSON."""

    uid: str
    category: str
    official_prediction: str | None
    strict_prediction: str | None
    official_parse_status: str
    strict_parse_status: str
    official_accuracy: float
    strict_parse_success: bool

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "category": self.category,
            "official_prediction": self.official_prediction,
            "strict_prediction": self.strict_prediction,
            "official_parse_status": self.official_parse_status,
            "strict_parse_status": self.strict_parse_status,
            "official_accuracy": self.official_accuracy,
            "strict_parse_success": self.strict_parse_success,
        }


@dataclass(frozen=True, slots=True, repr=False)
class PrivateChemBench4KEvaluation:
    """Private result retaining the target for trusted paired statistics only."""

    task_uid: str
    category: str
    raw_completion: str
    target: str
    official: ChemBench4KParseResult
    strict: ChemBench4KParseResult
    correct: bool

    def __repr__(self) -> str:
        return "PrivateChemBench4KEvaluation(<redacted>)"

    __str__ = __repr__

    def to_public_result(self) -> PublicChemBench4KEvaluation:
        return PublicChemBench4KEvaluation(
            uid=self.task_uid,
            category=self.category,
            official_prediction=self.official.prediction,
            strict_prediction=self.strict.prediction,
            official_parse_status=self.official.status.value,
            strict_parse_status=self.strict.status.value,
            official_accuracy=1.0 if self.correct else 0.0,
            strict_parse_success=self.strict.parsed,
        )


class ChemBench4KPrivateEvaluator:
    """Read a target only inside this private evaluator boundary."""

    __slots__ = ()

    def evaluate(
        self,
        *,
        task: PrivateChemBench4KTask,
        raw_completion: str,
    ) -> PrivateChemBench4KEvaluation:
        if type(task) is not PrivateChemBench4KTask:
            raise TypeError("task must be an exact PrivateChemBench4KTask")
        if type(raw_completion) is not str:
            raise TypeError("raw_completion must be a string")
        official = official_first_capital_parser(raw_completion)
        strict = strict_single_letter_parser(raw_completion)
        return PrivateChemBench4KEvaluation(
            task_uid=task.uid,
            category=task.category,
            raw_completion=raw_completion,
            target=task.target,
            official=official,
            strict=strict,
            correct=official.prediction == task.target,
        )


__all__ = [
    "ChemBench4KParseResult",
    "ChemBench4KParseStatus",
    "ChemBench4KPrivateEvaluator",
    "PrivateChemBench4KEvaluation",
    "PublicChemBench4KEvaluation",
    "official_first_capital_parser",
    "strict_single_letter_parser",
]
