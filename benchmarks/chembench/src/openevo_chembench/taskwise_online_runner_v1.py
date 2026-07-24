"""Stateful runner for the explicitly non-standard taskwise online protocol.

This auxiliary protocol is deliberately separate from the frozen-generalization
runner.  It executes three fresh sessions per item.  The control arm never
updates or injects memory; the online arm calls an injected Core update port
after rounds 0 and 1 and uses only Core-resolved text memory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol

from openevo_chembench.chembench4k_evaluation import (
    ChemBench4KParseStatus,
    ChemBench4KPrivateEvaluator,
    PrivateChemBench4KEvaluation,
)
from openevo_chembench.chembench4k_models import (
    PrivateChemBench4KTask,
    RenderedChemBench4KPrompt,
)
from openevo_chembench.frozen_runtime_v2 import (
    CoreResolvedTextMemoryV2,
    FrozenAgentRequestV2,
)
from openevo_chembench.models import RawAttempt
from openevo_chembench.meeting722_contract_v1 import (
    CONTRACT_VIOLATION as MEETING_722_CONTRACT_VIOLATION,
    issue_meeting722_taskwise_contract_v1,
)
from openevo_chembench.taskwise_feedback_v1 import (
    TaskwiseSafeEvolutionSignalV1,
    safe_signal_from_private_evaluation,
    taskwise_safe_signal_from_payload,
)
from openevo_chembench.taskwise_config_v1 import (
    TASKWISE_MEMORY_LIMITS_V1,
    TaskwiseMemoryLimitsV1,
)
from openevo_chembench.taskwise_context_binding_v1 import (
    CONTEXT_BINDING_VIOLATION,
    TaskwiseContextBindingReceiptV1,
    TaskwiseSessionContextBindingV1,
    taskwise_context_binding_receipt_from_public_dict,
)
from openevo_chembench.taskwise_reporting_v1 import (
    PROTOCOL_LABELS,
    PrivateTaskwiseRoundResultV1,
)
from openevo_chembench.taskwise_trajectory_v1 import (
    TaskwiseTrajectoryV1,
    ordered_safe_feedback_digest,
    ordered_taskwise_trajectory_digest,
)


_SHA256 = re.compile(r"[0-9a-f]{64}")
_RUNTIME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}")
_STREAM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,127}")
_ARMS = ("control", "online")
_ROUNDS_PER_TASK = 3
_UPDATES_PER_ONLINE_TASK = 2
_ZERO_SHA256 = "0" * 64
_DIAGNOSTIC_RECEIPT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
ONLINE_PROTOCOL_ID = "taskwise_online_evolution_v1"
CONTROL_PROTOCOL_ID = "repeated_session_control_v1"
TASKWISE_EXECUTOR_FAILURE_CODES_V1 = frozenset(
    {
        "EXECUTOR_PREFLIGHT_FAILED",
        "EXECUTOR_AUTH_MATERIALIZATION_FAILED",
        "EXECUTOR_PROCESS_SPAWN_FAILED",
        "EXECUTOR_CODEX_STARTUP_FAILED",
        "EXECUTOR_MODEL_TRANSPORT_FAILED",
        "EXECUTOR_TIMEOUT",
        "EXECUTOR_NONZERO_EXIT",
        "EXECUTOR_EVENT_STREAM_INVALID",
        "EXECUTOR_SECURITY_TOOL_USE_VIOLATION",
        "EXECUTOR_OUTPUT_MISSING",
        "EXECUTOR_OUTPUT_INVALID",
        "EXECUTOR_PRIVATE_DIAGNOSTICS_FAILED",
        "EXECUTOR_CLEANUP_FAILED",
        "EXECUTOR_POST_ATTESTATION_FAILED",
        "EXECUTOR_INTERNAL_ERROR",
    }
)
TASKWISE_EXECUTOR_STAGES_V1 = frozenset(
    {
        "PRE_INVOCATION_ATTESTATION",
        "INVOCATION_ROOT_CREATION",
        "AUTH_MATERIALIZATION",
        "CODEX_CONFIG_GENERATION",
        "PROCESS_SPAWN",
        "CODEX_STARTUP",
        "MODEL_TRANSPORT",
        "EVENT_STREAM_PARSE",
        "OUTPUT_LAST_MESSAGE_READ",
        "COMPLETION_VALIDATION",
        "PRIVATE_EVENT_PERSISTENCE",
        "PROCESS_GROUP_SHUTDOWN",
        "CLEANUP",
        "POST_INVOCATION_ATTESTATION",
        "INTERNAL",
    }
)
_TASKWISE_SECURITY_EVENT_CATEGORIES = frozenset(
    {
        "app",
        "browser",
        "command_execution",
        "external_tool",
        "file_read",
        "file_write",
        "mcp",
        "network",
        "plugin",
        "shell",
        "subagent",
        "unknown_tool",
        "web",
    }
)
_TASKWISE_INTERNAL_FAILURE_CODES = frozenset(
    {
        CONTEXT_BINDING_VIOLATION,
        MEETING_722_CONTRACT_VIOLATION,
        "TASKWISE_EVOLUTION_UPDATE_FAILED",
    }
)
_TASKWISE_FAILURE_CODES = TASKWISE_EXECUTOR_FAILURE_CODES_V1 | _TASKWISE_INTERNAL_FAILURE_CODES


class TaskwiseEpisodeStateV1(str, Enum):
    """The seven allowed states for one online episode."""

    TASK_OPENED = "TASK_OPENED"
    ROUND_0_COMPLETED = "ROUND_0_COMPLETED"
    UPDATE_1_COMPLETED = "UPDATE_1_COMPLETED"
    ROUND_1_COMPLETED = "ROUND_1_COMPLETED"
    UPDATE_2_COMPLETED = "UPDATE_2_COMPLETED"
    ROUND_2_COMPLETED = "ROUND_2_COMPLETED"
    TASK_FINALIZED = "TASK_FINALIZED"


class TaskwiseRunStatusV1(str, Enum):
    INITIALIZED = "INITIALIZED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    TASKWISE_EVOLUTION_UPDATE_FAILED = "TASKWISE_EVOLUTION_UPDATE_FAILED"
    TASKWISE_CONTEXT_BINDING_VIOLATION = "TASKWISE_CONTEXT_BINDING_VIOLATION"
    MEETING_722_TASKWISE_CONTRACT_VIOLATION = "MEETING_722_TASKWISE_CONTRACT_VIOLATION"
    SECURITY_TOOL_USE_VIOLATION = "SECURITY_TOOL_USE_VIOLATION"


@dataclass(frozen=True, slots=True)
class TaskwiseRunConfigV1:
    """Frozen execution budget and public runtime identity."""

    arm: Literal["control", "online"]
    run_id: str
    output_directory: Path
    protocol_sha256: str
    source_commit: str
    dataset_sha256: str
    task_manifest_sha256: str
    model_identity_sha256: str
    executor_policy_sha256: str
    stream_id: str | None = None
    memory_limits: TaskwiseMemoryLimitsV1 = TASKWISE_MEMORY_LIMITS_V1
    model: Literal["gpt-5.5"] = "gpt-5.5"
    reasoning_effort: Literal["medium"] = "medium"
    rounds_per_task: Literal[3] = 3
    updates_per_online_task: Literal[2] = 2

    def __post_init__(self) -> None:
        if self.arm not in _ARMS:
            raise ValueError("arm must be control or online")
        if type(self.run_id) is not str or _RUNTIME_ID.fullmatch(self.run_id) is None:
            raise ValueError("run_id must be a bounded runtime identifier")
        if not isinstance(self.output_directory, Path) or not self.output_directory.is_absolute():
            raise ValueError("output_directory must be an absolute Path")
        for field_name in (
            "protocol_sha256",
            "dataset_sha256",
            "task_manifest_sha256",
            "model_identity_sha256",
            "executor_policy_sha256",
        ):
            _require_sha256(getattr(self, field_name), field_name)
        if (
            type(self.source_commit) is not str
            or re.fullmatch(r"[0-9a-f]{40}", self.source_commit) is None
        ):
            raise ValueError("source_commit must be a lowercase Git commit")
        if (
            type(self.contract_stream_id) is not str
            or _STREAM_ID.fullmatch(self.contract_stream_id) is None
        ):
            raise ValueError("stream_id must be a bounded runtime identifier")
        if self.model != "gpt-5.5" or self.reasoning_effort != "medium":
            raise ValueError("taskwise v1 freezes gpt-5.5 with medium reasoning")
        if self.memory_limits != TASKWISE_MEMORY_LIMITS_V1:
            raise ValueError("taskwise v1 memory limits are not frozen")
        if (
            self.rounds_per_task != _ROUNDS_PER_TASK
            or self.updates_per_online_task != _UPDATES_PER_ONLINE_TASK
        ):
            raise ValueError("taskwise v1 freezes three rounds and two online updates")

    def digest(self) -> str:
        return _sha256_json(
            {
                "protocol_id": (
                    ONLINE_PROTOCOL_ID if self.arm == "online" else CONTROL_PROTOCOL_ID
                ),
                "arm": self.arm,
                "run_id": self.run_id,
                "output_directory": str(self.output_directory),
                "protocol_sha256": self.protocol_sha256,
                "source_commit": self.source_commit,
                "dataset_sha256": self.dataset_sha256,
                "task_manifest_sha256": self.task_manifest_sha256,
                "model_identity_sha256": self.model_identity_sha256,
                "executor_policy_sha256": self.executor_policy_sha256,
                "stream_id": self.contract_stream_id,
                "memory_limits": self.memory_limits.to_payload(),
                "memory_limits_sha256": self.memory_limits.digest,
                "model": self.model,
                "reasoning_effort": self.reasoning_effort,
                "rounds_per_task": self.rounds_per_task,
                "updates_per_online_task": self.updates_per_online_task,
            }
        )

    @property
    def contract_stream_id(self) -> str:
        return self.output_directory.parent.name if self.stream_id is None else self.stream_id


@dataclass(frozen=True, slots=True, repr=False)
class TaskwiseEpisodeV1:
    """One private evaluation item bound to its already-rendered public prompt."""

    task: PrivateChemBench4KTask
    prompt: RenderedChemBench4KPrompt

    def __repr__(self) -> str:
        return "TaskwiseEpisodeV1(<redacted>)"

    __str__ = __repr__

    def __post_init__(self) -> None:
        if type(self.task) is not PrivateChemBench4KTask:
            raise TypeError("task must be exact PrivateChemBench4KTask")
        if type(self.prompt) is not RenderedChemBench4KPrompt:
            raise TypeError("prompt must be exact RenderedChemBench4KPrompt")
        if self.task.source_split != "test":
            raise ValueError("taskwise online evaluation requires test items")
        if (
            self.task.uid != self.prompt.uid
            or self.task.category != self.prompt.category
            or self.task.dataset_revision != self.prompt.dataset_revision
        ):
            raise ValueError("private task and public prompt identity differ")


def build_taskwise_run_binding_v1(
    config: TaskwiseRunConfigV1,
    episodes: tuple[TaskwiseEpisodeV1, ...],
) -> dict[str, object]:
    """Build the immutable binding reused by launch, resume, and comparison."""

    if type(config) is not TaskwiseRunConfigV1:
        raise TypeError("config must be exact TaskwiseRunConfigV1")
    if (
        not isinstance(episodes, tuple)
        or not episodes
        or any(type(item) is not TaskwiseEpisodeV1 for item in episodes)
    ):
        raise ValueError("episodes must be a non-empty exact tuple")
    tasks = [
        {
            "ordinal": ordinal,
            "uid": episode.task.uid,
            "category": episode.task.category,
            "prompt_sha256": hashlib.sha256(episode.prompt.text.encode("utf-8")).hexdigest(),
        }
        for ordinal, episode in enumerate(episodes)
    ]
    fields: dict[str, object] = {
        "protocol_id": _protocol_id(config.arm),
        "protocol_sha256": config.protocol_sha256,
        "source_commit": config.source_commit,
        "dataset_sha256": config.dataset_sha256,
        "task_manifest_sha256": config.task_manifest_sha256,
        "ordered_task_sha256": _sha256_json(tasks),
        "model": config.model,
        "reasoning_effort": config.reasoning_effort,
        "model_identity_sha256": config.model_identity_sha256,
        "executor_policy_sha256": config.executor_policy_sha256,
        "memory_limits": config.memory_limits.to_payload(),
        "memory_limits_sha256": config.memory_limits.digest,
        "config_sha256": config.digest(),
        "planned_tasks": len(tasks),
    }
    fields["binding_sha256"] = _sha256_json(fields)
    return fields


@dataclass(frozen=True, slots=True)
class TaskwiseAgentRequestV1:
    """Public prompt plus optional Core memory and non-prompt runtime metadata."""

    rendered_public_prompt: str
    resolved_text_memory: CoreResolvedTextMemoryV2 | None
    session_id: str
    arm: Literal["control", "online"]
    task_ordinal: int
    round_index: Literal[0, 1, 2]
    run_id: str | None = None
    task_uid: str | None = None

    def __post_init__(self) -> None:
        if type(self.rendered_public_prompt) is not str or not self.rendered_public_prompt:
            raise ValueError("rendered_public_prompt must be non-empty")
        if (
            self.resolved_text_memory is not None
            and type(self.resolved_text_memory) is not CoreResolvedTextMemoryV2
        ):
            raise TypeError("memory must be exact CoreResolvedTextMemoryV2 or None")
        if type(self.session_id) is not str or _RUNTIME_ID.fullmatch(self.session_id) is None:
            raise ValueError("session_id must be a bounded runtime identifier")
        if self.arm not in _ARMS:
            raise ValueError("arm must be control or online")
        if (
            isinstance(self.task_ordinal, bool)
            or not isinstance(self.task_ordinal, int)
            or self.task_ordinal < 0
        ):
            raise ValueError("task_ordinal must be non-negative")
        if self.round_index not in (0, 1, 2):
            raise ValueError("round_index must be 0, 1, or 2")
        if self.run_id is not None and (
            type(self.run_id) is not str or _RUNTIME_ID.fullmatch(self.run_id) is None
        ):
            raise ValueError("run_id must be a bounded runtime identifier or None")
        if self.task_uid is not None and (
            type(self.task_uid) is not str or _SHA256.fullmatch(self.task_uid) is None
        ):
            raise ValueError("task_uid must be a lowercase SHA-256 or None")
        if self.arm == "control" and self.resolved_text_memory is not None:
            raise ValueError("control request must not contain memory")

    def to_frozen_agent_request(self) -> FrozenAgentRequestV2:
        """Project out session/round metadata before prompt construction."""

        return FrozenAgentRequestV2(
            rendered_public_prompt=self.rendered_public_prompt,
            resolved_text_memory=self.resolved_text_memory,
        )


class TaskwiseExecutorV1(Protocol):
    def execute_taskwise(self, request: TaskwiseAgentRequestV1) -> RawAttempt: ...

    def consume_taskwise_context_receipt(
        self,
        session_id: str,
    ) -> TaskwiseContextBindingReceiptV1: ...


@dataclass(frozen=True, slots=True)
class TaskwiseRunnerCoreUpdateRequestV1:
    """Complete current-task prefix supplied to an injected Core bridge."""

    task_uid: str
    task_index: int
    episode_id: str
    update_index: Literal[1, 2]
    source_round_index: Literal[0, 1]
    trajectories: tuple[TaskwiseTrajectoryV1, ...]
    safe_signals: tuple[TaskwiseSafeEvolutionSignalV1, ...]
    prior_resolved_text_memory: CoreResolvedTextMemoryV2 | None

    def __post_init__(self) -> None:
        if type(self.task_uid) is not str or _SHA256.fullmatch(self.task_uid) is None:
            raise ValueError("task_uid must be a lowercase SHA-256")
        if (
            isinstance(self.task_index, bool)
            or not isinstance(self.task_index, int)
            or self.task_index < 0
        ):
            raise ValueError("task_index must be non-negative")
        if type(self.episode_id) is not str or _RUNTIME_ID.fullmatch(self.episode_id) is None:
            raise ValueError("episode_id must be a bounded runtime identifier")
        if self.update_index not in (1, 2):
            raise ValueError("update_index must be 1 or 2")
        if self.source_round_index != self.update_index - 1:
            raise ValueError("update index must immediately follow its source round")
        if (
            not isinstance(self.trajectories, tuple)
            or len(self.trajectories) != self.update_index
            or any(type(item) is not TaskwiseTrajectoryV1 for item in self.trajectories)
            or tuple(item.round_index for item in self.trajectories)
            != tuple(range(self.update_index))
            or any(
                item.task_uid != self.task_uid or item.task_index != self.task_index
                for item in self.trajectories
            )
        ):
            raise ValueError("trajectories must be the complete ordered current-task prefix")
        if not isinstance(self.safe_signals, tuple) or self.safe_signals != tuple(
            trajectory.safe_feedback for trajectory in self.trajectories
        ):
            raise ValueError("safe_signals must exactly match the trajectory prefix")
        if (
            self.prior_resolved_text_memory is not None
            and type(self.prior_resolved_text_memory) is not CoreResolvedTextMemoryV2
        ):
            raise TypeError("prior memory must be Core-resolved or None")


@dataclass(frozen=True, slots=True)
class CoreMemoryReferenceV1:
    core_artifact_id: str
    artifact_payload_sha256: str
    context_resolution_digest: str
    resolved_memory_sha256: str

    @classmethod
    def from_memory(cls, memory: CoreResolvedTextMemoryV2) -> CoreMemoryReferenceV1:
        if type(memory) is not CoreResolvedTextMemoryV2:
            raise TypeError("memory must be exact CoreResolvedTextMemoryV2")
        return cls(
            core_artifact_id=memory.core_artifact_id,
            artifact_payload_sha256=memory.artifact_payload_sha256,
            context_resolution_digest=memory.context_resolution_digest,
            resolved_memory_sha256=memory.resolved_memory_sha256,
        )

    def __post_init__(self) -> None:
        if (
            type(self.core_artifact_id) is not str
            or not self.core_artifact_id
            or "/" in self.core_artifact_id
            or "\\" in self.core_artifact_id
        ):
            raise ValueError("core_artifact_id must be a non-path identifier")
        for field_name in (
            "artifact_payload_sha256",
            "context_resolution_digest",
            "resolved_memory_sha256",
        ):
            _require_sha256(getattr(self, field_name), field_name)

    def to_dict(self) -> dict[str, str]:
        return {
            "core_artifact_id": self.core_artifact_id,
            "artifact_payload_sha256": self.artifact_payload_sha256,
            "context_resolution_digest": self.context_resolution_digest,
            "resolved_memory_sha256": self.resolved_memory_sha256,
        }

    @classmethod
    def from_dict(cls, value: object) -> CoreMemoryReferenceV1:
        if type(value) is not dict or set(value) != {
            "core_artifact_id",
            "artifact_payload_sha256",
            "context_resolution_digest",
            "resolved_memory_sha256",
        }:
            raise ValueError("memory reference schema is invalid")
        return cls(**value)


class TaskwiseCoreUpdatePortV1(Protocol):
    def update_text_memory(
        self,
        request: TaskwiseRunnerCoreUpdateRequestV1,
    ) -> TaskwiseCoreUpdateOutcomeV1: ...

    def resolve_text_memory(
        self,
        reference: CoreMemoryReferenceV1,
    ) -> CoreResolvedTextMemoryV2: ...


@dataclass(frozen=True, slots=True)
class TaskwiseMemoryPublicMetricsV1:
    """Content-free memory measurements safe for public audit events."""

    memory_limits_sha256: str
    inspection_sha256: str
    token_estimator_id: Literal["ascii_word_or_unicode_codepoint_v1"]
    parser_id: Literal["exact_markdown_sections_v1"]
    utf8_byte_count: int
    estimated_token_count: int
    section_item_counts: tuple[int, ...]
    total_section_items: int
    max_section_items: int

    def __post_init__(self) -> None:
        _require_sha256(self.memory_limits_sha256, "memory_limits_sha256")
        _require_sha256(self.inspection_sha256, "inspection_sha256")
        if (
            self.memory_limits_sha256 != TASKWISE_MEMORY_LIMITS_V1.digest
            or len(self.section_item_counts) != len(TASKWISE_MEMORY_LIMITS_V1.required_sections)
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in (
                    self.utf8_byte_count,
                    self.estimated_token_count,
                    self.total_section_items,
                    self.max_section_items,
                    *self.section_item_counts,
                )
            )
            or sum(self.section_item_counts) != self.total_section_items
            or max(self.section_item_counts, default=0) != self.max_section_items
            or self.token_estimator_id != TASKWISE_MEMORY_LIMITS_V1.token_estimator_id
            or self.parser_id != TASKWISE_MEMORY_LIMITS_V1.parser_id
            or self.utf8_byte_count > TASKWISE_MEMORY_LIMITS_V1.max_utf8_bytes
            or self.estimated_token_count > TASKWISE_MEMORY_LIMITS_V1.max_estimated_tokens
            or self.max_section_items > TASKWISE_MEMORY_LIMITS_V1.max_items_per_section
        ):
            raise ValueError("memory metrics violate the frozen protocol limits")

    def to_dict(self) -> dict[str, object]:
        return {
            "memory_limits_sha256": self.memory_limits_sha256,
            "inspection_sha256": self.inspection_sha256,
            "token_estimator_id": self.token_estimator_id,
            "parser_id": self.parser_id,
            "utf8_byte_count": self.utf8_byte_count,
            "estimated_token_count": self.estimated_token_count,
            "section_item_counts": list(self.section_item_counts),
            "total_section_items": self.total_section_items,
            "max_section_items": self.max_section_items,
        }

    @classmethod
    def from_dict(cls, value: object) -> TaskwiseMemoryPublicMetricsV1:
        if type(value) is not dict or set(value) != {
            "memory_limits_sha256",
            "inspection_sha256",
            "token_estimator_id",
            "parser_id",
            "utf8_byte_count",
            "estimated_token_count",
            "section_item_counts",
            "total_section_items",
            "max_section_items",
        }:
            raise ValueError("memory metrics schema is invalid")
        counts = value["section_item_counts"]
        if not isinstance(counts, list):
            raise ValueError("memory section counts must be a list")
        return cls(
            memory_limits_sha256=value["memory_limits_sha256"],
            inspection_sha256=value["inspection_sha256"],
            token_estimator_id=value["token_estimator_id"],
            parser_id=value["parser_id"],
            utf8_byte_count=value["utf8_byte_count"],
            estimated_token_count=value["estimated_token_count"],
            section_item_counts=tuple(counts),
            total_section_items=value["total_section_items"],
            max_section_items=value["max_section_items"],
        )


@dataclass(frozen=True, slots=True)
class TaskwiseMemoryPublicAggregateV1:
    """Run-level maxima and counts; never contains memory text."""

    memory_limits_sha256: str
    token_estimator_id: Literal["ascii_word_or_unicode_codepoint_v1"]
    approved_artifact_count: int
    max_utf8_byte_count: int
    max_estimated_token_count: int
    max_total_section_items: int
    max_section_items: int

    def __post_init__(self) -> None:
        _require_sha256(self.memory_limits_sha256, "memory_limits_sha256")
        if (
            self.memory_limits_sha256 != TASKWISE_MEMORY_LIMITS_V1.digest
            or self.token_estimator_id != TASKWISE_MEMORY_LIMITS_V1.token_estimator_id
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in (
                    self.approved_artifact_count,
                    self.max_utf8_byte_count,
                    self.max_estimated_token_count,
                    self.max_total_section_items,
                    self.max_section_items,
                )
            )
        ):
            raise ValueError("memory aggregate is invalid")

    @classmethod
    def empty(cls, limits: TaskwiseMemoryLimitsV1) -> TaskwiseMemoryPublicAggregateV1:
        if type(limits) is not TaskwiseMemoryLimitsV1:
            raise TypeError("limits must be exact TaskwiseMemoryLimitsV1")
        return cls(
            memory_limits_sha256=limits.digest,
            token_estimator_id=limits.token_estimator_id,
            approved_artifact_count=0,
            max_utf8_byte_count=0,
            max_estimated_token_count=0,
            max_total_section_items=0,
            max_section_items=0,
        )

    def include(
        self,
        metrics: TaskwiseMemoryPublicMetricsV1,
    ) -> TaskwiseMemoryPublicAggregateV1:
        if (
            type(metrics) is not TaskwiseMemoryPublicMetricsV1
            or metrics.memory_limits_sha256 != self.memory_limits_sha256
        ):
            raise ValueError("memory metrics do not match the aggregate policy")
        return TaskwiseMemoryPublicAggregateV1(
            memory_limits_sha256=self.memory_limits_sha256,
            token_estimator_id=self.token_estimator_id,
            approved_artifact_count=self.approved_artifact_count + 1,
            max_utf8_byte_count=max(
                self.max_utf8_byte_count,
                metrics.utf8_byte_count,
            ),
            max_estimated_token_count=max(
                self.max_estimated_token_count,
                metrics.estimated_token_count,
            ),
            max_total_section_items=max(
                self.max_total_section_items,
                metrics.total_section_items,
            ),
            max_section_items=max(self.max_section_items, metrics.max_section_items),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "memory_limits_sha256": self.memory_limits_sha256,
            "token_estimator_id": self.token_estimator_id,
            "approved_artifact_count": self.approved_artifact_count,
            "max_utf8_byte_count": self.max_utf8_byte_count,
            "max_estimated_token_count": self.max_estimated_token_count,
            "max_total_section_items": self.max_total_section_items,
            "max_section_items": self.max_section_items,
        }

    @classmethod
    def from_dict(cls, value: object) -> TaskwiseMemoryPublicAggregateV1:
        if type(value) is not dict or set(value) != {
            "memory_limits_sha256",
            "token_estimator_id",
            "approved_artifact_count",
            "max_utf8_byte_count",
            "max_estimated_token_count",
            "max_total_section_items",
            "max_section_items",
        }:
            raise ValueError("memory aggregate schema is invalid")
        return cls(**value)


@dataclass(frozen=True, slots=True)
class TaskwiseCoreUpdateOutcomeV1:
    """Core job/artifact/context evidence plus its resolved runtime memory."""

    task_uid: str
    task_index: int
    update_index: Literal[1, 2]
    global_update_ordinal: int
    predecessor_artifact_id: str | None
    predecessor_memory_sha256: str | None
    trajectory_ids: tuple[str, ...]
    trajectory_digest: str
    safe_feedback_digest: str
    core_artifact_id: str
    job_id: str
    job_state: Literal["COMPLETED"]
    core_context_id: str
    validation_receipt_sha256: str
    memory_metrics: TaskwiseMemoryPublicMetricsV1
    resolved_text_memory: CoreResolvedTextMemoryV2

    def __post_init__(self) -> None:
        if type(self.task_uid) is not str or _SHA256.fullmatch(self.task_uid) is None:
            raise ValueError("task_uid must be a lowercase SHA-256")
        if (
            isinstance(self.task_index, bool)
            or not isinstance(self.task_index, int)
            or self.task_index < 0
            or type(self.update_index) is not int
            or self.update_index not in (1, 2)
            or type(self.global_update_ordinal) is not int
            or self.global_update_ordinal != self.task_index * 2 + self.update_index
        ):
            raise ValueError("Core outcome stream position is invalid")
        if (
            not isinstance(self.trajectory_ids, tuple)
            or len(self.trajectory_ids) != self.update_index
            or len(set(self.trajectory_ids)) != self.update_index
            or any(_SHA256.fullmatch(value) is None for value in self.trajectory_ids)
        ):
            raise ValueError("Core outcome trajectory identities are invalid")
        for field_name in (
            "trajectory_digest",
            "safe_feedback_digest",
        ):
            _require_sha256(getattr(self, field_name), field_name)
        if (self.predecessor_artifact_id is None) != (self.predecessor_memory_sha256 is None):
            raise ValueError("Core outcome predecessor identity must be complete")
        if self.global_update_ordinal == 1:
            if self.predecessor_artifact_id is not None:
                raise ValueError("generation-zero update cannot claim a predecessor")
        elif (
            type(self.predecessor_artifact_id) is not str
            or not self.predecessor_artifact_id
            or "/" in self.predecessor_artifact_id
            or "\\" in self.predecessor_artifact_id
        ):
            raise ValueError("noninitial Core outcome requires a safe predecessor id")
        if self.predecessor_memory_sha256 is not None:
            _require_sha256(
                self.predecessor_memory_sha256,
                "predecessor_memory_sha256",
            )
        for field_name in ("job_id", "core_context_id"):
            value = getattr(self, field_name)
            if type(value) is not str or _RUNTIME_ID.fullmatch(value) is None:
                raise ValueError(f"{field_name} must be a bounded Core identifier")
        if self.job_state != "COMPLETED":
            raise ValueError("Core update outcome must be completed")
        _require_sha256(
            self.validation_receipt_sha256,
            "validation_receipt_sha256",
        )
        if type(self.memory_metrics) is not TaskwiseMemoryPublicMetricsV1:
            raise TypeError("memory_metrics must be exact TaskwiseMemoryPublicMetricsV1")
        if type(self.resolved_text_memory) is not CoreResolvedTextMemoryV2:
            raise TypeError("resolved_text_memory must be exact CoreResolvedTextMemoryV2")
        if (
            type(self.core_artifact_id) is not str
            or self.core_artifact_id != self.resolved_text_memory.core_artifact_id
        ):
            raise ValueError("Core outcome artifact identity differs from resolved memory")


class TaskwiseExecutionFailureV1(RuntimeError):
    """Sanitized executor failure with explicit completion/resume semantics."""

    def __init__(
        self,
        code: str,
        *,
        completion_observed: bool,
        diagnostic_receipt: str | None = None,
        event_digest: str | None = None,
        event_counts: dict[str, int] | None = None,
        executor_stage: str | None = None,
    ) -> None:
        if type(code) is not str or code not in _TASKWISE_FAILURE_CODES:
            raise ValueError("failure code is outside the taskwise closed vocabulary")
        if type(completion_observed) is not bool:
            raise TypeError("completion_observed must be boolean")
        if diagnostic_receipt is not None and (
            type(diagnostic_receipt) is not str
            or _DIAGNOSTIC_RECEIPT.fullmatch(diagnostic_receipt) is None
            or "/" in diagnostic_receipt
            or "\\" in diagnostic_receipt
        ):
            raise ValueError("diagnostic_receipt must be a safe basename or None")
        if event_digest is not None and (
            type(event_digest) is not str or _SHA256.fullmatch(event_digest) is None
        ):
            raise ValueError("event_digest must be a lowercase SHA-256 or None")
        if event_counts is None:
            normalized_event_counts: dict[str, int] = {}
        elif type(event_counts) is not dict:
            raise TypeError("event_counts must be an exact dict or None")
        else:
            normalized_event_counts = {}
            for category, count in event_counts.items():
                if category not in _TASKWISE_SECURITY_EVENT_CATEGORIES:
                    raise ValueError("event_counts contains an unknown category")
                if type(count) is not int or count <= 0:
                    raise ValueError("event_counts values must be positive integers")
                normalized_event_counts[category] = count
        if executor_stage is not None and executor_stage not in TASKWISE_EXECUTOR_STAGES_V1:
            raise ValueError("executor_stage is outside the closed vocabulary")
        self.code = code
        self.completion_observed = completion_observed
        self.security_violation = code == "EXECUTOR_SECURITY_TOOL_USE_VIOLATION"
        self.diagnostic_receipt = diagnostic_receipt
        self.event_digest = event_digest
        self.event_counts = dict(sorted(normalized_event_counts.items()))
        self.executor_stage = executor_stage
        # A failed formal taskwise run is terminal even when the executor can
        # prove that no completion was observed.  Replaying it would alter the
        # fixed stream/session treatment and can fork the memory lineage.
        self.resume_allowed = False
        super().__init__(f"taskwise execution failed: error_type={code}")


@dataclass(frozen=True, slots=True)
class TaskwiseRunResultV1:
    status: TaskwiseRunStatusV1
    arm: Literal["control", "online"]
    planned_tasks: int
    completed_tasks: int
    completion_count: int
    update_count: int
    core_job_count: int
    core_artifact_count: int
    context_binding_violation_count: int
    session_attempt_count: int
    output_directory: Path
    resume_allowed: bool
    memory_aggregate: TaskwiseMemoryPublicAggregateV1
    finding_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.memory_aggregate) is not TaskwiseMemoryPublicAggregateV1:
            raise TypeError("memory_aggregate must be exact TaskwiseMemoryPublicAggregateV1")
        for field_name in (
            "completion_count",
            "update_count",
            "core_job_count",
            "core_artifact_count",
            "context_binding_violation_count",
            "session_attempt_count",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if (
            self.core_job_count != self.update_count
            or self.core_artifact_count != self.update_count
            or self.memory_aggregate.approved_artifact_count != self.update_count
        ):
            raise ValueError(
                "every accepted update must bind one Core job, artifact, and aggregate entry"
            )
        if self.arm == "control" and (
            self.update_count
            or self.core_job_count
            or self.core_artifact_count
            or self.memory_aggregate.approved_artifact_count
        ):
            raise ValueError("control result must contain zero Core evolution evidence")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema_version": "taskwise_online_run_result_v1",
            "protocol_id": _protocol_id(self.arm),
            "protocol_labels": list(PROTOCOL_LABELS),
            "standard_chembench4k_score_claimed": False,
            "status": self.status.value,
            "arm": self.arm,
            "planned_tasks": self.planned_tasks,
            "completed_tasks": self.completed_tasks,
            "completion_count": self.completion_count,
            "update_count": self.update_count,
            "core_job_count": self.core_job_count,
            "core_artifact_count": self.core_artifact_count,
            "context_binding_violation_count": (self.context_binding_violation_count),
            "session_attempt_count": self.session_attempt_count,
            "resume_allowed": self.resume_allowed,
            "memory_aggregate": self.memory_aggregate.to_dict(),
            "finding_codes": list(self.finding_codes),
        }


class TaskwiseOnlineRunnerV1:
    """Execute the fixed three-round control or online arm."""

    def __init__(
        self,
        *,
        config: TaskwiseRunConfigV1,
        episodes: tuple[TaskwiseEpisodeV1, ...],
        executor: TaskwiseExecutorV1,
        core_update_port: TaskwiseCoreUpdatePortV1 | None = None,
        resume: bool = False,
        session_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if type(config) is not TaskwiseRunConfigV1:
            raise TypeError("config must be exact TaskwiseRunConfigV1")
        if (
            not isinstance(episodes, tuple)
            or not episodes
            or any(type(item) is not TaskwiseEpisodeV1 for item in episodes)
        ):
            raise ValueError("episodes must be a non-empty tuple of TaskwiseEpisodeV1")
        if len({item.task.uid for item in episodes}) != len(episodes):
            raise ValueError("episode task UIDs must be unique")
        if any(item.task.dataset_sha256 != config.dataset_sha256 for item in episodes):
            raise ValueError("episode dataset hash does not match the run binding")
        if not callable(getattr(executor, "execute_taskwise", None)) or not callable(
            getattr(executor, "consume_taskwise_context_receipt", None)
        ):
            raise TypeError("executor must implement taskwise execution and context receipts")
        if config.arm == "control" and core_update_port is not None:
            raise ValueError("control arm must not receive a Core update port")
        if config.arm == "online" and (
            not callable(getattr(core_update_port, "update_text_memory", None))
            or not callable(getattr(core_update_port, "resolve_text_memory", None))
        ):
            raise TypeError("online arm requires the injected Core update/resolve port")
        if type(resume) is not bool:
            raise TypeError("resume must be boolean")
        if session_id_factory is not None and not callable(session_id_factory):
            raise TypeError("session_id_factory must be callable")

        self._config = config
        self._episodes = episodes
        self._executor = executor
        self._core = core_update_port
        self._resume = resume
        self._session_id_factory = session_id_factory or (lambda: f"session_{uuid.uuid4().hex}")
        self._evaluator = ChemBench4KPrivateEvaluator()
        self._binding = self._build_binding()
        self._started = False
        self._state: dict[str, Any] = {}
        self._public_path = config.output_directory / "public" / "events.jsonl"
        self._private_path = config.output_directory / "private" / "evaluations.jsonl"
        self._private_failures_path = config.output_directory / "private" / "failures.jsonl"
        self._state_path = config.output_directory / "run_state.json"
        self._meeting_contract_path = (
            config.output_directory / "public" / "meeting722_taskwise_contract_v1.json"
        )

    def _build_binding(self) -> dict[str, object]:
        return build_taskwise_run_binding_v1(self._config, self._episodes)

    def run(self) -> TaskwiseRunResultV1:
        if self._started:
            raise RuntimeError("taskwise runner instances are single-use")
        if self._build_binding() != self._binding:
            raise RuntimeError("taskwise run binding changed before launch")
        self._started = True
        if self._resume:
            self._load_resume_state()
        else:
            self._initialize_new_run()

        self._state["status"] = TaskwiseRunStatusV1.RUNNING.value
        self._state["resume_allowed"] = False
        self._state["resume_count"] += int(self._resume)
        self._state["failure"] = None
        self._write_state()
        try:
            self._drive()
        except TaskwiseExecutionFailureV1 as exc:
            return self._record_execution_failure(exc)
        except Exception:
            self._record_fail_closed_internal_error()
            raise
        return self._result()

    def _drive(self) -> None:
        while self._state["task_ordinal"] < len(self._episodes):
            ordinal = self._state["task_ordinal"]
            state = TaskwiseEpisodeStateV1(self._state["episode_state"])
            episode = self._episodes[ordinal]
            episode_runtime_id = _episode_runtime_id(self._config.run_id, ordinal)
            if state is TaskwiseEpisodeStateV1.TASK_OPENED:
                memory = self._restore_memory(self._state["carry_memory"])
                self._execute_round(episode, ordinal, 0, memory)
                self._transition(TaskwiseEpisodeStateV1.ROUND_0_COMPLETED)
            elif state is TaskwiseEpisodeStateV1.ROUND_0_COMPLETED:
                if self._config.arm == "online":
                    self._perform_update(episode_runtime_id, ordinal, update_index=1)
                    self._transition(TaskwiseEpisodeStateV1.UPDATE_1_COMPLETED)
                else:
                    self._execute_round(episode, ordinal, 1, None)
                    self._transition(TaskwiseEpisodeStateV1.ROUND_1_COMPLETED)
            elif state is TaskwiseEpisodeStateV1.UPDATE_1_COMPLETED:
                memory = self._restore_memory(self._state["active_memory"])
                self._execute_round(episode, ordinal, 1, memory)
                self._transition(TaskwiseEpisodeStateV1.ROUND_1_COMPLETED)
            elif state is TaskwiseEpisodeStateV1.ROUND_1_COMPLETED:
                if self._config.arm == "online":
                    self._perform_update(episode_runtime_id, ordinal, update_index=2)
                    self._transition(TaskwiseEpisodeStateV1.UPDATE_2_COMPLETED)
                else:
                    self._execute_round(episode, ordinal, 2, None)
                    self._transition(TaskwiseEpisodeStateV1.ROUND_2_COMPLETED)
            elif state is TaskwiseEpisodeStateV1.UPDATE_2_COMPLETED:
                memory = self._restore_memory(self._state["active_memory"])
                self._execute_round(episode, ordinal, 2, memory)
                self._transition(TaskwiseEpisodeStateV1.ROUND_2_COMPLETED)
            elif state is TaskwiseEpisodeStateV1.ROUND_2_COMPLETED:
                if self._config.arm == "online":
                    self._state["carry_memory"] = self._state["active_memory"]
                self._state["active_memory"] = None
                self._transition(TaskwiseEpisodeStateV1.TASK_FINALIZED)
                self._verify_meeting722_contract_prefix(ordinal + 1)
            elif state is TaskwiseEpisodeStateV1.TASK_FINALIZED:
                verified_tasks = self._state["meeting722_contract_verified_tasks"]
                if verified_tasks == ordinal:
                    self._verify_meeting722_contract_prefix(ordinal + 1)
                elif verified_tasks != ordinal + 1:
                    raise TaskwiseExecutionFailureV1(
                        MEETING_722_CONTRACT_VIOLATION,
                        completion_observed=True,
                    )
                self._state["completed_tasks"] += 1
                self._state["task_ordinal"] += 1
                if self._state["task_ordinal"] < len(self._episodes):
                    self._state["episode_state"] = TaskwiseEpisodeStateV1.TASK_OPENED.value
                    self._write_state()
            else:  # pragma: no cover - Enum exhaustiveness
                raise RuntimeError("unreachable taskwise episode state")

        expected_completions = len(self._episodes) * _ROUNDS_PER_TASK
        expected_updates = (
            len(self._episodes) * _UPDATES_PER_ONLINE_TASK if self._config.arm == "online" else 0
        )
        if (
            self._state["completion_count"] != expected_completions
            or self._state["update_count"] != expected_updates
            or self._state["core_job_count"] != expected_updates
            or self._state["core_artifact_count"] != expected_updates
            or len(self._state["registered_core_job_ids"]) != expected_updates
            or len(self._state["registered_core_artifact_ids"]) != expected_updates
            or self._state["context_binding_violation_count"] != 0
            or self._state["meeting722_contract_verified_tasks"] != len(self._episodes)
            or self._state["meeting722_contract_receipt_sha256"] is None
        ):
            raise TaskwiseExecutionFailureV1(
                MEETING_722_CONTRACT_VIOLATION,
                completion_observed=True,
            )
        self._state["status"] = TaskwiseRunStatusV1.COMPLETED.value
        self._state["resume_allowed"] = False
        self._state["pending_invocation"] = None
        self._write_state()

    def _execute_round(
        self,
        episode: TaskwiseEpisodeV1,
        ordinal: int,
        round_index: int,
        memory: CoreResolvedTextMemoryV2 | None,
    ) -> None:
        if self._config.arm == "control" and memory is not None:
            raise RuntimeError("control arm memory invariant failed")
        session_id = self._new_session_id()
        self._state["issued_session_ids"].append(session_id)
        self._state["session_attempt_count"] += 1
        self._state["pending_invocation"] = {
            "task_ordinal": ordinal,
            "round_index": round_index,
            "session_id": session_id,
            "completion_observed": False,
        }
        self._write_state()
        request = TaskwiseAgentRequestV1(
            rendered_public_prompt=episode.prompt.text,
            resolved_text_memory=memory,
            session_id=session_id,
            arm=self._config.arm,
            task_ordinal=ordinal,
            round_index=round_index,
            run_id=self._config.run_id,
            task_uid=episode.task.uid,
        )
        try:
            attempt = self._executor.execute_taskwise(request)
        except Exception as exc:
            raise _taskwise_failure_from_executor(exc) from exc
        if type(attempt) is not RawAttempt:
            raise TaskwiseExecutionFailureV1(
                "EXECUTOR_OUTPUT_INVALID",
                completion_observed=True,
                executor_stage="COMPLETION_VALIDATION",
            )
        self._state["pending_invocation"]["completion_observed"] = True
        self._write_state()
        context_receipt = self._require_context_binding(
            session_id=session_id,
            memory=memory,
            ordinal=ordinal,
            round_index=round_index,
        )
        evaluation = self._evaluator.evaluate(
            task=episode.task,
            raw_completion=attempt.response,
        )
        self._append_round_result(
            episode=episode,
            ordinal=ordinal,
            round_index=round_index,
            session_id=session_id,
            memory=memory,
            attempt=attempt,
            evaluation=evaluation,
            context_receipt=context_receipt,
        )
        self._state["completion_count"] += 1
        self._state["pending_invocation"] = None
        self._write_state()

    def _require_context_binding(
        self,
        *,
        session_id: str,
        memory: CoreResolvedTextMemoryV2 | None,
        ordinal: int,
        round_index: int,
    ) -> TaskwiseContextBindingReceiptV1:
        expected = TaskwiseSessionContextBindingV1.from_memory(
            session_id=session_id,
            memory=memory,
        )
        receipt: object = None
        try:
            receipt = self._executor.consume_taskwise_context_receipt(session_id)
            if type(receipt) is not TaskwiseContextBindingReceiptV1:
                raise TypeError("executor returned an untrusted context receipt")
            if (
                receipt.expected != expected
                or receipt.actual != expected
                or not receipt.passed
                or receipt.finding_codes
            ):
                raise ValueError("taskwise context receipt differs from runner expectation")
            receipt.require_match()
            return receipt
        except Exception as exc:
            self._state["context_binding_violation_count"] += 1
            event: dict[str, object] = {
                "schema_version": "taskwise_online_public_event_v1",
                "kind": "context_binding_violation",
                "arm": self._config.arm,
                "task_ordinal": ordinal,
                "round_index": round_index,
                "session_id": session_id,
                "finding_codes": [
                    CONTEXT_BINDING_VIOLATION,
                    MEETING_722_CONTRACT_VIOLATION,
                ],
            }
            if type(receipt) is TaskwiseContextBindingReceiptV1:
                event["context_binding"] = receipt.to_public_dict()
            self._append_public(event)
            self._write_state()
            raise TaskwiseExecutionFailureV1(
                MEETING_722_CONTRACT_VIOLATION,
                completion_observed=True,
            ) from exc

    def _perform_update(
        self,
        episode_runtime_id: str,
        ordinal: int,
        *,
        update_index: int,
    ) -> None:
        if self._core is None:
            raise RuntimeError("online Core update port is unavailable")
        episode = self._episodes[ordinal]
        try:
            trajectories = self._load_current_trajectories(
                ordinal,
                count=update_index,
            )
            prior = (
                self._restore_memory(self._state["carry_memory"])
                if update_index == 1
                else self._restore_memory(self._state["active_memory"])
            )
            request = TaskwiseRunnerCoreUpdateRequestV1(
                task_uid=episode.task.uid,
                task_index=ordinal,
                episode_id=episode_runtime_id,
                update_index=update_index,
                source_round_index=update_index - 1,
                trajectories=trajectories,
                safe_signals=tuple(item.safe_feedback for item in trajectories),
                prior_resolved_text_memory=prior,
            )
            outcome = self._core.update_text_memory(request)
            if type(outcome) is not TaskwiseCoreUpdateOutcomeV1:
                raise TypeError("Core update port did not return typed Core evidence")
            expected_trajectory_ids = tuple(item.trajectory_id for item in trajectories)
            expected_trajectory_digest = ordered_taskwise_trajectory_digest(trajectories)
            expected_safe_feedback_digest = ordered_safe_feedback_digest(trajectories)
            if (
                outcome.task_uid != episode.task.uid
                or outcome.task_index != ordinal
                or outcome.update_index != update_index
                or outcome.global_update_ordinal != ordinal * 2 + update_index
                or outcome.predecessor_artifact_id
                != (None if prior is None else prior.core_artifact_id)
                or outcome.predecessor_memory_sha256
                != (None if prior is None else prior.resolved_memory_sha256)
                or outcome.trajectory_ids != expected_trajectory_ids
                or outcome.trajectory_digest != expected_trajectory_digest
                or outcome.safe_feedback_digest != expected_safe_feedback_digest
            ):
                raise TaskwiseExecutionFailureV1(
                    MEETING_722_CONTRACT_VIOLATION,
                    completion_observed=True,
                )
            memory = outcome.resolved_text_memory
            reference = CoreMemoryReferenceV1.from_memory(memory)
            if outcome.core_artifact_id != reference.core_artifact_id:
                raise TaskwiseExecutionFailureV1(
                    MEETING_722_CONTRACT_VIOLATION,
                    completion_observed=True,
                )
            metrics = outcome.memory_metrics
            if metrics.memory_limits_sha256 != self._config.memory_limits.digest:
                raise ValueError("Core memory metrics do not match the run binding")
        except TaskwiseExecutionFailureV1:
            raise
        except Exception as exc:
            if _is_security_violation(exc):
                raise _taskwise_failure_from_executor(exc) from exc
            # A Core update may have created a job or artifact before failing.
            # Validation and reflector-security failures are update failures too.
            # Their side effects are ambiguous, so the whole run is terminal and
            # the update must never be replayed or replaced.
            raise _taskwise_evolution_failure(exc) from exc
        try:
            aggregate = TaskwiseMemoryPublicAggregateV1.from_dict(self._state["memory_aggregate"])
            next_aggregate = aggregate.include(metrics)
            if (
                outcome.job_id in self._state["registered_core_job_ids"]
                or reference.core_artifact_id in self._state["registered_core_artifact_ids"]
            ):
                raise ValueError("Core update reused a prior job or artifact")
            event = {
                "schema_version": "taskwise_online_public_event_v1",
                "kind": "core_update",
                "arm": "online",
                "task_uid": episode.task.uid,
                "stream_id": self._config.contract_stream_id,
                "task_ordinal": ordinal,
                "episode_runtime_id": episode_runtime_id,
                "update_index": update_index,
                "source_round_index": update_index - 1,
                "global_update_ordinal": outcome.global_update_ordinal,
                "predecessor_artifact_id": outcome.predecessor_artifact_id,
                "predecessor_memory_sha256": outcome.predecessor_memory_sha256,
                "input_memory_sha256": (None if prior is None else prior.resolved_memory_sha256),
                "trajectory_ids": list(outcome.trajectory_ids),
                "trajectory_digest": outcome.trajectory_digest,
                "safe_feedback_digest": outcome.safe_feedback_digest,
                "output_memory": reference.to_dict(),
                "core_job_id": outcome.job_id,
                "evolution_job_id": outcome.job_id,
                "core_job_state": outcome.job_state,
                "core_context_id": outcome.core_context_id,
                "context_resolution_digest": memory.context_resolution_digest,
                "artifact_id": outcome.core_artifact_id,
                "validation_receipt_sha256": outcome.validation_receipt_sha256,
                "memory_metrics": metrics.to_dict(),
            }
            self._append_public(event)
            self._state["active_memory"] = reference.to_dict()
            self._state["predecessor_artifact_ref"] = reference.to_dict()
            self._state["last_core_job_id"] = outcome.job_id
            self._state["last_core_job_state"] = outcome.job_state
            self._state["last_context_resolution_digest"] = memory.context_resolution_digest
            self._state["memory_aggregate"] = next_aggregate.to_dict()
            self._state["update_count"] += 1
            self._state["registered_core_job_ids"].append(outcome.job_id)
            self._state["registered_core_artifact_ids"].append(reference.core_artifact_id)
            self._state["core_job_count"] += 1
            self._state["core_artifact_count"] += 1
            self._write_state()
        except Exception as exc:
            if _is_security_violation(exc):
                raise _taskwise_failure_from_executor(exc) from exc
            raise _taskwise_evolution_failure(exc) from exc

    def _append_round_result(
        self,
        *,
        episode: TaskwiseEpisodeV1,
        ordinal: int,
        round_index: int,
        session_id: str,
        memory: CoreResolvedTextMemoryV2 | None,
        attempt: RawAttempt,
        evaluation: PrivateChemBench4KEvaluation,
        context_receipt: TaskwiseContextBindingReceiptV1,
    ) -> None:
        if type(context_receipt) is not TaskwiseContextBindingReceiptV1:
            raise TypeError("context_receipt must be exact and verified")
        context_receipt.require_match()
        public = {
            "schema_version": "taskwise_online_public_event_v1",
            "kind": "completion",
            "arm": self._config.arm,
            "task_uid": episode.task.uid,
            "stream_id": self._config.contract_stream_id,
            "task_ordinal": ordinal,
            "episode_runtime_id": _episode_runtime_id(self._config.run_id, ordinal),
            "category": episode.task.category,
            "round_index": round_index,
            "session_id": session_id,
            "context_binding": context_receipt.to_public_dict(),
            "memory": (
                None if memory is None else CoreMemoryReferenceV1.from_memory(memory).to_dict()
            ),
            "official_prediction": evaluation.official.prediction,
            "official_parse_status": evaluation.official.status.value,
            "strict_parse_success": evaluation.strict.parsed,
            "transcript_reference": attempt.transcript_reference.reference,
            "runtime_metadata": (
                None
                if attempt.runtime_metadata is None
                else attempt.runtime_metadata.to_result_payload()
            ),
        }
        trajectory: TaskwiseTrajectoryV1 | None = None
        if self._config.arm == "online" and round_index in (0, 1):
            trajectory = TaskwiseTrajectoryV1.from_attempt(
                task_uid=episode.task.uid,
                task_index=ordinal,
                category=episode.task.category,
                round_index=round_index,
                session_id=session_id,
                prompt=episode.prompt,
                attempt=attempt,
                safe_feedback=safe_signal_from_private_evaluation(evaluation),
                dataset_sha256=episode.task.dataset_sha256,
            )
        private = {
            "schema_version": "taskwise_online_private_evaluation_v1",
            "arm": self._config.arm,
            "task_ordinal": ordinal,
            "task_uid": episode.task.uid,
            "category": episode.task.category,
            "round_index": round_index,
            "target": episode.task.target,
            "raw_completion": evaluation.raw_completion,
            "official_prediction": evaluation.official.prediction,
            "official_parse_status": evaluation.official.status.value,
            "strict_prediction": evaluation.strict.prediction,
            "strict_parse_status": evaluation.strict.status.value,
            "correct": evaluation.correct,
            "context_binding": context_receipt.to_public_dict(),
            "memory": public["memory"],
            "trajectory": (None if trajectory is None else trajectory.to_core_record()),
        }
        self._append_public(public)
        self._append_private(private)

    def _load_current_trajectories(
        self,
        ordinal: int,
        *,
        count: int,
    ) -> tuple[TaskwiseTrajectoryV1, ...]:
        rows = _read_jsonl(self._private_path, private=True)
        by_round = {
            row["round_index"]: row.get("trajectory")
            for row in rows
            if row.get("task_ordinal") == ordinal and row.get("round_index") < count
        }
        if set(by_round) != set(range(count)):
            raise RuntimeError("complete current-task trajectory prefix is unavailable")
        trajectories = tuple(
            _trajectory_from_core_record(by_round[round_index]) for round_index in range(count)
        )
        episode = self._episodes[ordinal]
        if any(
            item.task_uid != episode.task.uid
            or item.task_index != ordinal
            or item.public_prompt != episode.prompt.text
            for item in trajectories
        ):
            raise RuntimeError("persisted trajectory prefix does not match the current task")
        return trajectories

    def _initialize_new_run(self) -> None:
        root = self._config.output_directory
        if root.exists():
            raise RuntimeError("taskwise output directory already exists")
        (root / "public").mkdir(parents=True, mode=0o755)
        (root / "private").mkdir(mode=0o700)
        _create_file(self._public_path, mode=0o644)
        _create_file(self._private_path, mode=0o600)
        _create_file(self._private_failures_path, mode=0o600)
        self._state = {
            "schema_version": "taskwise_online_run_state_v1",
            "protocol_id": _protocol_id(self._config.arm),
            "protocol_labels": list(PROTOCOL_LABELS),
            "standard_chembench4k_score_claimed": False,
            "binding": self._binding,
            "arm": self._config.arm,
            "status": TaskwiseRunStatusV1.INITIALIZED.value,
            "episode_state": TaskwiseEpisodeStateV1.TASK_OPENED.value,
            "task_ordinal": 0,
            "planned_tasks": len(self._episodes),
            "completed_tasks": 0,
            "completion_count": 0,
            "update_count": 0,
            "core_job_count": 0,
            "core_artifact_count": 0,
            "context_binding_violation_count": 0,
            "session_attempt_count": 0,
            "issued_session_ids": [],
            "registered_core_job_ids": [],
            "registered_core_artifact_ids": [],
            "carry_memory": None,
            "active_memory": None,
            "predecessor_artifact_ref": None,
            "last_core_job_id": None,
            "last_core_job_state": None,
            "last_context_resolution_digest": None,
            "memory_aggregate": TaskwiseMemoryPublicAggregateV1.empty(
                self._config.memory_limits
            ).to_dict(),
            "public_event_chain_sha256": _ZERO_SHA256,
            "private_evaluation_chain_sha256": _ZERO_SHA256,
            "pending_invocation": None,
            "resume_count": 0,
            "resume_allowed": False,
            "failure": None,
            "meeting722_contract_verified_tasks": 0,
            "meeting722_contract_receipt_sha256": None,
        }
        self._write_state()

    def _verify_meeting722_contract_prefix(self, verified_task_count: int) -> None:
        """Verify and atomically persist every finalized stream prefix."""

        try:
            receipt = issue_meeting722_taskwise_contract_v1(
                protocol_id=_protocol_id(self._config.arm),
                arm=self._config.arm,
                run_id=self._config.run_id,
                stream_id=self._config.contract_stream_id,
                expected_task_uids=tuple(episode.task.uid for episode in self._episodes),
                verified_task_count=verified_task_count,
                public_events=tuple(_read_jsonl(self._public_path, private=False)),
                private_evaluations=tuple(_read_jsonl(self._private_path, private=True)),
                run_state=self._state,
            )
            _atomic_json(
                self._meeting_contract_path,
                receipt.to_public_dict(),
                mode=0o644,
            )
            self._state["meeting722_contract_verified_tasks"] = verified_task_count
            self._state["meeting722_contract_receipt_sha256"] = receipt.receipt_sha256
            self._write_state()
        except TaskwiseExecutionFailureV1:
            raise
        except Exception as exc:
            raise TaskwiseExecutionFailureV1(
                MEETING_722_CONTRACT_VIOLATION,
                completion_observed=True,
            ) from exc

    def _load_resume_state(self) -> None:
        if not self._config.output_directory.is_dir():
            raise RuntimeError("resume output directory does not exist")
        self._state = _read_json(self._state_path)
        if (
            self._state.get("schema_version") != "taskwise_online_run_state_v1"
            or self._state.get("protocol_id") != _protocol_id(self._config.arm)
            or self._state.get("binding") != self._binding
            or self._state.get("arm") != self._config.arm
        ):
            raise RuntimeError("taskwise resume binding mismatch")
        status = self._state.get("status")
        pending = self._state.get("pending_invocation")
        safely_checkpointed = (
            status == TaskwiseRunStatusV1.RUNNING.value
            and pending is None
            and self._state.get("resume_allowed") is False
        )
        if not safely_checkpointed:
            raise RuntimeError("taskwise run state forbids resume")
        if type(self._state.get("issued_session_ids")) is not list or len(
            self._state["issued_session_ids"]
        ) != len(set(self._state["issued_session_ids"])):
            raise RuntimeError("resume cannot replace an observed or ambiguous completion")
        public_rows = _read_jsonl(self._public_path, private=False)
        private_rows = _read_jsonl(self._private_path, private=True)
        if _read_jsonl(self._private_failures_path, private=True):
            raise RuntimeError("taskwise failed run cannot resume")
        completion_rows = [row for row in public_rows if row.get("kind") == "completion"]
        update_rows = [row for row in public_rows if row.get("kind") == "core_update"]
        violation_rows = [
            row for row in public_rows if row.get("kind") == "context_binding_violation"
        ]
        if (
            _chain_rows(public_rows) != self._state["public_event_chain_sha256"]
            or _chain_rows(private_rows) != self._state["private_evaluation_chain_sha256"]
            or len(completion_rows) != self._state["completion_count"]
            or len(update_rows) != self._state["update_count"]
            or len(violation_rows) != self._state["context_binding_violation_count"]
            or len(private_rows) != self._state["completion_count"]
            or self._state["core_job_count"] != len(update_rows)
            or self._state["core_artifact_count"] != len(update_rows)
            or self._state["registered_core_job_ids"]
            != [row.get("core_job_id") for row in update_rows]
            or self._state["registered_core_artifact_ids"]
            != [row.get("output_memory", {}).get("core_artifact_id") for row in update_rows]
            or len(set(self._state["registered_core_job_ids"])) != len(update_rows)
            or len(set(self._state["registered_core_artifact_ids"])) != len(update_rows)
        ):
            raise RuntimeError("taskwise resume history digest or counters mismatch")
        for row in completion_rows:
            try:
                receipt = taskwise_context_binding_receipt_from_public_dict(
                    row.get("context_binding")
                )
                expected = _context_binding_from_public_completion(row)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("resume context-binding receipt is invalid") from exc
            if (
                not receipt.passed
                or receipt.expected != expected
                or receipt.actual != expected
                or receipt.session_id != row.get("session_id")
            ):
                raise RuntimeError("resume context-binding receipt mismatch")
        self._validate_event_sequence(public_rows)
        self._validate_checkpoint_position(public_rows)
        recomputed_aggregate = TaskwiseMemoryPublicAggregateV1.empty(self._config.memory_limits)
        try:
            for row in update_rows:
                recomputed_aggregate = recomputed_aggregate.include(
                    TaskwiseMemoryPublicMetricsV1.from_dict(row.get("memory_metrics"))
                )
            persisted_aggregate = TaskwiseMemoryPublicAggregateV1.from_dict(
                self._state.get("memory_aggregate")
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("resume memory aggregate is invalid") from exc
        if recomputed_aggregate != persisted_aggregate:
            raise RuntimeError("resume memory aggregate does not match Core update events")
        if update_rows:
            last_update = update_rows[-1]
            if (
                self._state.get("predecessor_artifact_ref") != last_update.get("output_memory")
                or self._state.get("last_core_job_id") != last_update.get("core_job_id")
                or self._state.get("last_core_job_state") != last_update.get("core_job_state")
                or self._state.get("last_context_resolution_digest")
                != last_update.get("context_resolution_digest")
            ):
                raise RuntimeError("resume Core artifact/job/context evidence mismatch")
        elif any(
            self._state.get(field_name) is not None
            for field_name in (
                "predecessor_artifact_ref",
                "last_core_job_id",
                "last_core_job_state",
                "last_context_resolution_digest",
            )
        ):
            raise RuntimeError("resume state claims Core evidence without an update")
        self._state["pending_invocation"] = None

    def _validate_checkpoint_position(self, rows: list[dict[str, Any]]) -> None:
        ordinal = self._state.get("task_ordinal")
        completed = self._state.get("completed_tasks")
        state_value = self._state.get("episode_state")
        if (
            type(ordinal) is not int
            or type(completed) is not int
            or ordinal != completed
            or not 0 <= ordinal <= len(self._episodes)
        ):
            raise RuntimeError("resume checkpoint task position is invalid")
        if ordinal == len(self._episodes):
            if (
                completed != len(self._episodes)
                or state_value != TaskwiseEpisodeStateV1.TASK_FINALIZED.value
            ):
                raise RuntimeError("resume terminal checkpoint is invalid")
            return
        try:
            state = TaskwiseEpisodeStateV1(state_value)
        except ValueError as exc:
            raise RuntimeError("resume checkpoint state is invalid") from exc
        expected_events_per_completed_task = 5 if self._config.arm == "online" else 3
        current_events = len(rows) - ordinal * expected_events_per_completed_task
        if self._config.arm == "online":
            expected_current_events = {
                TaskwiseEpisodeStateV1.TASK_OPENED: 0,
                TaskwiseEpisodeStateV1.ROUND_0_COMPLETED: 1,
                TaskwiseEpisodeStateV1.UPDATE_1_COMPLETED: 2,
                TaskwiseEpisodeStateV1.ROUND_1_COMPLETED: 3,
                TaskwiseEpisodeStateV1.UPDATE_2_COMPLETED: 4,
                TaskwiseEpisodeStateV1.ROUND_2_COMPLETED: 5,
                TaskwiseEpisodeStateV1.TASK_FINALIZED: 5,
            }[state]
        else:
            if state in {
                TaskwiseEpisodeStateV1.UPDATE_1_COMPLETED,
                TaskwiseEpisodeStateV1.UPDATE_2_COMPLETED,
            }:
                raise RuntimeError("control checkpoint contains an evolution state")
            expected_current_events = {
                TaskwiseEpisodeStateV1.TASK_OPENED: 0,
                TaskwiseEpisodeStateV1.ROUND_0_COMPLETED: 1,
                TaskwiseEpisodeStateV1.ROUND_1_COMPLETED: 2,
                TaskwiseEpisodeStateV1.ROUND_2_COMPLETED: 3,
                TaskwiseEpisodeStateV1.TASK_FINALIZED: 3,
            }[state]
        if current_events != expected_current_events:
            raise RuntimeError("resume checkpoint state and event prefix differ")

    def _validate_event_sequence(self, rows: list[dict[str, Any]]) -> None:
        expected: list[tuple[str, int, int]] = []
        for ordinal in range(len(self._episodes)):
            expected.append(("completion", ordinal, 0))
            if self._config.arm == "online":
                expected.append(("core_update", ordinal, 1))
            expected.append(("completion", ordinal, 1))
            if self._config.arm == "online":
                expected.append(("core_update", ordinal, 2))
            expected.append(("completion", ordinal, 2))
        observed = [
            (
                row.get("kind"),
                row.get("task_ordinal"),
                (
                    row.get("round_index")
                    if row.get("kind") == "completion"
                    else row.get("update_index")
                ),
            )
            for row in rows
        ]
        if observed != expected[: len(observed)]:
            raise RuntimeError("taskwise resume history is not a valid event prefix")

    def _restore_memory(self, value: object) -> CoreResolvedTextMemoryV2 | None:
        if value is None:
            return None
        try:
            if self._core is None:
                raise RuntimeError("memory reference exists without a Core resolve port")
            reference = CoreMemoryReferenceV1.from_dict(value)
            memory = self._core.resolve_text_memory(reference)
            if (
                type(memory) is not CoreResolvedTextMemoryV2
                or CoreMemoryReferenceV1.from_memory(memory) != reference
            ):
                raise RuntimeError("Core resolved memory does not match its persisted reference")
            return memory
        except TaskwiseExecutionFailureV1:
            raise
        except Exception as exc:
            # A persisted memory is usable only when Core re-resolves the exact
            # approved stream head.  Missing/stale/unreadable artifacts are
            # terminal evolution failures; silently falling back to an older
            # predecessor (or to empty memory) would fork the treatment.
            if self._config.arm == "online":
                raise TaskwiseExecutionFailureV1(
                    "TASKWISE_EVOLUTION_UPDATE_FAILED",
                    completion_observed=True,
                ) from exc
            raise

    def _new_session_id(self) -> str:
        value = self._session_id_factory()
        if type(value) is not str or _RUNTIME_ID.fullmatch(value) is None:
            raise RuntimeError("session_id_factory returned an invalid identifier")
        if value in self._state["issued_session_ids"]:
            raise RuntimeError("session_id_factory attempted to reuse a session")
        return value

    def _transition(self, target: TaskwiseEpisodeStateV1) -> None:
        current = TaskwiseEpisodeStateV1(self._state["episode_state"])
        allowed = {
            ("control", TaskwiseEpisodeStateV1.TASK_OPENED): {
                TaskwiseEpisodeStateV1.ROUND_0_COMPLETED
            },
            ("control", TaskwiseEpisodeStateV1.ROUND_0_COMPLETED): {
                TaskwiseEpisodeStateV1.ROUND_1_COMPLETED
            },
            ("control", TaskwiseEpisodeStateV1.ROUND_1_COMPLETED): {
                TaskwiseEpisodeStateV1.ROUND_2_COMPLETED
            },
            ("control", TaskwiseEpisodeStateV1.ROUND_2_COMPLETED): {
                TaskwiseEpisodeStateV1.TASK_FINALIZED
            },
            ("online", TaskwiseEpisodeStateV1.TASK_OPENED): {
                TaskwiseEpisodeStateV1.ROUND_0_COMPLETED
            },
            ("online", TaskwiseEpisodeStateV1.ROUND_0_COMPLETED): {
                TaskwiseEpisodeStateV1.UPDATE_1_COMPLETED
            },
            ("online", TaskwiseEpisodeStateV1.UPDATE_1_COMPLETED): {
                TaskwiseEpisodeStateV1.ROUND_1_COMPLETED
            },
            ("online", TaskwiseEpisodeStateV1.ROUND_1_COMPLETED): {
                TaskwiseEpisodeStateV1.UPDATE_2_COMPLETED
            },
            ("online", TaskwiseEpisodeStateV1.UPDATE_2_COMPLETED): {
                TaskwiseEpisodeStateV1.ROUND_2_COMPLETED
            },
            ("online", TaskwiseEpisodeStateV1.ROUND_2_COMPLETED): {
                TaskwiseEpisodeStateV1.TASK_FINALIZED
            },
        }
        if target not in allowed.get((self._config.arm, current), set()):
            raise RuntimeError("invalid taskwise episode state transition")
        self._state["episode_state"] = target.value
        self._write_state()

    def _append_public(self, value: dict[str, object]) -> None:
        line = _append_json(self._public_path, value)
        self._state["public_event_chain_sha256"] = _extend_chain(
            self._state["public_event_chain_sha256"],
            line,
        )

    def _append_private(self, value: dict[str, object]) -> None:
        line = _append_json(self._private_path, value)
        self._state["private_evaluation_chain_sha256"] = _extend_chain(
            self._state["private_evaluation_chain_sha256"],
            line,
        )

    def _append_private_failure(self, exc: TaskwiseExecutionFailureV1) -> str:
        """Persist only closed diagnostic metadata, never exception text."""

        if type(exc) is not TaskwiseExecutionFailureV1:
            raise TypeError("failure must be exact TaskwiseExecutionFailureV1")
        if self._private_failures_path.stat().st_mode & 0o077:
            raise RuntimeError("private taskwise failure permissions are unsafe")
        pending = self._state.get("pending_invocation")
        ordinal = self._state.get("task_ordinal")
        task_uid = (
            self._episodes[ordinal].task.uid
            if type(ordinal) is int and 0 <= ordinal < len(self._episodes)
            else None
        )
        episode_state = self._state.get("episode_state")
        update_index = (
            1
            if episode_state == TaskwiseEpisodeStateV1.ROUND_0_COMPLETED.value
            and type(pending) is not dict
            else (
                2
                if episode_state == TaskwiseEpisodeStateV1.ROUND_1_COMPLETED.value
                and type(pending) is not dict
                else None
            )
        )
        value: dict[str, object] = {
            "schema_version": "taskwise_private_failure_v1",
            "protocol_id": _protocol_id(self._config.arm),
            "arm": self._config.arm,
            "run_id": self._config.run_id,
            "task_uid": task_uid,
            "task_ordinal": ordinal,
            "episode_state": episode_state,
            "round_index": (
                pending.get("round_index")
                if type(pending) is dict
                else (None if update_index is None else update_index - 1)
            ),
            "update_index": update_index,
            "session_id": (pending.get("session_id") if type(pending) is dict else None),
            "code": exc.code,
            "completion_observed": exc.completion_observed,
            "security_violation": exc.security_violation,
            "executor_stage": exc.executor_stage,
            "diagnostic_receipt": exc.diagnostic_receipt,
            "event_digest": exc.event_digest,
            "event_counts": dict(exc.event_counts),
            "retry_allowed": False,
            "resume_allowed": False,
            "replacement_completion_allowed": False,
        }
        line = _append_json(self._private_failures_path, value)
        return hashlib.sha256(line).hexdigest()

    def _write_state(self) -> None:
        _atomic_json(self._state_path, self._state, mode=0o644)

    def _record_execution_failure(
        self,
        exc: TaskwiseExecutionFailureV1,
    ) -> TaskwiseRunResultV1:
        secondary_finding_codes: tuple[str, ...] = ()
        try:
            diagnostic_record_sha256 = self._append_private_failure(exc)
        except Exception:
            diagnostic_record_sha256 = None
            if exc.security_violation:
                # Never let a secondary persistence failure erase an already
                # latched tool-use violation.  The public state records only a
                # closed secondary code, never the private exception body.
                secondary_finding_codes = ("EXECUTOR_PRIVATE_DIAGNOSTICS_FAILED",)
            else:
                exc = TaskwiseExecutionFailureV1(
                    "EXECUTOR_PRIVATE_DIAGNOSTICS_FAILED",
                    completion_observed=True,
                    executor_stage="PRIVATE_EVENT_PERSISTENCE",
                )
        if self._state.get("pending_invocation") is not None:
            self._state["pending_invocation"]["completion_observed"] = exc.completion_observed
        if exc.security_violation:
            status = TaskwiseRunStatusV1.SECURITY_TOOL_USE_VIOLATION
        elif exc.code == CONTEXT_BINDING_VIOLATION:
            status = TaskwiseRunStatusV1.TASKWISE_CONTEXT_BINDING_VIOLATION
        elif exc.code == MEETING_722_CONTRACT_VIOLATION:
            status = TaskwiseRunStatusV1.MEETING_722_TASKWISE_CONTRACT_VIOLATION
        elif exc.code == "TASKWISE_EVOLUTION_UPDATE_FAILED":
            status = TaskwiseRunStatusV1.TASKWISE_EVOLUTION_UPDATE_FAILED
        else:
            status = TaskwiseRunStatusV1.EXECUTION_FAILED
        self._state["status"] = status.value
        self._state["resume_allowed"] = False
        failure: dict[str, object] = {
            "code": exc.code,
            "completion_observed": exc.completion_observed,
        }
        if exc.diagnostic_receipt is not None:
            failure["diagnostic_receipt"] = exc.diagnostic_receipt
        if exc.event_digest is not None:
            failure["event_digest"] = exc.event_digest
        if diagnostic_record_sha256 is not None:
            failure["diagnostic_record_sha256"] = diagnostic_record_sha256
        if secondary_finding_codes:
            failure["secondary_finding_codes"] = list(secondary_finding_codes)
        self._state["failure"] = failure
        self._write_state()
        return self._result(finding_codes=(exc.code, *secondary_finding_codes))

    def _record_fail_closed_internal_error(self) -> None:
        self._record_execution_failure(
            TaskwiseExecutionFailureV1(
                "EXECUTOR_INTERNAL_ERROR",
                completion_observed=True,
                executor_stage="INTERNAL",
            )
        )

    def _result(self, *, finding_codes: tuple[str, ...] = ()) -> TaskwiseRunResultV1:
        return TaskwiseRunResultV1(
            status=TaskwiseRunStatusV1(self._state["status"]),
            arm=self._config.arm,
            planned_tasks=len(self._episodes),
            completed_tasks=self._state["completed_tasks"],
            completion_count=self._state["completion_count"],
            update_count=self._state["update_count"],
            core_job_count=self._state["core_job_count"],
            core_artifact_count=self._state["core_artifact_count"],
            context_binding_violation_count=self._state["context_binding_violation_count"],
            session_attempt_count=self._state["session_attempt_count"],
            output_directory=self._config.output_directory,
            resume_allowed=self._state["resume_allowed"],
            memory_aggregate=TaskwiseMemoryPublicAggregateV1.from_dict(
                self._state["memory_aggregate"]
            ),
            finding_codes=finding_codes,
        )


def load_private_taskwise_results_v1(
    output_directory: Path,
) -> tuple[PrivateTaskwiseRoundResultV1, ...]:
    """Load private rows for aggregation without exposing raw completions."""

    path = output_directory / "private" / "evaluations.jsonl"
    return tuple(
        PrivateTaskwiseRoundResultV1(
            task_uid=row["task_uid"],
            task_index=row["task_ordinal"],
            category=row["category"],
            arm=row["arm"],
            round_index=row["round_index"],
            target=row["target"],
            official_prediction=row["official_prediction"],
            official_parsed=(row["official_parse_status"] == ChemBench4KParseStatus.PARSED.value),
            strict_parsed=row["strict_prediction"] is not None,
            core_artifact_id=(
                None if row["memory"] is None else row["memory"]["core_artifact_id"]
            ),
            memory_digest=(
                None if row["memory"] is None else row["memory"]["resolved_memory_sha256"]
            ),
        )
        for row in _read_jsonl(path, private=True)
    )


def _require_sha256(value: object, field_name: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256")


def _protocol_id(arm: str) -> str:
    if arm == "online":
        return ONLINE_PROTOCOL_ID
    if arm == "control":
        return CONTROL_PROTOCOL_ID
    raise ValueError("arm must be control or online")


def _context_binding_from_public_completion(
    row: dict[str, Any],
) -> TaskwiseSessionContextBindingV1:
    session_id = row.get("session_id")
    memory = row.get("memory")
    if memory is None:
        return TaskwiseSessionContextBindingV1(
            session_id=session_id,
            core_artifact_id=None,
            artifact_payload_sha256=None,
            resolved_memory_sha256=None,
            context_resolution_digest=None,
        )
    reference = CoreMemoryReferenceV1.from_dict(memory)
    return TaskwiseSessionContextBindingV1(
        session_id=session_id,
        core_artifact_id=reference.core_artifact_id,
        artifact_payload_sha256=reference.artifact_payload_sha256,
        resolved_memory_sha256=reference.resolved_memory_sha256,
        context_resolution_digest=reference.context_resolution_digest,
    )


def _episode_runtime_id(run_id: str, ordinal: int) -> str:
    return f"episode_{hashlib.sha256(f'{run_id}:{ordinal}'.encode()).hexdigest()[:24]}"


def _sha256_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _create_file(path: Path, *, mode: int) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    os.close(descriptor)


def _append_json(path: Path, value: object) -> bytes:
    line = _canonical_json(value) + b"\n"
    with path.open("ab", buffering=0) as handle:
        handle.write(line)
        os.fsync(handle.fileno())
    return line


def _atomic_json(path: Path, value: object, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_json(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("taskwise state is unreadable") from exc
    if type(value) is not dict or raw != _canonical_json(value) + b"\n":
        raise RuntimeError("taskwise state is not canonical")
    return value


def _read_jsonl(path: Path, *, private: bool) -> list[dict[str, Any]]:
    if private and path.stat().st_mode & 0o077:
        raise RuntimeError("private taskwise result permissions are unsafe")
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_bytes().splitlines(keepends=True)
        for line in lines:
            value = json.loads(line)
            if type(value) is not dict or line != _canonical_json(value) + b"\n":
                raise RuntimeError("taskwise JSONL is not canonical")
            rows.append(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("taskwise JSONL is unreadable") from exc
    return rows


def _extend_chain(previous: str, line: bytes) -> str:
    _require_sha256(previous, "previous chain digest")
    return hashlib.sha256(bytes.fromhex(previous) + line).hexdigest()


def _chain_rows(rows: list[dict[str, Any]]) -> str:
    digest = _ZERO_SHA256
    for row in rows:
        digest = _extend_chain(digest, _canonical_json(row) + b"\n")
    return digest


def _taskwise_failure_from_executor(exc: Exception) -> TaskwiseExecutionFailureV1:
    """Copy only validated, closed metadata from an executor exception."""

    if type(exc) is TaskwiseExecutionFailureV1:
        return exc
    raw_code = getattr(exc, "taskwise_failure_code", None)
    event_counts = getattr(exc, "event_counts", None)
    security_violation = (
        _is_security_violation(exc)
        or (raw_code == "EXECUTOR_SECURITY_TOOL_USE_VIOLATION")
        or (type(event_counts) is dict and bool(event_counts))
    )
    if security_violation:
        code = "EXECUTOR_SECURITY_TOOL_USE_VIOLATION"
    elif raw_code in TASKWISE_EXECUTOR_FAILURE_CODES_V1:
        code = raw_code
    else:
        code = "EXECUTOR_INTERNAL_ERROR"

    raw_completion_observed = getattr(exc, "completion_observed", None)
    completion_observed = (
        raw_completion_observed if type(raw_completion_observed) is bool else True
    )
    diagnostic_receipt = getattr(exc, "diagnostic_receipt", None)
    event_digest = getattr(exc, "event_digest", None)
    executor_stage = getattr(exc, "executor_stage", None)
    try:
        return TaskwiseExecutionFailureV1(
            code,
            completion_observed=completion_observed,
            diagnostic_receipt=diagnostic_receipt,
            event_digest=event_digest,
            event_counts=event_counts,
            executor_stage=executor_stage,
        )
    except (TypeError, ValueError):
        # Unsafe metadata is discarded rather than copied into either the
        # public state or private audit record.  Security evidence remains a
        # security violation; all other malformed exceptions collapse to the
        # closed internal executor code.
        return TaskwiseExecutionFailureV1(
            (
                "EXECUTOR_SECURITY_TOOL_USE_VIOLATION"
                if security_violation
                else "EXECUTOR_INTERNAL_ERROR"
            ),
            completion_observed=completion_observed,
            executor_stage="INTERNAL",
        )


def _taskwise_evolution_failure(exc: Exception) -> TaskwiseExecutionFailureV1:
    """Retain only a validated private Core receipt basename."""

    diagnostic_receipt = getattr(exc, "diagnostic_receipt", None)
    try:
        return TaskwiseExecutionFailureV1(
            "TASKWISE_EVOLUTION_UPDATE_FAILED",
            completion_observed=True,
            diagnostic_receipt=diagnostic_receipt,
        )
    except (TypeError, ValueError):
        return TaskwiseExecutionFailureV1(
            "TASKWISE_EVOLUTION_UPDATE_FAILED",
            completion_observed=True,
        )


def _is_security_violation(exc: Exception) -> bool:
    run_status = getattr(exc, "run_status", None)
    code = getattr(exc, "code", None)
    code_value = getattr(code, "value", code)
    finding_code = getattr(exc, "finding_code", None)
    event_counts = getattr(exc, "event_counts", None)
    return (
        run_status == "SECURITY_TOOL_USE_VIOLATION"
        or code_value == "security_tool_use_violation"
        or code_value == "SECURITY_TOOL_USE_VIOLATION"
        or (type(event_counts) is dict and bool(event_counts))
        or finding_code
        in {
            "REFLECTOR_SECURITY_TOOL_USE_VIOLATION",
            "TASKWISE_REFLECTOR_SECURITY_TOOL_USE_VIOLATION",
            "TASKWISE_SECURITY_TOOL_USE_VIOLATION",
        }
    )


def _trajectory_from_core_record(value: object) -> TaskwiseTrajectoryV1:
    if type(value) is not dict or set(value) != {
        "schema_version",
        "protocol_id",
        "trajectory_id",
        "task_uid",
        "task_index",
        "category",
        "round_index",
        "session_id",
        "public_prompt_sha256",
        "public_prompt",
        "raw_completion",
        "safe_feedback",
        "safe_feedback_digest",
        "dataset_revision",
        "dataset_sha256",
    }:
        raise RuntimeError("persisted trajectory schema is invalid")
    if (
        value["schema_version"] != "taskwise_core_trajectory_v1"
        or value["protocol_id"] != ONLINE_PROTOCOL_ID
    ):
        raise RuntimeError("persisted trajectory protocol identity is invalid")
    safe_feedback = taskwise_safe_signal_from_payload(value["safe_feedback"])
    if safe_feedback.digest != value["safe_feedback_digest"]:
        raise RuntimeError("persisted trajectory safe-feedback digest mismatch")
    trajectory = TaskwiseTrajectoryV1(
        trajectory_id=value["trajectory_id"],
        task_uid=value["task_uid"],
        task_index=value["task_index"],
        category=value["category"],
        round_index=value["round_index"],
        session_id=value["session_id"],
        public_prompt_sha256=value["public_prompt_sha256"],
        public_prompt=value["public_prompt"],
        raw_completion=value["raw_completion"],
        safe_feedback=safe_feedback,
        dataset_revision=value["dataset_revision"],
        dataset_sha256=value["dataset_sha256"],
    )
    if trajectory.to_core_record() != value:
        raise RuntimeError("persisted trajectory content changed")
    return trajectory


__all__ = [
    "CONTROL_PROTOCOL_ID",
    "CoreMemoryReferenceV1",
    "ONLINE_PROTOCOL_ID",
    "TASKWISE_EXECUTOR_FAILURE_CODES_V1",
    "TASKWISE_EXECUTOR_STAGES_V1",
    "TaskwiseAgentRequestV1",
    "TaskwiseCoreUpdatePortV1",
    "TaskwiseCoreUpdateOutcomeV1",
    "TaskwiseRunnerCoreUpdateRequestV1",
    "TaskwiseEpisodeStateV1",
    "TaskwiseEpisodeV1",
    "TaskwiseExecutionFailureV1",
    "TaskwiseExecutorV1",
    "TaskwiseOnlineRunnerV1",
    "TaskwiseRunConfigV1",
    "TaskwiseRunResultV1",
    "TaskwiseRunStatusV1",
    "build_taskwise_run_binding_v1",
    "load_private_taskwise_results_v1",
]
