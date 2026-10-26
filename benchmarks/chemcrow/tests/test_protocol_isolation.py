from __future__ import annotations

import pytest

from openevo_chemcrow.hashing import canonical_sha256
from openevo_chemcrow.models import (
    ArtifactKind,
    ArtifactReceipt,
    EvaluatorFeedback,
    FeedbackMode,
    RubricScores,
    TaskItem,
    Trajectory,
)
from openevo_chemcrow.protocol import BlindJudgeResult, TaskLocalProtocolRunner


class FakeCandidate:
    config_sha256 = "s0"

    def __init__(self):
        self.calls = []

    def run_candidate(self, *, task, role, artifact_ids, pair_id, mcp_url):
        self.calls.append((task.task_id, role, list(artifact_ids)))
        return Trajectory(
            run_id=f"{pair_id}-{role}",
            task_id=task.task_id,
            role=role,
            status="COMPLETED",
            answer=f"{role} answer",
            candidate_config_sha256=self.config_sha256,
            artifact_ids=artifact_ids,
        )


class FakeEvolution:
    def __init__(self):
        self.calls = 0

    def evolve(self, *, task, baseline, feedback_payload, pair_id):
        self.calls += 1
        return ArtifactReceipt(
            task_id=task.task_id,
            parent_baseline_run=baseline.run_id,
            reflector_run_id=f"reflector-{pair_id}",
            reflector_input_hash=canonical_sha256(feedback_payload),
            artifact_type=ArtifactKind.TEXT_MEMORY,
            artifact_id=f"artifact-{task.task_id}",
            content_hash="a" * 64,
            size_bytes=10,
            generation_time_seconds=0.1,
        )


class FakeEvolutionEvaluator:
    evaluator_id = "evolution-evaluator-config-a"

    def evaluate(self, *, task, trajectory):
        return EvaluatorFeedback(
            evaluator_role="evolution_evaluator",
            evaluator_run_id=f"eval-{trajectory.run_id}",
            scores=RubricScores(chemical_correctness=2, reasoning_quality=2, task_completion=2),
            actionable_critique=["be more precise"],
        )


class FakeFinalEvaluator:
    evaluator_id = "final-evaluator-config-b"

    def compare(self, *, task, answer_a, answer_b):
        return BlindJudgeResult(
            scores_a=RubricScores(chemical_correctness=2, reasoning_quality=2, task_completion=2),
            scores_b=RubricScores(chemical_correctness=3, reasoning_quality=3, task_completion=3),
            winner="B",
            confidence=0.7,
        )


def _another_task(task_item: TaskItem) -> TaskItem:
    body = task_item.model_dump(mode="json")
    body["task_id"] = "chemcrow-test-02"
    body["prompt"] = "A second independent chemistry task."
    body["sanitized_item_sha256"] = canonical_sha256({"task_id": body["task_id"], "prompt": body["prompt"]})
    return TaskItem.model_validate(body)


def test_task_local_artifact_isolation_reset_and_single_step(tmp_path, task_item):
    candidate = FakeCandidate()
    evolution = FakeEvolution()
    runner = TaskLocalProtocolRunner(
        run_root=tmp_path / "runs",
        candidate=candidate,
        evolution=evolution,
        evolution_evaluator=FakeEvolutionEvaluator(),
        final_evaluator=FakeFinalEvaluator(),
        feedback_mode=FeedbackMode.F2,
        s0_hash="s0",
        real_mode=False,
        ledger_root=tmp_path / "claims",
    )
    first = runner.run_item(task_item, pair_id="pair-1")
    second = runner.run_item(_another_task(task_item), pair_id="pair-2")
    assert evolution.calls == 2
    assert candidate.calls == [
        ("chemcrow-test-01", "baseline", []),
        ("chemcrow-test-01", "evolved", ["artifact-chemcrow-test-01"]),
        ("chemcrow-test-02", "baseline", []),
        ("chemcrow-test-02", "evolved", ["artifact-chemcrow-test-02"]),
    ]
    assert first.s0_hash == second.s0_hash == "s0"
    assert first.event_order.count("reflector") == second.event_order.count("reflector") == 1
    assert (tmp_path / "runs" / "pair-1" / "reset.receipt.json").is_file()
    assert (tmp_path / "runs" / "pair-2" / "reset.receipt.json").is_file()


def test_evaluator_must_be_separate(tmp_path):
    final = FakeFinalEvaluator()
    final.evaluator_id = FakeEvolutionEvaluator.evaluator_id
    with pytest.raises(ValueError, match="independently configured"):
        TaskLocalProtocolRunner(
            run_root=tmp_path,
            candidate=FakeCandidate(),
            evolution=FakeEvolution(),
            evolution_evaluator=FakeEvolutionEvaluator(),
            final_evaluator=final,
            feedback_mode=FeedbackMode.F2,
            s0_hash="s0",
            real_mode=False,
        )
