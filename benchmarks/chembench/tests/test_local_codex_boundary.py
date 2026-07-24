from __future__ import annotations

import ast
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openevo_chembench.artifacts import ArtifactKind, CandidateArtifact
from openevo_chembench.config import AgentConfig, ExperimentConfig
from openevo_chembench.local_codex_executor import (
    LocalCodexCLIExecutor,
    LocalCodexExecutionError,
    LocalCodexExecutionErrorCode,
    LocalCommandResult,
)
from openevo_chembench.models import (
    PrivateTargetScore,
    PrivateTask,
    PublicChoice,
    PublicPrompt,
    RawAttempt,
    SafeEvolutionSignal,
    SignalOutcome,
)
from openevo_chembench.reflector import SafeEvolutionReflector
from openevo_chembench.runner import ChemBenchOnlineEvolutionRunner
from openevo_chembench.runtime_context import (
    AgentRoundRequest,
    ArtifactContextResolver,
)
from openevo_chembench.security import EvolutionArtifactValidator


_PRIVATE_TARGET = "private-target-sentinel"
_PRIVATE_UUID = "private-uuid-sentinel"


def _config() -> ExperimentConfig:
    return ExperimentConfig(
        openevo_revision="a" * 40,
        chembench_revision="b" * 40,
        codex_cli_version="0.144.6",
        prompt_version="chembench-public-prompt.v1",
        execution_backend="local_codex_cli",
        agent=AgentConfig(harness="codex_cli"),
    )


def _public_prompt() -> PublicPrompt:
    return PublicPrompt(
        question="Which public choice is appropriate?",
        choices=(
            PublicChoice(label="A", text="public alpha"),
            PublicChoice(label="B", text="public beta"),
        ),
        answer_format="Return one choice label in angle tags.",
    )


