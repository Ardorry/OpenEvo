"""Security-preserving single-task, multi-round ChemBench orchestration."""

from __future__ import annotations

import secrets
from dataclasses import replace
from pathlib import Path

from openevo_chembench.artifacts import (
    ApprovedArtifact,
    ArtifactKind,
    CandidateArtifact,
    EvolutionArtifactSet,
    ValidationReceipt,
)
from openevo_chembench.config import ExperimentConfig
from openevo_chembench.evaluator import ChemBenchEvaluator
from openevo_chembench.feedback import SafeEvolutionFeedback
from openevo_chembench.models import (
    PrivateTask,
    PublicPrompt,
    RawAttempt,
    SafeEvolutionSignal,
    SealedEvaluation,
)
from openevo_chembench.prompt_adapter import ChemBenchPromptAdapter
from openevo_chembench.reflector import SafeEvolutionReflector
from openevo_chembench.reporting import (
    AppliedArtifactReference,
    ArtifactHistoryRecord,
    EpisodeResult,
    EpisodeStatus,
    RoundResult,
    RoundStatus,
    RunManifest,
    RunMode,
    _ResultStore,
)
from openevo_chembench.runtime_context import (
    AgentArtifactContext,
    AgentExecutor,
    AgentRoundRequest,
    ArtifactContextResolver,
)
from openevo_chembench.security import (
    ArtifactValidationResult,
    EvolutionArtifactValidator,
)


