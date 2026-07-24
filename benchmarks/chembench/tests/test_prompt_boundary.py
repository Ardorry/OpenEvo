from __future__ import annotations

import json
import unittest
from dataclasses import fields

from openevo_chembench.models import (
    PrivateTargetScore,
    PrivateTask,
    PublicPrompt,
)
from openevo_chembench.prompt_adapter import ChemBenchPromptAdapter


def _private_multiple_choice_task() -> PrivateTask:
    return PrivateTask(
        question="Which synthetic choice is valid?",
        options=("public option alpha", "public option beta"),
        target="",
        target_scores=(
            PrivateTargetScore(option="public option alpha", score=0),
            PrivateTargetScore(option="public option beta", score=1),
        ),
        metrics=("multiple_choice_grade",),
        uuid="private-task-uuid",
    )


class PromptBoundaryTests(unittest.TestCase):
    def test_target_scores_do_not_enter_projected_public_prompt(self) -> None:
        private_task = _private_multiple_choice_task()

        public_prompt = ChemBenchPromptAdapter().project(private_task)
        payload = public_prompt.to_agent_payload()

        self.assertEqual(
            tuple(choice.text for choice in public_prompt.choices),
            private_task.options,
        )
        self.assertNotIn("target_scores", payload)
        self.assertNotIn("score", json.dumps(payload, sort_keys=True))

    def test_public_prompt_serialization_excludes_all_private_keys(self) -> None:
        private_task = PrivateTask(
            question="What is the synthetic result?",
            options=(),
            target="private-target-literal",
            target_scores=(),
            metrics=("exact_str_match",),
            uuid="private-task-uuid",
        )
        public_prompt = ChemBenchPromptAdapter().project(private_task)

        payload = public_prompt.to_agent_payload()
        serialized = json.dumps(payload, sort_keys=True)

        self.assertEqual(
            set(payload),
            {"question", "choices", "answer_format"},
        )
        self.assertEqual(
            {item.name for item in fields(PublicPrompt)},
            {"question", "choices", "answer_format"},
        )
        for forbidden in ("target", "target_scores", "uuid", "canary"):
            self.assertNotIn(forbidden, _all_keys(payload))
        self.assertNotIn("private-target-literal", serialized)
        self.assertNotIn("private-task-uuid", serialized)

    def test_raw_chembench_row_cannot_enter_agent_projection(self) -> None:
        raw_row = {
            "canary": "private-canary",
            "description": "private-description",
            "examples": [
                {
                    "input": "Synthetic raw question?",
                    "target": "private-target",
                    "target_scores": None,
                }
            ],
            "metrics": ["exact_str_match"],
            "uuid": "private-row-uuid",
        }

        with self.assertRaises(TypeError):
            ChemBenchPromptAdapter().project(raw_row)  # type: ignore[arg-type]


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


if __name__ == "__main__":
    unittest.main()
