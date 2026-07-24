from __future__ import annotations

import json
import unittest
from dataclasses import fields

from openevo_chembench.evaluator import (
    ChemBenchEvaluator,
    EvaluationErrorType,
    PrivateEvaluationError,
)
from openevo_chembench.feedback import SafeEvolutionFeedback
from openevo_chembench.models import (
    CorrectnessBucket,
    FailureCategory,
    ParseStatus,
    PrivateTargetScore,
    PrivateTask,
    RawAttempt,
    SafeEvolutionSignal,
    SealedEvaluation,
    SignalOutcome,
    SignalSeverity,
    TranscriptReference,
)


_CONFIG = "general_chemistry"
_LOCAL_TASK_ID = "general_chemistry-000001"
_PRIVATE_UUID = "private-benchmark-uuid"
_CORRECT_OPTION = "private-correct-option-literal"


def _private_task() -> PrivateTask:
    return PrivateTask(
        question="Synthetic public chemistry question?",
        options=("public-wrong-option-literal", _CORRECT_OPTION),
        target="",
        target_scores=(
            PrivateTargetScore(option="public-wrong-option-literal", score=0.0),
            PrivateTargetScore(option=_CORRECT_OPTION, score=1.0),
        ),
        metrics=("multiple_choice_grade",),
        uuid=_PRIVATE_UUID,
    )


def _attempt(label: str) -> RawAttempt:
    return RawAttempt(
        response=f"Reasoning omitted.\n<answer>{label}</answer>",
        transcript_reference=TranscriptReference(
            reference="sealed/transcripts/attempt.jsonl"
        ),
    )


def _evaluate(label: str) -> SealedEvaluation:
    return ChemBenchEvaluator().evaluate(
        _attempt(label),
        _private_task(),
        config_name=_CONFIG,
        local_task_id=_LOCAL_TASK_ID,
    )