def _request(*, contexts=()) -> AgentRoundRequest:
    return AgentRoundRequest(
        run_id="run_" + "1" * 24,
        episode_id="episode_" + "2" * 24,
        round_index=1 if contexts else 0,
        public_prompt=_public_prompt(),
        artifact_context=contexts,
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


class _RecordingRunner:
    def __init__(
        self,
        *,
        returncode: int = 0,
        item_type: str = "agent_message",
    ) -> None:
        self.returncode = returncode
        self.item_type = item_type
        self.calls: list[tuple[tuple[str, ...], str | None, Path, dict[str, str], float]] = []

    def __call__(
        self,
        command,
        input_text,
        cwd,
        environment,
        timeout_seconds,
    ) -> LocalCommandResult:
        self.calls.append(
            (
                tuple(command),
                input_text,
                cwd,
                dict(environment),
                timeout_seconds,
            )
        )
        return LocalCommandResult(
            returncode=self.returncode,
            stdout=_transcript(item_type=self.item_type),
            stderr="private stderr must never be surfaced",
        )


def _approved_context():
    candidate = SafeEvolutionReflector().reflect(
        SafeEvolutionSignal(outcome=SignalOutcome.SATISFACTORY),
        artifact_type=ArtifactKind.TEXT_MEMORY,
        round_index=0,
    )
    result = EvolutionArtifactValidator().validate_and_approve(candidate)
    assert result.approved_artifact is not None
    return ArtifactContextResolver().resolve(result.approved_artifact)


class LocalCodexBoundaryTests(unittest.TestCase):
    def _executor(self, temporary: str, runner: _RecordingRunner):
        temporary_root = Path(temporary)
        auth_file = temporary_root / "auth.json"
        auth_file.write_text("{}\n", encoding="utf-8")
        auth_file.chmod(0o600)
        isolation_parent = temporary_root / "isolation"
        isolation_parent.mkdir(mode=0o700)
        return LocalCodexCLIExecutor(
            config=_config(),
            codex_executable=Path("/usr/bin/codex"),
            auth_file=auth_file,
            command_runner=runner,
            verified_codex_version="0.144.6",
            isolation_parent=isolation_parent,
            diagnostic_root=temporary_root / "diagnostics",
        )

    def test_private_task_is_not_an_executor_input_or_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executor = self._executor(temporary, _RecordingRunner())
            with self.assertRaises(TypeError):
                executor.execute(_private_task())  # type: ignore[arg-type]

        source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "openevo_chembench"
            / "local_codex_executor.py"
        )
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertNotIn("PrivateTask", imported)

    def test_compiled_prompt_has_only_public_and_approved_content(self) -> None:
        runner = _RecordingRunner()
        with tempfile.TemporaryDirectory() as temporary:
            executor = self._executor(temporary, runner)
            prompt = executor.build_prompt(_request(contexts=(_approved_context(),)))
            attempt = executor.execute(_request(contexts=(_approved_context(),)))

        self.assertIs(type(attempt), RawAttempt)
        self.assertIn("Which public choice is appropriate?", prompt)
        self.assertIn("Validator-approved strategy memory", prompt)
        normalized = prompt.casefold()
        for forbidden in (
            _PRIVATE_TARGET,
            _PRIVATE_UUID,
            "target_scores",
            "correct_answer",
            "uuid",
            "canary",
        ):
            self.assertNotIn(forbidden.casefold(), normalized)
        command, stdin_prompt, _cwd, environment, _timeout = runner.calls[0]
        self.assertEqual(stdin_prompt, prompt)
        self.assertNotIn(prompt, command)
        self.assertIn("--ephemeral", command)
        self.assertIn("shell_tool", command)
        self.assertIn('web_search="disabled"', command)
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("CODEX_API_KEY", environment)
        self.assertEqual(attempt.runtime_metadata.execution_backend, "local_codex_cli")
        self.assertTrue(
            attempt.transcript_reference.reference.startswith("local-codex-jsonl:sha256:")
        )

    def test_candidate_artifact_cannot_enter_context(self) -> None:
        candidate = SafeEvolutionReflector().reflect(
            SafeEvolutionSignal(outcome=SignalOutcome.SATISFACTORY),
            artifact_type=ArtifactKind.TEXT_MEMORY,
            round_index=0,
        )
        self.assertIs(type(candidate), CandidateArtifact)
        with self.assertRaises(TypeError):
            ArtifactContextResolver().resolve(candidate)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            _request(contexts=(candidate,))  # type: ignore[arg-type]

    def test_cli_failure_does_not_fallback(self) -> None:
        runner = _RecordingRunner(returncode=1)
        with tempfile.TemporaryDirectory() as temporary:
            executor = self._executor(temporary, runner)
            with self.assertRaises(LocalCodexExecutionError) as raised:
                executor.execute(_request())

        self.assertEqual(
            raised.exception.code,
            LocalCodexExecutionErrorCode.CLI_FAILED,
        )
        self.assertEqual(len(runner.calls), 1)
        self.assertNotIn("private stderr", str(raised.exception))

    def test_any_tool_event_fails_closed(self) -> None:
        runner = _RecordingRunner(item_type="command_execution")
        with tempfile.TemporaryDirectory() as temporary:
            executor = self._executor(temporary, runner)
            with self.assertRaises(LocalCodexExecutionError) as raised:
                executor.execute(_request())

        self.assertEqual(
            raised.exception.code,
            LocalCodexExecutionErrorCode.SECURITY_TOOL_USE_VIOLATION,
        )
        self.assertEqual(
            raised.exception.run_status,
            "SECURITY_TOOL_USE_VIOLATION",
        )

    def test_runner_selects_local_backend_only_when_explicitly_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch(
                "openevo_chembench.local_codex_executor.LocalCodexCLIExecutor",
                _FactoryExecutor,
            ):
                runner, executor = ChemBenchOnlineEvolutionRunner.with_configured_executor(
                    config=_config(),
                    result_root=Path(temporary) / "results",
                )

        self.assertIs(type(executor), _FactoryExecutor)
        self.assertEqual(runner.mode.value, "baseline")
        executor.close()


class _FactoryExecutor:
    def __init__(self, *, config, task_timeout_seconds) -> None:
        self.config = config
        self.task_timeout_seconds = task_timeout_seconds
        self.closed = False

    def execute(self, request) -> RawAttempt:
        raise AssertionError("factory selection test must not execute a model")

    def close(self) -> None:
        self.closed = True


if __name__ == "__main__":
    unittest.main()
