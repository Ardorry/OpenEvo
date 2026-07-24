"""Deterministic projection from sealed evaluation to safe evolution feedback."""

from __future__ import annotations

from openevo_chembench.models import (
    CorrectnessBucket,
    FailureCategory,
    SafeEvolutionSignal,
    SealedEvaluation,
    SignalOutcome,
    SignalSeverity,
)


class SafeEvolutionFeedback:
    """Generate a closed taxonomy without inspecting task or attempt content."""

    __slots__ = ()

    def generate(self, evaluation: SealedEvaluation) -> SafeEvolutionSignal:
        """Project one exact sealed result into the evolution-safe DTO."""

        if type(evaluation) is not SealedEvaluation:
            raise TypeError(
                "SafeEvolutionFeedback.generate requires an exact SealedEvaluation"
            )

        if evaluation.correctness_bucket is CorrectnessBucket.CORRECT:
            return SafeEvolutionSignal(
                outcome=SignalOutcome.SATISFACTORY,
                failure_categories=(),
                severity=SignalSeverity.NONE,
            )
        if evaluation.correctness_bucket is CorrectnessBucket.UNPARSEABLE:
            return SafeEvolutionSignal(
                outcome=SignalOutcome.INCOMPLETE,
                failure_categories=(FailureCategory.FORMAT_FAILURE,),
                severity=SignalSeverity.HIGH,
            )
        return SafeEvolutionSignal(
            outcome=SignalOutcome.NEEDS_IMPROVEMENT,
            failure_categories=(FailureCategory.INCORRECT_SELECTION,),
            severity=SignalSeverity.MEDIUM,
        )


def generate_safe_evolution_signal(
    evaluation: SealedEvaluation,
) -> SafeEvolutionSignal:
    """Convenience entrypoint for the same strict sealed-to-safe projection."""

    return SafeEvolutionFeedback().generate(evaluation)


__all__ = [
    "SafeEvolutionFeedback",
    "generate_safe_evolution_signal",
]
