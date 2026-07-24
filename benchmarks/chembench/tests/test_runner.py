from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path

from openevo_chembench.artifacts import (
    ArtifactFile,
    ArtifactKind,
    ArtifactLineage,
    CandidateArtifact,
    SkillBundlePayload,
    TextMemoryPayload,
)
from openevo_chembench.config import EvolutionConfig, ExperimentConfig
from openevo_chembench.models import (
    PrivateTargetScore,
    PrivateTask,
    RawAttempt,
    SafeEvolutionSignal,
    SealedEvaluation,
    SignalOutcome,
    TranscriptReference,
)
from openevo_chembench.reflector import (
    SafeEvolutionReflector,
    compute_source_signal_hash,
)
from openevo_chembench.reporting import (
    EpisodeStatus,
    RoundStatus,
    RunMode,
)
from openevo_chembench.runner import ChemBenchOnlineEvolutionRunner
from openevo_chembench.runtime_context import (
    AgentArtifactContext,
    AgentRoundRequest,
    ArtifactContextResolver,
)
from openevo_chembench.security import EvolutionArtifactValidator


_CONFIG_NAME = "general_chemistry"
_PRIVATE_UUID = "private-chembench-uuid-must-not-leave-controller"


def _experiment_config(
    *,
    max_rounds: int,
    enable_text_memory: bool = True,
    enable_skill_bundle: bool = False,
    enable_agent_system: bool = False,
) -> ExperimentConfig:
    return ExperimentConfig(
        openevo_revision="a" * 40,
        chembench_revision="b" * 40,
        codex_cli_version="0.144.1",
        prompt_version="chembench-public-prompt.v1",
        seed=7,
        evolution=EvolutionConfig(
            max_rounds=max_rounds,
            enable_text_memory=enable_text_memory,
            enable_skill_bundle=enable_skill_bundle,
            enable_agent_system=enable_agent_system,
        ),
    )


def _private_task(*, private_uuid: str = _PRIVATE_UUID) -> PrivateTask:
    return PrivateTask(
        question="Which choice is used by this orchestration test?",
        options=("first public choice", "second public choice"),
        target="private-target-literal",
        target_scores=(
            PrivateTargetScore(option="first public choice", score=1.0),
            PrivateTargetScore(option="second public choice", score=0.0),
        ),
        metrics=("multiple_choice_grade",),
        uuid=private_uuid,
    )


class _RecordingAgentExecutor:
    def __init__(self, response: str = "<answer>A</answer>") -> None:
        self.response = response
        self.requests: list[AgentRoundRequest] = []

    def execute(self, request: AgentRoundRequest) -> RawAttempt:
        if type(request) is not AgentRoundRequest:
            raise TypeError("test executor requires AgentRoundRequest")
        self.requests.append(request)
        return RawAttempt(
            response=self.response,
            transcript_reference=TranscriptReference(
                reference=f"opaque-transcript-round-{request.round_index}"
            ),
        )


class _RecordingReflector:
    def __init__(self) -> None:
        self.signals: list[SafeEvolutionSignal] = []
        self.calls: list[dict[str, object]] = []
        self._delegate = SafeEvolutionReflector()

    def reflect(
        self,
        signal: SafeEvolutionSignal,
        *,
        artifact_type: ArtifactKind,
        round_index: int,
        parent_hash: str | None = None,
        artifact_version: int = 1,
    ) -> CandidateArtifact:
        if type(signal) is not SafeEvolutionSignal:
            raise AssertionError("reflector received a non-safe signal")
        self.signals.append(signal)
        self.calls.append(
            {
                "artifact_type": artifact_type,
                "round_index": round_index,
                "parent_hash": parent_hash,
                "artifact_version": artifact_version,
            }
        )
        return self._delegate.reflect(
            signal,
            artifact_type=artifact_type,
            round_index=round_index,
            parent_hash=parent_hash,
            artifact_version=artifact_version,
        )


