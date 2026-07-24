from __future__ import annotations

import dataclasses

import pytest

from openevo_chembench.chembench4k_evaluation import ChemBench4KPrivateEvaluator
from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_CATEGORIES,
    PrivateChemBench4KTask,
    RenderedChemBench4KPrompt,
)
from openevo_chembench.models import RawAttempt, TranscriptReference
from openevo_chembench.taskwise_feedback_v1 import (
    safe_signal_from_private_evaluation,
)
from openevo_chembench.taskwise_trajectory_v1 import (
    TaskwiseTrajectoryV1,
    ordered_safe_feedback_digest,
    ordered_taskwise_trajectory_digest,
)


def _private_task(
    *,
    category: str = "Name_Conversion",
    target: str = "B",
) -> PrivateChemBench4KTask:
    return PrivateChemBench4KTask(
        uid="a" * 64,
        category=category,
        source_split="test",
        source_index=3,
        question="Which public compound name is represented?",
        A="public option alpha",
        B="public option beta",
        C="public option gamma",
        D="public option delta",
        target=target,
        dataset_revision="f8ad41a980170f4c5d0cc97e57722d06887c8f53",
        dataset_sha256="b" * 64,
    )


def _trajectory(
    round_index: int,
    response: str,
    *,
    category: str = "Name_Conversion",
    target: str = "B",
) -> TaskwiseTrajectoryV1:
    task = _private_task(category=category, target=target)
    prompt = RenderedChemBench4KPrompt(
        uid=task.uid,
        category=task.category,
        dataset_revision=task.dataset_revision,
        demonstration_uids=(),
        text=(
            "Question: Which public compound name is represented?\n"
            "A. public option alpha\nB. public option beta\n"
            "C. public option gamma\nD. public option delta\nAnswer:"
        ),
    )
    attempt = RawAttempt(
        response=response,
        transcript_reference=TranscriptReference(reference=f"synthetic:round-{round_index}"),
    )
    evaluation = ChemBench4KPrivateEvaluator().evaluate(
        task=task,
        raw_completion=response,
    )
    return TaskwiseTrajectoryV1.from_attempt(
        task_uid=task.uid,
        task_index=0,
        category=task.category,
        round_index=round_index,  # type: ignore[arg-type]
        session_id=f"session-task-0-round-{round_index}",
        prompt=prompt,
        attempt=attempt,
        safe_feedback=safe_signal_from_private_evaluation(evaluation),
        dataset_sha256=task.dataset_sha256,
    )


def test_core_trajectory_is_target_free_and_digest_bound() -> None:
    trajectory = _trajectory(0, "A")
    payload = trajectory.to_core_record()
    serialized = repr(payload).casefold()
    assert "target" not in serialized
    assert "correct_option" not in serialized
    assert "verifier_rationale" not in serialized
    assert payload["safe_feedback"] == {
        "schema_version": "taskwise_safe_evolution_signal_v1",
        "signals": ["incorrect"],
    }
    assert len(trajectory.digest) == 64
    assert "public option alpha" not in repr(trajectory)


def test_update_two_digest_binds_both_ordered_trajectories() -> None:
    first = _trajectory(0, "A")
    second = _trajectory(1, "B")
    prefix = (first, second)
    assert len(ordered_taskwise_trajectory_digest(prefix)) == 64
    assert len(ordered_safe_feedback_digest(prefix)) == 64
    assert ordered_taskwise_trajectory_digest(prefix) != (
        ordered_taskwise_trajectory_digest((first,))
    )
    with pytest.raises(ValueError):
        ordered_taskwise_trajectory_digest((second, first))


@pytest.mark.parametrize("category", CHEMBENCH4K_CATEGORIES)
def test_reflector_projection_cannot_reveal_choice_letter(category: str) -> None:
    trajectories = tuple(
        _trajectory(
            0,
            response,
            category=category,
            target=response,
        )
        for response in ("A", "B", "C", "D")
    )

    projections = {repr(trajectory.to_reflector_projection()) for trajectory in trajectories}
    assert len(projections) == 1
    projection = trajectories[0].to_reflector_projection()
    assert set(projection) == {
        "schema_version",
        "protocol_id",
        "category",
        "round_index",
        "safe_feedback",
        "safe_feedback_digest",
    }
    assert projection["safe_feedback"] == {
        "schema_version": "taskwise_safe_evolution_signal_v1",
        "signals": ["correct"],
    }


def test_trajectory_cannot_be_mutated_or_built_with_target_key() -> None:
    trajectory = _trajectory(0, "A")
    with pytest.raises(dataclasses.FrozenInstanceError):
        trajectory.raw_completion = "B"  # type: ignore[misc]
