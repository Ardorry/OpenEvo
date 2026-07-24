"""One-call infrastructure smoke for the taskwise Local Codex boundary.

This protocol is deliberately not an experimental arm.  It runs exactly one
fixed public ChemBench4K item, in one fresh taskwise control session, then
performs private evaluation.  It never constructs a Core evolution port, job,
or artifact and it has no resume path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

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
    DEFAULT_SOURCE_MANIFEST,
    SourceManifestError,
    verify_source_manifest,
)
from openevo_chembench.taskwise_config_v1 import (
    SOURCE_COMMIT_PLACEHOLDER,
    TaskwiseExperimentConfigV1,
    load_taskwise_config_v1,
)
from openevo_chembench.taskwise_cli_v1 import verify_taskwise_codex_policy_v1
from openevo_chembench.taskwise_context_binding_v1 import (
    TaskwiseSessionContextBindingV1,
)
from openevo_chembench.taskwise_online_runner_v1 import (
    TaskwiseAgentRequestV1,
    TaskwiseExecutorV1,
)
from openevo_chembench.taskwise_sampling_v1 import select_taskwise_stream


PROTOCOL_ID = "taskwise_round0_infrastructure_smoke_v1"
SCHEMA_VERSION = "chembench4k_taskwise_round0_smoke_config_v1"
MANIFEST_SCHEMA_VERSION = "chembench4k_taskwise_round0_smoke_manifest_v1"
RESULT_SCHEMA_VERSION = "chembench4k_taskwise_round0_smoke_result_v1"
CLASSIFICATION = (
    "NON_PERFORMANCE_INFRASTRUCTURE_SMOKE",
    "CONTROL_ONLY",
    "ROUND_0_ONLY",
    "NOT_A_STANDARD_CHEMBENCH4K_SCORE",
)
SCOPE = "smoke1"
ARM = "control"
MODEL = "gpt-5.5"
REASONING_EFFORT = "medium"
PROMPT_RENDERER_ID = "chembench4k_official_five_shot_v2"
PARSER_ID = "official_first_capital_parser_v2"
EVALUATOR_ID = "chembench4k_accuracy_v2"
SMOKE_SOURCE_SCOPE = "canary9"
SMOKE_SOURCE_ORDINAL = 0

_MODULE = Path(__file__).resolve()
PACKAGE_ROOT = _MODULE.parents[2]
REPOSITORY_ROOT = _MODULE.parents[4]
WORKSPACE_ROOT = _MODULE.parents[5]
CONFIG_ROOT = PACKAGE_ROOT / "configs"
DEFAULT_CONFIG_PATH = CONFIG_ROOT / "taskwise_round0_infrastructure_smoke_v1.yaml"
CONTROL_EXECUTOR_CONFIG = CONFIG_ROOT / "control_canary9_taskwise_online_v1.yaml"
SOURCE_MANIFEST_PATH = PACKAGE_ROOT / DEFAULT_SOURCE_MANIFEST
_RUN_ID = re.compile(r"[a-z0-9][a-z0-9_-]{7,95}\Z", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_PUBLIC_FORBIDDEN_KEYS = frozenset(
    {
        "answer",
        "correct_answer",
        "private_task",
        "source_index",
        "target",
        "target_scores",
    }
)


class Round0SmokeError(RuntimeError):
    """Sanitized smoke failure carrying only a closed finding code."""

    def __init__(self, finding_code: str) -> None:
        if type(finding_code) is not str or not finding_code:
            raise ValueError("finding_code must be non-empty text")
        self.finding_code = finding_code
        super().__init__(finding_code)


class Round0SmokeExecutor(Protocol):
    def execute_taskwise(self, request: TaskwiseAgentRequestV1) -> RawAttempt: ...

    def consume_taskwise_context_receipt(self, session_id: str) -> Any: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Round0SmokeExecutorPolicyV1:
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
            raise ValueError("smoke requires the local Codex CLI taskwise backend")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or self.timeout_seconds < 1
        ):
            raise ValueError("smoke timeout_seconds must be positive")
        if self.concurrency != 1 or self.infrastructure_retries != 0:
            raise ValueError("smoke requires one call with no retry")
        if any(
            (
                self.tools_enabled,
                self.mcp_enabled,
                self.web_enabled,
                self.network_enabled,
                self.subagents_enabled,
            )
        ):
            raise ValueError("smoke executor capabilities must remain disabled")

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
class Round0SmokeConfigV1:
    dataset_root: str
    dataset_manifest: str
    public_task_manifest: str
    private_task_manifest: str
    manifest_summary: str
    output_root: str
    diagnostic_root: str
    codex_cli_version: str
    executor: Round0SmokeExecutorPolicyV1
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
    attempts_per_task: int = 1
    rounds: int = 1
    evolution_updates: int = 0
    core_jobs: int = 0
    core_artifacts: int = 0
    memory_enabled: bool = False
    resume_enabled: bool = False

    def __post_init__(self) -> None:
        if (
            self.schema_version != SCHEMA_VERSION
            or self.protocol_id != PROTOCOL_ID
            or self.classification != CLASSIFICATION
            or self.scope != SCOPE
            or self.arm != ARM
        ):
            raise ValueError("smoke protocol identity is not frozen")
        if (
            self.dataset_repository != CHEMBENCH4K_REPOSITORY
            or self.dataset_revision != CHEMBENCH4K_REVISION
        ):
            raise ValueError("smoke dataset identity is not frozen")
        if self.model != MODEL or self.reasoning_effort != REASONING_EFFORT:
            raise ValueError("smoke model identity is not frozen")
        if (
            self.prompt_renderer_id != PROMPT_RENDERER_ID
            or self.parser_id != PARSER_ID
            or self.evaluator_id != EVALUATOR_ID
        ):
            raise ValueError("smoke prompt/parser/evaluator identity changed")
        if (
            self.tasks != 1
            or self.attempts_per_task != 1
            or self.rounds != 1
            or self.evolution_updates != 0
            or self.core_jobs != 0
            or self.core_artifacts != 0
            or self.memory_enabled is not False
            or self.resume_enabled is not False
        ):
            raise ValueError("smoke must remain one-call, memory-free, and non-resumable")
        for field_name in (
            "dataset_root",
            "dataset_manifest",
            "public_task_manifest",
            "private_task_manifest",
            "manifest_summary",
            "output_root",
            "diagnostic_root",
        ):
            _require_relative_path(getattr(self, field_name), field_name)
        if type(self.codex_cli_version) is not str or not self.codex_cli_version:
            raise ValueError("codex_cli_version must be non-empty text")
        if self.source_commit != SOURCE_COMMIT_PLACEHOLDER and (
            type(self.source_commit) is not str
            or _GIT_COMMIT.fullmatch(self.source_commit) is None
        ):
            raise ValueError("source_commit must be the placeholder or a Git commit")

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
                "public_task_manifest": self.public_task_manifest,
                "private_task_manifest": self.private_task_manifest,
                "manifest_summary": self.manifest_summary,
            },
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "codex_cli_version": self.codex_cli_version,
            "prompt_renderer_id": self.prompt_renderer_id,
            "parser_id": self.parser_id,
            "evaluator_id": self.evaluator_id,
            "tasks": self.tasks,
            "attempts_per_task": self.attempts_per_task,
            "rounds": self.rounds,
            "evolution_updates": self.evolution_updates,
            "core_jobs": self.core_jobs,
            "core_artifacts": self.core_artifacts,
            "memory_enabled": self.memory_enabled,
            "resume_enabled": self.resume_enabled,
            "executor": self.executor.to_payload(),
            "output_root": self.output_root,
            "diagnostic_root": self.diagnostic_root,
            "source_commit": self.source_commit,
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical_bytes(self.to_payload())).hexdigest()


@dataclass(frozen=True, slots=True)
class Round0SmokeManifestSetV1:
    public_path: Path
    private_path: Path
    summary_path: Path
    public_sha256: str
    private_sha256: str
    summary_sha256: str
    task_uid: str
    item_count: int = 1


ExecutorFactory = Callable[
    [Round0SmokeConfigV1, Path],
    Round0SmokeExecutor,
]
SourceGate = Callable[[Round0SmokeConfigV1], str]
ExecutorPolicyGate = Callable[[TaskwiseExperimentConfigV1], object]


def load_round0_smoke_config(path: Path = DEFAULT_CONFIG_PATH) -> Round0SmokeConfigV1:
    """Load the exact smoke YAML schema."""

    if not isinstance(path, Path) or not path.is_file():
        raise Round0SmokeError("SMOKE_CONFIG_MISSING")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise Round0SmokeError("SMOKE_CONFIG_UNREADABLE") from exc
    root_keys = {
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
        "attempts_per_task",
        "rounds",
        "evolution_updates",
        "core_jobs",
        "core_artifacts",
        "memory_enabled",
        "resume_enabled",
        "executor",
        "output_root",
        "diagnostic_root",
        "source_commit",
    }
    if type(payload) is not dict or set(payload) != root_keys:
        raise Round0SmokeError("SMOKE_CONFIG_SCHEMA_INVALID")
    dataset = _closed_mapping(
        payload["dataset"],
        {
            "repository",
            "revision",
            "root",
            "manifest",
            "public_task_manifest",
            "private_task_manifest",
            "manifest_summary",
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
        return Round0SmokeConfigV1(
            schema_version=payload["schema_version"],
            protocol_id=payload["protocol_id"],
            classification=tuple(payload["classification"]),
            scope=payload["scope"],
            arm=payload["arm"],
            dataset_repository=dataset["repository"],
            dataset_revision=dataset["revision"],
            dataset_root=dataset["root"],
            dataset_manifest=dataset["manifest"],
            public_task_manifest=dataset["public_task_manifest"],
            private_task_manifest=dataset["private_task_manifest"],
            manifest_summary=dataset["manifest_summary"],
            model=payload["model"],
            reasoning_effort=payload["reasoning_effort"],
            codex_cli_version=payload["codex_cli_version"],
            prompt_renderer_id=payload["prompt_renderer_id"],
            parser_id=payload["parser_id"],
            evaluator_id=payload["evaluator_id"],
            tasks=payload["tasks"],
            attempts_per_task=payload["attempts_per_task"],
            rounds=payload["rounds"],
            evolution_updates=payload["evolution_updates"],
            core_jobs=payload["core_jobs"],
            core_artifacts=payload["core_artifacts"],
            memory_enabled=payload["memory_enabled"],
            resume_enabled=payload["resume_enabled"],
            executor=Round0SmokeExecutorPolicyV1(**executor),
            output_root=payload["output_root"],
            diagnostic_root=payload["diagnostic_root"],
            source_commit=payload["source_commit"],
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise Round0SmokeError("SMOKE_CONFIG_INVALID") from exc


def generate_round0_smoke_manifests(
    loader: ChemBench4KDatasetLoader,
    *,
    public_path: Path,
    private_path: Path,
    summary_path: Path,
) -> Round0SmokeManifestSetV1:
    """Write the one fixed task manifest set deterministically."""

    public_bytes, private_bytes, summary_bytes, task = _expected_manifest_bytes(loader)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_bytes(public_bytes)
    summary_path.write_bytes(summary_bytes)
    _write_private_bytes(private_path, private_bytes)
    return _manifest_set(
        public_path,
        private_path,
        summary_path,
        public_bytes,
        private_bytes,
        summary_bytes,
        task.uid,
    )


def verify_round0_smoke_manifests(
    loader: ChemBench4KDatasetLoader,
    *,
    public_path: Path,
    private_path: Path,
    summary_path: Path,
) -> Round0SmokeManifestSetV1:
    """Require byte-exact public/private manifests before any executor exists."""

    expected_public, expected_private, expected_summary, task = _expected_manifest_bytes(loader)
    try:
        actual_public = public_path.read_bytes()
        actual_private = private_path.read_bytes()
        actual_summary = summary_path.read_bytes()
    except OSError as exc:
        raise Round0SmokeError("SMOKE_MANIFEST_SET_MISSING") from exc
    if (
        actual_public != expected_public
        or actual_private != expected_private
        or actual_summary != expected_summary
    ):
        raise Round0SmokeError("SMOKE_MANIFEST_SET_MISMATCH")
    return _manifest_set(
        public_path,
        private_path,
        summary_path,
        actual_public,
        actual_private,
        actual_summary,
        task.uid,
    )


def dry_run_round0_smoke(
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> dict[str, object]:
    """Validate config, dataset, and manifests without constructing an executor."""

    config = load_round0_smoke_config(config_path)
    loader, manifests, task = _verify_static_inputs(config)
    result = {
        "schema_version": RESULT_SCHEMA_VERSION,
        "protocol_id": PROTOCOL_ID,
        "classification": list(CLASSIFICATION),
        "status": "PASS",
        "task_count": 1,
        "task_uid": task.uid,
        "dataset_sha256": loader.manifest.combined_sha256,
        "public_manifest_sha256": manifests.public_sha256,
        "config_sha256": config.digest,
        "model_calls": 0,
        "sessions_created": 0,
        "evolution_updates": 0,
        "core_jobs_created": 0,
        "core_artifacts_registered": 0,
        "resume_enabled": False,
    }
    _require_public_payload(result)
    return result


def run_round0_smoke(
    config_path: Path = DEFAULT_CONFIG_PATH,
    *,
    run_id: str | None = None,
    executor_factory: ExecutorFactory | None = None,
    source_gate: SourceGate | None = None,
    executor_policy_gate: ExecutorPolicyGate | None = None,
) -> dict[str, object]:
    """Run exactly one new control session and one private evaluation."""

    config = load_round0_smoke_config(config_path)
    current_commit = (source_gate or verify_round0_smoke_source_gate)(config)
    loader, manifests, task = _verify_static_inputs(config)
    selected_run_id = generate_round0_smoke_run_id() if run_id is None else validate_run_id(run_id)
    output_root = _resolve_workspace_path(config.output_root, must_exist=False)
    diagnostic_root = _resolve_workspace_path(config.diagnostic_root, must_exist=False)
    run_root = output_root / selected_run_id
    output_directory = run_root / ARM
    run_diagnostic_root = diagnostic_root / selected_run_id / ARM
    compatibility_config = _load_executor_compat_config(config)
    if executor_policy_gate is not None:
        executor_policy_gate(compatibility_config)
    elif executor_factory is None:
        verify_taskwise_codex_policy_v1(compatibility_config)
    _claim_new_run_directory(run_root, output_directory, run_diagnostic_root)

    public_directory = output_directory / "public"
    private_directory = output_directory / "private"
    public_directory.mkdir(mode=0o755)
    private_directory.mkdir(mode=0o700)
    state_path = output_directory / "run_state.json"
    state = _initial_state(
        config=config,
        run_id=selected_run_id,
        current_commit=current_commit,
        dataset_sha256=loader.manifest.combined_sha256,
        manifests=manifests,
        task=task,
        diagnostic_root=run_diagnostic_root,
    )
    _write_public_json(state_path, state)

    dev = loader.load_category(task.category, split="dev")
    prompt = render_official_five_shot_prompt(task.to_public(), category_dev=dev)
    session_id = f"smoke_session_{uuid.uuid4().hex}"
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
            "session_id": session_id,
            "session_attempt_count": 1,
        }
    )
    _write_public_json(state_path, state)

    selected_factory = executor_factory or build_default_round0_smoke_executor
    executor: Round0SmokeExecutor | None = None
    completion_observed = False
    attempt: RawAttempt | None = None
    context_receipt: Any = None
    try:
        executor = selected_factory(config, run_diagnostic_root)
        attempt = executor.execute_taskwise(request)
        completion_observed = True
        if type(attempt) is not RawAttempt:
            raise TypeError("smoke executor must return exact RawAttempt")
        context_receipt = executor.consume_taskwise_context_receipt(session_id)
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
            raise Round0SmokeError("SMOKE_CONTEXT_BINDING_VIOLATION")
        executor.close()
        executor = None
    except Exception as exc:
        close_failure: Exception | None = None
        if executor is not None:
            try:
                executor.close()
            except Exception as close_exc:
                close_failure = close_exc
        terminal = _terminal_failure_code(close_failure or exc)
        state.update(
            {
                "status": terminal,
                "completion_observed": bool(
                    completion_observed or getattr(exc, "completion_observed", False)
                ),
                "resume_allowed": False,
                "failure_code": _sanitized_error_code(close_failure or exc),
            }
        )
        _write_public_json(state_path, state)
        raise Round0SmokeError(terminal) from None

    assert attempt is not None
    post_completion_stage = "PRIVATE_EVALUATION"
    try:
        evaluation = ChemBench4KPrivateEvaluator().evaluate(
            task=task,
            raw_completion=attempt.response,
        )
        private_payload = {
            "schema_version": "chembench4k_taskwise_round0_smoke_private_evaluation_v1",
            "protocol_id": PROTOCOL_ID,
            "run_id": selected_run_id,
            "task_uid": task.uid,
            "category": task.category,
            "source_split": task.source_split,
            "source_index": task.source_index,
            "target": task.target,
            "raw_completion": attempt.response,
            "transcript_reference": attempt.transcript_reference.reference,
            "official_prediction": evaluation.official.prediction,
            "strict_prediction": evaluation.strict.prediction,
            "correct": evaluation.correct,
        }
        if attempt.runtime_metadata is not None:
            private_payload["runtime_metadata"] = attempt.runtime_metadata.to_result_payload()
        post_completion_stage = "PRIVATE_PERSISTENCE"
        _write_private_json(private_directory / "evaluation.json", private_payload)

        post_completion_stage = "PUBLIC_RESULT_PERSISTENCE"
        public_result = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "protocol_id": PROTOCOL_ID,
            "classification": list(CLASSIFICATION),
            "status": "COMPLETED",
            "run_id": selected_run_id,
            "arm": ARM,
            "task_count": 1,
            "task_uid": task.uid,
            "category": task.category,
            "round_index": 0,
            "session_id": session_id,
            "sessions_created": 1,
            "completion_count": 1,
            "official_parse_status": evaluation.official.status.value,
            "strict_parse_status": evaluation.strict.status.value,
            "strict_parse_success": evaluation.strict.parsed,
            "context_binding_receipt": context_receipt.to_public_dict(),
            "memory_present": False,
            "evolution_updates": 0,
            "core_jobs_created": 0,
            "core_artifacts_registered": 0,
            "resume_allowed": False,
            "diagnostic_namespace_sha256": hashlib.sha256(
                os.fspath(run_diagnostic_root).encode("utf-8")
            ).hexdigest(),
        }
        if attempt.runtime_metadata is not None:
            public_result["runtime_metadata"] = attempt.runtime_metadata.to_result_payload()
        _require_public_payload(public_result)
        result_path = public_directory / "result.json"
        _write_public_json(result_path, public_result)
        state.update(
            {
                "status": "COMPLETED",
                "completion_observed": True,
                "completion_count": 1,
                "resume_allowed": False,
                "result_sha256": hashlib.sha256(result_path.read_bytes()).hexdigest(),
            }
        )
        post_completion_stage = "PUBLIC_STATE_PERSISTENCE"
        _write_public_json(state_path, state)
    except Exception:
        failure_code = {
            "PRIVATE_EVALUATION": "SMOKE_PRIVATE_EVALUATION_FAILED",
            "PRIVATE_PERSISTENCE": "SMOKE_PRIVATE_PERSISTENCE_FAILED",
            "PUBLIC_RESULT_PERSISTENCE": "SMOKE_PUBLIC_PERSISTENCE_FAILED",
            "PUBLIC_STATE_PERSISTENCE": "SMOKE_PUBLIC_PERSISTENCE_FAILED",
        }[post_completion_stage]
        state.update(
            {
                "status": "EXECUTION_FAILED",
                "completion_observed": True,
                "completion_count": 1,
                "resume_allowed": False,
                "failure_code": failure_code,
            }
        )
        try:
            _write_public_json(state_path, state)
        except Exception:
            pass
        raise Round0SmokeError(failure_code) from None
    return public_result


def build_default_round0_smoke_executor(
    config: Round0SmokeConfigV1,
    diagnostic_root: Path,
) -> TaskwiseExecutorV1:
    """Reuse the audited taskwise control executor without a formal runner."""

    base = _load_executor_compat_config(config)
    return LocalCodexCLIExecutor(
        config=base,
        task_timeout_seconds=config.executor.timeout_seconds,
        diagnostic_root=diagnostic_root,
    )


def _load_executor_compat_config(
    config: Round0SmokeConfigV1,
) -> TaskwiseExperimentConfigV1:
    """Bind the smoke to the audited taskwise control executor policy."""

    base = load_taskwise_config_v1(CONTROL_EXECUTOR_CONFIG)
    if type(base) is not TaskwiseExperimentConfigV1 or base.arm != ARM:
        raise Round0SmokeError("SMOKE_EXECUTOR_COMPAT_CONFIG_INVALID")
    parity = {
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
        "subagents_enabled": (
            base.executor.subagents_enabled == config.executor.subagents_enabled
        ),
    }
    if not all(parity.values()):
        raise Round0SmokeError("SMOKE_EXECUTOR_POLICY_PARITY_FAILED")
    return base


def verify_round0_smoke_source_gate(config: Round0SmokeConfigV1) -> str:
    """Require a clean committed package before a paid smoke invocation."""

    head = _git("rev-parse", "HEAD").strip()
    if _GIT_COMMIT.fullmatch(head) is None:
        raise Round0SmokeError("SMOKE_SOURCE_COMMIT_UNAVAILABLE")
    if config.source_commit not in {SOURCE_COMMIT_PLACEHOLDER, head}:
        raise Round0SmokeError("SMOKE_SOURCE_COMMIT_MISMATCH")
    status = _git(
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        "benchmarks/chembench",
    )
    if status.strip():
        raise Round0SmokeError("SMOKE_PACKAGE_SOURCE_DIRTY")
    try:
        verify_source_manifest(PACKAGE_ROOT, SOURCE_MANIFEST_PATH)
    except (OSError, ValueError, SourceManifestError) as exc:
        raise Round0SmokeError("SMOKE_SOURCE_MANIFEST_MISMATCH") from exc
    return head


def generate_round0_smoke_run_id() -> str:
    """Return a collision-resistant runtime identifier with no dataset identity."""

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ").lower()
    return validate_run_id(f"smoke1_{timestamp}_{uuid.uuid4().hex[:12]}")


def validate_run_id(run_id: str) -> str:
    if type(run_id) is not str or _RUN_ID.fullmatch(run_id) is None:
        raise Round0SmokeError("SMOKE_RUN_ID_INVALID")
    return run_id


def _verify_static_inputs(
    config: Round0SmokeConfigV1,
) -> tuple[ChemBench4KDatasetLoader, Round0SmokeManifestSetV1, PrivateChemBench4KTask]:
    snapshot_root = _resolve_workspace_path(config.dataset_root, must_exist=True)
    dataset_manifest = _resolve_workspace_path(config.dataset_manifest, must_exist=True)
    loader = ChemBench4KDatasetLoader(
        snapshot_root=snapshot_root,
        manifest_path=dataset_manifest,
    )
    manifests = verify_round0_smoke_manifests(
        loader,
        public_path=_resolve_workspace_path(config.public_task_manifest, must_exist=True),
        private_path=_resolve_workspace_path(config.private_task_manifest, must_exist=True),
        summary_path=_resolve_workspace_path(config.manifest_summary, must_exist=True),
    )
    task = _fixed_smoke_task(loader)
    if manifests.task_uid != task.uid:
        raise Round0SmokeError("SMOKE_TASK_IDENTITY_MISMATCH")
    return loader, manifests, task


def _expected_manifest_bytes(
    loader: ChemBench4KDatasetLoader,
) -> tuple[bytes, bytes, bytes, PrivateChemBench4KTask]:
    task = _fixed_smoke_task(loader)
    public_bytes = _json_line({"ordinal": 0, **task.to_public().to_public_dict()})
    private_bytes = _json_line(
        {
            "ordinal": 0,
            "uid": task.uid,
            "category": task.category,
            "source_split": task.source_split,
            "source_index": task.source_index,
            "target": task.target,
            "dataset_revision": task.dataset_revision,
            "dataset_sha256": task.dataset_sha256,
        }
    )
    summary = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "protocol_id": PROTOCOL_ID,
        "classification": list(CLASSIFICATION),
        "scope": SCOPE,
        "source_scope": SMOKE_SOURCE_SCOPE,
        "source_ordinal": SMOKE_SOURCE_ORDINAL,
        "item_count": 1,
        "task_uid": task.uid,
        "dataset_repository": loader.manifest.repository,
        "dataset_revision": loader.manifest.revision,
        "dataset_sha256": loader.manifest.combined_sha256,
        "public_manifest_sha256": hashlib.sha256(public_bytes).hexdigest(),
    }
    summary_bytes = _json_line(summary)
    return public_bytes, private_bytes, summary_bytes, task


def _fixed_smoke_task(loader: ChemBench4KDatasetLoader) -> PrivateChemBench4KTask:
    tasks, _allocation, _seed = select_taskwise_stream(loader, scope=SMOKE_SOURCE_SCOPE)
    if len(tasks) != 9:
        raise Round0SmokeError("SMOKE_SOURCE_CANARY_INVALID")
    return tasks[SMOKE_SOURCE_ORDINAL]


def _manifest_set(
    public_path: Path,
    private_path: Path,
    summary_path: Path,
    public_bytes: bytes,
    private_bytes: bytes,
    summary_bytes: bytes,
    task_uid: str,
) -> Round0SmokeManifestSetV1:
    return Round0SmokeManifestSetV1(
        public_path=public_path,
        private_path=private_path,
        summary_path=summary_path,
        public_sha256=hashlib.sha256(public_bytes).hexdigest(),
        private_sha256=hashlib.sha256(private_bytes).hexdigest(),
        summary_sha256=hashlib.sha256(summary_bytes).hexdigest(),
        task_uid=task_uid,
    )


def _claim_new_run_directory(
    run_root: Path,
    output_directory: Path,
    diagnostic_root: Path,
) -> None:
    if run_root.exists() or diagnostic_root.exists():
        raise Round0SmokeError("SMOKE_OUTPUT_TARGET_EXISTS")
    run_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        run_root.mkdir(mode=0o700)
        output_directory.mkdir(mode=0o700)
    except FileExistsError:
        raise Round0SmokeError("SMOKE_OUTPUT_TARGET_EXISTS") from None


def _initial_state(
    *,
    config: Round0SmokeConfigV1,
    run_id: str,
    current_commit: str,
    dataset_sha256: str,
    manifests: Round0SmokeManifestSetV1,
    task: PrivateChemBench4KTask,
    diagnostic_root: Path,
) -> dict[str, object]:
    state = {
        "schema_version": "chembench4k_taskwise_round0_smoke_state_v1",
        "protocol_id": PROTOCOL_ID,
        "classification": list(CLASSIFICATION),
        "status": "INITIALIZED",
        "run_id": run_id,
        "arm": ARM,
        "scope": SCOPE,
        "task_count": 1,
        "task_uid": task.uid,
        "category": task.category,
        "current_round": 0,
        "session_attempt_count": 0,
        "completion_count": 0,
        "completion_observed": False,
        "memory_present": False,
        "evolution_updates": 0,
        "core_jobs_created": 0,
        "core_artifacts_registered": 0,
        "resume_allowed": False,
        "source_commit": current_commit,
        "dataset_sha256": dataset_sha256,
        "public_manifest_sha256": manifests.public_sha256,
        "config_sha256": config.digest,
        "diagnostic_namespace_sha256": hashlib.sha256(
            os.fspath(diagnostic_root).encode("utf-8")
        ).hexdigest(),
    }
    _require_public_payload(state)
    return state


def _terminal_failure_code(error: Exception) -> str:
    run_status = getattr(error, "run_status", None)
    code = getattr(error, "code", None)
    code_value = getattr(code, "value", code)
    finding_code = getattr(error, "finding_code", None)
    taskwise_failure_code = getattr(error, "taskwise_failure_code", None)
    if (
        run_status == "SECURITY_TOOL_USE_VIOLATION"
        or code_value
        in {
            "security_tool_use_violation",
            "SECURITY_TOOL_USE_VIOLATION",
        }
        or finding_code
        in {
            "EXECUTOR_SECURITY_TOOL_USE_VIOLATION",
            "SECURITY_TOOL_USE_VIOLATION",
        }
        or taskwise_failure_code == "EXECUTOR_SECURITY_TOOL_USE_VIOLATION"
    ):
        return "SECURITY_TOOL_USE_VIOLATION"
    return "EXECUTION_FAILED"


def _sanitized_error_code(error: Exception) -> str:
    taskwise_failure_code = getattr(error, "taskwise_failure_code", None)
    if type(taskwise_failure_code) is str and re.fullmatch(
        r"[A-Z][A-Z0-9_]{1,95}",
        taskwise_failure_code,
    ):
        return taskwise_failure_code
    code = getattr(error, "code", None)
    value = getattr(code, "value", code)
    if type(value) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,96}", value):
        return value
    finding = getattr(error, "finding_code", None)
    if type(finding) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,96}", finding):
        return finding
    return "EXECUTOR_INTERNAL_ERROR"


def _require_public_payload(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if type(key) is not str or key.casefold() in _PUBLIC_FORBIDDEN_KEYS:
                raise Round0SmokeError("SMOKE_PUBLIC_TARGET_LEAK")
            _require_public_payload(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _require_public_payload(item)


def _write_public_json(path: Path, payload: dict[str, object]) -> None:
    _require_public_payload(payload)
    _write_json(path, payload, mode=0o644)


def _write_private_json(path: Path, payload: dict[str, object]) -> None:
    _write_json(path, payload, mode=0o600)


def _write_json(path: Path, payload: dict[str, object], *, mode: int) -> None:
    data = (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        path.chmod(mode)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _write_private_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        path.chmod(0o600)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _json_line(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _canonical_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _closed_mapping(
    value: object,
    keys: set[str],
    field_name: str,
) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise Round0SmokeError(f"SMOKE_{field_name.upper()}_SCHEMA_INVALID")
    return dict(value)


def _require_relative_path(value: object, field_name: str) -> None:
    if type(value) is not str or not value:
        raise ValueError(f"{field_name} must be non-empty text")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{field_name} must be a clean relative path")


def _resolve_workspace_path(value: str, *, must_exist: bool) -> Path:
    _require_relative_path(value, "workspace path")
    candidate = (WORKSPACE_ROOT / value).resolve()
    try:
        candidate.relative_to(WORKSPACE_ROOT.resolve())
    except ValueError as exc:
        raise Round0SmokeError("SMOKE_PATH_ESCAPES_WORKSPACE") from exc
    if must_exist and not candidate.exists():
        raise Round0SmokeError("SMOKE_REQUIRED_PATH_MISSING")
    return candidate


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=REPOSITORY_ROOT,
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise Round0SmokeError("SMOKE_GIT_QUERY_FAILED")
    return completed.stdout


def _prepare_manifests(config_path: Path) -> dict[str, object]:
    config = load_round0_smoke_config(config_path)
    loader = ChemBench4KDatasetLoader(
        snapshot_root=_resolve_workspace_path(config.dataset_root, must_exist=True),
        manifest_path=_resolve_workspace_path(config.dataset_manifest, must_exist=True),
    )
    manifests = generate_round0_smoke_manifests(
        loader,
        public_path=_resolve_workspace_path(config.public_task_manifest, must_exist=False),
        private_path=_resolve_workspace_path(config.private_task_manifest, must_exist=False),
        summary_path=_resolve_workspace_path(config.manifest_summary, must_exist=False),
    )
    return {
        "status": "PASS",
        "protocol_id": PROTOCOL_ID,
        "item_count": manifests.item_count,
        "task_uid": manifests.task_uid,
        "public_manifest_sha256": manifests.public_sha256,
        "summary_sha256": manifests.summary_sha256,
        "model_calls": 0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare-manifests", "dry-run"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    run_parser.add_argument("--run-id")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "prepare-manifests":
            result = _prepare_manifests(arguments.config)
        elif arguments.command == "dry-run":
            result = dry_run_round0_smoke(arguments.config)
        else:
            result = run_round0_smoke(arguments.config, run_id=arguments.run_id)
    except Round0SmokeError as exc:
        parser.exit(status=2, message=f"BLOCKED: {exc.finding_code}\n")
    except Exception:
        parser.exit(status=2, message="BLOCKED: SMOKE_INTERNAL_ERROR\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "CLASSIFICATION",
    "DEFAULT_CONFIG_PATH",
    "MANIFEST_SCHEMA_VERSION",
    "PROTOCOL_ID",
    "RESULT_SCHEMA_VERSION",
    "Round0SmokeConfigV1",
    "Round0SmokeError",
    "Round0SmokeExecutorPolicyV1",
    "Round0SmokeManifestSetV1",
    "build_default_round0_smoke_executor",
    "dry_run_round0_smoke",
    "generate_round0_smoke_manifests",
    "generate_round0_smoke_run_id",
    "load_round0_smoke_config",
    "run_round0_smoke",
    "validate_run_id",
    "verify_round0_smoke_manifests",
    "verify_round0_smoke_source_gate",
]