class _RejectingReflector:
    def reflect(
        self,
        signal: SafeEvolutionSignal,
        *,
        artifact_type: ArtifactKind,
        round_index: int,
        parent_hash: str | None = None,
        artifact_version: int = 1,
    ) -> CandidateArtifact:
        if type(signal) is not SafeEvolutionSignal:
            raise AssertionError("reflector received a non-safe signal")
        if artifact_type is not ArtifactKind.TEXT_MEMORY:
            raise AssertionError("rejecting fixture only supports text memory")
        return CandidateArtifact(
            version=artifact_version,
            payload=TextMemoryPayload(markdown="The answer is B"),
            lineage=ArtifactLineage(
                round_index=round_index,
                safe_signal_hash=compute_source_signal_hash(signal),
                parent_artifact_hash=parent_hash,
            ),
        )


class _PartialBatchRejectingReflector:
    def __init__(self) -> None:
        self._delegate = SafeEvolutionReflector()

    def reflect(
        self,
        signal: SafeEvolutionSignal,
        *,
        artifact_type: ArtifactKind,
        round_index: int,
        parent_hash: str | None = None,
        artifact_version: int = 1,
    ) -> CandidateArtifact:
        if artifact_type is ArtifactKind.TEXT_MEMORY:
            return self._delegate.reflect(
                signal,
                artifact_type=artifact_type,
                round_index=round_index,
                parent_hash=parent_hash,
                artifact_version=artifact_version,
            )
        if artifact_type is not ArtifactKind.SKILL_BUNDLE:
            raise AssertionError("unexpected artifact type")
        return CandidateArtifact(
            version=artifact_version,
            payload=SkillBundlePayload(
                files=(
                    ArtifactFile(
                        relative_path="SKILL.md",
                        content=(
                            "---\n"
                            "name: unsafe-answer\n"
                            "description: Unsafe test fixture.\n"
                            "---\n\n"
                            "The answer is B\n"
                        ),
                    ),
                )
            ),
            lineage=ArtifactLineage(
                round_index=round_index,
                safe_signal_hash=compute_source_signal_hash(signal),
                parent_artifact_hash=parent_hash,
            ),
        )


