from __future__ import annotations

import dataclasses

import pytest

from openevo_chembench.chembench4k_evaluation import ChemBench4KPrivateEvaluator
from openevo_chembench.chembench4k_models import PrivateChemBench4KTask
from openevo_chembench.taskwise_feedback_v1 import (
    TaskwiseSafeEvolutionSignalV1,
    TaskwiseSafeSignalCodeV1,
    safe_signal_from_private_evaluation,
    taskwise_safe_signal_from_payload,
)


def _task(*, target: str = "A") -> PrivateChemBench4KTask:
    return PrivateChemBench4KTask(
        uid="a" * 64,
        category="Name_Conversion",
        source_split="test",
        source_index=0,
        question="private sentinel question",
        A="private correct option",
        B="other option",
        C="third option",
        D="fourth option",
        target=target,
        dataset_revision="f8ad41a980170f4c5d0cc97e57722d06887c8f53",
        dataset_sha256="b" * 64,
    )


@pytest.mark.parametrize(
    ("completion", "expected"),
    [
        ("A", ("correct",)),
        ("B", ("incorrect",)),
        ("", ("incorrect", "parse_failure", "format_violation")),
        ("The answer is A", ("incorrect", "format_violation")),
        ("A because", ("correct", "format_violation")),
    ],
)
def test_private_evaluation_projects_only_closed_feedback(
    completion: str,
    expected: tuple[str, ...],
) -> None:
    evaluation = ChemBench4KPrivateEvaluator().evaluate(
        task=_task(),
        raw_completion=completion,
    )

    signal = safe_signal_from_private_evaluation(evaluation)

    assert tuple(code.value for code in signal.codes) == expected
    payload = signal.to_evolution_payload()
    assert payload == {
        "schema_version": "taskwise_safe_evolution_signal_v1",
        "signals": list(expected),
    }
    serialized = repr(payload).casefold()
    assert "target" not in serialized
    assert "private sentinel" not in serialized
    assert "correct option" not in serialized
    assert len(signal.digest) == 64


@pytest.mark.parametrize(
    "payload",
    [
        {
            "schema_version": "taskwise_safe_evolution_signal_v1",
            "signals": ["incorrect"],
            "target": "A",
        },
        {
            "schema_version": "taskwise_safe_evolution_signal_v1",
            "signals": ["incorrect"],
            "nested": {"correct_answer": "A"},
        },
        {
            "schema_version": "taskwise_safe_evolution_signal_v1",
            "signals": ["incorrect"],
            "verifier": {"rationale": "the answer is A"},
        },
        {
            "schema_version": "taskwise_safe_evolution_signal_v1",
            "signals": ["incorrect", "answer_is_a"],
        },
    ],
)
def test_recursive_answer_leakage_is_rejected(payload: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        taskwise_safe_signal_from_payload(payload)


def test_signal_has_no_free_text_or_mutation_channel() -> None:
    fields = {field.name for field in dataclasses.fields(TaskwiseSafeEvolutionSignalV1)}
    assert fields == {"codes", "schema_version"}
    signal = TaskwiseSafeEvolutionSignalV1(codes=(TaskwiseSafeSignalCodeV1.INCORRECT,))
    with pytest.raises(dataclasses.FrozenInstanceError):
        signal.codes = (TaskwiseSafeSignalCodeV1.CORRECT,)  # type: ignore[misc]


def test_optional_diagnostics_are_closed_and_answer_free() -> None:
    evaluation = ChemBench4KPrivateEvaluator().evaluate(
        task=_task(),
        raw_completion="B",
    )
    signal = safe_signal_from_private_evaluation(
        evaluation,
        diagnostic_codes=(
            TaskwiseSafeSignalCodeV1.UNIT_CONSISTENCY_ISSUE,
            TaskwiseSafeSignalCodeV1.REACTION_DIRECTION_ISSUE,
        ),
    )
    assert tuple(code.value for code in signal.codes) == (
        "incorrect",
        "unit_consistency_issue",
        "reaction_direction_issue",
    )
