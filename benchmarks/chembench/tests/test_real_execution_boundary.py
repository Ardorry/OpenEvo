from __future__ import annotations

import ast
import json
from pathlib import Path
import unittest

from openevo_chembench.agent_executor import (
    AgentExecutionError,
    AgentExecutionErrorCode,
    OpenEvoCodexExecutor,
)
from openevo_chembench.artifacts import ArtifactKind, CandidateArtifact
from openevo_chembench.config import ExperimentConfig
from openevo_chembench.models import (
    AgentRuntimeMetadata,
    PrivateTargetScore,
    PrivateTask,
    RawAttempt,
    SafeEvolutionSignal,
    SignalOutcome,
)
from openevo_chembench.prompt_adapter import ChemBenchPromptAdapter
from openevo_chembench.reflector import SafeEvolutionReflector
from openevo_chembench.runtime_context import (
    AgentRoundRequest,
    ArtifactContextResolver,
)
from openevo_chembench.security import EvolutionArtifactValidator


_PRIVATE_TARGET = "private-target-must-not-enter-open-evo"
_PRIVATE_UUID = "private-uuid-must-not-enter-open-evo"


def _config() -> ExperimentConfig:
    return ExperimentConfig(
        openevo_revision="a" * 40,
        chembench_revision="b" * 40,
        codex_cli_version="0.144.1",
        prompt_version="chembench-public-prompt.v1",
        seed=11,
    )


def _private_task() -> PrivateTask:
    return PrivateTask(
        question="Which public choice is appropriate?",
        options=("public alpha", "public beta"),
        target=_PRIVATE_TARGET,
        target_scores=(
            PrivateTargetScore(option="public alpha", score=1.0),
            PrivateTargetScore(option="public beta", score=0.0),
        ),
        metrics=("multiple_choice_grade",),
        uuid=_PRIVATE_UUID,
    )


def _round_request(
    *,
    contexts=(),
) -> AgentRoundRequest:
    return AgentRoundRequest(
        run_id="run_" + "1" * 24,
        episode_id="episode_" + "2" * 24,
        round_index=1 if contexts else 0,
        public_prompt=ChemBenchPromptAdapter().project(_private_task()),
        artifact_context=contexts,
    )


def _transcript(*, item_type: str = "agent_message") -> str:
    events = [
        {"type": "thread.started", "thread_id": "thread-safe"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "id": "item-safe",
                "type": item_type,
                "text": "<answer>A</answer>",
            },
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 20,
                "cached_input_tokens": 5,
                "output_tokens": 4,
                "reasoning_output_tokens": 2,
            },
        },
    ]
    return "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n"


class _FakeRolloutClient:
    def __init__(self, *, item_type: str = "agent_message") -> None:
        self.item_type = item_type
        self.submitted_payloads: list[dict[str, object]] = []
        self.workspace_snapshots: list[dict[str, str]] = []
        self.closed = False

    def submit_task(self, payload: dict[str, object]) -> str:
        self.submitted_payloads.append(payload)
        self.workspace_snapshots.append(_workspace_snapshot(payload))
        return str(payload["task_id"])

    def get_task(self, task_id: str) -> dict[str, object]:
        transcript = _transcript(item_type=self.item_type)
        return {
            "task_id": task_id,
            "status": "completed",
            "total_sessions": 1,
            "completed_sessions": 1,
            "results": [
                {
                    "session_id": "sk-openevo-controller-generated",
                    "task_id": task_id,
                    "status": "COMPLETED",
                    "trajectory": {
                        "status": "COMPLETED",
                        "metadata": {
                            "builder": "agent_transcript",
                            "capture_mode": "transcript",
                        },
                        "traces": [
                            {
                                "response_messages": [
                                    {
                                        "role": "assistant",
                                        "content": "<answer>A</answer>",
                                    }
                                ],
                                "finish_reason": "transcript",
                                "metadata": {
                                    "capture_mode": "transcript",
                                    "transcript": transcript,
                                },
                            }
                        ],
                    },
                    "timing": {
                        "register_to_init_queue_ms": 1.0,
                        "init_ms": 2.0,
                        "run_ms": 3.0,
                        "postrun_ms": 4.0,
                    },
                    "node_id": "gateway-safe",
                    "error": None,
                    "metadata": {},
                }
            ],
            "result_paths": [f"/core/results/{task_id}/session.json"],
        }

    def cancel_task(self, task_id: str) -> dict[str, object]:
        return {"task_id": task_id, "status": "cancelled"}

    def close(self) -> None:
        self.closed = True


def _workspace_snapshot(payload: dict[str, object]) -> dict[str, str]:
    runtime = payload.get("runtime")
    if not isinstance(runtime, dict):
        return {}
    prepare = runtime.get("prepare")
    if not isinstance(prepare, list) or not prepare:
        return {}
    first = prepare[0]
    if not isinstance(first, dict) or first.get("type") != "upload_dir":
        return {}
    source = first.get("source")
    if not isinstance(source, str):
        return {}
    root = Path(source)
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _approved_contexts() -> tuple:
    signal = SafeEvolutionSignal(outcome=SignalOutcome.SATISFACTORY)
    reflector = SafeEvolutionReflector()
    validator = EvolutionArtifactValidator()
    resolver = ArtifactContextResolver()
    contexts = []
    for artifact_type in ArtifactKind:
        candidate = reflector.reflect(
            signal,
            artifact_type=artifact_type,
            round_index=0,
        )
        validation = validator.validate_and_approve(candidate)
        assert validation.approved_artifact is not None
        contexts.append(resolver.resolve(validation.approved_artifact))
    return tuple(contexts)