class ChemBenchOnlineEvolutionRunner:
    """Orchestrate one independent ChemBench evolution episode at a time."""

    __slots__ = (
        "_agent_executor",
        "_config",
        "_context_resolver",
        "_evaluator",
        "_feedback",
        "_mode",
        "_prompt_adapter",
        "_reflector",
        "_result_store",
        "_run_id",
        "_validator",
    )

    def __init__(
        self,
        *,
        config: ExperimentConfig,
        agent_executor: AgentExecutor,
        result_root: Path,
    ) -> None:
        if type(config) is not ExperimentConfig:
            raise TypeError(
                "ChemBenchOnlineEvolutionRunner.config must be exact ExperimentConfig"
            )
        if not callable(getattr(agent_executor, "execute", None)):
            raise TypeError("agent_executor must implement execute(request)")
        if (
            config.evolution.max_rounds > 0
            and not config.evolution.enabled_artifact_kinds
        ):
            raise ValueError(
                "evolution mode requires at least one enabled artifact type"
            )

        self._config = config
        self._agent_executor = agent_executor
        self._prompt_adapter = ChemBenchPromptAdapter()
        self._evaluator = ChemBenchEvaluator()
        self._feedback = SafeEvolutionFeedback()
        self._reflector = SafeEvolutionReflector()
        self._validator = EvolutionArtifactValidator()
        self._context_resolver = ArtifactContextResolver()
        self._mode = (
            RunMode.BASELINE
            if config.evolution.max_rounds == 0
            else RunMode.EVOLUTION
        )
        self._run_id = _new_runtime_id("run")
        self._result_store = _ResultStore(result_root)
        self._result_store.write_manifest(self._build_manifest())

    @classmethod
    def with_open_evo_executor(
        cls,
        *,
        config: ExperimentConfig,
        result_root: Path,
        rollout_url: str = "http://127.0.0.1:8080",
        task_timeout_seconds: float = 600.0,
        poll_interval_seconds: float = 1.0,
        max_poll_attempts: int = 900,
    ) -> tuple[ChemBenchOnlineEvolutionRunner, AgentExecutor]:
        """Construct the production runner with the real OpenEvo Codex adapter."""

        from openevo_chembench.agent_executor import OpenEvoCodexExecutor

        if config.execution_backend != "managed_codex":
            raise ValueError(
                "with_open_evo_executor requires execution_backend=managed_codex"
            )
        executor = OpenEvoCodexExecutor(
            config=config,
            rollout_url=rollout_url,
            task_timeout_seconds=task_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            max_poll_attempts=max_poll_attempts,
        )
        try:
            runner = cls(
                config=config,
                agent_executor=executor,
                result_root=result_root,
            )
        except BaseException:
            executor.close()
            raise
        return runner, executor

    @classmethod
    def with_local_codex_executor(
        cls,
        *,
        config: ExperimentConfig,
        result_root: Path,
        task_timeout_seconds: float = 600.0,
    ) -> tuple[ChemBenchOnlineEvolutionRunner, AgentExecutor]:
        """Construct the runner with the isolated local subscription CLI adapter."""

        from openevo_chembench.local_codex_executor import LocalCodexCLIExecutor

        if config.execution_backend != "local_codex_cli":
            raise ValueError(
                "with_local_codex_executor requires "
                "execution_backend=local_codex_cli"
            )
        executor = LocalCodexCLIExecutor(
            config=config,
            task_timeout_seconds=task_timeout_seconds,
        )
        try:
            runner = cls(
                config=config,
                agent_executor=executor,
                result_root=result_root,
            )
        except BaseException:
            executor.close()
            raise
        return runner, executor

    @classmethod
    def with_configured_executor(
        cls,
        *,
        config: ExperimentConfig,
        result_root: Path,
        rollout_url: str = "http://127.0.0.1:8080",
        task_timeout_seconds: float = 600.0,
        poll_interval_seconds: float = 1.0,
        max_poll_attempts: int = 900,
    ) -> tuple[ChemBenchOnlineEvolutionRunner, AgentExecutor]:
        """Select only one of the two closed, explicitly configured backends."""

        if config.execution_backend == "managed_codex":
            return cls.with_open_evo_executor(
                config=config,
                result_root=result_root,
                rollout_url=rollout_url,
                task_timeout_seconds=task_timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                max_poll_attempts=max_poll_attempts,
            )
        if config.execution_backend == "local_codex_cli":
            return cls.with_local_codex_executor(
                config=config,
                result_root=result_root,
                task_timeout_seconds=task_timeout_seconds,
            )
        raise ValueError("unsupported execution backend")

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def mode(self) -> RunMode:
        return self._mode

    def run_episode(
        self,
        task: PrivateTask,
        *,
        config_name: str,
    ) -> EpisodeResult:
        """Execute round zero through ``max_rounds`` for one private task."""

        if type(task) is not PrivateTask:
            raise TypeError("run_episode requires an exact PrivateTask")
        if config_name not in self._config.dataset.configurations:
            raise ValueError("config_name is not selected by experiment config")

        episode_id, episode_root = self._create_episode()
        public_prompt = self._prompt_adapter.project(task)
        if type(public_prompt) is not PublicPrompt:
            raise TypeError("prompt adapter returned an invalid public prompt")

        artifact_state = EvolutionArtifactSet()
        round_results: list[RoundResult] = []
        artifact_history: list[ArtifactHistoryRecord] = []

        for round_index in range(self._config.evolution.max_rounds + 1):
            contexts = self._resolve_artifact_state(artifact_state)
            request = AgentRoundRequest(
                run_id=self._run_id,
                episode_id=episode_id,
                round_index=round_index,
                public_prompt=public_prompt,
                artifact_context=contexts,
            )
            attempt = self._agent_executor.execute(request)
            if type(attempt) is not RawAttempt:
                raise TypeError("AgentExecutor.execute must return an exact RawAttempt")

            evaluation = self._evaluator.evaluate(
                attempt,
                task,
                config_name=config_name,
                local_task_id=episode_id,
            )
            if type(evaluation) is not SealedEvaluation:
                raise TypeError("private evaluator returned an invalid result")
            round_score = float(evaluation.score)
            round_parse_status = evaluation.parse_status
            runtime_metadata = attempt.runtime_metadata
            del attempt
            applied_references = tuple(
                AppliedArtifactReference.from_context(context)
                for context in contexts
            )

            if round_index == self._config.evolution.max_rounds:
                result = RoundResult(
                    round_index=round_index,
                    score=round_score,
                    parse_status=round_parse_status,
                    status=RoundStatus.EVALUATED,
                    applied_artifacts=applied_references,
                    runtime_metadata=runtime_metadata,
                )
                round_results.append(result)
                self._persist_round(
                    episode_root,
                    episode_id=episode_id,
                    result=result,
                    artifact_history=tuple(artifact_history),
                )
                return EpisodeResult(
                    run_id=self._run_id,
                    episode_id=episode_id,
                    mode=self._mode,
                    status=EpisodeStatus.COMPLETED,
                    rounds=tuple(round_results),
                    artifact_history=tuple(artifact_history),
                )

            safe_signal = self._feedback.generate(evaluation)
            del evaluation
            if type(safe_signal) is not SafeEvolutionSignal:
                raise TypeError("safe feedback generator returned an invalid signal")

            (
                next_artifact_state,
                new_history,
                validator_receipts,
            ) = self._evolve_after_round(
                artifact_state,
                safe_signal,
                source_round_index=round_index,
            )
            artifact_history.extend(new_history)
            if next_artifact_state is None:
                status = RoundStatus.ARTIFACT_REJECTED
                episode_status = EpisodeStatus.ARTIFACT_REJECTED
            else:
                status = RoundStatus.EVOLVED
                episode_status = None

            result = RoundResult(
                round_index=round_index,
                score=round_score,
                parse_status=round_parse_status,
                status=status,
                applied_artifacts=applied_references,
                validator_receipts=validator_receipts,
                runtime_metadata=runtime_metadata,
            )
            round_results.append(result)
            self._persist_round(
                episode_root,
                episode_id=episode_id,
                result=result,
                artifact_history=tuple(artifact_history),
            )
            if episode_status is not None:
                return EpisodeResult(
                    run_id=self._run_id,
                    episode_id=episode_id,
                    mode=self._mode,
                    status=episode_status,
                    rounds=tuple(round_results),
                    artifact_history=tuple(artifact_history),
                )
            if next_artifact_state is None:
                raise RuntimeError("missing next artifact state after approved batch")
            artifact_state = next_artifact_state

        raise RuntimeError("runner exhausted an unreachable round state")

    def _evolve_after_round(
        self,
        artifact_state: EvolutionArtifactSet,
        safe_signal: SafeEvolutionSignal,
        *,
        source_round_index: int,
    ) -> tuple[
        EvolutionArtifactSet | None,
        tuple[ArtifactHistoryRecord, ...],
        tuple[ValidationReceipt, ...],
    ]:
        approved_batch: list[ApprovedArtifact] = []
        history: list[ArtifactHistoryRecord] = []
        receipts: list[ValidationReceipt] = []

        for artifact_type in self._config.evolution.enabled_artifact_kinds:
            parent = _artifact_for_kind(artifact_state, artifact_type)
            expected_version = 1 if parent is None else parent.version + 1
            expected_parent_hash = (
                None if parent is None else parent.artifact_hash
            )
            candidate = self._reflector.reflect(
                safe_signal,
                artifact_type=artifact_type,
                round_index=source_round_index,
                parent_hash=expected_parent_hash,
                artifact_version=expected_version,
            )
            if type(candidate) is not CandidateArtifact:
                raise TypeError("reflector must return an exact CandidateArtifact")
            _verify_candidate_controller_metadata(
                candidate,
                expected_type=artifact_type,
                expected_version=expected_version,
                expected_parent_hash=expected_parent_hash,
                expected_round_index=source_round_index,
            )

            validation = self._validator.validate_and_approve(candidate)
            if type(validation) is not ArtifactValidationResult:
                raise TypeError("validator returned an invalid result")
            receipt = validation.receipt
            receipts.append(receipt)
            approved = validation.approved_artifact
            history.append(
                ArtifactHistoryRecord(
                    artifact_type=artifact_type,
                    artifact_version=expected_version,
                    source_round_index=source_round_index,
                    target_round_index=source_round_index + 1,
                    parent_hash=expected_parent_hash,
                    candidate_hash=candidate.candidate_hash,
                    approved_artifact_hash=(
                        None if approved is None else approved.artifact_hash
                    ),
                    validator_receipt=receipt,
                    activated_for_next_round=False,
                )
            )
            if approved is None:
                return None, tuple(history), tuple(receipts)
            _verify_approved_controller_metadata(
                approved,
                expected_type=artifact_type,
                expected_version=expected_version,
                expected_parent_hash=expected_parent_hash,
                expected_round_index=source_round_index,
            )
            approved_batch.append(approved)

        next_state = artifact_state
        for approved in approved_batch:
            next_state = next_state.with_artifact(approved)
        activated_history = tuple(
            replace(record, activated_for_next_round=True) for record in history
        )
        return next_state, activated_history, tuple(receipts)

    def _resolve_artifact_state(
        self,
        artifact_state: EvolutionArtifactSet,
    ) -> tuple[AgentArtifactContext, ...]:
        if type(artifact_state) is not EvolutionArtifactSet:
            raise TypeError("artifact_state must be exact EvolutionArtifactSet")
        contexts: list[AgentArtifactContext] = []
        for artifact in (
            artifact_state.text_memory,
            artifact_state.skill_bundle,
            artifact_state.agent_system,
        ):
            if artifact is not None:
                contexts.append(self._context_resolver.resolve(artifact))
        return tuple(contexts)

    def _create_episode(self) -> tuple[str, Path]:
        for _ in range(8):
            episode_id = _new_runtime_id("episode")
            try:
                return episode_id, self._result_store.create_episode(
                    run_id=self._run_id,
                    episode_id=episode_id,
                )
            except FileExistsError:
                continue
        raise RuntimeError("failed to allocate an independent episode id")

    def _persist_round(
        self,
        episode_root: Path,
        *,
        episode_id: str,
        result: RoundResult,
        artifact_history: tuple[ArtifactHistoryRecord, ...],
    ) -> None:
        self._result_store.write_round(
            episode_root,
            result=result,
            run_id=self._run_id,
            episode_id=episode_id,
            mode=self._mode,
        )
        self._result_store.write_artifact_history(
            episode_root,
            run_id=self._run_id,
            episode_id=episode_id,
            records=artifact_history,
        )

    def _build_manifest(self) -> RunManifest:
        return RunManifest(
            run_id=self._run_id,
            protocol_id=self._config.protocol_id,
            mode=self._mode,
            max_rounds=self._config.evolution.max_rounds,
            openevo_revision=self._config.openevo_revision,
            chembench_revision=self._config.chembench_revision,
            seed=self._config.seed,
            prompt_version=self._config.prompt_version,
            codex_cli_version=self._config.codex_cli_version,
            harness=self._config.agent.harness,
            model=self._config.agent.model,
            capture_mode=self._config.agent.capture_mode,
            execution_backend=self._config.execution_backend,
            enabled_artifact_types=(
                self._config.evolution.enabled_artifact_kinds
            ),
        )