class RunnerTests(unittest.TestCase):
    def test_max_rounds_zero_runs_baseline_without_artifacts(self) -> None:
        agent = _RecordingAgentExecutor()
        reflector = _RecordingReflector()

        with tempfile.TemporaryDirectory() as temporary_directory:
            result_root = Path(temporary_directory) / "results"
            runner = ChemBenchOnlineEvolutionRunner(
                config=_experiment_config(max_rounds=0),
                agent_executor=agent,
                result_root=result_root,
            )
            setattr(runner, "_reflector", reflector)
            result = runner.run_episode(
                _private_task(),
                config_name=_CONFIG_NAME,
            )

            self.assertIs(runner.mode, RunMode.BASELINE)
            self.assertIs(result.status, EpisodeStatus.COMPLETED)
            self.assertEqual(len(result.rounds), 1)
            self.assertIs(result.rounds[0].status, RoundStatus.EVALUATED)
            self.assertEqual(result.artifact_history, ())
            self.assertEqual(reflector.calls, [])
            self.assertEqual(len(agent.requests), 1)
            self.assertEqual(agent.requests[0].artifact_context, ())

            manifest = _read_json(result_root / "run_manifest.json")
            self.assertEqual(manifest["mode"], "baseline")
            self.assertEqual(manifest["max_rounds"], 0)
            episode_root = result_root / "episodes" / result.episode_id
            self.assertFalse((episode_root / "round_1.json").exists())
            history = _read_json(episode_root / "artifact_history.json")
            self.assertEqual(history["artifacts"], [])

    def test_max_rounds_one_injects_round_zero_artifact_into_round_one(self) -> None:
        agent = _RecordingAgentExecutor()
        reflector = _RecordingReflector()

        with tempfile.TemporaryDirectory() as temporary_directory:
            runner = ChemBenchOnlineEvolutionRunner(
                config=_experiment_config(max_rounds=1),
                agent_executor=agent,
                result_root=Path(temporary_directory) / "results",
            )
            setattr(runner, "_reflector", reflector)
            result = runner.run_episode(
                _private_task(),
                config_name=_CONFIG_NAME,
            )

        self.assertIs(runner.mode, RunMode.EVOLUTION)
        self.assertEqual(len(agent.requests), 2)
        self.assertEqual(agent.requests[0].artifact_context, ())
        round_one_context = agent.requests[1].artifact_context
        self.assertEqual(len(round_one_context), 1)
        self.assertIs(
            round_one_context[0].artifact_type,
            ArtifactKind.TEXT_MEMORY,
        )
        self.assertEqual(round_one_context[0].artifact_version, 1)
        self.assertEqual(round_one_context[0].source_round_index, 0)
        self.assertEqual(len(reflector.calls), 1)
        self.assertEqual(len(result.artifact_history), 1)
        self.assertTrue(result.artifact_history[0].activated_for_next_round)
        self.assertEqual(
            tuple(round_result.status for round_result in result.rounds),
            (RoundStatus.EVOLVED, RoundStatus.EVALUATED),
        )

    def test_round_two_cannot_see_artifact_created_for_round_three(self) -> None:
        agent = _RecordingAgentExecutor()
        reflector = _RecordingReflector()

        with tempfile.TemporaryDirectory() as temporary_directory:
            runner = ChemBenchOnlineEvolutionRunner(
                config=_experiment_config(max_rounds=3),
                agent_executor=agent,
                result_root=Path(temporary_directory) / "results",
            )
            setattr(runner, "_reflector", reflector)
            result = runner.run_episode(
                _private_task(),
                config_name=_CONFIG_NAME,
            )

        self.assertEqual(len(agent.requests), 4)
        self.assertEqual(len(reflector.calls), 3)
        round_two_context = agent.requests[2].artifact_context[0]
        round_three_context = agent.requests[3].artifact_context[0]
        self.assertEqual(round_two_context.artifact_version, 2)
        self.assertEqual(round_two_context.source_round_index, 1)
        self.assertEqual(round_three_context.artifact_version, 3)
        self.assertEqual(round_three_context.source_round_index, 2)
        self.assertEqual(
            round_two_context.artifact_hash,
            result.artifact_history[1].approved_artifact_hash,
        )
        self.assertNotEqual(
            result.artifact_history[1].candidate_hash,
            result.artifact_history[2].candidate_hash,
        )
        for request in agent.requests:
            for context in request.artifact_context:
                self.assertLess(context.source_round_index, request.round_index)

    def test_validator_rejection_stops_episode_before_next_round(self) -> None:
        agent = _RecordingAgentExecutor()

        with tempfile.TemporaryDirectory() as temporary_directory:
            result_root = Path(temporary_directory) / "results"
            runner = ChemBenchOnlineEvolutionRunner(
                config=_experiment_config(max_rounds=3),
                agent_executor=agent,
                result_root=result_root,
            )
            setattr(runner, "_reflector", _RejectingReflector())
            result = runner.run_episode(
                _private_task(),
                config_name=_CONFIG_NAME,
            )

            episode_root = result_root / "episodes" / result.episode_id
            persisted_round = _read_json(episode_root / "round_0.json")
            persisted_history = _read_json(
                episode_root / "artifact_history.json"
            )

        self.assertIs(result.status, EpisodeStatus.ARTIFACT_REJECTED)
        self.assertEqual(len(agent.requests), 1)
        self.assertEqual(len(result.rounds), 1)
        self.assertIs(
            result.rounds[0].status,
            RoundStatus.ARTIFACT_REJECTED,
        )
        self.assertFalse(result.rounds[0].validator_receipts[-1].passed)
        self.assertFalse(result.artifact_history[-1].activated_for_next_round)
        self.assertIsNone(result.artifact_history[-1].approved_artifact_hash)
        self.assertEqual(persisted_round["status"], "artifact_rejected")
        self.assertFalse(
            persisted_history["artifacts"][-1]["validator_receipt"]["passed"]
        )

    def test_artifact_batch_is_not_partially_activated_on_rejection(self) -> None:
        agent = _RecordingAgentExecutor()

        with tempfile.TemporaryDirectory() as temporary_directory:
            runner = ChemBenchOnlineEvolutionRunner(
                config=_experiment_config(
                    max_rounds=1,
                    enable_text_memory=True,
                    enable_skill_bundle=True,
                ),
                agent_executor=agent,
                result_root=Path(temporary_directory) / "results",
            )
            setattr(runner, "_reflector", _PartialBatchRejectingReflector())
            result = runner.run_episode(
                _private_task(),
                config_name=_CONFIG_NAME,
            )

        self.assertIs(result.status, EpisodeStatus.ARTIFACT_REJECTED)
        self.assertEqual(len(agent.requests), 1)
        self.assertEqual(len(result.artifact_history), 2)
        self.assertTrue(result.artifact_history[0].validator_receipt.passed)
        self.assertIsNotNone(
            result.artifact_history[0].approved_artifact_hash
        )
        self.assertFalse(
            result.artifact_history[0].activated_for_next_round
        )
        self.assertFalse(result.artifact_history[1].validator_receipt.passed)
        self.assertFalse(
            result.artifact_history[1].activated_for_next_round
        )

    def test_reflector_receives_safe_signal_never_sealed_evaluation(self) -> None:
        agent = _RecordingAgentExecutor()
        reflector = _RecordingReflector()

        with tempfile.TemporaryDirectory() as temporary_directory:
            runner = ChemBenchOnlineEvolutionRunner(
                config=_experiment_config(max_rounds=1),
                agent_executor=agent,
                result_root=Path(temporary_directory) / "results",
            )
            setattr(runner, "_reflector", reflector)
            runner.run_episode(_private_task(), config_name=_CONFIG_NAME)

        self.assertEqual(len(reflector.signals), 1)
        self.assertTrue(
            all(type(signal) is SafeEvolutionSignal for signal in reflector.signals)
        )
        self.assertTrue(
            all(
                not isinstance(signal, SealedEvaluation)
                for signal in reflector.signals
            )
        )

    def test_private_uuid_never_enters_runtime_or_result_files(self) -> None:
        private_uuid = "uuid-private-boundary-literal-4f7809"
        agent = _RecordingAgentExecutor()

        with tempfile.TemporaryDirectory() as temporary_directory:
            result_root = Path(temporary_directory) / "results"
            runner = ChemBenchOnlineEvolutionRunner(
                config=_experiment_config(max_rounds=1),
                agent_executor=agent,
                result_root=result_root,
            )
            result = runner.run_episode(
                _private_task(private_uuid=private_uuid),
                config_name=_CONFIG_NAME,
            )

            runtime_log = json.dumps(
                [request.to_runtime_payload() for request in agent.requests],
                sort_keys=True,
            )
            persisted_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in sorted(result_root.rglob("*.json"))
            )
            persisted_paths = "\n".join(
                str(path.relative_to(result_root))
                for path in sorted(result_root.rglob("*"))
            )

        self.assertNotIn(private_uuid, runtime_log)
        self.assertNotIn(private_uuid, persisted_text)
        self.assertNotIn(private_uuid, persisted_paths)
        self.assertNotEqual(result.run_id, private_uuid)
        self.assertNotEqual(result.episode_id, private_uuid)

    def test_result_files_exclude_task_attempt_and_artifact_content(self) -> None:
        task = _private_task()
        agent = _RecordingAgentExecutor()

        with tempfile.TemporaryDirectory() as temporary_directory:
            result_root = Path(temporary_directory) / "results"
            runner = ChemBenchOnlineEvolutionRunner(
                config=_experiment_config(max_rounds=1),
                agent_executor=agent,
                result_root=result_root,
            )
            result = runner.run_episode(task, config_name=_CONFIG_NAME)
            episode_root = result_root / "episodes" / result.episode_id
            round_zero = _read_json(episode_root / "round_0.json")
            round_one = _read_json(episode_root / "round_1.json")
            history = _read_json(episode_root / "artifact_history.json")
            persisted_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in sorted(result_root.rglob("*.json"))
            )

        self.assertEqual(
            set(round_zero),
            {
                "schema_version",
                "run_id",
                "episode_id",
                "mode",
                "round",
                "status",
                "score",
                "parse_status",
                "applied_artifacts",
                "validator_receipts",
            },
        )
        self.assertEqual(set(round_one), set(round_zero))
        self.assertEqual(
            set(history),
            {"schema_version", "run_id", "episode_id", "artifacts"},
        )
        for private_literal in (
            task.question,
            task.target,
            task.options[0],
            task.options[1],
            agent.response,
            "opaque-transcript-round",
            "# General scientific strategies",
        ):
            self.assertNotIn(private_literal, persisted_text)

    def test_all_three_approved_artifact_types_resolve_for_next_round(self) -> None:
        agent = _RecordingAgentExecutor()

        with tempfile.TemporaryDirectory() as temporary_directory:
            runner = ChemBenchOnlineEvolutionRunner(
                config=_experiment_config(
                    max_rounds=1,
                    enable_text_memory=True,
                    enable_skill_bundle=True,
                    enable_agent_system=True,
                ),
                agent_executor=agent,
                result_root=Path(temporary_directory) / "results",
            )
            runner.run_episode(_private_task(), config_name=_CONFIG_NAME)

        self.assertEqual(
            tuple(
                context.artifact_type
                for context in agent.requests[1].artifact_context
            ),
            (
                ArtifactKind.TEXT_MEMORY,
                ArtifactKind.SKILL_BUNDLE,
                ArtifactKind.AGENT_SYSTEM,
            ),
        )

    def test_each_task_starts_with_an_independent_empty_artifact_state(self) -> None:
        agent = _RecordingAgentExecutor()

        with tempfile.TemporaryDirectory() as temporary_directory:
            runner = ChemBenchOnlineEvolutionRunner(
                config=_experiment_config(max_rounds=1),
                agent_executor=agent,
                result_root=Path(temporary_directory) / "results",
            )
            first = runner.run_episode(_private_task(), config_name=_CONFIG_NAME)
            second = runner.run_episode(
                _private_task(private_uuid="second-private-uuid"),
                config_name=_CONFIG_NAME,
            )

        self.assertNotEqual(first.episode_id, second.episode_id)
        self.assertEqual(
            tuple(request.round_index for request in agent.requests),
            (0, 1, 0, 1),
        )
        self.assertEqual(agent.requests[0].artifact_context, ())
        self.assertEqual(agent.requests[2].artifact_context, ())
        self.assertEqual(first.artifact_history[0].artifact_version, 1)
        self.assertEqual(second.artifact_history[0].artifact_version, 1)

    def test_context_resolver_rejects_candidate_and_accepts_approved(self) -> None:
        safe_signal = SafeEvolutionSignal(
            outcome=SignalOutcome.SATISFACTORY,
        )
        resolver = ArtifactContextResolver()

        for artifact_type in ArtifactKind:
            with self.subTest(artifact_type=artifact_type.value):
                candidate = SafeEvolutionReflector().reflect(
                    safe_signal,
                    artifact_type=artifact_type,
                    round_index=0,
                )
                with self.assertRaises(TypeError):
                    resolver.resolve(candidate)  # type: ignore[arg-type]
                validation = EvolutionArtifactValidator().validate_and_approve(
                    candidate
                )
                assert validation.approved_artifact is not None
                context = resolver.resolve(validation.approved_artifact)
                self.assertIs(context.artifact_type, artifact_type)

        with self.assertRaises(TypeError):
            AgentArtifactContext(
                artifact_type=ArtifactKind.TEXT_MEMORY,
                artifact_hash="1" * 64,
                artifact_version=1,
                source_round_index=0,
                markdown="unvalidated content",
            )

    def test_runner_source_does_not_read_private_task_fields(self) -> None:
        runner_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "openevo_chembench"
            / "runner.py"
        )
        tree = ast.parse(
            runner_path.read_text(encoding="utf-8"),
            filename=str(runner_path),
        )
        accessed_attributes = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }

        self.assertNotIn("uuid", accessed_attributes)
        self.assertNotIn("target", accessed_attributes)
        self.assertNotIn("target_scores", accessed_attributes)

    def test_orchestration_modules_have_no_llm_or_process_dependency(self) -> None:
        source_root = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "openevo_chembench"
        )
        imported_modules: set[str] = set()
        for filename in ("runner.py", "runtime_context.py", "reporting.py"):
            path = source_root / filename
            tree = ast.parse(
                path.read_text(encoding="utf-8"),
                filename=str(path),
            )
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_modules.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module is not None:
                    imported_modules.add(node.module)

        for forbidden_module in (
            "openai",
            "litellm",
            "subprocess",
            "requests",
            "httpx",
        ):
            self.assertNotIn(forbidden_module, imported_modules)


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError("expected JSON object")
    return value


if __name__ == "__main__":
    unittest.main()
