from __future__ import annotations

import json
import re
from typing import Any

from .hashing import canonical_sha256
from .models import EvaluatorFeedback, RubricScores, TaskItem, Trajectory
from .protocol import BlindJudgeResult
from .runtime import OpenEvoRolloutPort


def _json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    candidates = [stripped]
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if fenced:
        candidates.insert(0, fenced.group(1))
    first = stripped.find("{")
    last = stripped.rfind("}")
    if first >= 0 and last > first:
        candidates.append(stripped[first : last + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("evaluator did not return one JSON object")


def _scores(value: Any) -> RubricScores:
    if not isinstance(value, dict):
        raise TypeError("evaluator score object is missing")
    return RubricScores.model_validate(value)


class OpenEvoEvolutionEvaluator:
    def __init__(self, rollout: OpenEvoRolloutPort) -> None:
        self.rollout = rollout
        self.evaluator_id = f"evolution-{rollout.config_sha256}"

    def evaluate(self, *, task: TaskItem, trajectory: Trajectory) -> EvaluatorFeedback:
        prompt = (
            "You are the evolution feedback evaluator. Evaluate the answer independently. "
            "Do not browse, search the web, or use external evidence. "
            "Do not infer or request historical ChemCrow answers. Score each rubric from 0 to 4. "
            "Return JSON only with keys scores, strengths, weaknesses, actionable_critique, confidence. "
            "scores must contain chemical_correctness, reasoning_quality, task_completion.\n\n"
            f"TASK:\n{task.prompt}\n\nOBSERVABLE ANSWER:\n{trajectory.answer}\n\n"
            f"OBSERVABLE TOOL EVIDENCE:\n{json.dumps([x.model_dump(mode='json') for x in trajectory.observations], ensure_ascii=True)}"
        )
        judge_task = task.model_copy(
            update={
                "task_id": f"{task.task_id}-evolution-evaluator",
                "prompt": prompt,
                "sanitized_item_sha256": canonical_sha256({"prompt": prompt}),
            }
        )
        result = self.rollout.run_candidate(
            task=judge_task,
            role="baseline",
            artifact_ids=[],
            pair_id=f"{trajectory.run_id}-evolution-eval",
            mcp_url=None,
        )
        value = _json_object(result.answer)
        return EvaluatorFeedback(
            evaluator_role="evolution_evaluator",
            evaluator_run_id=result.run_id,
            scores=_scores(value.get("scores")),
            strengths=[str(x) for x in value.get("strengths", [])],
            weaknesses=[str(x) for x in value.get("weaknesses", [])],
            actionable_critique=[str(x) for x in value.get("actionable_critique", [])],
            confidence=value.get("confidence"),
        )


class OpenEvoFinalEvaluator:
    def __init__(self, rollout: OpenEvoRolloutPort) -> None:
        self.rollout = rollout
        self.evaluator_id = f"final-{rollout.config_sha256}"

    def compare(
        self,
        *,
        task: TaskItem,
        answer_a: str,
        answer_b: str,
    ) -> BlindJudgeResult:
        prompt = (
            "You are the blinded final evaluator, separate from the evolution evaluator. "
            "Do not browse, search the web, or use external evidence. "
            "Do not infer or request historical ChemCrow answers. Compare A and B under the task. "
            "Score each from 0 to 4 for chemical_correctness, reasoning_quality, task_completion. "
            "Return JSON only: {scores_a:{...}, scores_b:{...}, winner:'A'|'B'|'tie', confidence:0..1}.\n\n"
            f"TASK:\n{task.prompt}\n\nANSWER A:\n{answer_a}\n\nANSWER B:\n{answer_b}"
        )
        judge_task = task.model_copy(
            update={
                "task_id": f"{task.task_id}-final-evaluator",
                "prompt": prompt,
                "sanitized_item_sha256": canonical_sha256({"prompt": prompt}),
            }
        )
        result = self.rollout.run_candidate(
            task=judge_task,
            role="baseline",
            artifact_ids=[],
            pair_id=f"{task.task_id}-final-eval",
            mcp_url=None,
        )
        value = _json_object(result.answer)
        return BlindJudgeResult(
            scores_a=_scores(value.get("scores_a")),
            scores_b=_scores(value.get("scores_b")),
            winner=str(value.get("winner")),
            confidence=value.get("confidence"),
        )
