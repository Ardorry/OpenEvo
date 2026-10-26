from __future__ import annotations

import json

import pytest

from openevo_chemcrow.compatible_evaluation import (
    CompatibleOpenEvoEvolutionEvaluator,
    CompatibleOpenEvoFinalEvaluator,
    build_evolution_evaluator_prompt,
    normalize_internal_confidence,
)
from openevo_chemcrow.evaluation import OpenEvoEvolutionEvaluator, OpenEvoFinalEvaluator
from openevo_chemcrow.models import Trajectory


class _Rollout:
    config_sha256 = "config"

    def __init__(
        self,
        confidence: float,
        *,
        actionable_critique: str | list[str] | None = None,
    ) -> None:
        self.confidence = confidence
        self.actionable_critique = [] if actionable_critique is None else actionable_critique
        self.tasks = []

    def run_candidate(self, *, task, role, artifact_ids, pair_id, mcp_url):
        self.tasks.append(task)
        return Trajectory(
            run_id=f"{pair_id}-baseline",
            task_id=task.task_id,
            role="baseline",
            status="COMPLETED",
            answer=json.dumps(
                {
                    "scores": {
                        "chemical_correctness": 3,
                        "reasoning_quality": 4,
                        "task_completion": 2,
                    },
                    "strengths": ["grounded"],
                    "weaknesses": [],
                    "actionable_critique": self.actionable_critique,
                    "confidence": self.confidence,
                }
            ),
            candidate_config_sha256=self.config_sha256,
        )


class _FinalRollout:
    config_sha256 = "final-config"

    def __init__(self) -> None:
        self.tasks = []

    def run_candidate(self, *, task, role, artifact_ids, pair_id, mcp_url):
        self.tasks.append(task)
        return Trajectory(
            run_id=f"{pair_id}-baseline",
            task_id=task.task_id,
            role="baseline",
            status="COMPLETED",
            answer=json.dumps(
                {
                    "scores_a": {
                        "chemical_correctness": 3,
                        "reasoning_quality": 4,
                        "task_completion": 2,
                    },
                    "scores_b": {
                        "chemical_correctness": 2,
                        "reasoning_quality": 3,
                        "task_completion": 2,
                    },
                    "winner": "A",
                    "confidence": 0.8,
                }
            ),
            candidate_config_sha256=self.config_sha256,
        )


def test_confidence_transport_adapter_is_frozen_and_fail_closed():
    assert normalize_internal_confidence(0.8)[0] == 0.8
    normalized, receipt = normalize_internal_confidence(4)
    assert normalized == 1.0
    assert receipt["adapted"] is True
    assert receipt["rubric_scores_changed"] is False
    assert receipt["prompt_changed"] is False
    with pytest.raises(ValueError, match="outside"):
        normalize_internal_confidence(5)
    with pytest.raises(TypeError, match="numeric"):
        normalize_internal_confidence(True)


def test_compatible_evaluator_preserves_original_prompt_for_valid_output(task_item):
    baseline = Trajectory(
        run_id="baseline-run",
        task_id=task_item.task_id,
        role="baseline",
        status="COMPLETED",
        answer="observable answer",
        candidate_config_sha256="s0",
    )
    original_rollout = _Rollout(0.8)
    compatible_rollout = _Rollout(0.8)
    original = OpenEvoEvolutionEvaluator(original_rollout)
    compatible = CompatibleOpenEvoEvolutionEvaluator(compatible_rollout)

    original_feedback = original.evaluate(task=task_item, trajectory=baseline)
    compatible_feedback = compatible.evaluate(task=task_item, trajectory=baseline)

    assert original_rollout.tasks[0].prompt == compatible_rollout.tasks[0].prompt
    assert original_rollout.tasks[0].sanitized_item_sha256 == (
        compatible_rollout.tasks[0].sanitized_item_sha256
    )
    assert compatible_rollout.tasks[0].prompt == build_evolution_evaluator_prompt(
        task=task_item,
        trajectory=baseline,
    )
    assert original_feedback.model_dump(exclude={"evaluator_run_id"}) == (
        compatible_feedback.model_dump(exclude={"evaluator_run_id"})
    )
    assert compatible.last_confidence_parse_receipt == {
        "schema_version": "chemcrow_internal_confidence_parse_receipt_v1",
        "policy_id": "chemcrow_internal_confidence_transport_v1",
        "input_scale": "unit_interval",
        "normalized_confidence": 0.8,
        "adapted": False,
        "rubric_scores_changed": False,
        "prompt_changed": False,
    }


def test_compatible_evaluator_treats_scalar_critique_as_one_item(task_item):
    baseline = Trajectory(
        run_id="baseline-run",
        task_id=task_item.task_id,
        role="baseline",
        status="COMPLETED",
        answer="observable answer",
        candidate_config_sha256="s0",
    )
    critique = "Verify the missing evidence."
    compatible = CompatibleOpenEvoEvolutionEvaluator(_Rollout(0.8, actionable_critique=critique))

    assert compatible.evaluate(task=task_item, trajectory=baseline).actionable_critique == [
        critique
    ]


def test_compatible_evaluator_reports_incomplete_core_call_before_json_parse(task_item):
    baseline = Trajectory(
        run_id="baseline-run",
        task_id=task_item.task_id,
        role="baseline",
        status="COMPLETED",
        answer="observable answer",
        candidate_config_sha256="s0",
    )
    rollout = _Rollout(0.8)
    rollout.run_candidate = lambda **_: Trajectory(
        run_id="failed-evaluator",
        task_id=task_item.task_id,
        role="baseline",
        status="ERROR",
        answer="",
        candidate_config_sha256="config",
    )

    with pytest.raises(RuntimeError, match="did not complete"):
        CompatibleOpenEvoEvolutionEvaluator(rollout).evaluate(
            task=task_item, trajectory=baseline
        )


def test_compatible_final_evaluator_preserves_original_prompt(task_item):
    original_rollout = _FinalRollout()
    compatible_rollout = _FinalRollout()
    original = OpenEvoFinalEvaluator(original_rollout)
    compatible = CompatibleOpenEvoFinalEvaluator(compatible_rollout)

    original_result = original.compare(
        task=task_item,
        answer_a="student A answer",
        answer_b="student B answer",
    )
    compatible_result = compatible.compare(
        task=task_item,
        answer_a="student A answer",
        answer_b="student B answer",
    )

    assert original_rollout.tasks[0].prompt == compatible_rollout.tasks[0].prompt
    assert original_rollout.tasks[0].sanitized_item_sha256 == (
        compatible_rollout.tasks[0].sanitized_item_sha256
    )
    assert original_result == compatible_result
    assert compatible.last_confidence_parse_receipt["adapted"] is False
