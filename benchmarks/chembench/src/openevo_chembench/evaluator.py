"""Private evaluator for the legacy jablonkagroup strict-MCQ subset."""

from __future__ import annotations

import re
from enum import Enum
from typing import Final

from openevo_chembench.config import CHEMBENCH_CONFIGS
from openevo_chembench.models import (
    CorrectnessBucket,
    ParseStatus,
    PrivateTargetScore,
    PrivateTask,
    RawAttempt,
    SealedEvaluation,
)


MULTIPLE_CHOICE_METRIC: Final = "multiple_choice_grade"
_ANSWER_TAG = re.compile(r"<answer>(.*?)</answer>", re.IGNORECASE | re.DOTALL)
_CHOICE_LABEL = re.compile(r"[A-Z]+")
_LOCAL_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class EvaluationErrorType(str, Enum):
    """Closed error codes safe for controller logs."""

    INVALID_ATTEMPT_TYPE = "invalid_attempt_type"
    INVALID_TASK_TYPE = "invalid_task_type"
    UNSUPPORTED_METRIC = "unsupported_metric"
    INVALID_PRIVATE_SCORES = "invalid_private_scores"
    INTERNAL_EVALUATION_ERROR = "internal_evaluation_error"


class PrivateEvaluationError(RuntimeError):
    """Sanitized evaluator failure with only allowlisted log fields."""

    __slots__ = ("config_name", "local_task_id", "error_type")

    def __init__(
        self,
        *,
        config_name: str,
        local_task_id: str,
        error_type: EvaluationErrorType,
    ) -> None:
        if type(config_name) is not str or config_name not in CHEMBENCH_CONFIGS:
            raise ValueError("PrivateEvaluationError requires a known config")
        if (
            type(local_task_id) is not str
            or _LOCAL_TASK_ID.fullmatch(local_task_id) is None
        ):
            raise ValueError("PrivateEvaluationError requires a safe local task id")
        if not isinstance(error_type, EvaluationErrorType):
            raise TypeError("error_type must be an EvaluationErrorType")
        self.config_name = config_name
        self.local_task_id = local_task_id
        self.error_type = error_type
        super().__init__(
            "private evaluation failed: "
            f"config={config_name} "
            f"local_task_id={local_task_id} "
            f"error_type={error_type.value}"
        )

    def to_log_fields(self) -> dict[str, str]:
        """Return the complete and only payload permitted in evaluator logs."""

        return {
            "config": self.config_name,
            "local_task_id": self.local_task_id,
            "error_type": self.error_type.value,
        }


class ChemBenchEvaluator:
    """Interpret ground truth only inside the trusted evaluator boundary."""

    __slots__ = ()

    def supports_task(self, task: PrivateTask) -> bool:
        """Return only whether a private task matches this evaluator protocol."""

        if type(task) is not PrivateTask:
            raise TypeError("supports_task requires an exact PrivateTask")
        return (
            task.metrics == (MULTIPLE_CHOICE_METRIC,)
            and _has_valid_single_choice_scores(task)
        )

    def evaluate(
        self,
        attempt: RawAttempt,
        task: PrivateTask,
        *,
        config_name: str,
        local_task_id: str,
    ) -> SealedEvaluation:
        """Evaluate one attempt without returning answer-bearing intermediates."""

        _validate_log_context(
            config_name=config_name,
            local_task_id=local_task_id,
        )
        if type(attempt) is not RawAttempt:
            raise _evaluation_error(
                config_name,
                local_task_id,
                EvaluationErrorType.INVALID_ATTEMPT_TYPE,
            )
        if type(task) is not PrivateTask:
            raise _evaluation_error(
                config_name,
                local_task_id,
                EvaluationErrorType.INVALID_TASK_TYPE,
            )
        if task.metrics != (MULTIPLE_CHOICE_METRIC,):
            raise _evaluation_error(
                config_name,
                local_task_id,
                EvaluationErrorType.UNSUPPORTED_METRIC,
            )
        if not _has_valid_single_choice_scores(task):
            raise _evaluation_error(
                config_name,
                local_task_id,
                EvaluationErrorType.INVALID_PRIVATE_SCORES,
            )

        choice_index, parse_status = _parse_choice_index(
            attempt.response,
            choice_count=len(task.options),
        )
        if choice_index is None:
            return SealedEvaluation(
                metric_name=MULTIPLE_CHOICE_METRIC,
                score=0.0,
                correctness_bucket=CorrectnessBucket.UNPARSEABLE,
                parse_status=parse_status,
            )

        try:
            selected_option = task.options[choice_index]
            score = _multiple_choice_grade(
                selected_options=(selected_option,),
                private_scores=task.target_scores,
            )
        except Exception:
            raise _evaluation_error(
                config_name,
                local_task_id,
                EvaluationErrorType.INTERNAL_EVALUATION_ERROR,
            ) from None

        return SealedEvaluation(
            metric_name=MULTIPLE_CHOICE_METRIC,
            score=score,
            correctness_bucket=(
                CorrectnessBucket.CORRECT
                if score == 1.0
                else CorrectnessBucket.INCORRECT
            ),
            parse_status=ParseStatus.PARSED,
        )


