from __future__ import annotations

import unittest

from openevo.runtime.managed import MANAGED_RUNTIME_RELEASES

from openevo_chembench.agent_executor import OpenEvoCodexExecutor
from openevo_chembench.artifacts import ArtifactKind, CandidateArtifact
from openevo_chembench.config import ExperimentConfig
from openevo_chembench.models import (
    PrivateTargetScore,
    PrivateTask,
    PublicChoice,
    PublicPrompt,
    SafeEvolutionSignal,
    SignalOutcome,
)
from openevo_chembench.reflector import SafeEvolutionReflector
from openevo_chembench.runtime_context import AgentRoundRequest, ArtifactContextResolver
from openevo_chembench.security import EvolutionArtifactValidator


def _config() -> ExperimentConfig:
    return ExperimentConfig(
        openevo_revision="a" * 40,
        chembench_revision="b" * 40,
        codex_cli_version="0.144.1",
        prompt_version="chembench-public-prompt.v1",
    )


def _public_prompt() -> PublicPrompt:
    return PublicPrompt(
        question="Which public choice is appropriate?",
        choices=(
            PublicChoice(label="A", text="public alpha"),
            PublicChoice(label="B", text="public beta"),
        ),
        answer_format="Return one choice label.",
    )


def _round_request(*, artifact_context=()) -> AgentRoundRequest:
    return AgentRoundRequest(
        run_id="run_" + "1" * 24,
        episode_id="episode_" + "2" * 24,
        round_index=1 if artifact_context else 0,
        public_prompt=_public_prompt(),
        artifact_context=artifact_context,
    )


class RealAgentExecutorTests(unittest.TestCase):
    def test_public_prompt_compiles_to_rollout_task_request(self) -> None:
        executor = OpenEvoCodexExecutor(config=_config(), rollout_client=_NoopClient())

        task_request = executor.build_task_request(_round_request())

        self.assertIn("Which public choice is appropriate?", task_request.instruction)
        self.assertEqual(task_request.agent.harness, "codex")
        self.assertEqual(task_request.agent.model_name, "gpt-5.5")
        self.assertEqual(task_request.agent.mcp_servers, [])
        self.assertEqual(
            task_request.runtime.image,
            MANAGED_RUNTIME_RELEASES["managed_science"].loaded_image_id,
        )
        self.assertFalse(task_request.runtime.allow_internet)
        self.assertEqual(task_request.runtime.network, "none")

    def test_private_task_is_not_an_executor_input(self) -> None:
        private_task = PrivateTask(
            question="public question",
            options=("public alpha", "public beta"),
            target="private target",
            target_scores=(
                PrivateTargetScore(option="public alpha", score=1.0),
                PrivateTargetScore(option="public beta", score=0.0),
            ),
            metrics=("multiple_choice_grade",),
            uuid="private uuid",
        )
        executor = OpenEvoCodexExecutor(config=_config(), rollout_client=_NoopClient())

        with self.assertRaises(TypeError):
            executor.build_task_request(private_task)  # type: ignore[arg-type]

    def test_only_approved_artifact_context_is_compiled(self) -> None:
        candidate = SafeEvolutionReflector().reflect(
            SafeEvolutionSignal(outcome=SignalOutcome.SATISFACTORY),
            artifact_type=ArtifactKind.TEXT_MEMORY,
            round_index=0,
        )
        validated = EvolutionArtifactValidator().validate_and_approve(candidate)
        self.assertTrue(validated.receipt.passed)
        assert validated.approved_artifact is not None
        context = ArtifactContextResolver().resolve(validated.approved_artifact)
        executor = OpenEvoCodexExecutor(config=_config(), rollout_client=_NoopClient())

        task_request = executor.build_task_request(
            _round_request(artifact_context=(context,))
        )

        self.assertIn("validator-approved long-term strategy memory", task_request.instruction)
        self.assertIn(context.markdown or "", task_request.instruction)

    def test_candidate_artifact_cannot_enter_runtime_context(self) -> None:
        candidate = SafeEvolutionReflector().reflect(
            SafeEvolutionSignal(outcome=SignalOutcome.SATISFACTORY),
            artifact_type=ArtifactKind.TEXT_MEMORY,
            round_index=0,
        )
        self.assertIs(type(candidate), CandidateArtifact)

        with self.assertRaises(TypeError):
            ArtifactContextResolver().resolve(candidate)  # type: ignore[arg-type]


class _NoopClient:
    def submit_task(self, payload: dict[str, object]) -> str:
        raise AssertionError("unit test must not submit a real task")

    def get_task(self, task_id: str) -> dict[str, object]:
        raise AssertionError("unit test must not poll a real task")

    def cancel_task(self, task_id: str) -> dict[str, object]:
        raise AssertionError("unit test must not cancel a real task")

    def close(self) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
