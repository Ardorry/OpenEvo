"""Bounded online-update smoke ending before the Round 1 task session.

This is an infrastructure diagnostic, not an experimental arm.  It executes
one fixed public ChemBench4K task in a fresh Round 0 session, performs private
evaluation, projects closed safe feedback, executes exactly one registered
OpenEvo Core text-memory update, revalidates the Core-resolved memory
capability, and stops before creating Round 1.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
from typing import Any, Protocol
import uuid

import yaml

from openevo_chembench.chembench4k_dataset import ChemBench4KDatasetLoader
from openevo_chembench.chembench4k_evaluation import ChemBench4KPrivateEvaluator
from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_REPOSITORY,
    CHEMBENCH4K_REVISION,
    PrivateChemBench4KTask,
)
from openevo_chembench.chembench4k_prompt import render_official_five_shot_prompt
from openevo_chembench.local_codex_executor import LocalCodexCLIExecutor
from openevo_chembench.models import RawAttempt
from openevo_chembench.source_identity_v2 import (
    SourceManifestError,
    verify_source_manifest,
)
from openevo_chembench.taskwise_cli_v1 import verify_taskwise_codex_policy_v1
from openevo_chembench.taskwise_config_v1 import (
    SOURCE_COMMIT_PLACEHOLDER,
    TASKWISE_MEMORY_LIMITS_V1,
    TaskwiseExperimentConfigV1,
    load_taskwise_config_v1,
)
from openevo_chembench.taskwise_context_binding_v1 import (
    TaskwiseContextBindingReceiptV1,
    TaskwiseSessionContextBindingV1,
)
from openevo_chembench.taskwise_core_evolution_v1 import (
    build_taskwise_core_port_at_roots_v1,
)
from openevo_chembench.taskwise_feedback_v1 import (
    safe_signal_from_private_evaluation,
)
from openevo_chembench.taskwise_online_runner_v1 import (
    CoreMemoryReferenceV1,
    TaskwiseAgentRequestV1,
    TaskwiseCoreUpdateOutcomeV1,
    TaskwiseCoreUpdatePortV1,
    TaskwiseExecutorV1,
    TaskwiseRunnerCoreUpdateRequestV1,
)
from openevo_chembench.taskwise_round0_smoke_v1 import (
    Round0SmokeManifestSetV1,
    verify_round0_smoke_manifests,
)
from openevo_chembench.taskwise_sampling_v1 import select_taskwise_stream
from openevo_chembench.taskwise_trajectory_v1 import (
    TaskwiseTrajectoryV1,
    ordered_safe_feedback_digest,
    ordered_taskwise_trajectory_digest,
)


PROTOCOL_ID = "taskwise_update1_infrastructure_smoke_v1"
SCHEMA_VERSION = "chembench4k_taskwise_update1_smoke_config_v1"
RESULT_SCHEMA_VERSION = "chembench4k_taskwise_update1_smoke_result_v1"
STATE_SCHEMA_VERSION = "chembench4k_taskwise_update1_smoke_state_v1"
PRIVATE_SCHEMA_VERSION = "chembench4k_taskwise_update1_smoke_private_v1"
CLASSIFICATION = (
    "NON_PERFORMANCE_INFRASTRUCTURE_SMOKE",
    "ONLINE_UPDATE_PATH_ONLY",
    "ROUND_0_PLUS_UPDATE_1",
    "STOP_BEFORE_ROUND_1",
    "NOT_A_STANDARD_CHEMBENCH4K_SCORE",
)
SCOPE = "update1_smoke"
ARM = "online"
MODEL = "gpt-5.5"
REASONING_EFFORT = "medium"
PROMPT_RENDERER_ID = "chembench4k_official_five_shot_v2"
PARSER_ID = "official_first_capital_parser_v2"
EVALUATOR_ID = "chembench4k_accuracy_v2"
SOURCE_SCOPE = "canary9"
SOURCE_ORDINAL = 0

_MODULE = Path(__file__).resolve()
PACKAGE_ROOT = _MODULE.parents[2]
REPOSITORY_ROOT = _MODULE.parents[4]
WORKSPACE_ROOT = _MODULE.parents[5]
CONFIG_ROOT = PACKAGE_ROOT / "configs"
DEFAULT_CONFIG_PATH = CONFIG_ROOT / "taskwise_update1_infrastructure_smoke_v1.yaml"
_RUN_ID = re.compile(r"[a-z0-9][a-z0-9_-]{7,95}\Z", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_CLOSED_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,95}\Z", re.ASCII)
_PUBLIC_FORBIDDEN_KEYS = frozenset(
    {
        "answer",
        "correct",
        "correct_answer",
        "correct_option",
        "markdown",
        "official_prediction",
        "private_task",
        "public_prompt",
        "question",
        "raw_completion",
        "resolved_text_memory",
        "score",
        "source_index",
        "strict_prediction",
        "target",
        "target_scores",
        "transcript_reference",
    }
)


class Update1SmokeError(RuntimeError):
    """Sanitized terminal finding carrying no task or model content."""

    def __init__(self, finding_code: str) -> None:
        if type(finding_code) is not str or _CLOSED_CODE.fullmatch(finding_code) is None:
            raise ValueError("finding_code must use the closed smoke vocabulary")
        self.finding_code = finding_code
        super().__init__(finding_code)


class Update1SmokeExecutor(Protocol):
    def execute_taskwise(self, request: TaskwiseAgentRequestV1) -> RawAttempt: ...

    def consume_taskwise_context_receipt(
        self,
        session_id: str,
    ) -> TaskwiseContextBindingReceiptV1: ...

    def close(self) -> None: ...


class Update1SmokeCorePort(TaskwiseCoreUpdatePortV1, Protocol):
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Update1SmokeExecutorPolicyV1:
    backend: str
    harness: str
    timeout_seconds: int
    concurrency: int
    infrastructure_retries: int
    tools_enabled: bool
    mcp_enabled: bool
    web_enabled: bool
    network_enabled: bool
    subagents_enabled: bool

    def __post_init__(self) -> None:
        if self.backend != "local_codex_cli" or self.harness != "codex_cli":
            raise ValueError("update1 smoke requires local Codex CLI")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or not 0 < self.timeout_seconds <= 86_400
        ):
            raise ValueError("update1 smoke timeout must be positive and bounded")
        if self.concurrency != 1 or self.infrastructure_retries != 0:
            raise ValueError("update1 smoke requires concurrency one and no retry")
        if any(
            (
                self.tools_enabled,
                self.mcp_enabled,
                self.web_enabled,
                self.network_enabled,
                self.subagents_enabled,
            )
        ):
            raise ValueError("update1 smoke capabilities must remain disabled")

    def to_payload(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "harness": self.harness,
            "timeout_seconds": self.timeout_seconds,
            "concurrency": self.concurrency,
            "infrastructure_retries": self.infrastructure_retries,
            "tools_enabled": self.tools_enabled,
            "mcp_enabled": self.mcp_enabled,
            "web_enabled": self.web_enabled,
            "network_enabled": self.network_enabled,
            "subagents_enabled": self.subagents_enabled,
        }


@dataclass(frozen=True, slots=True)
class Update1SmokeConfigV1:
    dataset_root: str
    dataset_manifest: str
    source_public_task_manifest: str
    source_private_task_manifest: str
    source_manifest_summary: str
    output_root: str
    state_root: str
    diagnostic_root: str
    framework_lock: str
    source_manifest: str
    online_executor_compat_config: str
    codex_cli_version: str
    executor: Update1SmokeExecutorPolicyV1
    source_commit: str
    schema_version: str = SCHEMA_VERSION
    protocol_id: str = PROTOCOL_ID
    classification: tuple[str, ...] = CLASSIFICATION
    scope: str = SCOPE
    arm: str = ARM
    dataset_repository: str = CHEMBENCH4K_REPOSITORY
    dataset_revision: str = CHEMBENCH4K_REVISION
    model: str = MODEL
    reasoning_effort: str = REASONING_EFFORT
    prompt_renderer_id: str = PROMPT_RENDERER_ID
    parser_id: str = PARSER_ID
    evaluator_id: str = EVALUATOR_ID
    tasks: int = 1
    task_sessions: int = 1
    rounds_completed: int = 1
    evolution_updates: int = 1
    core_jobs: int = 1
    core_artifacts: int = 1
    core_context_resolutions: int = 1
    round_1_sessions: int = 0
    memory_injected_into_task_session: bool = False
    resume_enabled: bool = False

    def __post_init__(self) -> None:
        if (
            self.schema_version != SCHEMA_VERSION
            or self.protocol_id != PROTOCOL_ID
            or self.classification != CLASSIFICATION
            or self.scope != SCOPE
            or self.arm != ARM
        ):
            raise ValueError("update1 smoke identity is not frozen")
        if (
            self.dataset_repository != CHEMBENCH4K_REPOSITORY
            or self.dataset_revision != CHEMBENCH4K_REVISION
        ):
            raise ValueError("update1 smoke dataset identity is not frozen")
        if self.model != MODEL or self.reasoning_effort != REASONING_EFFORT:
            raise ValueError("update1 smoke model identity is not frozen")
        if (
            self.prompt_renderer_id != PROMPT_RENDERER_ID
            or self.parser_id != PARSER_ID
            or self.evaluator_id != EVALUATOR_ID
        ):
            raise ValueError("update1 smoke evaluation identity changed")
        if (
            self.tasks != 1
            or self.task_sessions != 1
            or self.rounds_completed != 1
            or self.evolution_updates != 1
            or self.core_jobs != 1
            or self.core_artifacts != 1
            or self.core_context_resolutions != 1
            or self.round_1_sessions != 0
            or self.memory_injected_into_task_session is not False
            or self.resume_enabled is not False
        ):
            raise ValueError("update1 smoke bounded execution budget changed")
        for field_name in (
            "dataset_root",
            "dataset_manifest",
            "source_public_task_manifest",
            "source_private_task_manifest",
            "source_manifest_summary",
            "output_root",
            "state_root",
            "diagnostic_root",
            "framework_lock",
            "source_manifest",
            "online_executor_compat_config",
        ):
            _require_relative_path(getattr(self, field_name), field_name)
        if type(self.codex_cli_version) is not str or not self.codex_cli_version:
            raise ValueError("codex_cli_version must be non-empty text")
        if self.source_commit != SOURCE_COMMIT_PLACEHOLDER and (
            type(self.source_commit) is not str
            or _GIT_COMMIT.fullmatch(self.source_commit) is None
        ):
            raise ValueError("source_commit must be a commit or clean-HEAD placeholder")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "protocol_id": self.protocol_id,
            "classification": list(self.classification),
            "scope": self.scope,
            "arm": self.arm,
            "dataset": {
                "repository": self.dataset_repository,
                "revision": self.dataset_revision,
                "root": self.dataset_root,
                "manifest": self.dataset_manifest,
                "source_public_task_manifest": self.source_public_task_manifest,
                "source_private_task_manifest": self.source_private_task_manifest,
                "source_manifest_summary": self.source_manifest_summary,
            },
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "codex_cli_version": self.codex_cli_version,
            "prompt_renderer_id": self.prompt_renderer_id,
            "parser_id": self.parser_id,
            "evaluator_id": self.evaluator_id,
            "tasks": self.tasks,
            "task_sessions": self.task_sessions,
            "rounds_completed": self.rounds_completed,
            "evolution_updates": self.evolution_updates,
            "core_jobs": self.core_jobs,
            "core_artifacts": self.core_artifacts,
            "core_context_resolutions": self.core_context_resolutions,
            "round_1_sessions": self.round_1_sessions,
            "memory_injected_into_task_session": self.memory_injected_into_task_session,
            "resume_enabled": self.resume_enabled,
            "executor": self.executor.to_payload(),
            "output_root": self.output_root,
            "state_root": self.state_root,
            "diagnostic_root": self.diagnostic_root,
            "framework_lock": self.framework_lock,
            "source_manifest": self.source_manifest,
            "online_executor_compat_config": self.online_executor_compat_config,
            "source_commit": self.source_commit,
        }

    @property
    def digest(self) -> str:
        return _sha256_json(self.to_payload())


@dataclass(frozen=True, slots=True)
class Update1SmokeStaticInputsV1:
    loader: ChemBench4KDatasetLoader
    manifests: Round0SmokeManifestSetV1
    task: PrivateChemBench4KTask


ExecutorFactory = Callable[
    [Update1SmokeConfigV1, Path],
    Update1SmokeExecutor,
]
CorePortFactory = Callable[
    [Update1SmokeConfigV1, Path],
    Update1SmokeCorePort,
]
SourceGate = Callable[[Update1SmokeConfigV1], str]
ExecutorPolicyGate = Callable[[TaskwiseExperimentConfigV1], object]


def load_update1_smoke_config(
    path: Path = DEFAULT_CONFIG_PATH,
) -> Update1SmokeConfigV1:
    """Load the exact bounded-smoke YAML schema."""

    if not isinstance(path, Path) or not path.is_file():
        raise Update1SmokeError("UPDATE1_SMOKE_CONFIG_MISSING")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise Update1SmokeError("UPDATE1_SMOKE_CONFIG_UNREADABLE") from exc
    expected = {
        "schema_version",
        "protocol_id",
        "classification",
        "scope",
        "arm",
        "dataset",
        "model",
        "reasoning_effort",
        "codex_cli_version",
        "prompt_renderer_id",
        "parser_id",
        "evaluator_id",
        "tasks",
        "task_sessions",
        "rounds_completed",
        "evolution_updates",
        "core_jobs",
        "core_artifacts",
        "core_context_resolutions",
        "round_1_sessions",
        "memory_injected_into_task_session",
        "resume_enabled",
        "executor",
        "output_root",
        "state_root",
        "diagnostic_root",
        "framework_lock",
        "source_manifest",
        "online_executor_compat_config",
        "source_commit",
    }
    if type(payload) is not dict or set(payload) != expected:
        raise Update1SmokeError("UPDATE1_SMOKE_CONFIG_SCHEMA_INVALID")
    dataset = _closed_mapping(
        payload["dataset"],
        {
            "repository",
            "revision",
            "root",
            "manifest",
            "source_public_task_manifest",
            "source_private_task_manifest",
            "source_manifest_summary",
        },
        "dataset",
    )
    executor = _closed_mapping(
        payload["executor"],
        {
            "backend",
            "harness",
            "timeout_seconds",
            "concurrency",
            "infrastructure_retries",
            "tools_enabled",
            "mcp_enabled",
            "web_enabled",
            "network_enabled",
            "subagents_enabled",
        },
        "executor",
    )
    try:
        return Update1SmokeConfigV1(
            schema_version=payload["schema_version"],
            protocol_id=payload["protocol_id"],
            classification=tuple(payload["classification"]),
            scope=payload["scope"],
            arm=payload["arm"],
            dataset_repository=dataset["repository"],
            dataset_revision=dataset["revision"],
            dataset_root=dataset["root"],
            dataset_manifest=dataset["manifest"],
            source_public_task_manifest=dataset["source_public_task_manifest"],
            source_private_task_manifest=dataset["source_private_task_manifest"],
            source_manifest_summary=dataset["source_manifest_summary"],
            model=payload["model"],
            reasoning_effort=payload["reasoning_effort"],
            codex_cli_version=payload["codex_cli_version"],
            prompt_renderer_id=payload["prompt_renderer_id"],
            parser_id=payload["parser_id"],
            evaluator_id=payload["evaluator_id"],
            tasks=payload["tasks"],
            task_sessions=payload["task_sessions"],
            rounds_completed=payload["rounds_completed"],
            evolution_updates=payload["evolution_updates"],
            core_jobs=payload["core_jobs"],
            core_artifacts=payload["core_artifacts"],
            core_context_resolutions=payload["core_context_resolutions"],
            round_1_sessions=payload["round_1_sessions"],
            memory_injected_into_task_session=payload["memory_injected_into_task_session"],
            resume_enabled=payload["resume_enabled"],
            executor=Update1SmokeExecutorPolicyV1(**executor),
            output_root=payload["output_root"],
            state_root=payload["state_root"],
            diagnostic_root=payload["diagnostic_root"],
            framework_lock=payload["framework_lock"],
            source_manifest=payload["source_manifest"],
            online_executor_compat_config=payload["online_executor_compat_config"],
            source_commit=payload["source_commit"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise Update1SmokeError("UPDATE1_SMOKE_CONFIG_INVALID") from exc


def dry_run_update1_smoke(
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> dict[str, object]:
    """Validate immutable inputs without constructing executor or Core objects."""

    config = load_update1_smoke_config(config_path)
    inputs = _verify_static_inputs(config)
    compatibility = _load_online_compat_config(config)
    result = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "protocol_id": PROTOCOL_ID,
        "classification": list(CLASSIFICATION),
        "status": "PASS",
        "task_count": 1,
        "task_uid": inputs.task.uid,
        "dataset_sha256": inputs.loader.manifest.combined_sha256,
        "public_manifest_sha256": inputs.manifests.public_sha256,
        "config_sha256": config.digest,
        "executor_compat_config_sha256": compatibility.config_sha256(),
        "model_calls": 0,
        "executor_instantiated": False,
        "core_port_instantiated": False,
        "sessions_created": 0,
        "rounds_completed": 0,
        "evolution_updates": 0,
        "core_jobs_created": 0,
        "core_artifacts_registered": 0,
        "core_context_resolutions": 0,
        "round_1_session_created": False,
        "resume_enabled": False,
    }
    _require_public_payload(result)
    return result


def run_update1_smoke(
    config_path: Path = DEFAULT_CONFIG_PATH,
    *,
    run_id: str | None = None,
    executor_factory: ExecutorFactory | None = None,
    core_port_factory: CorePortFactory | None = None,
    source_gate: SourceGate | None = None,
    executor_policy_gate: ExecutorPolicyGate | None = None,
) -> dict[str, object]:
    """Execute one Round 0 and one Core update, then stop before Round 1."""

    config = load_update1_smoke_config(config_path)
    current_commit = (source_gate or verify_update1_smoke_source_gate)(config)
    inputs = _verify_static_inputs(config)
    compatibility = _load_online_compat_config(config)
    if executor_policy_gate is not None:
        executor_policy_gate(compatibility)
    elif executor_factory is None:
        verify_taskwise_codex_policy_v1(compatibility)

    selected_run_id = (
        generate_update1_smoke_run_id() if run_id is None else validate_run_id(run_id)
    )
    output_parent = _resolve_workspace_path(config.output_root, must_exist=False)
    state_parent = _resolve_workspace_path(config.state_root, must_exist=False)
    diagnostic_parent = _resolve_workspace_path(config.diagnostic_root, must_exist=False)
    run_root = output_parent / selected_run_id
    output_directory = run_root / ARM
    state_run_root = state_parent / selected_run_id
    core_state_root = state_run_root / "core"
    diagnostic_root = diagnostic_parent / selected_run_id / ARM
    _claim_new_roots(
        run_root=run_root,
        output_directory=output_directory,
        state_run_root=state_run_root,
        diagnostic_root=diagnostic_root,
    )

    public_directory = output_directory / "public"
    private_directory = output_directory / "private"
    public_directory.mkdir(mode=0o755)
    private_directory.mkdir(mode=0o700)
    state_path = output_directory / "run_state.json"
    private_failure_path = private_directory / "failure.json"
    state = _initial_state(
        config=config,
        run_id=selected_run_id,
        current_commit=current_commit,
        inputs=inputs,
        diagnostic_root=diagnostic_root,
        core_state_root=core_state_root,
    )
    _write_state(state_path, state)

    executor: Update1SmokeExecutor | None = None
    core: Update1SmokeCorePort | None = None
    phase = "CORE_PREFLIGHT"
    completion_observed = False
    try:
        selected_core_factory = core_port_factory or build_default_update1_smoke_core_port
        core = selected_core_factory(config, core_state_root)
        state["core_port_instantiated"] = True
        _write_state(state_path, state)

        phase = "EXECUTOR_CONSTRUCTION"
        selected_executor_factory = executor_factory or build_default_update1_smoke_executor
        executor = selected_executor_factory(config, diagnostic_root)

        task = inputs.task
        dev = inputs.loader.load_category(task.category, split="dev")
        prompt = render_official_five_shot_prompt(task.to_public(), category_dev=dev)
        session_id = f"update1_smoke_session_{uuid.uuid4().hex}"
        request = TaskwiseAgentRequestV1(
            rendered_public_prompt=prompt.text,
            resolved_text_memory=None,
            session_id=session_id,
            arm=ARM,
            task_ordinal=0,
            round_index=0,
            run_id=selected_run_id,
            task_uid=task.uid,
        )
        state.update(
            {
                "status": "RUNNING",
                "state": "ROUND_0_SESSION_STARTED",
                "session_id": session_id,
                "session_attempt_count": 1,
            }
        )
        _write_state(state_path, state)

        phase = "ROUND_0_EXECUTION"
        attempt = executor.execute_taskwise(request)
        completion_observed = True
        if type(attempt) is not RawAttempt:
            raise Update1SmokeError("UPDATE1_SMOKE_EXECUTOR_OUTPUT_INVALID")
        state["completion_observed"] = True
        _write_state(state_path, state)

        phase = "ROUND_0_CONTEXT_BINDING"
        context_receipt = executor.consume_taskwise_context_receipt(session_id)
        if type(context_receipt) is not TaskwiseContextBindingReceiptV1:
            raise Update1SmokeError("UPDATE1_SMOKE_CONTEXT_BINDING_VIOLATION")
        context_receipt.require_match()
        expected_binding = TaskwiseSessionContextBindingV1.from_memory(
            session_id=session_id,
            memory=None,
        )
        if (
            context_receipt.expected != expected_binding
            or context_receipt.actual != expected_binding
            or context_receipt.actual.memory_present
        ):
            raise Update1SmokeError("UPDATE1_SMOKE_CONTEXT_BINDING_VIOLATION")
        executor.close()
        executor = None

        phase = "PRIVATE_EVALUATION"
        evaluation = ChemBench4KPrivateEvaluator().evaluate(
            task=task,
            raw_completion=attempt.response,
        )
        safe_signal = safe_signal_from_private_evaluation(evaluation)
        trajectory = TaskwiseTrajectoryV1.from_attempt(
            task_uid=task.uid,
            task_index=0,
            category=task.category,
            round_index=0,
            session_id=session_id,
            prompt=prompt,
            attempt=attempt,
            safe_feedback=safe_signal,
            dataset_sha256=task.dataset_sha256,
        )
        trajectories = (trajectory,)
        trajectory_digest = ordered_taskwise_trajectory_digest(trajectories)
        safe_feedback_digest = ordered_safe_feedback_digest(trajectories)
        if safe_feedback_digest != _sha256_json([safe_signal.to_evolution_payload()]):
            raise Update1SmokeError("UPDATE1_SMOKE_TRAJECTORY_BINDING_INVALID")

        phase = "PRIVATE_PERSISTENCE"
        private_payload = {
            "schema_version": PRIVATE_SCHEMA_VERSION,
            "protocol_id": PROTOCOL_ID,
            "run_id": selected_run_id,
            "task_uid": task.uid,
            "category": task.category,
            "source_split": task.source_split,
            "source_index": task.source_index,
            "target": task.target,
            "raw_completion": evaluation.raw_completion,
            "transcript_reference": attempt.transcript_reference.reference,
            "official_prediction": evaluation.official.prediction,
            "strict_prediction": evaluation.strict.prediction,
            "correct": evaluation.correct,
            "safe_feedback": safe_signal.to_evolution_payload(),
            "safe_feedback_digest": safe_signal.digest,
            "trajectory": trajectory.to_core_record(),
            "trajectory_digest": trajectory_digest,
            "context_binding": context_receipt.to_public_dict(),
        }
        if attempt.runtime_metadata is not None:
            private_payload["runtime_metadata"] = attempt.runtime_metadata.to_result_payload()
        _write_new_json(private_directory / "round_0_evaluation.json", private_payload, mode=0o600)
        state.update(
            {
                "state": "SAFE_FEEDBACK_PERSISTED",
                "completion_count": 1,
                "rounds_completed": 1,
                "trajectory_digest": trajectory_digest,
                "safe_feedback_digest": safe_feedback_digest,
            }
        )
        _write_state(state_path, state)

        phase = "CORE_UPDATE_1"
        state["state"] = "UPDATE_1_STARTED"
        _write_state(state_path, state)
        outcome = core.update_text_memory(
            TaskwiseRunnerCoreUpdateRequestV1(
                task_uid=task.uid,
                task_index=0,
                episode_id=f"{selected_run_id}-episode-00000000",
                update_index=1,
                source_round_index=0,
                trajectories=trajectories,
                safe_signals=(safe_signal,),
                prior_resolved_text_memory=None,
            )
        )
        if type(outcome) is not TaskwiseCoreUpdateOutcomeV1:
            raise Update1SmokeError("UPDATE1_SMOKE_CORE_OUTCOME_INVALID")
        memory = outcome.resolved_text_memory
        reference = CoreMemoryReferenceV1.from_memory(memory)
        if outcome.memory_metrics.memory_limits_sha256 != TASKWISE_MEMORY_LIMITS_V1.digest:
            raise Update1SmokeError("UPDATE1_SMOKE_MEMORY_LIMIT_BINDING_INVALID")
        state.update(
            {
                "state": "UPDATE_1_COMPLETED",
                "evolution_updates": 1,
                "core_jobs_created": 1,
                "core_artifacts_registered": 1,
                "core_context_resolutions": 1,
                "core_job_id": outcome.job_id,
                "core_job_state": outcome.job_state,
                "core_context_id": outcome.core_context_id,
                "validation_receipt_sha256": outcome.validation_receipt_sha256,
                "output_memory": reference.to_dict(),
                "memory_metrics": outcome.memory_metrics.to_dict(),
            }
        )
        _write_state(state_path, state)

        phase = "CORE_CONTEXT_REFERENCE_REVALIDATION"
        resolved_again = core.resolve_text_memory(reference)
        if resolved_again != memory:
            raise Update1SmokeError("UPDATE1_SMOKE_CONTEXT_REVALIDATION_FAILED")
        state.update(
            {
                "state": "CONTEXT_REFERENCE_REVERIFIED",
                "context_reference_revalidation_count": 1,
            }
        )
        _write_state(state_path, state)

        phase = "CORE_CLOSE"
        core.close()
        core = None

        phase = "PUBLIC_RESULT_PERSISTENCE"
        public_result = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "protocol_id": PROTOCOL_ID,
            "classification": list(CLASSIFICATION),
            "standard_chembench4k_score_claimed": False,
            "status": "COMPLETED_BEFORE_ROUND_1",
            "run_id": selected_run_id,
            "arm": ARM,
            "task_count": 1,
            "task_uid": task.uid,
            "category": task.category,
            "sessions_created": 1,
            "completion_count": 1,
            "rounds_completed": 1,
            "safe_feedback_digest": safe_feedback_digest,
            "trajectory_digest": trajectory_digest,
            "evolution_updates": 1,
            "core_jobs_created": 1,
            "core_artifacts_registered": 1,
            "core_context_resolutions": 1,
            "context_reference_revalidation_count": 1,
            "core_job_id": outcome.job_id,
            "core_job_state": outcome.job_state,
            "core_context_id": outcome.core_context_id,
            "validation_receipt_sha256": outcome.validation_receipt_sha256,
            "output_memory": reference.to_dict(),
            "memory_metrics": outcome.memory_metrics.to_dict(),
            "round_1_session_created": False,
            "memory_injected_into_task_session": False,
            "resume_allowed": False,
            "retry_allowed": False,
            "replacement_completion_allowed": False,
            "config_sha256": config.digest,
            "source_commit": current_commit,
            "dataset_sha256": inputs.loader.manifest.combined_sha256,
            "public_manifest_sha256": inputs.manifests.public_sha256,
        }
        _require_public_payload(public_result)
        result_path = public_directory / "result.json"
        _write_new_json(result_path, public_result, mode=0o644)
        state.update(
            {
                "status": "COMPLETED_BEFORE_ROUND_1",
                "state": "COMPLETED_BEFORE_ROUND_1",
                "round_1_session_created": False,
                "resume_allowed": False,
                "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
            }
        )
        _write_state(state_path, state)
        return public_result
    except Exception as exc:
        close_error: Exception | None = None
        if executor is not None:
            try:
                executor.close()
            except Exception as candidate:
                close_error = candidate
        if core is not None:
            try:
                core.close()
            except Exception as candidate:
                close_error = close_error or candidate
        terminal_error = close_error or exc
        status, finding_code = _terminal_failure(terminal_error, phase=phase)
        state.update(
            {
                "status": status,
                "completion_observed": bool(
                    completion_observed or getattr(exc, "completion_observed", False)
                ),
                "round_1_session_created": False,
                "resume_allowed": False,
                "retry_allowed": False,
                "replacement_completion_allowed": False,
                "failure_code": finding_code,
                "failure_phase": phase,
            }
        )
        try:
            _write_state(state_path, state)
        except Exception:
            pass
        try:
            _write_new_json(
                private_failure_path,
                {
                    "schema_version": "chembench4k_taskwise_update1_smoke_failure_v1",
                    "protocol_id": PROTOCOL_ID,
                    "run_id": selected_run_id,
                    "phase": phase,
                    "finding_code": finding_code,
                    "completion_observed": state["completion_observed"],
                    "resume_allowed": False,
                    "retry_allowed": False,
                    "replacement_completion_allowed": False,
                },
                mode=0o600,
            )
        except Exception:
            pass
        raise Update1SmokeError(status) from None


def build_default_update1_smoke_executor(
    config: Update1SmokeConfigV1,
    diagnostic_root: Path,
) -> TaskwiseExecutorV1:
    """Construct the audited taskwise online executor in an isolated namespace."""

    return LocalCodexCLIExecutor(
        config=_load_online_compat_config(config),
        task_timeout_seconds=config.executor.timeout_seconds,
        diagnostic_root=diagnostic_root,
    )


def build_default_update1_smoke_core_port(
    config: Update1SmokeConfigV1,
    core_state_root: Path,
) -> TaskwiseCoreUpdatePortV1:
    """Construct the shared production Core path at the claimed smoke root."""

    return build_taskwise_core_port_at_roots_v1(
        state_root=core_state_root,
        framework_lock=_resolve_workspace_path(config.framework_lock, must_exist=True),
        timeout_seconds=config.executor.timeout_seconds,
        memory_limits=TASKWISE_MEMORY_LIMITS_V1,
    )


def verify_update1_smoke_source_gate(config: Update1SmokeConfigV1) -> str:
    """Require clean committed benchmark source before any paid construction."""

    head = _git("rev-parse", "HEAD").strip()
    if _GIT_COMMIT.fullmatch(head) is None:
        raise Update1SmokeError("UPDATE1_SMOKE_SOURCE_COMMIT_UNAVAILABLE")
    if config.source_commit not in {SOURCE_COMMIT_PLACEHOLDER, head}:
        raise Update1SmokeError("UPDATE1_SMOKE_SOURCE_COMMIT_MISMATCH")
    status = _git(
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        "benchmarks/chembench",
    )
    if status.strip():
        raise Update1SmokeError("UPDATE1_SMOKE_PACKAGE_SOURCE_DIRTY")
    try:
        verify_source_manifest(
            PACKAGE_ROOT,
            _resolve_workspace_path(config.source_manifest, must_exist=True),
        )
    except (OSError, ValueError, SourceManifestError) as exc:
        raise Update1SmokeError("UPDATE1_SMOKE_SOURCE_MANIFEST_MISMATCH") from exc
    return head


def generate_update1_smoke_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ").lower()
    return validate_run_id(f"update1_smoke_{timestamp}_{uuid.uuid4().hex[:12]}")


def validate_run_id(run_id: str) -> str:
    if type(run_id) is not str or _RUN_ID.fullmatch(run_id) is None:
        raise Update1SmokeError("UPDATE1_SMOKE_RUN_ID_INVALID")
    return run_id


def _verify_static_inputs(
    config: Update1SmokeConfigV1,
) -> Update1SmokeStaticInputsV1:
    loader = ChemBench4KDatasetLoader(
        snapshot_root=_resolve_workspace_path(config.dataset_root, must_exist=True),
        manifest_path=_resolve_workspace_path(config.dataset_manifest, must_exist=True),
    )
    manifests = verify_round0_smoke_manifests(
        loader,
        public_path=_resolve_workspace_path(
            config.source_public_task_manifest,
            must_exist=True,
        ),
        private_path=_resolve_workspace_path(
            config.source_private_task_manifest,
            must_exist=True,
        ),
        summary_path=_resolve_workspace_path(
            config.source_manifest_summary,
            must_exist=True,
        ),
    )
    tasks, _allocation, _seed = select_taskwise_stream(loader, scope=SOURCE_SCOPE)
    if len(tasks) != 9:
        raise Update1SmokeError("UPDATE1_SMOKE_SOURCE_CANARY_INVALID")
    task = tasks[SOURCE_ORDINAL]
    if manifests.item_count != 1 or manifests.task_uid != task.uid:
        raise Update1SmokeError("UPDATE1_SMOKE_TASK_IDENTITY_MISMATCH")
    return Update1SmokeStaticInputsV1(
        loader=loader,
        manifests=manifests,
        task=task,
    )


def _load_online_compat_config(
    config: Update1SmokeConfigV1,
) -> TaskwiseExperimentConfigV1:
    base = load_taskwise_config_v1(
        _resolve_workspace_path(config.online_executor_compat_config, must_exist=True)
    )
    parity = {
        "arm": base.arm == ARM,
        "evolution": base.evolution.enabled
        and base.evolution.method_id == "text_memory_expel_reflector",
        "model": base.model == config.model,
        "reasoning_effort": base.reasoning_effort == config.reasoning_effort,
        "codex_cli_version": base.codex_cli_version == config.codex_cli_version,
        "timeout_seconds": base.executor.timeout_seconds == config.executor.timeout_seconds,
        "concurrency": base.executor.concurrency == config.executor.concurrency,
        "infrastructure_retries": (
            base.executor.infrastructure_retries_before_completion
            == config.executor.infrastructure_retries
        ),
        "tools_enabled": base.executor.tools_enabled == config.executor.tools_enabled,
        "mcp_enabled": base.executor.mcp_enabled == config.executor.mcp_enabled,
        "web_enabled": base.executor.web_enabled == config.executor.web_enabled,
        "network_enabled": base.executor.network_enabled == config.executor.network_enabled,
        "subagents_enabled": base.executor.subagents_enabled == config.executor.subagents_enabled,
    }
    if not all(parity.values()):
        raise Update1SmokeError("UPDATE1_SMOKE_EXECUTOR_POLICY_PARITY_FAILED")
    return base


def _claim_new_roots(
    *,
    run_root: Path,
    output_directory: Path,
    state_run_root: Path,
    diagnostic_root: Path,
) -> None:
    if run_root.exists() or state_run_root.exists() or diagnostic_root.exists():
        raise Update1SmokeError("UPDATE1_SMOKE_OUTPUT_TARGET_EXISTS")
    run_root.parent.mkdir(parents=True, exist_ok=True)
    state_run_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        run_root.mkdir(mode=0o700)
        output_directory.mkdir(mode=0o700)
        state_run_root.mkdir(mode=0o700)
    except FileExistsError:
        raise Update1SmokeError("UPDATE1_SMOKE_OUTPUT_TARGET_EXISTS") from None


def _initial_state(
    *,
    config: Update1SmokeConfigV1,
    run_id: str,
    current_commit: str,
    inputs: Update1SmokeStaticInputsV1,
    diagnostic_root: Path,
    core_state_root: Path,
) -> dict[str, object]:
    value = {
        "schema_version": STATE_SCHEMA_VERSION,
        "protocol_id": PROTOCOL_ID,
        "classification": list(CLASSIFICATION),
        "standard_chembench4k_score_claimed": False,
        "status": "INITIALIZED",
        "state": "INITIALIZED",
        "run_id": run_id,
        "arm": ARM,
        "scope": SCOPE,
        "task_count": 1,
        "task_uid": inputs.task.uid,
        "category": inputs.task.category,
        "session_attempt_count": 0,
        "completion_count": 0,
        "completion_observed": False,
        "rounds_completed": 0,
        "evolution_updates": 0,
        "core_jobs_created": 0,
        "core_artifacts_registered": 0,
        "core_context_resolutions": 0,
        "context_reference_revalidation_count": 0,
        "round_1_session_created": False,
        "memory_injected_into_task_session": False,
        "core_port_instantiated": False,
        "resume_allowed": False,
        "retry_allowed": False,
        "replacement_completion_allowed": False,
        "source_commit": current_commit,
        "config_sha256": config.digest,
        "dataset_sha256": inputs.loader.manifest.combined_sha256,
        "public_manifest_sha256": inputs.manifests.public_sha256,
        "diagnostic_namespace_sha256": hashlib.sha256(
            os.fspath(diagnostic_root).encode()
        ).hexdigest(),
        "core_state_namespace_sha256": hashlib.sha256(
            os.fspath(core_state_root).encode()
        ).hexdigest(),
    }
    _require_public_payload(value)
    return value


def _terminal_failure(error: Exception, *, phase: str) -> tuple[str, str]:
    if _is_security_violation(error):
        return "SECURITY_TOOL_USE_VIOLATION", "SECURITY_TOOL_USE_VIOLATION"
    if phase.startswith("CORE_"):
        return "TASKWISE_EVOLUTION_UPDATE_FAILED", "TASKWISE_EVOLUTION_UPDATE_FAILED"
    if type(error) is Update1SmokeError:
        return "EXECUTION_FAILED", error.finding_code
    return "EXECUTION_FAILED", _sanitized_error_code(error)


def _is_security_violation(error: Exception) -> bool:
    values = (
        getattr(error, "run_status", None),
        getattr(error, "taskwise_failure_code", None),
        getattr(error, "finding_code", None),
        getattr(getattr(error, "code", None), "value", getattr(error, "code", None)),
    )
    return any(type(value) is str and "SECURITY_TOOL_USE_VIOLATION" in value for value in values)


def _sanitized_error_code(error: Exception) -> str:
    for value in (
        getattr(error, "taskwise_failure_code", None),
        getattr(error, "finding_code", None),
        getattr(getattr(error, "code", None), "value", getattr(error, "code", None)),
    ):
        if type(value) is str:
            normalized = value.upper().replace("-", "_")
            if _CLOSED_CODE.fullmatch(normalized) is not None:
                return normalized
    return "UPDATE1_SMOKE_INTERNAL_ERROR"


def _require_public_payload(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if type(key) is not str:
                raise Update1SmokeError("UPDATE1_SMOKE_PUBLIC_PRIVATE_FIELD_LEAK")
            normalized = key.casefold().replace("-", "_")
            if normalized in _PUBLIC_FORBIDDEN_KEYS:
                raise Update1SmokeError("UPDATE1_SMOKE_PUBLIC_PRIVATE_FIELD_LEAK")
            _require_public_payload(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _require_public_payload(item)


def _write_state(path: Path, payload: dict[str, object]) -> None:
    _require_public_payload(payload)
    encoded = _canonical_bytes(payload)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _write_new_json(path: Path, payload: dict[str, object], *, mode: int) -> None:
    if mode != 0o600:
        _require_public_payload(payload)
    encoded = _canonical_bytes(payload)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    if stat.S_IMODE(path.stat().st_mode) != mode:
        path.unlink(missing_ok=True)
        raise Update1SmokeError("UPDATE1_SMOKE_OUTPUT_PERMISSIONS_INVALID")


def _closed_mapping(
    value: object,
    keys: set[str],
    field_name: str,
) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise Update1SmokeError("UPDATE1_SMOKE_CONFIG_SCHEMA_INVALID")
    if any(type(key) is not str for key in value):
        raise Update1SmokeError("UPDATE1_SMOKE_CONFIG_SCHEMA_INVALID")
    del field_name
    return dict(value)


def _require_relative_path(value: object, field_name: str) -> None:
    if type(value) is not str or not value:
        raise ValueError(f"{field_name} must be non-empty text")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{field_name} must be a clean relative path")


def _resolve_workspace_path(value: str, *, must_exist: bool) -> Path:
    _require_relative_path(value, "workspace path")
    path = (WORKSPACE_ROOT / PurePosixPath(value)).resolve()
    if path != WORKSPACE_ROOT and WORKSPACE_ROOT not in path.parents:
        raise Update1SmokeError("UPDATE1_SMOKE_PATH_OUTSIDE_WORKSPACE")
    if must_exist and not path.exists():
        raise Update1SmokeError("UPDATE1_SMOKE_REQUIRED_PATH_MISSING")
    return path


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=REPOSITORY_ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise Update1SmokeError("UPDATE1_SMOKE_GIT_QUERY_FAILED")
    return completed.stdout


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    dry_parser = subparsers.add_parser("dry-run")
    dry_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    run_parser.add_argument("--run-id")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "dry-run":
            result = dry_run_update1_smoke(arguments.config)
        else:
            result = run_update1_smoke(arguments.config, run_id=arguments.run_id)
    except Update1SmokeError as exc:
        parser.exit(status=2, message=f"BLOCKED: {exc.finding_code}\n")
    except Exception:
        parser.exit(status=2, message="BLOCKED: UPDATE1_SMOKE_INTERNAL_ERROR\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "CLASSIFICATION",
    "DEFAULT_CONFIG_PATH",
    "PROTOCOL_ID",
    "RESULT_SCHEMA_VERSION",
    "STATE_SCHEMA_VERSION",
    "Update1SmokeConfigV1",
    "Update1SmokeError",
    "Update1SmokeExecutorPolicyV1",
    "build_default_update1_smoke_core_port",
    "build_default_update1_smoke_executor",
    "dry_run_update1_smoke",
    "generate_update1_smoke_run_id",
    "load_update1_smoke_config",
    "run_update1_smoke",
    "validate_run_id",
    "verify_update1_smoke_source_gate",
]