def evaluate_attempt(
    attempt: RawAttempt,
    task: PrivateTask,
    *,
    config_name: str,
    local_task_id: str,
) -> SealedEvaluation:
    """Convenience entrypoint retaining the exact private evaluator boundary."""

    return ChemBenchEvaluator().evaluate(
        attempt,
        task,
        config_name=config_name,
        local_task_id=local_task_id,
    )


def _validate_log_context(*, config_name: str, local_task_id: str) -> None:
    if type(config_name) is not str or config_name not in CHEMBENCH_CONFIGS:
        raise ValueError("config_name must be a supported ChemBench config")
    if (
        type(local_task_id) is not str
        or _LOCAL_TASK_ID.fullmatch(local_task_id) is None
    ):
        raise ValueError("local_task_id must be a safe controller-local identifier")


def _evaluation_error(
    config_name: str,
    local_task_id: str,
    error_type: EvaluationErrorType,
) -> PrivateEvaluationError:
    return PrivateEvaluationError(
        config_name=config_name,
        local_task_id=local_task_id,
        error_type=error_type,
    )


def _parse_choice_index(
    response: str,
    *,
    choice_count: int,
) -> tuple[int | None, ParseStatus]:
    matches = _ANSWER_TAG.findall(response)
    if not matches:
        return None, ParseStatus.MISSING_ANSWER_TAG
    if len(matches) != 1:
        return None, ParseStatus.MULTIPLE_ANSWER_TAGS

    answer = matches[0].strip()
    if not answer:
        return None, ParseStatus.EMPTY_ANSWER_TAG
    normalized_label = answer.upper()
    if _CHOICE_LABEL.fullmatch(normalized_label) is None:
        return None, ParseStatus.INVALID_CHOICE_LABEL

    choice_index = _choice_label_to_index(normalized_label)
    if choice_index >= choice_count:
        return None, ParseStatus.INVALID_CHOICE_LABEL
    return choice_index, ParseStatus.PARSED


def _choice_label_to_index(label: str) -> int:
    value = 0
    for character in label:
        value = value * 26 + (ord(character) - ord("A") + 1)
    return value - 1


def _has_valid_single_choice_scores(task: PrivateTask) -> bool:
    if not task.options or len(task.target_scores) != len(task.options):
        return False
    correct_bindings = 0
    for binding in task.target_scores:
        score = float(binding.score)
        if score not in (0.0, 1.0):
            return False
        if score == 1.0:
            correct_bindings += 1
    return correct_bindings == 1


def _multiple_choice_grade(
    *,
    selected_options: tuple[str, ...],
    private_scores: tuple[PrivateTargetScore, ...],
) -> float:
    """Match ChemBench's sum-of-selected-target-scores metric privately."""

    total = 0.0
    for selected_option in selected_options:
        for binding in private_scores:
            if binding.option == selected_option:
                total += float(binding.score)
                break
        else:
            raise LookupError("selected option has no private score binding")
    return total


__all__ = [
    "MULTIPLE_CHOICE_METRIC",
    "ChemBenchEvaluator",
    "EvaluationErrorType",
    "PrivateEvaluationError",
    "evaluate_attempt",
]