class EvaluatorBoundaryTests(unittest.TestCase):
    def test_capability_check_returns_only_a_boolean(self) -> None:
        evaluator = ChemBenchEvaluator()
        unsupported_task = PrivateTask(
            question="private unsupported question",
            options=(),
            target="private unsupported target",
            target_scores=(),
            metrics=("exact_str_match",),
            uuid="private unsupported uuid",
        )

        self.assertIs(evaluator.supports_task(_private_task()), True)
        self.assertIs(evaluator.supports_task(unsupported_task), False)
        with self.assertRaises(TypeError):
            evaluator.supports_task({"metrics": ["multiple_choice_grade"]})  # type: ignore[arg-type]

    def test_evaluator_reads_private_target_scores(self) -> None:
        evaluation = _evaluate("B")

        self.assertEqual(evaluation.metric_name, "multiple_choice_grade")
        self.assertEqual(evaluation.score, 1.0)
        self.assertIs(evaluation.correctness_bucket, CorrectnessBucket.CORRECT)
        self.assertIs(evaluation.parse_status, ParseStatus.PARSED)

    def test_sealed_serialization_contains_only_allowlisted_fields(self) -> None:
        evaluation = _evaluate("B")

        payload = evaluation.to_private_payload()
        serialized = json.dumps(payload, sort_keys=True)

        self.assertEqual(
            {item.name for item in fields(SealedEvaluation)},
            {
                "metric_name",
                "score",
                "correctness_bucket",
                "parse_status",
            },
        )
        self.assertEqual(
            set(payload),
            {
                "metric_name",
                "score",
                "correctness_bucket",
                "parse_status",
            },
        )
        for forbidden in (
            "target",
            "target_scores",
            "answer",
            "option",
            "choices",
        ):
            self.assertNotIn(forbidden, _all_keys(payload))
        self.assertNotIn(_CORRECT_OPTION, serialized)
        self.assertNotIn(_PRIVATE_UUID, serialized)
        self.assertFalse(hasattr(evaluation, "to_evolution_payload"))

    def test_safe_signal_excludes_private_and_question_fields(self) -> None:
        signal = SafeEvolutionFeedback().generate(_evaluate("A"))

        payload = signal.to_evolution_payload()
        serialized = json.dumps(payload, sort_keys=True)

        self.assertEqual(
            {item.name for item in fields(SafeEvolutionSignal)},
            {"schema_version", "outcome", "failure_categories", "severity"},
        )
        self.assertEqual(
            set(payload),
            {"schema_version", "outcome", "failure_categories", "severity"},
        )
        for forbidden in (
            "question",
            "uuid",
            "correct_answer",
            "target",
            "score",
            "response",
            "transcript",
            "option",
        ):
            self.assertNotIn(forbidden, _all_keys(payload))
        for private_literal in (
            "Synthetic public chemistry question?",
            _PRIVATE_UUID,
            _CORRECT_OPTION,
        ):
            self.assertNotIn(private_literal, serialized)

    def test_wrong_answer_signal_never_contains_correct_option(self) -> None:
        evaluation = _evaluate("A")
        signal = SafeEvolutionFeedback().generate(evaluation)
        payload = signal.to_evolution_payload()

        self.assertEqual(evaluation.score, 0.0)
        self.assertIs(evaluation.correctness_bucket, CorrectnessBucket.INCORRECT)
        self.assertIs(signal.outcome, SignalOutcome.NEEDS_IMPROVEMENT)
        self.assertEqual(
            signal.failure_categories,
            (FailureCategory.INCORRECT_SELECTION,),
        )
        self.assertIs(signal.severity, SignalSeverity.MEDIUM)
        values = _all_scalar_values(payload)
        self.assertTrue({"A", "B"}.isdisjoint(values))
        self.assertNotIn(_CORRECT_OPTION, values)
        self.assertNotIn("public-wrong-option-literal", values)

    def test_format_failure_maps_to_closed_high_severity_signal(self) -> None:
        evaluation = ChemBenchEvaluator().evaluate(
            RawAttempt(
                response="No answer tag was produced.",
                transcript_reference=TranscriptReference(
                    reference="sealed/transcripts/missing-tag.jsonl"
                ),
            ),
            _private_task(),
            config_name=_CONFIG,
            local_task_id=_LOCAL_TASK_ID,
        )
        signal = SafeEvolutionFeedback().generate(evaluation)

        self.assertIs(evaluation.correctness_bucket, CorrectnessBucket.UNPARSEABLE)
        self.assertIs(evaluation.parse_status, ParseStatus.MISSING_ANSWER_TAG)
        self.assertEqual(evaluation.score, 0.0)
        self.assertIs(signal.outcome, SignalOutcome.INCOMPLETE)
        self.assertEqual(
            signal.failure_categories,
            (FailureCategory.FORMAT_FAILURE,),
        )
        self.assertIs(signal.severity, SignalSeverity.HIGH)

    def test_multiple_answer_tags_fail_closed_without_selecting_one(self) -> None:
        evaluation = ChemBenchEvaluator().evaluate(
            RawAttempt(
                response="<answer>A</answer>\n<answer>B</answer>",
                transcript_reference=TranscriptReference(
                    reference="sealed/transcripts/ambiguous.jsonl"
                ),
            ),
            _private_task(),
            config_name=_CONFIG,
            local_task_id=_LOCAL_TASK_ID,
        )
        signal = SafeEvolutionFeedback().generate(evaluation)

        self.assertIs(evaluation.correctness_bucket, CorrectnessBucket.UNPARSEABLE)
        self.assertIs(evaluation.parse_status, ParseStatus.MULTIPLE_ANSWER_TAGS)
        self.assertEqual(
            signal.to_evolution_payload(),
            {
                "schema_version": "1",
                "outcome": "incomplete",
                "failure_categories": ["format_failure"],
                "severity": "high",
            },
        )

    def test_error_has_only_sanitized_log_fields(self) -> None:
        unsupported_task = PrivateTask(
            question="private-question-that-must-not-be-logged",
            options=(),
            target="private-answer-that-must-not-be-logged",
            target_scores=(),
            metrics=("exact_str_match",),
            uuid="private-uuid-that-must-not-be-logged",
        )

        with self.assertRaises(PrivateEvaluationError) as raised:
            ChemBenchEvaluator().evaluate(
                _attempt("A"),
                unsupported_task,
                config_name=_CONFIG,
                local_task_id=_LOCAL_TASK_ID,
            )

        error = raised.exception
        self.assertIs(error.error_type, EvaluationErrorType.UNSUPPORTED_METRIC)
        self.assertEqual(
            error.to_log_fields(),
            {
                "config": _CONFIG,
                "local_task_id": _LOCAL_TASK_ID,
                "error_type": "unsupported_metric",
            },
        )
        rendered = str(error)
        for forbidden_literal in (
            unsupported_task.question,
            unsupported_task.target,
            unsupported_task.uuid,
            _attempt("A").response,
        ):
            self.assertNotIn(forbidden_literal, rendered)


def _all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        keys = set(value)
        for item in value.values():
            keys.update(_all_keys(item))
        return keys
    if isinstance(value, list):
        keys: set[str] = set()
        for item in value:
            keys.update(_all_keys(item))
        return keys
    return set()


def _all_scalar_values(value: object) -> set[object]:
    if isinstance(value, dict):
        values: set[object] = set()
        for item in value.values():
            values.update(_all_scalar_values(item))
        return values
    if isinstance(value, list):
        values: set[object] = set()
        for item in value:
            values.update(_all_scalar_values(item))
        return values
    return {value}


if __name__ == "__main__":
    unittest.main()
