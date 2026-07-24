"""Closed configuration contracts for paired taskwise online evolution."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml


ONLINE_PROTOCOL_ID = "taskwise_online_evolution_v1"
CONTROL_PROTOCOL_ID = "repeated_session_control_v1"
CONFIG_SCHEMA_V1 = "chembench4k_taskwise_online_config_v1"
EXECUTION_MODE = "standalone_openevo_maintainer_benchmark"
DATASET_REPOSITORY = "AI4Chem/ChemBench4K"
DATASET_REVISION = "f8ad41a980170f4c5d0cc97e57722d06887c8f53"
MODEL = "gpt-5.5"
REASONING_EFFORT = "medium"
SOURCE_COMMIT_PLACEHOLDER = "BIND_CLEAN_HEAD_AT_LAUNCH"
PROTOCOL_MARKERS = (
    "ONLINE_TASKWISE_EVOLUTION",
    "TEST_TIME_ADAPTATION",
    "NON_STANDARD_CHEMBENCH4K_PROTOCOL",
    "NOT_A_STANDARD_LEADERBOARD_SCORE",
)
TASKWISE_MEMORY_REQUIRED_SECTIONS = (
    "Do",
    "Avoid",
    "Validate",
    "When Applicable",
    "Retired Or Superseded",
)
TASKWISE_MEMORY_MAX_UTF8_BYTES = 16 * 1024
TASKWISE_MEMORY_MAX_ESTIMATED_TOKENS = 4096
TASKWISE_MEMORY_MAX_ITEMS_PER_SECTION = 24
TASKWISE_MEMORY_TOKEN_ESTIMATOR_ID = "ascii_word_or_unicode_codepoint_v1"
TASKWISE_MEMORY_PARSER_ID = "exact_markdown_sections_v1"

_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
PILOT500_STREAM_COUNT = 10
PILOT500_STREAM_SCOPES = tuple(
    f"pilot500_stream_{index:02d}" for index in range(PILOT500_STREAM_COUNT)
)
FULL_STREAM_COUNT = 10
FULL_STREAM_SCOPES = tuple(f"full_stream_{index:02d}" for index in range(FULL_STREAM_COUNT))
_SCOPES = frozenset(
    {
        "canary9",
        "pilot500",
        *PILOT500_STREAM_SCOPES,
        *FULL_STREAM_SCOPES,
    }
)


class TaskwiseConfigError(ValueError):
    """Raised when an online/control configuration is not closed."""


@dataclass(frozen=True, slots=True)
class TaskwiseMemoryLimitsV1:
    """Protocol-fixed limits for every approved taskwise text-memory artifact."""

    max_utf8_bytes: int
    max_estimated_tokens: int
    max_items_per_section: int
    required_sections: tuple[str, ...]
    token_estimator_id: str
    parser_id: str

    def __post_init__(self) -> None:
        if (
            self.max_utf8_bytes != TASKWISE_MEMORY_MAX_UTF8_BYTES
            or self.max_estimated_tokens != TASKWISE_MEMORY_MAX_ESTIMATED_TOKENS
            or self.max_items_per_section != TASKWISE_MEMORY_MAX_ITEMS_PER_SECTION
            or self.required_sections != TASKWISE_MEMORY_REQUIRED_SECTIONS
            or self.token_estimator_id != TASKWISE_MEMORY_TOKEN_ESTIMATOR_ID
            or self.parser_id != TASKWISE_MEMORY_PARSER_ID
        ):
            raise TaskwiseConfigError("taskwise memory limits are protocol-fixed")

    def to_payload(self) -> dict[str, object]:
        return {
            "max_utf8_bytes": self.max_utf8_bytes,
            "max_estimated_tokens": self.max_estimated_tokens,
            "max_items_per_section": self.max_items_per_section,
            "required_sections": list(self.required_sections),
            "token_estimator_id": self.token_estimator_id,
            "parser_id": self.parser_id,
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_taskwise_config_bytes(self.to_payload())).hexdigest()


TASKWISE_MEMORY_LIMITS_V1 = TaskwiseMemoryLimitsV1(
    max_utf8_bytes=TASKWISE_MEMORY_MAX_UTF8_BYTES,
    max_estimated_tokens=TASKWISE_MEMORY_MAX_ESTIMATED_TOKENS,
    max_items_per_section=TASKWISE_MEMORY_MAX_ITEMS_PER_SECTION,
    required_sections=TASKWISE_MEMORY_REQUIRED_SECTIONS,
    token_estimator_id=TASKWISE_MEMORY_TOKEN_ESTIMATOR_ID,
    parser_id=TASKWISE_MEMORY_PARSER_ID,
)


def _clean_relative_path(value: object, field_name: str) -> str:
    if type(value) is not str or not value:
        raise TaskwiseConfigError(f"{field_name} must be non-empty text")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise TaskwiseConfigError(f"{field_name} must be a clean relative path")
    return value


def _closed_mapping(
    value: object,
    *,
    keys: frozenset[str],
    field_name: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise TaskwiseConfigError(f"{field_name} must contain exactly {sorted(keys)}")
    if any(type(key) is not str for key in value):
        raise TaskwiseConfigError(f"{field_name} keys must be strings")
    return dict(value)


@dataclass(frozen=True, slots=True)
class TaskwiseExecutorPolicyV1:
    backend: str
    harness: str
    timeout_seconds: int
    concurrency: int
    infrastructure_retries_before_completion: int
    tools_enabled: bool
    mcp_enabled: bool
    web_enabled: bool
    network_enabled: bool
    subagents_enabled: bool

    def __post_init__(self) -> None:
        if self.backend != "local_codex_cli" or self.harness != "codex_cli":
            raise TaskwiseConfigError("taskwise protocol requires local Codex CLI")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or self.timeout_seconds < 1
        ):
            raise TaskwiseConfigError("executor timeout_seconds must be positive")
        if self.concurrency != 1:
            raise TaskwiseConfigError("stateful task stream requires concurrency one")
        if self.infrastructure_retries_before_completion != 0:
            raise TaskwiseConfigError("taskwise protocol does not retry model invocations")
        for field_name in (
            "tools_enabled",
            "mcp_enabled",
            "web_enabled",
            "network_enabled",
            "subagents_enabled",
        ):
            if getattr(self, field_name) is not False:
                raise TaskwiseConfigError(f"executor {field_name} must remain false")


@dataclass(frozen=True, slots=True)
class TaskwiseEvolutionPolicyV1:
    enabled: bool
    target: str
    method_id: str | None

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise TypeError("evolution.enabled must be boolean")
        if self.target != "text_memory":
            raise TaskwiseConfigError("taskwise evolution target must be text_memory")
        if self.enabled:
            if self.method_id != "text_memory_expel_reflector":
                raise TaskwiseConfigError("online arm must use the registered Core method")
        elif self.method_id is not None:
            raise TaskwiseConfigError("control arm must not configure an evolution method")


@dataclass(frozen=True, slots=True)
class TaskwiseExperimentConfigV1:
    """One arm of the paired fixed-budget online adaptation experiment."""

    protocol_id: str
    protocol_markers: tuple[str, ...]
    arm: Literal["control", "online"]
    scope: str
    run_name: str
    output_directory: str
    dataset_root: str
    dataset_manifest: str
    task_manifest: str
    private_task_manifest: str
    codex_cli_version: str
    prompt_renderer_id: str
    parser_id: str
    evaluator_id: str
    attempts_per_task: int
    inter_round_slots: int
    evolution_updates_per_task: int
    carry_memory_across_tasks: bool
    fixed_round_budget: bool
    stop_when_correct: bool
    final_score_round: int
    memory_limits: TaskwiseMemoryLimitsV1
    evolution: TaskwiseEvolutionPolicyV1
    executor: TaskwiseExecutorPolicyV1
    source_commit: str
    schema_version: str = CONFIG_SCHEMA_V1
    execution_mode: str = EXECUTION_MODE
    dataset_repository: str = DATASET_REPOSITORY
    dataset_revision: str = DATASET_REVISION
    model: str = MODEL
    reasoning_effort: str = REASONING_EFFORT

    def __post_init__(self) -> None:
        if self.schema_version != CONFIG_SCHEMA_V1:
            raise TaskwiseConfigError("unexpected taskwise config schema")
        if self.execution_mode != EXECUTION_MODE:
            raise TaskwiseConfigError("unexpected execution mode")
        if self.dataset_repository != DATASET_REPOSITORY:
            raise TaskwiseConfigError("unexpected dataset repository")
        if self.dataset_revision != DATASET_REVISION:
            raise TaskwiseConfigError("unexpected dataset revision")
        if self.model != MODEL or self.reasoning_effort != REASONING_EFFORT:
            raise TaskwiseConfigError("model identity is not frozen")
        if self.scope not in _SCOPES:
            raise TaskwiseConfigError(
                "scope must be canary9, legacy pilot500, or a frozen pilot/full stream"
            )
        if self.protocol_markers != PROTOCOL_MARKERS:
            raise TaskwiseConfigError("non-standard protocol markers are incomplete")
        if self.arm == "online":
            if (
                self.protocol_id != ONLINE_PROTOCOL_ID
                or not self.evolution.enabled
                or self.evolution_updates_per_task != 2
                or self.carry_memory_across_tasks is not True
            ):
                raise TaskwiseConfigError("online arm treatment contract is invalid")
        elif self.arm == "control":
            if (
                self.protocol_id != CONTROL_PROTOCOL_ID
                or self.evolution.enabled
                or self.evolution_updates_per_task != 0
                or self.carry_memory_across_tasks is not False
            ):
                raise TaskwiseConfigError("control arm treatment contract is invalid")
        else:
            raise TaskwiseConfigError("arm must be control or online")
        if (
            self.attempts_per_task != 3
            or self.inter_round_slots != 2
            or self.fixed_round_budget is not True
            or self.stop_when_correct is not False
            or self.final_score_round != 2
        ):
            raise TaskwiseConfigError("taskwise protocol requires fixed 3-attempt budget")
        if self.memory_limits != TASKWISE_MEMORY_LIMITS_V1:
            raise TaskwiseConfigError("taskwise protocol memory limits are not frozen")
        for value, field_name in (
            (self.run_name, "run_name"),
            (self.codex_cli_version, "codex_cli_version"),
            (self.prompt_renderer_id, "prompt_renderer_id"),
            (self.parser_id, "parser_id"),
            (self.evaluator_id, "evaluator_id"),
        ):
            if type(value) is not str or not value:
                raise TaskwiseConfigError(f"{field_name} must be non-empty text")
        for value, field_name in (
            (self.output_directory, "output_directory"),
            (self.dataset_root, "dataset_root"),
            (self.dataset_manifest, "dataset_manifest"),
            (self.task_manifest, "task_manifest"),
            (self.private_task_manifest, "private_task_manifest"),
        ):
            _clean_relative_path(value, field_name)
        if (
            self.source_commit != SOURCE_COMMIT_PLACEHOLDER
            and _GIT_COMMIT.fullmatch(self.source_commit) is None
        ):
            raise TaskwiseConfigError("source_commit must be a commit or freeze placeholder")

    @property
    def source_commit_is_explicit(self) -> bool:
        """Whether the template uses an explicit commit rather than clean HEAD."""

        return _GIT_COMMIT.fullmatch(self.source_commit) is not None

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "protocol_id": self.protocol_id,
            "protocol_markers": list(self.protocol_markers),
            "execution_mode": self.execution_mode,
            "arm": self.arm,
            "scope": self.scope,
            "run_name": self.run_name,
            "output_directory": self.output_directory,
            "dataset": {
                "repository": self.dataset_repository,
                "revision": self.dataset_revision,
                "root": self.dataset_root,
                "manifest": self.dataset_manifest,
                "task_manifest": self.task_manifest,
                "private_task_manifest": self.private_task_manifest,
            },
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "codex_cli_version": self.codex_cli_version,
            "prompt_renderer_id": self.prompt_renderer_id,
            "parser_id": self.parser_id,
            "evaluator_id": self.evaluator_id,
            "attempts_per_task": self.attempts_per_task,
            "inter_round_slots": self.inter_round_slots,
            "evolution_updates_per_task": self.evolution_updates_per_task,
            "carry_memory_across_tasks": self.carry_memory_across_tasks,
            "fixed_round_budget": self.fixed_round_budget,
            "stop_when_correct": self.stop_when_correct,
            "final_score_round": self.final_score_round,
            "memory_limits": self.memory_limits.to_payload(),
            "evolution": {
                "enabled": self.evolution.enabled,
                "target": self.evolution.target,
                "method_id": self.evolution.method_id,
            },
            "executor": {
                "backend": self.executor.backend,
                "harness": self.executor.harness,
                "timeout_seconds": self.executor.timeout_seconds,
                "concurrency": self.executor.concurrency,
                "infrastructure_retries_before_completion": (
                    self.executor.infrastructure_retries_before_completion
                ),
                "tools_enabled": self.executor.tools_enabled,
                "mcp_enabled": self.executor.mcp_enabled,
                "web_enabled": self.executor.web_enabled,
                "network_enabled": self.executor.network_enabled,
                "subagents_enabled": self.executor.subagents_enabled,
            },
            "source_commit": self.source_commit,
        }

    def config_sha256(self) -> str:
        return hashlib.sha256(canonical_taskwise_config_bytes(self.to_payload())).hexdigest()


def canonical_taskwise_config_bytes(payload: dict[str, object]) -> bytes:
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


def load_taskwise_config_v1(path: Path) -> TaskwiseExperimentConfigV1:
    """Load one exact online/control YAML configuration."""

    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise TaskwiseConfigError("failed to read taskwise config") from exc
    root = _closed_mapping(
        loaded,
        keys=frozenset(
            {
                "schema_version",
                "protocol_id",
                "protocol_markers",
                "execution_mode",
                "arm",
                "scope",
                "run_name",
                "output_directory",
                "dataset",
                "model",
                "reasoning_effort",
                "codex_cli_version",
                "prompt_renderer_id",
                "parser_id",
                "evaluator_id",
                "attempts_per_task",
                "inter_round_slots",
                "evolution_updates_per_task",
                "carry_memory_across_tasks",
                "fixed_round_budget",
                "stop_when_correct",
                "final_score_round",
                "memory_limits",
                "evolution",
                "executor",
                "source_commit",
            }
        ),
        field_name="config",
    )
    dataset = _closed_mapping(
        root["dataset"],
        keys=frozenset(
            {
                "repository",
                "revision",
                "root",
                "manifest",
                "task_manifest",
                "private_task_manifest",
            }
        ),
        field_name="dataset",
    )
    evolution = _closed_mapping(
        root["evolution"],
        keys=frozenset({"enabled", "target", "method_id"}),
        field_name="evolution",
    )
    memory_limits = _closed_mapping(
        root["memory_limits"],
        keys=frozenset(
            {
                "max_utf8_bytes",
                "max_estimated_tokens",
                "max_items_per_section",
                "required_sections",
                "token_estimator_id",
                "parser_id",
            }
        ),
        field_name="memory_limits",
    )
    required_sections = memory_limits["required_sections"]
    if not isinstance(required_sections, list) or any(
        type(section) is not str for section in required_sections
    ):
        raise TaskwiseConfigError("memory_limits.required_sections must be strings")
    executor = _closed_mapping(
        root["executor"],
        keys=frozenset(
            {
                "backend",
                "harness",
                "timeout_seconds",
                "concurrency",
                "infrastructure_retries_before_completion",
                "tools_enabled",
                "mcp_enabled",
                "web_enabled",
                "network_enabled",
                "subagents_enabled",
            }
        ),
        field_name="executor",
    )
    markers = root["protocol_markers"]
    if not isinstance(markers, list) or any(type(marker) is not str for marker in markers):
        raise TaskwiseConfigError("protocol_markers must be a list of strings")
    return TaskwiseExperimentConfigV1(
        schema_version=root["schema_version"],
        protocol_id=root["protocol_id"],
        protocol_markers=tuple(markers),
        execution_mode=root["execution_mode"],
        arm=root["arm"],
        scope=root["scope"],
        run_name=root["run_name"],
        output_directory=root["output_directory"],
        dataset_repository=dataset["repository"],
        dataset_revision=dataset["revision"],
        dataset_root=dataset["root"],
        dataset_manifest=dataset["manifest"],
        task_manifest=dataset["task_manifest"],
        private_task_manifest=dataset["private_task_manifest"],
        model=root["model"],
        reasoning_effort=root["reasoning_effort"],
        codex_cli_version=root["codex_cli_version"],
        prompt_renderer_id=root["prompt_renderer_id"],
        parser_id=root["parser_id"],
        evaluator_id=root["evaluator_id"],
        attempts_per_task=root["attempts_per_task"],
        inter_round_slots=root["inter_round_slots"],
        evolution_updates_per_task=root["evolution_updates_per_task"],
        carry_memory_across_tasks=root["carry_memory_across_tasks"],
        fixed_round_budget=root["fixed_round_budget"],
        stop_when_correct=root["stop_when_correct"],
        final_score_round=root["final_score_round"],
        memory_limits=TaskwiseMemoryLimitsV1(
            max_utf8_bytes=memory_limits["max_utf8_bytes"],
            max_estimated_tokens=memory_limits["max_estimated_tokens"],
            max_items_per_section=memory_limits["max_items_per_section"],
            required_sections=tuple(required_sections),
            token_estimator_id=memory_limits["token_estimator_id"],
            parser_id=memory_limits["parser_id"],
        ),
        evolution=TaskwiseEvolutionPolicyV1(**evolution),
        executor=TaskwiseExecutorPolicyV1(**executor),
        source_commit=root["source_commit"],
    )


def taskwise_arm_parity_findings(
    control: TaskwiseExperimentConfigV1,
    online: TaskwiseExperimentConfigV1,
) -> tuple[str, ...]:
    """Allow only the registered online-evolution treatment differences."""

    if control.arm != "control" or online.arm != "online":
        return ("ARM_IDENTITY_INVALID",)
    if control.scope != online.scope:
        return ("TASK_SCOPE_MISMATCH",)
    left = control.to_payload()
    right = online.to_payload()
    for payload in (left, right):
        payload.pop("protocol_id")
        payload.pop("arm")
        payload.pop("run_name")
        payload.pop("output_directory")
        payload.pop("evolution_updates_per_task")
        payload.pop("carry_memory_across_tasks")
        payload.pop("evolution")
    return () if left == right else ("ARM_PARITY_MISMATCH",)


__all__ = [
    "CONFIG_SCHEMA_V1",
    "CONTROL_PROTOCOL_ID",
    "DATASET_REPOSITORY",
    "DATASET_REVISION",
    "EXECUTION_MODE",
    "FULL_STREAM_COUNT",
    "FULL_STREAM_SCOPES",
    "MODEL",
    "ONLINE_PROTOCOL_ID",
    "PILOT500_STREAM_COUNT",
    "PILOT500_STREAM_SCOPES",
    "PROTOCOL_MARKERS",
    "REASONING_EFFORT",
    "SOURCE_COMMIT_PLACEHOLDER",
    "TASKWISE_MEMORY_LIMITS_V1",
    "TASKWISE_MEMORY_MAX_ESTIMATED_TOKENS",
    "TASKWISE_MEMORY_MAX_ITEMS_PER_SECTION",
    "TASKWISE_MEMORY_MAX_UTF8_BYTES",
    "TASKWISE_MEMORY_PARSER_ID",
    "TASKWISE_MEMORY_REQUIRED_SECTIONS",
    "TASKWISE_MEMORY_TOKEN_ESTIMATOR_ID",
    "TaskwiseConfigError",
    "TaskwiseEvolutionPolicyV1",
    "TaskwiseExecutorPolicyV1",
    "TaskwiseExperimentConfigV1",
    "TaskwiseMemoryLimitsV1",
    "canonical_taskwise_config_bytes",
    "load_taskwise_config_v1",
    "taskwise_arm_parity_findings",
]
