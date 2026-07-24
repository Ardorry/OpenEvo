from __future__ import annotations

import pickle
import unittest
from dataclasses import FrozenInstanceError, fields

from openevo_chembench.models import (
    AgentRuntimeMetadata,
    CorrectnessBucket,
    FailureCategory,
    ParseStatus,
    PrivateTargetScore,
    PrivateTask,
    PublicChoice,
    PublicPrompt,
    RawAttempt,
    SafeEvolutionSignal,
    SealedEvaluation,
    SignalOutcome,
    SignalSeverity,
    TranscriptReference,
)


class BoundaryModelTests(unittest.TestCase):
    def test_private_task_is_redacted_and_has_no_public_dump_api(self) -> None:
        task = PrivateTask(
            question="private question literal",
            options=("choice one", "choice two"),
            target="choice two",
            target_scores=(
                PrivateTargetScore(option="choice one", score=0.0),
                PrivateTargetScore(option="choice two", score=1.0),
            ),
            metrics=("multiple_choice_grade",),
            uuid="private-uuid",
        )

        self.assertEqual(repr(task), "PrivateTask(<redacted>)")
        self.assertNotIn("private question literal", repr(task))
        self.assertFalse(hasattr(task, "to_agent_payload"))
        self.assertFalse(hasattr(task, "to_evolution_payload"))
        with self.assertRaises(TypeError):
            pickle.dumps(task)
        with self.assertRaises(FrozenInstanceError):
            task.question = "changed"  # type: ignore[misc]

    def test_public_prompt_schema_contains_only_public_fields(self) -> None:
        prompt = PublicPrompt(
            question="Choose the valid statement.",
            choices=(
                PublicChoice(label="A", text="first"),
                PublicChoice(label="B", text="second"),
            ),
            answer_format="Return one option label.",
        )

        self.assertEqual(
            {item.name for item in fields(PublicPrompt)},
            {"question", "choices", "answer_format"},
        )
        self.assertEqual(
            prompt.to_agent_payload(),
            {
                "question": "Choose the valid statement.",
                "choices": [
                    {"label": "A", "text": "first"},
                    {"label": "B", "text": "second"},
                ],
                "answer_format": "Return one option label.",
            },
        )
        with self.assertRaises(TypeError):
            PublicPrompt(  # type: ignore[call-arg]
                question="q",
                choices=(),
                answer_format="format",
                target="forbidden",
            )

    def test_private_task_accepts_target_scores_as_the_ground_truth_form(self) -> None:
        task = PrivateTask(
            question="private question",
            options=("first", "second"),
            target="",
            target_scores=(
                PrivateTargetScore(option="first", score=0.0),
                PrivateTargetScore(option="second", score=1.0),
            ),
            metrics=("multiple_choice_grade",),
            uuid="private-uuid",
        )

        self.assertEqual(task.target, "")
        with self.assertRaises(ValueError):
            PrivateTask(
                question="private question",
                options=(),
                target="",
                target_scores=(),
                metrics=("exact_match",),
                uuid="private-uuid",
            )

    def test_raw_attempt_cannot_carry_ground_truth_by_schema(self) -> None:
        attempt = RawAttempt(
            response="agent response",
            transcript_reference=TranscriptReference(reference="sealed/transcript.jsonl"),
        )

        self.assertEqual(
            {item.name for item in fields(RawAttempt)},
            {"response", "transcript_reference", "runtime_metadata"},
        )
        self.assertEqual(
            {item.name for item in fields(AgentRuntimeMetadata)},
            {
                "duration_ms",
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
                "harness",
                "model",
                "capture_mode",
                "execution_backend",
                "codex_cli_version",
                "started_at_utc",
            },
        )
        self.assertEqual(repr(attempt), "RawAttempt(<redacted>)")
        with self.assertRaises(TypeError):
            RawAttempt(  # type: ignore[call-arg]
                response="agent response",
                transcript_reference=TranscriptReference(reference="sealed/t.jsonl"),
                target="forbidden",
            )

    def test_sealed_evaluation_has_no_evolution_serialization(self) -> None:
        evaluation = SealedEvaluation(
            metric_name="multiple_choice_grade",
            score=1.0,
            correctness_bucket=CorrectnessBucket.CORRECT,
            parse_status=ParseStatus.PARSED,
        )

        self.assertEqual(repr(evaluation), "SealedEvaluation(<redacted>)")
        self.assertFalse(hasattr(evaluation, "to_evolution_payload"))
        self.assertEqual(
            evaluation.to_private_payload(),
            {
                "metric_name": "multiple_choice_grade",
                "score": 1.0,
                "correctness_bucket": "correct",
                "parse_status": "parsed",
            },
        )

    def test_safe_signal_serializes_only_closed_taxonomy(self) -> None:
        signal = SafeEvolutionSignal(
            outcome=SignalOutcome.NEEDS_IMPROVEMENT,
            failure_categories=(
                FailureCategory.CALCULATION_VERIFICATION,
                FailureCategory.REASONING_CHECK,
            ),
            severity=SignalSeverity.MEDIUM,
        )

        self.assertEqual(
            signal.to_evolution_payload(),
            {
                "schema_version": "1",
                "outcome": "needs_improvement",
                "failure_categories": [
                    "calculation_verification",
                    "reasoning_check",
                ],
                "severity": "medium",
            },
        )
        self.assertEqual(
            {item.name for item in fields(SafeEvolutionSignal)},
            {"outcome", "failure_categories", "severity", "schema_version"},
        )

    def test_safe_signal_rejects_free_text_categories(self) -> None:
        with self.assertRaises(TypeError):
            SafeEvolutionSignal(
                outcome=SignalOutcome.NEEDS_IMPROVEMENT,
                failure_categories=("the answer is B",),  # type: ignore[arg-type]
                severity=SignalSeverity.MEDIUM,
            )


if __name__ == "__main__":
    unittest.main()