class RealExecutionBoundaryTests(unittest.TestCase):
    def test_agent_payload_contains_public_prompt_but_no_private_fields(self) -> None:
        client = _FakeRolloutClient()
        executor = OpenEvoCodexExecutor(
            config=_config(),
            rollout_client=client,
        )

        attempt = executor.execute(_round_request())

        self.assertIs(type(attempt), RawAttempt)
        self.assertIs(type(attempt.runtime_metadata), AgentRuntimeMetadata)
        assert attempt.runtime_metadata is not None
        self.assertEqual(attempt.runtime_metadata.duration_ms, 10.0)
        self.assertEqual(attempt.runtime_metadata.input_tokens, 20)
        payload_text = json.dumps(client.submitted_payloads[0], sort_keys=True)
        self.assertIn("Which public choice is appropriate?", payload_text)
        for private_literal in (
            _PRIVATE_TARGET,
            _PRIVATE_UUID,
            "target_scores",
            "correct_answer",
            "canary",
        ):
            self.assertNotIn(private_literal, payload_text)

        payload = client.submitted_payloads[0]
        agent = payload["agent"]
        runtime = payload["runtime"]
        self.assertIsInstance(agent, dict)
        self.assertIsInstance(runtime, dict)
        assert isinstance(agent, dict)
        assert isinstance(runtime, dict)
        self.assertEqual(agent["harness"], "codex")
        self.assertEqual(agent["model_name"], "gpt-5.5")
        self.assertEqual(agent["mcp_servers"], [])
        self.assertEqual(
            agent["settings"],
            {"auth_mode": "subscription", "capture_mode": "transcript"},
        )
        self.assertFalse(runtime["allow_internet"])
        self.assertEqual(runtime["network"], "none")
        self.assertIsNone(payload["evaluator"])
        self.assertEqual(payload["builder"], {"strategy": "agent_transcript", "config": {}})

    def test_executor_rejects_private_controller_dto_and_never_imports_it(self) -> None:
        executor = OpenEvoCodexExecutor(
            config=_config(),
            rollout_client=_FakeRolloutClient(),
        )
        with self.assertRaises(TypeError):
            executor.execute(_private_task())  # type: ignore[arg-type]

        source_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "openevo_chembench"
            / "agent_executor.py"
        )
        tree = ast.parse(
            source_path.read_text(encoding="utf-8"),
            filename=str(source_path),
        )
        imported_names: set[str] = set()
        accessed_attributes = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported_names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported_names.update(alias.name for alias in node.names)

        self.assertNotIn("PrivateTask", imported_names)
        self.assertNotIn("target_scores", accessed_attributes)
        self.assertNotIn("uuid", accessed_attributes)

    def test_candidate_cannot_be_wrapped_or_injected(self) -> None:
        signal = SafeEvolutionSignal(outcome=SignalOutcome.SATISFACTORY)
        candidate = SafeEvolutionReflector().reflect(
            signal,
            artifact_type=ArtifactKind.TEXT_MEMORY,
            round_index=0,
        )
        self.assertIs(type(candidate), CandidateArtifact)

        with self.assertRaises(TypeError):
            ArtifactContextResolver().resolve(candidate)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            _round_request(contexts=(candidate,))  # type: ignore[arg-type]

    def test_only_validator_approved_context_reaches_instruction_and_workspace(self) -> None:
        contexts = _approved_contexts()
        client = _FakeRolloutClient()
        executor = OpenEvoCodexExecutor(
            config=_config(),
            rollout_client=client,
        )

        executor.execute(_round_request(contexts=contexts))

        payload = client.submitted_payloads[0]
        instruction = payload["instruction"]
        self.assertIsInstance(instruction, str)
        assert isinstance(instruction, str)
        self.assertIn("validator-approved long-term strategy memory", instruction)
        self.assertIn("validator-approved evolved agent system", instruction)
        agent = payload["agent"]
        assert isinstance(agent, dict)
        self.assertEqual(agent["skills_path"], "/openevo/session/workspace/.openevo-approved-skills")
        workspace = client.workspace_snapshots[0]
        self.assertIn("AGENTS.md", workspace)
        self.assertTrue(
            any(path.endswith("/SKILL.md") for path in workspace),
            workspace,
        )
        self.assertFalse(any("answer" in path.casefold() for path in workspace))

    def test_disallowed_mcp_transcript_event_fails_closed(self) -> None:
        executor = OpenEvoCodexExecutor(
            config=_config(),
            rollout_client=_FakeRolloutClient(item_type="mcp_tool_call"),
        )

        with self.assertRaises(AgentExecutionError) as captured:
            executor.execute(_round_request())

        self.assertIs(
            captured.exception.code,
            AgentExecutionErrorCode.DISALLOWED_TOOL_EVENT,
        )
        self.assertNotIn("<answer>", str(captured.exception))
        self.assertNotIn(_PRIVATE_TARGET, str(captured.exception))


if __name__ == "__main__":
    unittest.main()