def _artifact_for_kind(
    artifact_state: EvolutionArtifactSet,
    artifact_type: ArtifactKind,
) -> ApprovedArtifact | None:
    if artifact_type is ArtifactKind.TEXT_MEMORY:
        return artifact_state.text_memory
    if artifact_type is ArtifactKind.SKILL_BUNDLE:
        return artifact_state.skill_bundle
    if artifact_type is ArtifactKind.AGENT_SYSTEM:
        return artifact_state.agent_system
    raise TypeError("unsupported artifact type")


def _verify_candidate_controller_metadata(
    candidate: CandidateArtifact,
    *,
    expected_type: ArtifactKind,
    expected_version: int,
    expected_parent_hash: str | None,
    expected_round_index: int,
) -> None:
    if candidate.kind is not expected_type:
        raise RuntimeError("reflector changed requested artifact type")
    if candidate.version != expected_version:
        raise RuntimeError("reflector changed controller-managed artifact version")
    if candidate.parent_hash != expected_parent_hash:
        raise RuntimeError("reflector changed controller-managed parent hash")
    if candidate.round_index != expected_round_index:
        raise RuntimeError("reflector changed controller-managed round index")


def _verify_approved_controller_metadata(
    artifact: ApprovedArtifact,
    *,
    expected_type: ArtifactKind,
    expected_version: int,
    expected_parent_hash: str | None,
    expected_round_index: int,
) -> None:
    if (
        artifact.kind is not expected_type
        or artifact.version != expected_version
        or artifact.lineage.parent_artifact_hash != expected_parent_hash
        or artifact.lineage.round_index != expected_round_index
    ):
        raise RuntimeError("validator-approved artifact metadata mismatch")


def _new_runtime_id(prefix: str) -> str:
    if prefix not in {"run", "episode"}:
        raise ValueError("runtime id prefix must be run or episode")
    return f"{prefix}_{secrets.token_hex(12)}"


__all__ = [
    "ChemBenchOnlineEvolutionRunner",
]
