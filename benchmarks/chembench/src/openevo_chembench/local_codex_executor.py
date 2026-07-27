"""Fail-closed local Codex CLI backend for public ChemBench round requests."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from openevo_chembench.artifacts import ArtifactKind
from openevo_chembench.config import ExperimentConfig
from openevo_chembench.frozen_runtime_v2 import (
    CoreResolvedTextMemoryV2,
    FrozenAgentRequestV2,
)
from openevo_chembench.models import (
    AgentRuntimeMetadata,
    RawAttempt,
    TranscriptReference,
)
from openevo_chembench.runtime_context import (
    AgentArtifactContext,
    AgentRoundRequest,
)
from openevo_chembench.supervised_transfer_v1.context_binding import (
    SupervisedAgentRequestV1,
    SupervisedContextBindingReceiptV1,
    SupervisedSessionContextBindingV1,
    issue_supervised_context_binding_receipt_v1,
)
from openevo_chembench.taskwise_config_v1 import (
    TaskwiseExperimentConfigV1,
    canonical_taskwise_config_bytes,
)
from openevo_chembench.taskwise_context_binding_v1 import (
    TaskwiseContextBindingReceiptV1,
    TaskwiseSessionContextBindingV1,
    issue_taskwise_context_binding_receipt_v1,
)
from openevo_chembench.taskwise_online_runner_v1 import TaskwiseAgentRequestV1
from openevo_chembench.v2_config import FrozenExperimentConfigV2

_CODEX_VERSION = re.compile(r"codex-cli ([0-9A-Za-z.+-]+)")
_TASKWISE_RUNTIME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}\Z", re.ASCII)
_FORBIDDEN_PAYLOAD_KEYS = frozenset(
    {
        "answer_mapping",
        "canary",
        "correct_answer",
        "private_task",
        "target",
        "target_scores",
        "uuid",
    }
)
_FORBIDDEN_PROMPT_MARKERS = (
    "answer_mapping",
    "correct_answer",
    "private_task",
    "target_scores",
    "uuid",
    "canary",
)
_ALLOWED_ITEM_TYPES = frozenset({"agent_message", "reasoning", "todo_list"})
_TEXT_ITEM_KEYS = frozenset({"id", "text", "type"})
_TODO_LIST_ITEM_KEYS = frozenset({"id", "items", "type"})
_TODO_LIST_ENTRY_KEYS = frozenset({"completed", "text"})
_MAX_TODO_LIST_ITEMS = 100
_MAX_TODO_LIST_TEXT_BYTES = 4096
_SECURITY_TOOL_USE_VIOLATION = "SECURITY_TOOL_USE_VIOLATION"
_SECURITY_EVENT_CATEGORIES = (
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
)
_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
_OPTIONAL_TOKEN_FIELDS = ("cache_write_input_tokens",)
_RECOVERABLE_TRANSPORT_ERROR_KEYS = frozenset({"type", "message"})
_MAX_RECOVERABLE_TRANSPORT_ERRORS = 8
_MAX_TRANSPORT_ERROR_MESSAGE_BYTES = 1024
_TASKWISE_EXECUTOR_FAILURE_CODES = frozenset(
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
_EXECUTOR_STAGES = frozenset(
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
_MODEL_TRANSPORT_ENV_KEYS = (
    "ALL_PROXY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "all_proxy",
    "https_proxy",
    "http_proxy",
    "no_proxy",
)
_SAFE_ENV_KEYS = (*_MODEL_TRANSPORT_ENV_KEYS, "LANG", "LC_ALL", "PATH")
_ISOLATION_PREFIX = "openevo-chembench-local-codex-"
_DIAGNOSTIC_DIRECTORY_NAME = "openevo-chembench-codex-diagnostics"
_CLEANUP_RETRY_DELAYS_SECONDS = (
    0.0,
    0.05,
    0.1,
    0.2,
    0.4,
    0.8,
    1.6,
    3.2,
    5.0,
    5.0,
    5.0,
    5.0,
    5.0,
)
_TRANSIENT_CLEANUP_ERRNOS = frozenset(
    {
        errno.EACCES,
        errno.EBUSY,
        errno.ENOTEMPTY,
        errno.EPERM,
    }
)
_DISABLED_CODEX_FEATURES = (
    "auth_elicitation",
    "shell_tool",
    "unified_exec",
    "shell_snapshot",
    "apps",
    "enable_mcp_apps",
    "plugins",
    "plugin_sharing",
    "remote_plugin",
    "multi_agent",
    "multi_agent_v2",
    "enable_fanout",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "goals",
    "hooks",
    "image_generation",
    "in_app_browser",
    "memories",
    "request_permissions_tool",
    "skill_mcp_dependency_install",
    "tool_call_mcp_elicitation",
    "tool_suggest",
    "workspace_dependencies",
    "code_mode",
    "code_mode_host",
    "code_mode_only",
)
_DISALLOWED_PLUGIN_RELATIVE_PATHS = (
    Path("codex_home") / ".tmp",
    Path("codex_home") / "plugins" / ".remote-plugin-install-staging",
    Path("codex_home") / "plugins" / "cache" / "openai-curated-remote",
)
_REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
_WORKSPACE_ROOT = _REPOSITORY_ROOT
_PROTECTED_HOST_ROOTS = tuple(
    path.resolve()
    for path in (
        _REPOSITORY_ROOT,
        _WORKSPACE_ROOT / "data",
        _WORKSPACE_ROOT / "results",
        _WORKSPACE_ROOT / "remote_storage",
        _WORKSPACE_ROOT / "external_refs",
        _REPOSITORY_ROOT / "data",
        _REPOSITORY_ROOT / "results",
        _REPOSITORY_ROOT / "remote_storage",
        _REPOSITORY_ROOT / "external_refs",
        _REPOSITORY_ROOT / "benchmarks" / "chembench" / "data",
        _REPOSITORY_ROOT / "benchmarks" / "chembench" / "results",
    )
)


@dataclass(frozen=True, slots=True)
class LocalCommandResult:
    """Minimal subprocess result used by the executor and deterministic tests."""

    returncode: int
    stdout: str
    stderr: str
    output_last_message_exists: bool | None = None
    output_last_message: str | None = None
    output_last_message_invalid: bool = False


@dataclass(frozen=True, slots=True)
class _CleanupOutcome:
    complete: bool
    auth_removed: bool
    disallowed_activity_seen: bool
    attempts: int
    last_errno: int | None


@dataclass(frozen=True, slots=True)
class _InvocationLayout:
    root: Path
    home: Path
    codex_home: Path
    xdg_config: Path
    xdg_cache: Path
    xdg_state: Path
    sqlite: Path
    tmp: Path
    work: Path
    private_events: Path
    output_last_message: Path


@dataclass(frozen=True, slots=True)
class _InvocationDiagnosticContext:
    run_id: str | None = None
    task_uid: str | None = None
    task_index: int | None = None
    round_index: int | None = None
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class TaskwiseExecutorSuccessReceiptV1:
    """Content-free private evidence for one completed Codex invocation."""

    run_id: str | None
    task_uid: str | None
    task_index: int | None
    round_index: int | None
    session_id: str | None
    event_stream_sha256: str
    event_count: int
    tool_event_count: int
    completion_observed: bool
    process_return_code: int
    process_signal: int | None
    cleanup_status: str
    residual_root_count: int
    codex_cli_version: str
    model: str
    executor_policy_sha256: str
    created_at_utc: str
    codex_executable_sha256: str | None = None

    def to_payload(self) -> dict[str, object]:
        """Return the closed, content-free serialization schema."""

        payload: dict[str, object] = {
            "schema_version": (
                "taskwise_executor_private_success_v2"
                if self.codex_executable_sha256 is not None
                else "taskwise_executor_private_success_v1"
            ),
            "status": "COMPLETED",
            "run_id": self.run_id,
            "task_uid": self.task_uid,
            "task_index": self.task_index,
            "round_index": self.round_index,
            "session_id": self.session_id,
            "event_stream_sha256": self.event_stream_sha256,
            "event_count": self.event_count,
            "tool_event_count": self.tool_event_count,
            "completion_observed": self.completion_observed,
            "process_return_code": self.process_return_code,
            "process_signal": self.process_signal,
            "cleanup_status": self.cleanup_status,
            "residual_root_count": self.residual_root_count,
            "codex_cli_version": self.codex_cli_version,
            "model": self.model,
            "executor_policy_sha256": self.executor_policy_sha256,
            "created_at_utc": self.created_at_utc,
        }
        if self.codex_executable_sha256 is not None:
            payload["codex_executable_sha256"] = self.codex_executable_sha256
        return payload

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
    ) -> TaskwiseExecutorSuccessReceiptV1:
        """Parse a success receipt without accepting schema extensions."""

        expected = {
            "schema_version",
            "status",
            "run_id",
            "task_uid",
            "task_index",
            "round_index",
            "session_id",
            "event_stream_sha256",
            "event_count",
            "tool_event_count",
            "completion_observed",
            "process_return_code",
            "process_signal",
            "cleanup_status",
            "residual_root_count",
            "codex_cli_version",
            "model",
            "executor_policy_sha256",
            "created_at_utc",
        }
        schema_version = payload.get("schema_version") if isinstance(payload, Mapping) else None
        if schema_version == "taskwise_executor_private_success_v2":
            expected.add("codex_executable_sha256")
        if (
            not isinstance(payload, Mapping)
            or set(payload) != expected
            or schema_version
            not in {
                "taskwise_executor_private_success_v1",
                "taskwise_executor_private_success_v2",
            }
            or payload["status"] != "COMPLETED"
        ):
            raise ValueError("taskwise executor success receipt schema is invalid")
        optional_text = ("run_id", "task_uid", "session_id")
        if any(
            value is not None and (type(value) is not str or not value)
            for key in optional_text
            if (value := payload[key]) is not None
        ):
            raise ValueError("taskwise executor success receipt identity is invalid")
        if (
            payload["task_uid"] is not None
            and re.fullmatch(r"[0-9a-f]{64}", str(payload["task_uid"])) is None
        ) or any(
            payload[key] is not None and _TASKWISE_RUNTIME_ID.fullmatch(str(payload[key])) is None
            for key in ("run_id", "session_id")
        ):
            raise ValueError("taskwise executor success receipt identity is invalid")
        optional_indices = ("task_index", "round_index")
        if any(
            value is not None
            and (isinstance(value, bool) or not isinstance(value, int) or value < 0)
            for key in optional_indices
            if (value := payload[key]) is not None
        ):
            raise ValueError("taskwise executor success receipt index is invalid")
        if payload["round_index"] is not None and payload["round_index"] not in (0, 1, 2):
            raise ValueError("taskwise executor success receipt index is invalid")
        digest_fields = ["event_stream_sha256", "executor_policy_sha256"]
        if schema_version == "taskwise_executor_private_success_v2":
            digest_fields.append("codex_executable_sha256")
        if any(
            type(payload[key]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", str(payload[key])) is None
            for key in digest_fields
        ):
            raise ValueError("taskwise executor success receipt digest is invalid")
        if (
            isinstance(payload["event_count"], bool)
            or not isinstance(payload["event_count"], int)
            or payload["event_count"] <= 0
            or payload["tool_event_count"] != 0
            or payload["completion_observed"] is not True
            or payload["process_return_code"] != 0
            or payload["process_signal"] is not None
            or payload["cleanup_status"] != "COMPLETE"
            or payload["residual_root_count"] != 0
        ):
            raise ValueError("taskwise executor success receipt outcome is invalid")
        if any(
            type(payload[key]) is not str or not payload[key]
            for key in ("codex_cli_version", "model", "created_at_utc")
        ):
            raise ValueError("taskwise executor success receipt metadata is invalid")
        try:
            created_at = datetime.fromisoformat(str(payload["created_at_utc"]))
        except ValueError as exc:
            raise ValueError("taskwise executor success receipt timestamp is invalid") from exc
        if created_at.tzinfo is None:
            raise ValueError("taskwise executor success receipt timestamp is invalid")
        return cls(
            run_id=payload["run_id"],  # type: ignore[arg-type]
            task_uid=payload["task_uid"],  # type: ignore[arg-type]
            task_index=payload["task_index"],  # type: ignore[arg-type]
            round_index=payload["round_index"],  # type: ignore[arg-type]
            session_id=payload["session_id"],  # type: ignore[arg-type]
            event_stream_sha256=str(payload["event_stream_sha256"]),
            event_count=int(payload["event_count"]),
            tool_event_count=0,
            completion_observed=True,
            process_return_code=0,
            process_signal=None,
            cleanup_status="COMPLETE",
            residual_root_count=0,
            codex_cli_version=str(payload["codex_cli_version"]),
            model=str(payload["model"]),
            executor_policy_sha256=str(payload["executor_policy_sha256"]),
            created_at_utc=str(payload["created_at_utc"]),
            codex_executable_sha256=(
                None
                if schema_version == "taskwise_executor_private_success_v1"
                else str(payload["codex_executable_sha256"])
            ),
        )


CommandRunner = Callable[
    [Sequence[str], str | None, Path, Mapping[str, str], float],
    LocalCommandResult,
]


class LocalCodexExecutionErrorCode(str, Enum):
    """Closed local-execution failure vocabulary safe for controller logs."""

    INVALID_REQUEST = "invalid_request"
    CODEX_UNAVAILABLE = "codex_unavailable"
    CODEX_VERSION_MISMATCH = "codex_version_mismatch"
    SUBSCRIPTION_AUTH_UNAVAILABLE = "subscription_auth_unavailable"
    SUBSCRIPTION_AUTH_PERMISSIONS_UNSAFE = "subscription_auth_permissions_unsafe"
    PROMPT_AUDIT_FAILED = "prompt_audit_failed"
    CLI_TIMEOUT = "cli_timeout"
    CLI_FAILED = "cli_failed"
    TRANSCRIPT_INVALID = "transcript_invalid"
    DISALLOWED_TOOL_EVENT = "disallowed_tool_event"
    SECURITY_TOOL_USE_VIOLATION = "security_tool_use_violation"
    RESPONSE_MISSING = "response_missing"
    ISOLATION_SETUP_FAILED = "isolation_setup_failed"
    CLEANUP_FAILED = "cleanup_failed"
    DISALLOWED_PLUGIN_ACTIVITY = "disallowed_plugin_activity"
    PROCESS_TERMINATION_FAILED = "process_termination_failed"


def _default_taskwise_failure_code(code: LocalCodexExecutionErrorCode) -> str:
    mapping = {
        LocalCodexExecutionErrorCode.INVALID_REQUEST: "EXECUTOR_PREFLIGHT_FAILED",
        LocalCodexExecutionErrorCode.CODEX_UNAVAILABLE: "EXECUTOR_PREFLIGHT_FAILED",
        LocalCodexExecutionErrorCode.CODEX_VERSION_MISMATCH: "EXECUTOR_PREFLIGHT_FAILED",
        LocalCodexExecutionErrorCode.SUBSCRIPTION_AUTH_UNAVAILABLE: (
            "EXECUTOR_AUTH_MATERIALIZATION_FAILED"
        ),
        LocalCodexExecutionErrorCode.SUBSCRIPTION_AUTH_PERMISSIONS_UNSAFE: (
            "EXECUTOR_AUTH_MATERIALIZATION_FAILED"
        ),
        LocalCodexExecutionErrorCode.PROMPT_AUDIT_FAILED: "EXECUTOR_PREFLIGHT_FAILED",
        LocalCodexExecutionErrorCode.CLI_TIMEOUT: "EXECUTOR_TIMEOUT",
        LocalCodexExecutionErrorCode.CLI_FAILED: "EXECUTOR_NONZERO_EXIT",
        LocalCodexExecutionErrorCode.TRANSCRIPT_INVALID: "EXECUTOR_EVENT_STREAM_INVALID",
        LocalCodexExecutionErrorCode.DISALLOWED_TOOL_EVENT: (
            "EXECUTOR_SECURITY_TOOL_USE_VIOLATION"
        ),
        LocalCodexExecutionErrorCode.SECURITY_TOOL_USE_VIOLATION: (
            "EXECUTOR_SECURITY_TOOL_USE_VIOLATION"
        ),
        LocalCodexExecutionErrorCode.RESPONSE_MISSING: "EXECUTOR_OUTPUT_MISSING",
        LocalCodexExecutionErrorCode.ISOLATION_SETUP_FAILED: "EXECUTOR_PREFLIGHT_FAILED",
        LocalCodexExecutionErrorCode.CLEANUP_FAILED: "EXECUTOR_CLEANUP_FAILED",
        LocalCodexExecutionErrorCode.DISALLOWED_PLUGIN_ACTIVITY: (
            "EXECUTOR_SECURITY_TOOL_USE_VIOLATION"
        ),
        LocalCodexExecutionErrorCode.PROCESS_TERMINATION_FAILED: ("EXECUTOR_CLEANUP_FAILED"),
    }
    return mapping[code]


class _ProcessTerminationError(RuntimeError):
    """Internal marker for a process group that did not reach quiescence."""


class LocalCodexExecutionError(RuntimeError):
    """Sanitized error that never embeds prompt, response, transcript, or stderr."""

    __slots__ = (
        "code",
        "completion_observed",
        "diagnostic_receipt",
        "executor_stage",
        "event_counts",
        "event_digest",
        "private_event_reference",
        "replacement_completion_allowed",
        "resume_allowed",
        "retry_allowed",
        "run_status",
        "taskwise_failure_code",
        "timed_out",
    )

    def __init__(
        self,
        code: LocalCodexExecutionErrorCode,
        *,
        diagnostic_receipt: str | None = None,
        event_counts: Mapping[str, int] | None = None,
        event_digest: str | None = None,
        private_event_reference: str | None = None,
        taskwise_failure_code: str | None = None,
        executor_stage: str | None = None,
        completion_observed: bool = False,
        timed_out: bool = False,
    ) -> None:
        if type(code) is not LocalCodexExecutionErrorCode:
            raise TypeError("LocalCodexExecutionError.code must be LocalCodexExecutionErrorCode")
        if diagnostic_receipt is not None and type(diagnostic_receipt) is not str:
            raise TypeError("diagnostic_receipt must be a string or None")
        if event_counts is None:
            normalized_event_counts: dict[str, int] = {}
        else:
            normalized_event_counts = {}
            for category, count in event_counts.items():
                if category not in _SECURITY_EVENT_CATEGORIES:
                    raise ValueError("event_counts contains an unknown category")
                if type(count) is not int or count <= 0:
                    raise ValueError("event_counts values must be positive integers")
                normalized_event_counts[category] = count
        if event_digest is not None and (
            type(event_digest) is not str or re.fullmatch(r"[0-9a-f]{64}", event_digest) is None
        ):
            raise ValueError("event_digest must be a lowercase sha256 or None")
        if private_event_reference is not None and (
            type(private_event_reference) is not str or not private_event_reference
        ):
            raise TypeError("private_event_reference must be a non-empty string or None")
        resolved_failure_code = (
            _default_taskwise_failure_code(code)
            if taskwise_failure_code is None
            else taskwise_failure_code
        )
        if resolved_failure_code not in _TASKWISE_EXECUTOR_FAILURE_CODES:
            raise ValueError("taskwise_failure_code is outside the closed vocabulary")
        if executor_stage is not None and executor_stage not in _EXECUTOR_STAGES:
            raise ValueError("executor_stage is outside the closed vocabulary")
        if type(completion_observed) is not bool or type(timed_out) is not bool:
            raise TypeError("completion_observed and timed_out must be booleans")
        self.code = code
        self.taskwise_failure_code = resolved_failure_code
        self.executor_stage = executor_stage
        self.completion_observed = completion_observed
        self.timed_out = timed_out
        self.diagnostic_receipt = diagnostic_receipt
        self.event_counts = dict(sorted(normalized_event_counts.items()))
        self.event_digest = event_digest
        self.private_event_reference = private_event_reference
        is_security_violation = resolved_failure_code == "EXECUTOR_SECURITY_TOOL_USE_VIOLATION"
        self.run_status = _SECURITY_TOOL_USE_VIOLATION if is_security_violation else None
        infrastructure_retry_safe = (
            not is_security_violation
            and not completion_observed
            and resolved_failure_code
            in {
                "EXECUTOR_PREFLIGHT_FAILED",
                "EXECUTOR_AUTH_MATERIALIZATION_FAILED",
                "EXECUTOR_PROCESS_SPAWN_FAILED",
                "EXECUTOR_CODEX_STARTUP_FAILED",
                "EXECUTOR_MODEL_TRANSPORT_FAILED",
                "EXECUTOR_TIMEOUT",
                "EXECUTOR_NONZERO_EXIT",
            }
        )
        self.retry_allowed = infrastructure_retry_safe
        self.resume_allowed = infrastructure_retry_safe
        self.replacement_completion_allowed = infrastructure_retry_safe
        super().__init__(f"local Codex execution failed: error_type={code.value}")

    def to_log_fields(self) -> dict[str, object]:
        """Return a public-safe receipt; raw event payloads are never included."""

        fields: dict[str, object] = {
            "error_type": self.taskwise_failure_code,
            "executor_stage": self.executor_stage,
            "completion_observed": self.completion_observed,
            "timed_out": self.timed_out,
            "retry_allowed": self.retry_allowed,
            "resume_allowed": self.resume_allowed,
            "replacement_completion_allowed": self.replacement_completion_allowed,
        }
        if self.diagnostic_receipt is not None:
            fields["diagnostic_receipt"] = self.diagnostic_receipt
        if self.run_status is not None:
            fields.update(
                {
                    "run_status": self.run_status,
                    "retry_allowed": self.retry_allowed,
                    "resume_allowed": self.resume_allowed,
                    "replacement_completion_allowed": (self.replacement_completion_allowed),
                    "event_counts": dict(self.event_counts),
                    "event_types": sorted(self.event_counts),
                    "event_digest": self.event_digest,
                }
            )
        return fields


class LocalCodexCLIExecutor:
    """Execute one public round through an isolated ChatGPT-authenticated CLI."""

    __slots__ = (
        "_auth_file",
        "_closed",
        "_codex_executable",
        "_codex_executable_sha256",
        "_codex_version",
        "_command_runner",
        "_config",
        "_diagnostic_root",
        "_executor_policy_sha256",
        "_isolation_parent",
        "_model",
        "_pending_cleanup",
        "_reasoning_effort",
        "_security_violation",
        "_supervised_context_receipts",
        "_task_timeout_seconds",
        "_taskwise_context_receipts",
        "_taskwise_mode",
        "_taskwise_session_ids",
        "_v2_mode",
    )

    def __init__(
        self,
        *,
        config: ExperimentConfig | FrozenExperimentConfigV2 | TaskwiseExperimentConfigV1,
        task_timeout_seconds: float = 600.0,
        codex_executable: Path | None = None,
        auth_file: Path | None = None,
        command_runner: CommandRunner | None = None,
        verified_codex_version: str | None = None,
        isolation_parent: Path | None = None,
        diagnostic_root: Path | None = None,
    ) -> None:
        if (
            isinstance(task_timeout_seconds, bool)
            or not isinstance(task_timeout_seconds, (int, float))
            or float(task_timeout_seconds) <= 0
        ):
            raise ValueError("task_timeout_seconds must be positive")
        if type(config) is ExperimentConfig:
            if config.execution_backend != "local_codex_cli":
                raise ValueError(
                    "LocalCodexCLIExecutor requires execution_backend=local_codex_cli"
                )
            if config.agent.harness != "codex_cli":
                raise ValueError("LocalCodexCLIExecutor requires harness=codex_cli")
            if any(
                (
                    config.runtime.network_enabled,
                    config.runtime.network_tools_enabled,
                    config.runtime.browser_enabled,
                    config.runtime.external_web_enabled,
                    bool(config.runtime.mcp_servers),
                )
            ):
                raise ValueError("local Codex tool and agent networking must remain disabled")
            model = config.agent.model
            expected_codex_version = config.codex_cli_version
            reasoning_effort = None
            v2_mode = False
            taskwise_mode = False
        elif type(config) is FrozenExperimentConfigV2:
            model = config.model
            expected_codex_version = config.codex_cli_version
            reasoning_effort = config.reasoning_effort
            v2_mode = True
            taskwise_mode = False
            if float(task_timeout_seconds) != float(config.executor.timeout_seconds):
                raise ValueError("task_timeout_seconds must match the frozen v2 executor policy")
        elif type(config) is TaskwiseExperimentConfigV1:
            model = config.model
            expected_codex_version = config.codex_cli_version
            reasoning_effort = config.reasoning_effort
            v2_mode = False
            taskwise_mode = True
            if float(task_timeout_seconds) != float(config.executor.timeout_seconds):
                raise ValueError("task_timeout_seconds must match the taskwise executor policy")
        else:
            raise TypeError(
                "config must be exact ExperimentConfig, FrozenExperimentConfigV2, "
                "or TaskwiseExperimentConfigV1"
            )
        resolved_executable = codex_executable
        if resolved_executable is None:
            discovered = shutil.which("codex")
            if discovered is None:
                raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.CODEX_UNAVAILABLE)
            resolved_executable = Path(discovered)
        if not isinstance(resolved_executable, Path):
            raise TypeError("codex_executable must be pathlib.Path or None")
        if not resolved_executable.is_absolute():
            raise ValueError("codex_executable must be absolute")

        resolved_auth = auth_file if auth_file is not None else _default_auth_file()
        if not isinstance(resolved_auth, Path):
            raise TypeError("auth_file must be pathlib.Path or None")
        _validate_auth_file(resolved_auth)

        self._config = config
        self._model = model
        self._executor_policy_sha256 = _executor_policy_sha256(config)
        self._reasoning_effort = reasoning_effort
        self._v2_mode = v2_mode
        self._taskwise_mode = taskwise_mode
        self._taskwise_context_receipts: dict[
            str,
            TaskwiseContextBindingReceiptV1,
        ] = {}
        self._supervised_context_receipts: dict[
            str,
            SupervisedContextBindingReceiptV1,
        ] = {}
        self._taskwise_session_ids: set[str] = set()
        self._task_timeout_seconds = float(task_timeout_seconds)
        self._codex_executable = resolved_executable
        try:
            self._codex_executable_sha256 = _sha256_regular_file(resolved_executable)
        except OSError:
            # Unit tests and research harness simulators historically inject both a
            # command runner and a verified version while using a sentinel binary
            # path. Preserve that non-production seam and its v1 receipt. Real
            # execution (command_runner=None), including supervised transfer, must
            # bind an existing regular executable and emits the digest-bound v2
            # receipt below.
            if command_runner is None or verified_codex_version is None:
                raise LocalCodexExecutionError(
                    LocalCodexExecutionErrorCode.CODEX_UNAVAILABLE
                ) from None
            self._codex_executable_sha256 = None
        self._auth_file = resolved_auth
        self._command_runner = _run_local_command if command_runner is None else command_runner
        resolved_isolation_parent = (
            Path(tempfile.gettempdir()) if isolation_parent is None else isolation_parent
        )
        if not isinstance(resolved_isolation_parent, Path):
            raise TypeError("isolation_parent must be pathlib.Path or None")
        if not resolved_isolation_parent.is_dir() or resolved_isolation_parent.is_symlink():
            raise ValueError("isolation_parent must be an existing real directory")
        resolved_diagnostic_root = (
            resolved_isolation_parent / _DIAGNOSTIC_DIRECTORY_NAME
            if diagnostic_root is None
            else diagnostic_root
        )
        if not isinstance(resolved_diagnostic_root, Path):
            raise TypeError("diagnostic_root must be pathlib.Path or None")
        if resolved_diagnostic_root == resolved_isolation_parent:
            raise ValueError("diagnostic_root must not equal isolation_parent")
        if resolved_diagnostic_root.exists() and (
            not resolved_diagnostic_root.is_dir() or resolved_diagnostic_root.is_symlink()
        ):
            raise ValueError("diagnostic_root must be a real directory")
        self._isolation_parent = resolved_isolation_parent.resolve()
        self._diagnostic_root = resolved_diagnostic_root
        self._pending_cleanup: set[Path] = set()
        self._security_violation: LocalCodexExecutionError | None = None
        self._closed = False

        if verified_codex_version is None:
            result = self._command_runner(
                (os.fspath(self._codex_executable), "--version"),
                None,
                Path.cwd(),
                _base_environment(),
                10.0,
            )
            parsed_version = _parse_codex_version(result)
        else:
            if type(verified_codex_version) is not str:
                raise TypeError("verified_codex_version must be string or None")
            parsed_version = verified_codex_version
        if parsed_version != expected_codex_version:
            raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.CODEX_VERSION_MISMATCH)
        self._codex_version = parsed_version

    def __enter__(self) -> LocalCodexCLIExecutor:
        if self._closed:
            raise RuntimeError("LocalCodexCLIExecutor is closed")
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        pending_outcome = self._drain_pending_cleanup()
        diagnostic_reference: str | None = None
        if pending_outcome is not None and not pending_outcome.complete:
            diagnostic_reference = self._write_diagnostic_receipt(
                phase="CLEANUP",
                error_code=LocalCodexExecutionErrorCode.CLEANUP_FAILED,
                result=None,
                error_errno=pending_outcome.last_errno,
                plugin_activity=pending_outcome.disallowed_activity_seen,
                cleanup_outcome=pending_outcome,
                timed_out=False,
            )
        self._closed = True
        if pending_outcome is not None and not pending_outcome.complete:
            raise LocalCodexExecutionError(
                LocalCodexExecutionErrorCode.CLEANUP_FAILED,
                diagnostic_receipt=diagnostic_reference,
            )

    @property
    def run_status(self) -> str | None:
        """Expose the terminal executor security state without event payloads."""

        if self._security_violation is None:
            return None
        return _SECURITY_TOOL_USE_VIOLATION

    def build_prompt(self, request: AgentRoundRequest) -> str:
        """Compile only public prompt data and resolver-issued artifact contexts."""

        if type(request) is not AgentRoundRequest:
            raise TypeError("LocalCodexCLIExecutor.build_prompt requires AgentRoundRequest")
        try:
            _audit_runtime_payload(request.to_runtime_payload())
            prompt = _compile_prompt(request)
            _audit_compiled_prompt(prompt)
        except (TypeError, ValueError):
            raise LocalCodexExecutionError(
                LocalCodexExecutionErrorCode.PROMPT_AUDIT_FAILED
            ) from None
        return prompt

    def execute(self, request: AgentRoundRequest) -> RawAttempt:
        """Call ``codex exec`` exactly once; failures never fall back."""

        if self._v2_mode or self._taskwise_mode:
            raise TypeError("formal executor mode requires its strong execution method")
        if type(request) is not AgentRoundRequest:
            raise TypeError("LocalCodexCLIExecutor.execute requires exact AgentRoundRequest")
        return self._execute_prompt_once(self.build_prompt(request))

    def build_frozen_prompt(self, request: FrozenAgentRequestV2) -> str:
        """Prepend policy/memory without rewriting the official rendered prompt."""

        if not self._v2_mode:
            raise TypeError("build_frozen_prompt requires frozen v2 executor mode")
        if type(request) is not FrozenAgentRequestV2:
            raise TypeError("build_frozen_prompt requires exact FrozenAgentRequestV2")
        _audit_runtime_payload(request.to_runtime_payload())
        config = self._config
        if type(config) is not FrozenExperimentConfigV2:
            raise AssertionError("v2 mode config type changed")
        memory = request.resolved_text_memory
        if config.arm == "baseline":
            if memory is not None:
                raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.PROMPT_AUDIT_FAILED)
        elif memory is None:
            raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.PROMPT_AUDIT_FAILED)
        else:
            expected = config.artifact
            if (
                memory.core_artifact_id != expected.frozen_artifact_id
                or memory.artifact_payload_sha256 != expected.frozen_artifact_sha256
                or memory.context_resolution_digest != expected.context_resolution_digest
                or memory.resolved_memory_sha256 != expected.resolved_memory_sha256
            ):
                raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.PROMPT_AUDIT_FAILED)
        sections = [
            (
                "Runtime policy:\n"
                "- Solve only from the supplied public prompt and frozen "
                "Core-resolved text memory.\n"
                "- Do not use shell, commands, files, network, web, browser, "
                "MCP, plugins, apps, subagents, or external tools.\n"
                "- Reply exactly as required by the public prompt."
            )
        ]
        if memory is not None:
            sections.append("Frozen Core-resolved text memory:\n" + memory.markdown)
        sections.append(request.rendered_public_prompt)
        prompt = "\n\n".join(sections)
        if not prompt.endswith(request.rendered_public_prompt):
            raise AssertionError("official rendered prompt was not preserved")
        return prompt

    def execute_frozen(self, request: FrozenAgentRequestV2) -> RawAttempt:
        """Execute one frozen v2 item; no legacy artifact context is accepted."""

        if not self._v2_mode:
            raise TypeError("execute_frozen requires frozen v2 executor mode")
        if type(request) is not FrozenAgentRequestV2:
            raise TypeError("execute_frozen requires exact FrozenAgentRequestV2")
        return self._execute_prompt_once(self.build_frozen_prompt(request))

    def build_taskwise_prompt(self, request: TaskwiseAgentRequestV1) -> str:
        """Compile one new-session online/control request without runtime IDs."""

        if not self._taskwise_mode:
            raise TypeError("build_taskwise_prompt requires taskwise executor mode")
        if type(request) is not TaskwiseAgentRequestV1:
            raise TypeError("build_taskwise_prompt requires exact TaskwiseAgentRequestV1")
        config = self._config
        if type(config) is not TaskwiseExperimentConfigV1:
            raise AssertionError("taskwise executor config type changed")
        if request.arm != config.arm:
            raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.PROMPT_AUDIT_FAILED)
        memory = request.resolved_text_memory
        if config.arm == "control":
            if memory is not None:
                raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.PROMPT_AUDIT_FAILED)
        elif request.task_ordinal == 0 and request.round_index == 0:
            if memory is not None:
                raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.PROMPT_AUDIT_FAILED)
        elif type(memory) is not CoreResolvedTextMemoryV2:
            raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.PROMPT_AUDIT_FAILED)

        projected = request.to_frozen_agent_request()
        _audit_runtime_payload(projected.to_runtime_payload())
        sections = [
            (
                "Runtime policy:\n"
                "- Solve only from the supplied public prompt and, when present, "
                "the approved Core-resolved text memory.\n"
                "- Do not use shell, commands, files, network, web, browser, "
                "MCP, plugins, apps, subagents, or external tools.\n"
                "- Reply exactly as required by the public prompt."
            )
        ]
        if memory is not None:
            sections.append("Approved Core-resolved text memory:\n" + memory.markdown)
        sections.append(request.rendered_public_prompt)
        prompt = "\n\n".join(sections)
        if not prompt.endswith(request.rendered_public_prompt):
            raise AssertionError("official rendered prompt was not preserved")
        _audit_compiled_prompt(prompt)
        return prompt

    def execute_taskwise(self, request: TaskwiseAgentRequestV1) -> RawAttempt:
        """Execute one immutable taskwise session exactly once."""

        if not self._taskwise_mode:
            raise TypeError("execute_taskwise requires taskwise executor mode")
        if type(request) is not TaskwiseAgentRequestV1:
            raise TypeError("execute_taskwise requires exact TaskwiseAgentRequestV1")
        if request.session_id in self._taskwise_session_ids:
            raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.PROMPT_AUDIT_FAILED)
        self._taskwise_session_ids.add(request.session_id)
        prompt = self.build_taskwise_prompt(request)
        expected = TaskwiseSessionContextBindingV1.from_memory(
            session_id=request.session_id,
            memory=request.resolved_text_memory,
        )
        # ``actual`` is deliberately derived at the final prompt-execution
        # boundary, separately from the runner's expected view.
        memory = request.resolved_text_memory
        actual = TaskwiseSessionContextBindingV1.from_injected_markdown(
            session_id=request.session_id,
            resolved_artifact_id=(None if memory is None else memory.core_artifact_id),
            artifact_payload_sha256=(None if memory is None else memory.artifact_payload_sha256),
            context_resolution_digest=(
                None if memory is None else memory.context_resolution_digest
            ),
            injected_markdown=None if memory is None else memory.markdown,
        )
        receipt = issue_taskwise_context_binding_receipt_v1(
            expected=expected,
            actual=actual,
        )
        receipt.require_match()
        attempt = self._execute_prompt_once(
            prompt,
            diagnostic_context=_InvocationDiagnosticContext(
                run_id=getattr(request, "run_id", None),
                task_uid=getattr(request, "task_uid", None),
                task_index=request.task_ordinal,
                round_index=request.round_index,
                session_id=request.session_id,
            ),
        )
        if request.session_id in self._taskwise_context_receipts:
            raise RuntimeError("taskwise context receipt was issued twice")
        self._taskwise_context_receipts[request.session_id] = receipt
        return attempt

    def consume_taskwise_context_receipt(
        self,
        session_id: str,
    ) -> TaskwiseContextBindingReceiptV1:
        """Return the one-shot receipt for one completed taskwise session."""

        if type(session_id) is not str:
            raise TypeError("session_id must be a string")
        try:
            return self._taskwise_context_receipts.pop(session_id)
        except KeyError as exc:
            raise RuntimeError("taskwise context receipt is unavailable") from exc

    def build_supervised_prompt(self, request: SupervisedAgentRequestV1) -> str:
        """Compile one supervised session with either zero or all three targets."""

        if not self._taskwise_mode:
            raise TypeError("build_supervised_prompt requires taskwise executor mode")
        if type(request) is not SupervisedAgentRequestV1:
            raise TypeError("build_supervised_prompt requires exact request type")
        config = self._config
        if type(config) is not TaskwiseExperimentConfigV1:
            raise AssertionError("taskwise executor config type changed")
        if request.arm != config.arm:
            raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.PROMPT_AUDIT_FAILED)
        if config.arm == "control" and request.resolved_context is not None:
            raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.PROMPT_AUDIT_FAILED)
        _audit_runtime_payload(request.to_runtime_payload())
        sections = [
            (
                "Runtime policy:\n"
                "- Solve only from the supplied public prompt and, when present, "
                "the approved Core-resolved category context.\n"
                "- Do not use shell, commands, files, network, web, browser, "
                "MCP, plugins, apps, subagents, or external tools.\n"
                "- Reply exactly as required by the public prompt."
            )
        ]
        context = request.resolved_context
        if context is not None:
            sections.extend(
                (
                    "Approved Core-resolved category memory:\n" + context.memory.markdown,
                    "Approved Core-resolved category skill:\n" + context.skill.markdown,
                    (
                        "Approved Core-resolved category agent system:\n"
                        + context.agent_system.markdown
                    ),
                )
            )
        sections.append(request.rendered_public_prompt)
        prompt = "\n\n".join(sections)
        if not prompt.endswith(request.rendered_public_prompt):
            raise AssertionError("official rendered prompt was not preserved")
        _audit_compiled_prompt(prompt)
        return prompt

    def execute_supervised(self, request: SupervisedAgentRequestV1) -> RawAttempt:
        """Execute one immutable supervised session and bind all target bytes."""

        if not self._taskwise_mode:
            raise TypeError("execute_supervised requires taskwise executor mode")
        if type(request) is not SupervisedAgentRequestV1:
            raise TypeError("execute_supervised requires exact request type")
        if request.session_id in self._taskwise_session_ids:
            raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.PROMPT_AUDIT_FAILED)
        self._taskwise_session_ids.add(request.session_id)
        prompt = self.build_supervised_prompt(request)
        expected = SupervisedSessionContextBindingV1.from_context(
            session_id=request.session_id,
            context=request.resolved_context,
        )
        actual = SupervisedSessionContextBindingV1.from_injected_context(
            session_id=request.session_id,
            context=request.resolved_context,
        )
        receipt = issue_supervised_context_binding_receipt_v1(
            expected=expected,
            actual=actual,
        )
        receipt.require_match()
        attempt = self._execute_prompt_once(
            prompt,
            diagnostic_context=_InvocationDiagnosticContext(
                run_id=request.run_id,
                task_uid=request.task_uid,
                task_index=request.task_ordinal,
                round_index=request.round_index,
                session_id=request.session_id,
            ),
        )
        if request.session_id in self._supervised_context_receipts:
            raise RuntimeError("supervised context receipt was issued twice")
        self._supervised_context_receipts[request.session_id] = receipt
        return attempt

    def consume_supervised_context_receipt(
        self,
        session_id: str,
    ) -> SupervisedContextBindingReceiptV1:
        """Return the one-shot three-target receipt for a completed session."""

        if type(session_id) is not str:
            raise TypeError("session ID must be a string")
        try:
            return self._supervised_context_receipts.pop(session_id)
        except KeyError as exc:
            raise RuntimeError("supervised context receipt is unavailable") from exc

    def _execute_prompt_once(
        self,
        prompt: str,
        *,
        diagnostic_context: _InvocationDiagnosticContext | None = None,
    ) -> RawAttempt:
        if type(prompt) is not str or not prompt:
            raise TypeError("prompt must be non-empty text")
        if self._closed:
            raise RuntimeError("LocalCodexCLIExecutor is closed")
        if self._security_violation is not None:
            raise _copy_security_violation(self._security_violation)
        pending_outcome = self._drain_pending_cleanup()
        if pending_outcome is not None and not pending_outcome.complete:
            diagnostic_reference = self._write_diagnostic_receipt(
                phase="CLEANUP",
                error_code=LocalCodexExecutionErrorCode.CLEANUP_FAILED,
                result=None,
                error_errno=pending_outcome.last_errno,
                plugin_activity=pending_outcome.disallowed_activity_seen,
                cleanup_outcome=pending_outcome,
                timed_out=False,
            )
            raise LocalCodexExecutionError(
                LocalCodexExecutionErrorCode.CLEANUP_FAILED,
                diagnostic_receipt=diagnostic_reference,
            )
        started_at = datetime.now(UTC).isoformat(timespec="milliseconds")
        started = time.monotonic()
        result: LocalCommandResult | None = None
        response: str | None = None
        usage: dict[str, int] | None = None
        transcript_digest: str | None = None
        failure: LocalCodexExecutionError | None = None
        unexpected: BaseException | None = None
        failure_phase = "INVOCATION_ROOT_CREATION"
        failure_errno: int | None = None
        command: tuple[str, ...] | None = None
        isolation_root: Path | None = None
        invocation_root_id: str | None = None
        isolated_auth: Path | None = None
        output_last_message_exists = False
        output_last_message_sha256: str | None = None
        output_last_message_invalid = False
        diagnostic_exception_class: str | None = None
        diagnostic_exception_message = ""
        plugin_activity = False
        cleanup_outcome = _CleanupOutcome(
            complete=True,
            auth_removed=True,
            disallowed_activity_seen=False,
            attempts=0,
            last_errno=None,
        )

        try:
            layout = _create_invocation_layout(self._isolation_parent)
            isolation_root = layout.root
            invocation_root_id = layout.root.name
            failure_phase = "AUTH_MATERIALIZATION"
            isolated_auth = layout.codex_home / "auth.json"
            shutil.copyfile(self._auth_file, isolated_auth)
            isolated_auth.chmod(0o600)

            environment = _sanitized_execution_environment()
            environment.update(
                {
                    "CODEX_HOME": os.fspath(layout.codex_home),
                    "CODEX_SQLITE_HOME": os.fspath(layout.sqlite),
                    "HOME": os.fspath(layout.home),
                    "TEMP": os.fspath(layout.tmp),
                    "TMP": os.fspath(layout.tmp),
                    "TMPDIR": os.fspath(layout.tmp),
                    "XDG_CACHE_HOME": os.fspath(layout.xdg_cache),
                    "XDG_CONFIG_HOME": os.fspath(layout.xdg_config),
                    "XDG_STATE_HOME": os.fspath(layout.xdg_state),
                }
            )
            failure_phase = "PRE_INVOCATION_ATTESTATION"
            _attest_invocation(layout, environment=environment)
            failure_phase = "CODEX_CONFIG_GENERATION"
            command = _codex_command(
                executable=self._codex_executable,
                model=self._model,
                reasoning_effort=self._reasoning_effort,
                workspace=layout.work,
                output_last_message=layout.output_last_message,
            )
            failure_phase = "PROCESS_SPAWN"
            result = self._command_runner(
                command,
                prompt,
                layout.work,
                environment,
                self._task_timeout_seconds,
            )
            if type(result) is not LocalCommandResult:
                raise TypeError("command runner must return LocalCommandResult")
            if result.output_last_message_exists is not None:
                output_last_message_exists = result.output_last_message_exists
                output_last_message_invalid = result.output_last_message_invalid
                if result.output_last_message is not None:
                    output_last_message_sha256 = hashlib.sha256(
                        result.output_last_message.encode("utf-8")
                    ).hexdigest()
            failure_phase = "PRIVATE_EVENT_PERSISTENCE"
            _write_invocation_event_log(
                layout.private_events,
                result.stdout,
            )
            failure_phase = "EVENT_STREAM_PARSE"
            security_violation = _find_security_tool_use(result.stdout)
            if security_violation is not None:
                raise security_violation
            if result.returncode != 0:
                taskwise_code, classified_stage = _classify_nonzero_result(result)
                failure_phase = classified_stage
                failure = LocalCodexExecutionError(
                    LocalCodexExecutionErrorCode.CLI_FAILED,
                    taskwise_failure_code=taskwise_code,
                    executor_stage=classified_stage,
                    completion_observed=_completion_observed(result.stdout),
                )
            else:
                failure_phase = "EVENT_STREAM_PARSE"
                response, usage, transcript_digest = _parse_jsonl_transcript(result.stdout)
                failure_phase = "OUTPUT_LAST_MESSAGE_READ"
                if result.output_last_message_exists is not None:
                    output_last_message_exists = result.output_last_message_exists
                    output_last_message_invalid = result.output_last_message_invalid
                    if output_last_message_invalid:
                        failure = LocalCodexExecutionError(
                            LocalCodexExecutionErrorCode.TRANSCRIPT_INVALID,
                            taskwise_failure_code="EXECUTOR_OUTPUT_INVALID",
                            executor_stage="COMPLETION_VALIDATION",
                            completion_observed=True,
                        )
                    elif not output_last_message_exists or result.output_last_message is None:
                        failure = LocalCodexExecutionError(
                            LocalCodexExecutionErrorCode.RESPONSE_MISSING,
                            taskwise_failure_code="EXECUTOR_OUTPUT_MISSING",
                            executor_stage=failure_phase,
                            completion_observed=True,
                        )
                    else:
                        failure_phase = "COMPLETION_VALIDATION"
                        if result.output_last_message.strip() != response.strip():
                            failure = LocalCodexExecutionError(
                                LocalCodexExecutionErrorCode.TRANSCRIPT_INVALID,
                                taskwise_failure_code="EXECUTOR_OUTPUT_INVALID",
                                executor_stage=failure_phase,
                                completion_observed=True,
                            )
        except subprocess.TimeoutExpired as exc:
            diagnostic_exception_class = type(exc).__name__
            diagnostic_exception_message = "executor command timed out"
            result = LocalCommandResult(
                returncode=-signal.SIGKILL,
                stdout=_subprocess_stream_text(exc.output),
                stderr=_subprocess_stream_text(exc.stderr),
            )
            failure = LocalCodexExecutionError(
                LocalCodexExecutionErrorCode.CLI_TIMEOUT,
                executor_stage=("MODEL_TRANSPORT"),
                completion_observed=_completion_observed(result.stdout),
                timed_out=True,
            )
        except LocalCodexExecutionError as exc:
            diagnostic_exception_class = type(exc).__name__
            diagnostic_exception_message = "closed executor failure"
            if exc.executor_stage is None:
                exc.executor_stage = failure_phase
            if result is not None and _completion_observed(result.stdout):
                exc.completion_observed = True
                exc.retry_allowed = False
                exc.resume_allowed = False
                exc.replacement_completion_allowed = False
            if (
                exc.code is LocalCodexExecutionErrorCode.SECURITY_TOOL_USE_VIOLATION
                and result is not None
            ):
                exc.private_event_reference = _persist_private_event_log(
                    self._diagnostic_root,
                    result.stdout,
                    expected_digest=exc.event_digest,
                )
            failure = exc
        except OSError as exc:
            diagnostic_exception_class = type(exc).__name__
            diagnostic_exception_message = "executor operating-system error"
            failure_errno = exc.errno
            taskwise_code = (
                "EXECUTOR_PROCESS_SPAWN_FAILED"
                if failure_phase == "PROCESS_SPAWN"
                else (
                    "EXECUTOR_AUTH_MATERIALIZATION_FAILED"
                    if failure_phase == "AUTH_MATERIALIZATION"
                    else "EXECUTOR_PREFLIGHT_FAILED"
                )
            )
            failure = LocalCodexExecutionError(
                (
                    LocalCodexExecutionErrorCode.CODEX_UNAVAILABLE
                    if failure_phase == "PROCESS_SPAWN"
                    else LocalCodexExecutionErrorCode.ISOLATION_SETUP_FAILED
                ),
                taskwise_failure_code=taskwise_code,
                executor_stage=failure_phase,
            )
        except _ProcessTerminationError as exc:
            diagnostic_exception_class = type(exc).__name__
            diagnostic_exception_message = "executor process group did not terminate"
            failure = LocalCodexExecutionError(
                LocalCodexExecutionErrorCode.PROCESS_TERMINATION_FAILED,
                executor_stage="PROCESS_GROUP_SHUTDOWN",
            )
        except BaseException as exc:
            unexpected = exc
            diagnostic_exception_class = type(exc).__name__
            diagnostic_exception_message = "unexpected executor internal error"
        finally:
            if isolation_root is not None:
                plugin_activity = _has_disallowed_plugin_activity(isolation_root)
                cleanup_outcome = _cleanup_isolation_root(
                    isolation_root,
                    isolated_auth=isolated_auth,
                    expected_parent=self._isolation_parent,
                )
                plugin_activity = plugin_activity or cleanup_outcome.disallowed_activity_seen
                if not cleanup_outcome.complete:
                    self._pending_cleanup.add(isolation_root)

        if plugin_activity:
            if (
                failure is None
                or failure.taskwise_failure_code != "EXECUTOR_SECURITY_TOOL_USE_VIOLATION"
            ):
                failure_phase = "CLEANUP"
                failure = LocalCodexExecutionError(
                    LocalCodexExecutionErrorCode.DISALLOWED_PLUGIN_ACTIVITY,
                    executor_stage=failure_phase,
                    completion_observed=(
                        False if result is None else _completion_observed(result.stdout)
                    ),
                )
                diagnostic_exception_class = type(failure).__name__
                diagnostic_exception_message = "disallowed plugin activity detected"
                unexpected = None
        if not cleanup_outcome.complete and failure is None and unexpected is None:
            failure_phase = "CLEANUP"
            failure_errno = cleanup_outcome.last_errno
            failure = LocalCodexExecutionError(
                LocalCodexExecutionErrorCode.CLEANUP_FAILED,
                executor_stage=failure_phase,
                completion_observed=(
                    False if result is None else _completion_observed(result.stdout)
                ),
            )

        if unexpected is not None:
            failure_phase = "INTERNAL"
            failure = LocalCodexExecutionError(
                LocalCodexExecutionErrorCode.ISOLATION_SETUP_FAILED,
                taskwise_failure_code="EXECUTOR_INTERNAL_ERROR",
                executor_stage=failure_phase,
                completion_observed=(
                    False if result is None else _completion_observed(result.stdout)
                ),
            )
        if failure is not None and diagnostic_exception_class is None:
            diagnostic_exception_class = type(failure).__name__
            diagnostic_exception_message = "closed executor failure"

        diagnostic_reference: str | None = None
        if (
            failure is not None
            or plugin_activity
            or cleanup_outcome.attempts > 1
            or not cleanup_outcome.auth_removed
        ):
            diagnostic_reference = self._write_diagnostic_receipt(
                phase=failure_phase,
                error_code=None if failure is None else failure.code,
                result=result,
                error_errno=(
                    failure_errno if failure_errno is not None else cleanup_outcome.last_errno
                ),
                plugin_activity=plugin_activity,
                cleanup_outcome=cleanup_outcome,
                timed_out=(
                    failure is not None
                    and failure.code is LocalCodexExecutionErrorCode.CLI_TIMEOUT
                ),
                security_violation=(
                    failure
                    if failure is not None
                    and failure.code is LocalCodexExecutionErrorCode.SECURITY_TOOL_USE_VIOLATION
                    else None
                ),
                diagnostic_context=diagnostic_context,
                exception=unexpected,
                exception_class=diagnostic_exception_class,
                exception_message=diagnostic_exception_message,
                command=command,
                invocation_root_id=invocation_root_id,
                output_last_message_exists=output_last_message_exists,
                output_last_message_sha256=output_last_message_sha256,
                taskwise_failure_code=(None if failure is None else failure.taskwise_failure_code),
                completion_observed=(False if failure is None else failure.completion_observed),
                retry_allowed=False if failure is None else failure.retry_allowed,
                resume_allowed=False if failure is None else failure.resume_allowed,
                replacement_completion_allowed=(
                    False if failure is None else failure.replacement_completion_allowed
                ),
            )

        if failure is not None:
            if diagnostic_reference is None:
                if failure.taskwise_failure_code != "EXECUTOR_SECURITY_TOOL_USE_VIOLATION":
                    failure = LocalCodexExecutionError(
                        LocalCodexExecutionErrorCode.ISOLATION_SETUP_FAILED,
                        taskwise_failure_code="EXECUTOR_PRIVATE_DIAGNOSTICS_FAILED",
                        executor_stage="PRIVATE_EVENT_PERSISTENCE",
                        completion_observed=failure.completion_observed,
                    )
            if diagnostic_reference is not None:
                failure.diagnostic_receipt = diagnostic_reference
            if failure.taskwise_failure_code == "EXECUTOR_SECURITY_TOOL_USE_VIOLATION":
                self._security_violation = _copy_security_violation(failure)
            raise failure
        if response is None or usage is None or transcript_digest is None:
            raise AssertionError("successful local Codex execution is incomplete")
        if result is None or result.returncode != 0:
            raise AssertionError("successful local Codex process result is incomplete")
        event_summary = _event_stream_summary(result.stdout)
        context = diagnostic_context or _InvocationDiagnosticContext()
        success_receipt = TaskwiseExecutorSuccessReceiptV1(
            run_id=context.run_id,
            task_uid=context.task_uid,
            task_index=context.task_index,
            round_index=context.round_index,
            session_id=context.session_id,
            event_stream_sha256=transcript_digest,
            event_count=int(event_summary["event_count"]),
            tool_event_count=int(event_summary["tool_event_count"]),
            completion_observed=True,
            process_return_code=0,
            process_signal=None,
            cleanup_status="COMPLETE",
            residual_root_count=0,
            codex_cli_version=self._codex_version,
            model=self._model,
            executor_policy_sha256=self._executor_policy_sha256,
            created_at_utc=datetime.now(UTC).isoformat(timespec="milliseconds"),
            codex_executable_sha256=self._codex_executable_sha256,
        )
        try:
            success_reference = _persist_success_receipt(
                self._diagnostic_root,
                success_receipt,
            )
        except (TypeError, ValueError):
            success_reference = None
        if success_reference is None:
            persistence_failure = LocalCodexExecutionError(
                LocalCodexExecutionErrorCode.ISOLATION_SETUP_FAILED,
                taskwise_failure_code="EXECUTOR_PRIVATE_DIAGNOSTICS_FAILED",
                executor_stage="PRIVATE_EVENT_PERSISTENCE",
                completion_observed=True,
            )
            persistence_failure.diagnostic_receipt = self._write_diagnostic_receipt(
                phase="PRIVATE_EVENT_PERSISTENCE",
                error_code=persistence_failure.code,
                result=result,
                error_errno=None,
                plugin_activity=False,
                cleanup_outcome=cleanup_outcome,
                timed_out=False,
                diagnostic_context=diagnostic_context,
                exception_class=type(persistence_failure).__name__,
                exception_message="success receipt persistence failed",
                command=command,
                invocation_root_id=invocation_root_id,
                output_last_message_exists=output_last_message_exists,
                output_last_message_sha256=output_last_message_sha256,
                taskwise_failure_code=persistence_failure.taskwise_failure_code,
                completion_observed=True,
                retry_allowed=False,
                resume_allowed=False,
                replacement_completion_allowed=False,
            )
            raise persistence_failure
        duration_ms = (time.monotonic() - started) * 1000.0
        return RawAttempt(
            response=response,
            transcript_reference=TranscriptReference(
                reference=f"local-codex-jsonl:sha256:{transcript_digest}"
            ),
            runtime_metadata=AgentRuntimeMetadata(
                duration_ms=duration_ms,
                input_tokens=usage.get("input_tokens"),
                cached_input_tokens=usage.get("cached_input_tokens"),
                output_tokens=usage.get("output_tokens"),
                reasoning_output_tokens=usage.get("reasoning_output_tokens"),
                harness="codex_cli",
                model=self._model,
                capture_mode="transcript",
                execution_backend="local_codex_cli",
                codex_cli_version=self._codex_version,
                started_at_utc=started_at,
            ),
        )

    def _drain_pending_cleanup(self) -> _CleanupOutcome | None:
        outcomes: list[_CleanupOutcome] = []
        for root in tuple(self._pending_cleanup):
            outcome = _cleanup_isolation_root(
                root,
                isolated_auth=root / "codex_home" / "auth.json",
                expected_parent=self._isolation_parent,
            )
            outcomes.append(outcome)
            if outcome.complete:
                self._pending_cleanup.discard(root)
        if not outcomes:
            return None
        return _CleanupOutcome(
            complete=not self._pending_cleanup,
            auth_removed=all(outcome.auth_removed for outcome in outcomes),
            disallowed_activity_seen=any(outcome.disallowed_activity_seen for outcome in outcomes),
            attempts=sum(outcome.attempts for outcome in outcomes),
            last_errno=next(
                (
                    outcome.last_errno
                    for outcome in reversed(outcomes)
                    if outcome.last_errno is not None
                ),
                None,
            ),
        )

    def _write_diagnostic_receipt(
        self,
        *,
        phase: str,
        error_code: LocalCodexExecutionErrorCode | None,
        result: LocalCommandResult | None,
        error_errno: int | None,
        plugin_activity: bool,
        cleanup_outcome: _CleanupOutcome,
        timed_out: bool,
        security_violation: LocalCodexExecutionError | None = None,
        diagnostic_context: _InvocationDiagnosticContext | None = None,
        exception: BaseException | None = None,
        exception_class: str | None = None,
        exception_message: str = "",
        command: Sequence[str] | None = None,
        invocation_root_id: str | None = None,
        output_last_message_exists: bool = False,
        output_last_message_sha256: str | None = None,
        taskwise_failure_code: str | None = None,
        completion_observed: bool = False,
        retry_allowed: bool = False,
        resume_allowed: bool = False,
        replacement_completion_allowed: bool = False,
    ) -> str | None:
        findings: list[str] = []
        stderr = "" if result is None else result.stderr
        stderr_bytes = stderr.encode("utf-8", errors="replace")
        if stderr_bytes:
            findings.append("STDERR_CAPTURED")
        if result is not None and result.returncode != 0:
            findings.append("COMMAND_NONZERO")
        if timed_out:
            findings.append("COMMAND_TIMEOUT")
        if plugin_activity:
            findings.append("STARTUP_PLUGIN_ACTIVITY")
        if cleanup_outcome.attempts > 1:
            findings.append("CLEANUP_RETRY")
        if not cleanup_outcome.complete:
            findings.append("CLEANUP_FAILED")
        if not cleanup_outcome.auth_removed:
            findings.append("AUTH_COPY_REMAINS")
        if security_violation is not None:
            findings.append(
                "PRIVATE_EVENT_RETAINED"
                if security_violation.private_event_reference is not None
                else "PRIVATE_EVENT_RETENTION_FAILED"
            )
        event_counts = {} if security_violation is None else dict(security_violation.event_counts)
        stdout = "" if result is None else result.stdout
        stdout_bytes = stdout.encode("utf-8", errors="replace")
        event_summary = _event_stream_summary(stdout)
        context = diagnostic_context or _InvocationDiagnosticContext()
        effective_failure_code = taskwise_failure_code
        if effective_failure_code is None and error_code is not None:
            effective_failure_code = _default_taskwise_failure_code(error_code)
        if security_violation is not None:
            effective_failure_code = "EXECUTOR_SECURITY_TOOL_USE_VIOLATION"
        if exception is not None:
            effective_failure_code = "EXECUTOR_INTERNAL_ERROR"
        payload = {
            "schema_version": "taskwise_executor_private_diagnostic_v1",
            "run_id": context.run_id,
            "task_uid": context.task_uid,
            "task_index": context.task_index,
            "round_index": context.round_index,
            "session_id": context.session_id,
            "executor_stage": phase,
            "exception_class": (
                type(exception).__name__
                if exception_class is None and exception is not None
                else exception_class
            ),
            "redacted_exception_message": _redact_diagnostic_text(
                exception_message,
                isolation_root_id=invocation_root_id,
            ),
            "error_code": effective_failure_code,
            "legacy_error_code": None if error_code is None else error_code.value,
            "run_status": (
                _SECURITY_TOOL_USE_VIOLATION
                if effective_failure_code == "EXECUTOR_SECURITY_TOOL_USE_VIOLATION"
                else None
            ),
            "process_return_code": None if result is None else result.returncode,
            "process_signal": (
                None if result is None or result.returncode >= 0 else -result.returncode
            ),
            "timed_out": timed_out,
            "redacted_argv": _redacted_command(command),
            "codex_version": self._codex_version,
            "real_codex_binary": os.fspath(self._codex_executable),
            "real_codex_binary_sha256": self._codex_executable_sha256,
            "model": self._model,
            "invocation_root_id": invocation_root_id,
            "auth_materialization_status": (
                "NOT_STARTED"
                if invocation_root_id is None
                else ("REMOVED" if cleanup_outcome.auth_removed else "RESIDUAL")
            ),
            "stdout_sha256": hashlib.sha256(stdout_bytes).hexdigest(),
            "stdout_bytes": len(stdout_bytes),
            "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
            "redacted_stdout_tail": _redact_diagnostic_text(
                _diagnostic_tail(stdout),
                isolation_root_id=invocation_root_id,
            ),
            "redacted_stderr_tail": _redact_diagnostic_text(
                _diagnostic_tail(stderr),
                isolation_root_id=invocation_root_id,
            ),
            "last_event_type": event_summary["last_event_type"],
            "event_count": event_summary["event_count"],
            "tool_event_count": event_summary["tool_event_count"],
            "output_last_message_exists": output_last_message_exists,
            "output_last_message_sha256": output_last_message_sha256,
            "completion_observed": completion_observed,
            "cleanup_status": "COMPLETE" if cleanup_outcome.complete else "FAILED",
            "residual_root_count": 0 if cleanup_outcome.complete else 1,
            "created_at_utc": datetime.now(UTC).isoformat(timespec="milliseconds"),
            # Backward-compatible metadata used by existing forensic tooling.
            "phase": phase,
            "returncode": None if result is None else result.returncode,
            "errno": error_errno,
            "errno_name": (None if error_errno is None else errno.errorcode.get(error_errno)),
            "stderr_bytes": len(stderr_bytes),
            "stderr_lines": 0 if not stderr else len(stderr.splitlines()),
            "finding_codes": sorted(set(findings)),
            "cleanup_attempts": cleanup_outcome.attempts,
            "cleanup_complete": cleanup_outcome.complete,
            "auth_removed": cleanup_outcome.auth_removed,
            "event_counts": event_counts,
            "event_types": sorted(event_counts),
            "event_digest": (
                None if security_violation is None else security_violation.event_digest
            ),
            "retry_allowed": retry_allowed,
            "resume_allowed": resume_allowed,
            "replacement_completion_allowed": replacement_completion_allowed,
            "timestamp_utc": datetime.now(UTC).isoformat(timespec="milliseconds"),
        }
        return _persist_diagnostic_receipt(self._diagnostic_root, payload)


def _default_auth_file() -> Path:
    configured_root = os.environ.get("CODEX_HOME")
    root = Path(configured_root).expanduser() if configured_root else Path.home() / ".codex"
    return root / "auth.json"


def _validate_auth_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        raise LocalCodexExecutionError(
            LocalCodexExecutionErrorCode.SUBSCRIPTION_AUTH_UNAVAILABLE
        ) from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.SUBSCRIPTION_AUTH_UNAVAILABLE)
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise LocalCodexExecutionError(
            LocalCodexExecutionErrorCode.SUBSCRIPTION_AUTH_PERMISSIONS_UNSAFE
        )


def _sha256_regular_file(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("Codex executable is not a regular file")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise OSError("Codex executable changed while hashing")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _base_environment() -> dict[str, str]:
    environment = {key: value for key in _SAFE_ENV_KEYS if (value := os.environ.get(key))}
    environment.setdefault("PATH", os.defpath)
    environment.setdefault("LANG", "C.UTF-8")
    return environment


def _sanitized_model_transport_environment() -> dict[str, str]:
    """Return only non-empty values from the closed model-transport allowlist."""

    environment: dict[str, str] = {}
    for key in _MODEL_TRANSPORT_ENV_KEYS:
        value = os.environ.get(key)
        if not value:
            continue
        if "\x00" in value:
            raise ValueError("model transport environment contains NUL")
        environment[key] = value
    return environment


def _sanitized_execution_environment() -> dict[str, str]:
    """Return the minimal model-transport environment without benchmark paths."""

    environment = _base_environment()
    path_value = environment.get("PATH", os.defpath)
    safe_path_entries = [
        entry
        for entry in path_value.split(os.pathsep)
        if entry
        and not _path_is_inside_any_protected_root(Path(entry).expanduser().resolve(strict=False))
    ]
    environment["PATH"] = os.pathsep.join(safe_path_entries) or os.defpath
    return environment


def _executor_policy_sha256(
    config: ExperimentConfig | FrozenExperimentConfigV2 | TaskwiseExperimentConfigV1,
) -> str:
    if type(config) is TaskwiseExperimentConfigV1:
        payload = config.to_payload()["executor"]
    elif type(config) is FrozenExperimentConfigV2:
        payload = config.to_payload()["executor"]
    elif type(config) is ExperimentConfig:
        payload = {
            "backend": config.execution_backend,
            "harness": config.agent.harness,
            "model": config.agent.model,
            "runtime": {
                "browser_enabled": config.runtime.browser_enabled,
                "external_web_enabled": config.runtime.external_web_enabled,
                "mcp_servers": list(config.runtime.mcp_servers),
                "network_enabled": config.runtime.network_enabled,
                "network_tools_enabled": config.runtime.network_tools_enabled,
            },
        }
    else:
        raise TypeError("unsupported executor config")
    return hashlib.sha256(canonical_taskwise_config_bytes(payload)).hexdigest()


def _create_isolation_root(parent: Path) -> Path:
    root = Path(
        tempfile.mkdtemp(
            prefix=_ISOLATION_PREFIX,
            dir=os.fspath(parent),
        )
    )
    root.chmod(0o700)
    return root


def _create_invocation_layout(parent: Path) -> _InvocationLayout:
    root = _create_isolation_root(parent)
    layout = _InvocationLayout(
        root=root,
        home=root / "home",
        codex_home=root / "codex_home",
        xdg_config=root / "xdg_config",
        xdg_cache=root / "xdg_cache",
        xdg_state=root / "xdg_state",
        sqlite=root / "xdg_state" / "sqlite",
        tmp=root / "tmp",
        work=root / "work",
        private_events=root / "private_events",
        output_last_message=root / "tmp" / "output_last_message.txt",
    )
    try:
        for directory in (
            layout.home,
            layout.codex_home,
            layout.xdg_config,
            layout.xdg_cache,
            layout.xdg_state,
            layout.sqlite,
            layout.tmp,
            layout.work,
            layout.private_events,
        ):
            directory.mkdir(mode=0o700)
    except BaseException:
        _cleanup_isolation_root(
            root,
            isolated_auth=None,
            expected_parent=parent,
        )
        raise
    return layout


def _attest_invocation(
    layout: _InvocationLayout,
    *,
    environment: Mapping[str, str],
) -> None:
    """Verify the empty-workspace and path boundary before starting Codex."""

    if type(layout) is not _InvocationLayout:
        raise TypeError("layout must be exact _InvocationLayout")
    expected_directories = (
        layout.root,
        layout.home,
        layout.codex_home,
        layout.xdg_config,
        layout.xdg_cache,
        layout.xdg_state,
        layout.sqlite,
        layout.tmp,
        layout.work,
        layout.private_events,
    )
    if _path_is_inside_any_protected_root(layout.root.resolve(strict=False)):
        raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.ISOLATION_SETUP_FAILED)
    for directory in expected_directories:
        try:
            metadata = directory.lstat()
        except OSError:
            raise LocalCodexExecutionError(
                LocalCodexExecutionErrorCode.ISOLATION_SETUP_FAILED
            ) from None
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.ISOLATION_SETUP_FAILED)
        for ancestor in (directory, *directory.parents):
            if _path_is_inside_any_protected_root(ancestor.resolve(strict=False)):
                raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.ISOLATION_SETUP_FAILED)
    try:
        if any(layout.work.iterdir()):
            raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.ISOLATION_SETUP_FAILED)
    except OSError:
        raise LocalCodexExecutionError(
            LocalCodexExecutionErrorCode.ISOLATION_SETUP_FAILED
        ) from None
    isolated_auth = layout.codex_home / "auth.json"
    try:
        auth_metadata = isolated_auth.lstat()
    except OSError:
        raise LocalCodexExecutionError(
            LocalCodexExecutionErrorCode.ISOLATION_SETUP_FAILED
        ) from None
    if (
        stat.S_ISLNK(auth_metadata.st_mode)
        or not stat.S_ISREG(auth_metadata.st_mode)
        or stat.S_IMODE(auth_metadata.st_mode) != 0o600
        or (hasattr(os, "getuid") and auth_metadata.st_uid != os.getuid())
    ):
        raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.ISOLATION_SETUP_FAILED)
    for key, value in environment.items():
        if type(key) is not str or type(value) is not str:
            raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.ISOLATION_SETUP_FAILED)
        normalized_key = key.casefold()
        if any(marker in normalized_key for marker in ("dataset", "repository", "result")):
            raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.ISOLATION_SETUP_FAILED)
        if any(os.fspath(root) in value for root in _PROTECTED_HOST_ROOTS):
            raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.ISOLATION_SETUP_FAILED)
    if not _path_is_within(layout.codex_home, layout.root):
        raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.ISOLATION_SETUP_FAILED)


def _path_is_inside_any_protected_root(path: Path) -> bool:
    return any(_path_is_within(path, root) for root in _PROTECTED_HOST_ROOTS)


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _write_invocation_event_log(private_events: Path, transcript: str) -> Path:
    """Keep a mode-600 raw stream inside the invocation until cleanup."""

    if type(transcript) is not str:
        raise TypeError("transcript must be a string")
    target = private_events / "events.jsonl"
    encoded = transcript.encode("utf-8", errors="replace")
    try:
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise LocalCodexExecutionError(
            LocalCodexExecutionErrorCode.ISOLATION_SETUP_FAILED
        ) from None
    return target


def _persist_private_event_log(
    diagnostic_root: Path,
    transcript: str,
    *,
    expected_digest: str | None,
) -> str | None:
    """Persist a violation stream privately; callers only expose its digest."""

    encoded = transcript.encode("utf-8", errors="replace")
    actual_digest = hashlib.sha256(encoded).hexdigest()
    if expected_digest != actual_digest:
        return None
    try:
        diagnostic_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        private_root = diagnostic_root / "private_events"
        private_root.mkdir(mode=0o700, exist_ok=True)
        root_metadata = diagnostic_root.lstat()
        private_metadata = private_root.lstat()
        for metadata in (root_metadata, private_metadata):
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o700
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            ):
                return None
        name = f"events_{uuid.uuid4().hex}.jsonl"
        target = private_root / name
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        return f"private_events/{name}"
    except OSError:
        return None


def _has_disallowed_plugin_activity(root: Path) -> bool:
    for relative in _DISALLOWED_PLUGIN_RELATIVE_PATHS:
        candidate = root / relative
        if relative == Path("codex_home") / ".tmp":
            try:
                if any(candidate.glob("plugins-clone-*")):
                    return True
            except OSError:
                return True
        elif os.path.lexists(candidate):
            return True
    return False


def _cleanup_isolation_root(
    root: Path,
    *,
    isolated_auth: Path | None,
    expected_parent: Path,
) -> _CleanupOutcome:
    if not os.path.lexists(root):
        return _CleanupOutcome(
            complete=True,
            auth_removed=True,
            disallowed_activity_seen=False,
            attempts=0,
            last_errno=None,
        )
    if not _is_owned_isolation_root(root, expected_parent=expected_parent):
        return _CleanupOutcome(
            complete=False,
            auth_removed=False,
            disallowed_activity_seen=False,
            attempts=0,
            last_errno=errno.EPERM,
        )

    last_errno: int | None = None
    disallowed_activity_seen = _has_disallowed_plugin_activity(root)
    if isolated_auth is not None:
        try:
            isolated_auth.unlink(missing_ok=True)
        except OSError as exc:
            last_errno = exc.errno

    attempts = 0
    for delay_seconds in _CLEANUP_RETRY_DELAYS_SECONDS:
        if delay_seconds:
            time.sleep(delay_seconds)
        disallowed_activity_seen = disallowed_activity_seen or _has_disallowed_plugin_activity(
            root
        )
        attempts += 1
        try:
            _remove_tree_once(root)
        except FileNotFoundError:
            break
        except OSError as exc:
            last_errno = exc.errno
            if exc.errno not in _TRANSIENT_CLEANUP_ERRNOS:
                break
        else:
            break

    complete = not os.path.lexists(root)
    auth_removed = complete or isolated_auth is None or not os.path.lexists(isolated_auth)
    return _CleanupOutcome(
        complete=complete,
        auth_removed=auth_removed,
        disallowed_activity_seen=disallowed_activity_seen,
        attempts=attempts,
        last_errno=None if complete else last_errno,
    )


def _is_owned_isolation_root(root: Path, *, expected_parent: Path) -> bool:
    if root.name.startswith(_ISOLATION_PREFIX) is False:
        return False
    try:
        metadata = root.lstat()
        actual_parent = root.parent.resolve()
        safe_parent = expected_parent.resolve()
    except OSError:
        return False
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        return False
    if actual_parent != safe_parent:
        return False
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        return False
    return stat.S_IMODE(metadata.st_mode) == 0o700


def _remove_tree_once(root: Path) -> None:
    shutil.rmtree(root)


def _persist_diagnostic_receipt(
    diagnostic_root: Path,
    payload: Mapping[str, object],
) -> str | None:
    try:
        diagnostic_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = diagnostic_root.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            return None
        receipt_name = f"receipt_{uuid.uuid4().hex}.json"
        target = diagnostic_root / receipt_name
        encoded = (
            json.dumps(
                dict(payload),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        return receipt_name
    except OSError:
        return None


def _persist_success_receipt(
    diagnostic_root: Path,
    receipt: TaskwiseExecutorSuccessReceiptV1,
) -> str | None:
    """Persist one mode-600 content-free success receipt, or fail closed."""

    if type(receipt) is not TaskwiseExecutorSuccessReceiptV1:
        raise TypeError("receipt must be exact TaskwiseExecutorSuccessReceiptV1")
    TaskwiseExecutorSuccessReceiptV1.from_payload(receipt.to_payload())
    try:
        diagnostic_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = diagnostic_root.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            return None
        receipt_name = f"success_{uuid.uuid4().hex}.json"
        target = diagnostic_root / receipt_name
        encoded = (
            json.dumps(
                receipt.to_payload(),
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        return receipt_name
    except OSError:
        return None


def load_taskwise_success_receipts_v1(
    diagnostic_root: Path,
    *,
    expected_session_ids: Sequence[str] | None = None,
) -> tuple[TaskwiseExecutorSuccessReceiptV1, ...]:
    """Load private success evidence with strict schema, mode, and identity checks."""

    if not isinstance(diagnostic_root, Path):
        raise TypeError("diagnostic_root must be pathlib.Path")
    try:
        root_metadata = diagnostic_root.lstat()
    except OSError as exc:
        raise ValueError("taskwise executor success receipt directory is unavailable") from exc
    if (
        stat.S_ISLNK(root_metadata.st_mode)
        or not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_IMODE(root_metadata.st_mode) != 0o700
        or (hasattr(os, "getuid") and root_metadata.st_uid != os.getuid())
    ):
        raise ValueError("taskwise executor success receipt directory is unsafe")
    malformed_names = tuple(
        path.name
        for path in diagnostic_root.iterdir()
        if path.name.startswith("success_")
        and re.fullmatch(r"success_[0-9a-f]{32}\.json", path.name) is None
    )
    if malformed_names:
        raise ValueError("taskwise executor success receipt filename is invalid")
    receipts: list[TaskwiseExecutorSuccessReceiptV1] = []
    for path in sorted(diagnostic_root.glob("success_*.json")):
        try:
            metadata = path.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            ):
                raise OSError
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("taskwise executor success receipt is unreadable") from exc
        if not isinstance(payload, dict):
            raise ValueError("taskwise executor success receipt schema is invalid")
        receipts.append(TaskwiseExecutorSuccessReceiptV1.from_payload(payload))
    session_ids = [receipt.session_id for receipt in receipts]
    non_null_session_ids = [session_id for session_id in session_ids if session_id is not None]
    if len(non_null_session_ids) != len(set(non_null_session_ids)):
        raise ValueError("taskwise executor success receipt session is duplicated")
    if expected_session_ids is not None:
        if (
            isinstance(expected_session_ids, (str, bytes))
            or any(type(value) is not str or not value for value in expected_session_ids)
            or len(expected_session_ids) != len(set(expected_session_ids))
        ):
            raise TypeError("expected_session_ids must be a unique string sequence")
        if len(receipts) != len(expected_session_ids) or set(non_null_session_ids) != set(
            expected_session_ids
        ):
            raise ValueError("taskwise executor success receipt set does not match sessions")
    return tuple(
        sorted(
            receipts,
            key=lambda receipt: (
                -1 if receipt.task_index is None else receipt.task_index,
                -1 if receipt.round_index is None else receipt.round_index,
                "" if receipt.session_id is None else receipt.session_id,
            ),
        )
    )


def _codex_command(
    *,
    executable: Path,
    model: str,
    reasoning_effort: str | None,
    workspace: Path,
    output_last_message: Path,
) -> tuple[str, ...]:
    command: list[str] = [
        os.fspath(executable),
        "exec",
        "--model",
        model,
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--strict-config",
        "--color",
        "never",
        "--json",
    ]
    for feature_name in _DISABLED_CODEX_FEATURES:
        command.extend(("--disable", feature_name))
    if reasoning_effort is not None:
        command.extend(
            (
                "--config",
                f'model_reasoning_effort="{reasoning_effort}"',
            )
        )
    command.extend(
        (
            "--config",
            'web_search="disabled"',
            "--config",
            'approval_policy="never"',
            "--config",
            'forced_login_method="chatgpt"',
            "--config",
            "check_for_update_on_startup=false",
            "--config",
            "allow_login_shell=false",
            "--config",
            'shell_environment_policy.inherit="none"',
            "--output-last-message",
            os.fspath(output_last_message),
            "--cd",
            os.fspath(workspace),
            "-",
        )
    )
    return tuple(command)


def _compile_prompt(request: AgentRoundRequest) -> str:
    sections = [
        (
            "Runtime policy:\n"
            "- Solve only from the supplied public prompt and approved context.\n"
            "- Do not use shell, network, web, browser, MCP, plugins, or external tools.\n"
            "- Follow the required output format exactly."
        )
    ]
    for context in request.artifact_context:
        if type(context) is not AgentArtifactContext:
            raise TypeError("artifact context must be exact AgentArtifactContext")
        if context.artifact_type is ArtifactKind.TEXT_MEMORY:
            sections.append(f"Validator-approved strategy memory:\n{context.markdown}")
        elif context.artifact_type is ArtifactKind.AGENT_SYSTEM:
            sections.append(f"Validator-approved general agent instructions:\n{context.markdown}")
        elif context.artifact_type is ArtifactKind.SKILL_BUNDLE:
            rendered_files = "\n\n".join(
                f"Approved skill file {file.relative_path}:\n{file.content}"
                for file in context.files
            )
            sections.append(f"Validator-approved skill bundle:\n{rendered_files}")
        else:
            raise TypeError("unsupported approved artifact context")

    public = request.public_prompt
    prompt_parts = ["Question:", public.question]
    if public.choices:
        prompt_parts.extend(
            [
                "",
                "Choices:",
                *(f"{choice.label}. {choice.text}" for choice in public.choices),
            ]
        )
    prompt_parts.extend(["", "Required output:", public.answer_format])
    sections.append("\n".join(prompt_parts))
    return "\n\n".join(sections)


def _audit_runtime_payload(value: object, *, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if type(key) is not str:
                raise TypeError("runtime payload keys must be strings")
            normalized = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
            if normalized in _FORBIDDEN_PAYLOAD_KEYS:
                raise ValueError("runtime payload contains a forbidden field")
            _audit_runtime_payload(child, path=(*path, normalized))
    elif isinstance(value, list):
        for child in value:
            _audit_runtime_payload(child, path=path)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise TypeError("runtime payload contains an unsupported value")


def _audit_compiled_prompt(prompt: str) -> None:
    if type(prompt) is not str or not prompt.strip():
        raise ValueError("compiled prompt must be non-empty")
    normalized = prompt.casefold()
    if any(marker in normalized for marker in _FORBIDDEN_PROMPT_MARKERS):
        raise ValueError("compiled prompt contains a forbidden control marker")


def _parse_codex_version(result: LocalCommandResult) -> str:
    if type(result) is not LocalCommandResult or result.returncode != 0:
        raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.CODEX_UNAVAILABLE)
    match = _CODEX_VERSION.fullmatch(result.stdout.strip())
    if match is None:
        raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.CODEX_UNAVAILABLE)
    return match.group(1)


def _subprocess_stream_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if type(value) is str:
        return value
    if type(value) is bytes:
        return value.decode("utf-8", errors="replace")
    return ""


def _copy_security_violation(
    error: LocalCodexExecutionError,
) -> LocalCodexExecutionError:
    if (
        type(error) is not LocalCodexExecutionError
        or error.taskwise_failure_code != "EXECUTOR_SECURITY_TOOL_USE_VIOLATION"
    ):
        raise TypeError("error must be a security tool-use violation")
    return LocalCodexExecutionError(
        LocalCodexExecutionErrorCode.SECURITY_TOOL_USE_VIOLATION,
        diagnostic_receipt=error.diagnostic_receipt,
        event_counts=error.event_counts,
        event_digest=error.event_digest,
        private_event_reference=error.private_event_reference,
        executor_stage=error.executor_stage,
        completion_observed=error.completion_observed,
    )


def _classify_nonzero_result(result: LocalCommandResult) -> tuple[str, str]:
    text = f"{result.stderr}\n{result.stdout}".casefold()
    if any(
        marker in text
        for marker in (
            "auth",
            "credential",
            "forced_login_method",
            "login required",
            "not logged in",
        )
    ):
        return "EXECUTOR_AUTH_MATERIALIZATION_FAILED", "CODEX_STARTUP"
    if any(
        marker in text
        for marker in (
            "failed to load codex config",
            "config could not be loaded",
            "invalid type",
            "unknown config",
            "unknown feature",
            "unexpected argument",
            "unrecognized option",
            "strict config",
        )
    ):
        return "EXECUTOR_CODEX_STARTUP_FAILED", "CODEX_STARTUP"
    if any(
        marker in text
        for marker in (
            "429",
            "rate limit",
            "connection",
            "connectivity",
            "dns",
            "network",
            "proxy",
            "tls",
            "certificate",
            "transport",
            "stream disconnected",
            "model provider",
            "model is not available",
        )
    ):
        return "EXECUTOR_MODEL_TRANSPORT_FAILED", "MODEL_TRANSPORT"
    return "EXECUTOR_NONZERO_EXIT", "CODEX_STARTUP"


def _completion_observed(transcript: str) -> bool:
    if type(transcript) is not str:
        return False
    for raw_line in transcript.splitlines():
        try:
            event = json.loads(raw_line)
        except (json.JSONDecodeError, RecursionError):
            continue
        if not isinstance(event, Mapping) or event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if (
            isinstance(item, Mapping)
            and item.get("type") == "agent_message"
            and type(item.get("text")) is str
            and bool(str(item["text"]).strip())
        ):
            return True
    return False


def _event_stream_summary(transcript: str) -> dict[str, object]:
    event_count = 0
    tool_event_count = 0
    last_event_type: str | None = None
    if type(transcript) is not str:
        return {
            "event_count": event_count,
            "tool_event_count": tool_event_count,
            "last_event_type": last_event_type,
        }
    for raw_line in transcript.splitlines():
        try:
            event = json.loads(raw_line)
        except (json.JSONDecodeError, RecursionError):
            continue
        if not isinstance(event, Mapping) or type(event.get("type")) is not str:
            continue
        event_count += 1
        event_type = str(event["type"])
        last_event_type = event_type
        categories = _security_categories_in_event_schema(event)
        tool_event_count += int(bool(categories))
    return {
        "event_count": event_count,
        "tool_event_count": tool_event_count,
        "last_event_type": last_event_type,
    }


def _diagnostic_tail(text: str, *, limit: int = 4096) -> str:
    encoded = text.encode("utf-8", errors="replace")
    return encoded[-limit:].decode("utf-8", errors="replace")


def _redact_diagnostic_text(
    text: str,
    *,
    isolation_root_id: str | None,
) -> str:
    if type(text) is not str or not text:
        return ""
    redacted = text
    protected_values = {
        os.fspath(_REPOSITORY_ROOT),
        os.fspath(_WORKSPACE_ROOT),
        os.fspath(Path.home()),
    }
    if isolation_root_id:
        protected_values.add(isolation_root_id)
    for value in sorted(protected_values, key=len, reverse=True):
        if value:
            redacted = redacted.replace(value, "<redacted-path>")
    redacted = re.sub(
        r"""(?ix)
        (?P<prefix>["']?authorization["']?\s*[:=]\s*["']?)
        (?:bearer\s+)?
        [^"'\s,;}\]]+
        """,
        r"\g<prefix><redacted-secret>",
        redacted,
    )
    redacted = re.sub(
        r"""(?ix)
        (?P<prefix>
            ["']?
            (?:cookie|set-cookie|api[_-]?key|access[_-]?token|
               refresh[_-]?token|id[_-]?token)
            ["']?\s*[:=]\s*["']?
        )
        [^"'\s,;}\]]+
        """,
        r"\g<prefix><redacted-secret>",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+",
        "bearer <redacted-secret>",
        redacted,
    )
    redacted = re.sub(r"https?://[^\s]+", "<redacted-url>", redacted)
    for marker in (
        "target_scores",
        "target",
        "correct_answer",
        "answer_mapping",
        "private-uuid",
        "auth.json",
    ):
        redacted = re.sub(re.escape(marker), "<redacted-field>", redacted, flags=re.I)
    return redacted


def _redacted_command(command: Sequence[str] | None) -> list[str]:
    if command is None:
        return []
    redacted: list[str] = []
    hide_next_path = False
    for value in command:
        if hide_next_path:
            redacted.append("<isolated-path>")
            hide_next_path = False
            continue
        if value in {"--cd", "--output-last-message"}:
            redacted.append(value)
            hide_next_path = True
            continue
        redacted.append(value)
    return redacted


def _find_security_tool_use(
    transcript: str,
) -> LocalCodexExecutionError | None:
    """Classify tool-like JSONL events without inspecting their payload text."""

    if type(transcript) is not str or not transcript.strip():
        return None
    counts = {category: 0 for category in _SECURITY_EVENT_CATEGORIES}
    for raw_line in transcript.splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except (json.JSONDecodeError, RecursionError):
            continue
        if not isinstance(event, Mapping) or type(event.get("type")) is not str:
            continue
        for category in _security_categories_in_event_schema(event):
            counts[category] += 1
    findings = {category: count for category, count in counts.items() if count}
    if not findings:
        return None
    digest = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
    return LocalCodexExecutionError(
        LocalCodexExecutionErrorCode.SECURITY_TOOL_USE_VIOLATION,
        event_counts=findings,
        event_digest=digest,
    )


def _security_categories_in_event_schema(
    event: Mapping[str, object],
) -> frozenset[str]:
    """Inspect structural names only, never assistant/message text.

    Codex normally identifies a tool at ``event.type`` or ``item.type``.  This
    recursive structural pass also fails closed if a future lifecycle schema
    nests a tool descriptor inside an otherwise inert ``agent_message`` or
    ``reasoning`` item.  Free-form ``text`` and ``message`` values are never
    classified, so ordinary discussion of a tool does not become a tool event.
    """

    findings: set[str] = set()
    type_keys = frozenset({"event_type", "item_type", "kind", "tool_type", "type"})

    def visit(value: object, *, field_name: str | None = None) -> None:
        if isinstance(value, Mapping):
            for raw_key, child in value.items():
                if type(raw_key) is not str:
                    findings.add("unknown_tool")
                    continue
                category = _security_event_category(raw_key)
                if category is not None:
                    findings.add(category)
                normalized_key = re.sub(
                    r"[^a-z0-9]+",
                    "_",
                    raw_key.casefold(),
                ).strip("_")
                if (
                    normalized_key in type_keys
                    and type(child) is str
                    and (category := _security_event_category(child)) is not None
                ):
                    findings.add(category)
                if normalized_key not in {"error", "message", "text"}:
                    visit(child, field_name=normalized_key)
        elif isinstance(value, list) and field_name not in {
            "error",
            "message",
            "text",
        }:
            for child in value:
                visit(child, field_name=field_name)

    visit(event)
    return frozenset(findings)


def _security_event_category(event_name: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", "_", event_name.casefold()).strip("_")
    tokens = frozenset(part for part in normalized.split("_") if part)
    if (
        "subagent" in normalized
        or "collab" in tokens
        or ("agent" in tokens and {"spawn", "delegate", "collaboration"} & tokens)
    ):
        return "subagent"
    if "mcp" in tokens or normalized.startswith("mcp"):
        return "mcp"
    if "file" in tokens and {"read", "open", "fetch"} & tokens:
        return "file_read"
    if (
        ("file" in tokens and {"write", "edit", "patch", "change", "create", "delete"} & tokens)
        or "patch" in tokens
        or normalized in {"apply_patch", "unified_exec"}
    ):
        return "file_write"
    if "shell" in tokens:
        return "shell"
    if {"command", "exec", "execution", "terminal"} & tokens:
        return "command_execution"
    if "browser" in tokens or "computer" in tokens:
        return "browser"
    if "web" in tokens or "search" in tokens:
        return "web"
    if "network" in tokens or {"http", "https", "socket"} & tokens:
        return "network"
    if "plugin" in tokens:
        return "plugin"
    if "app" in tokens or "connector" in tokens:
        return "app"
    if "external" in tokens:
        return "external_tool"
    if (
        "tool" in tokens
        or "function" in tokens
        or normalized in {"function_call", "image_generation", "dynamic_tool_call"}
    ):
        return "unknown_tool"
    return None


def _parse_jsonl_transcript(
    transcript: str,
) -> tuple[str, dict[str, int], str]:
    if type(transcript) is not str or not transcript.strip():
        raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.TRANSCRIPT_INVALID)
    security_violation = _find_security_tool_use(transcript)
    if security_violation is not None:
        raise security_violation
    digest = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
    thread_started = False
    turn_started = False
    turn_completed = False
    responses: list[str] = []
    usage: dict[str, int] = {}
    recoverable_transport_error_count = 0

    for raw_line in transcript.splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except (json.JSONDecodeError, RecursionError):
            raise LocalCodexExecutionError(
                LocalCodexExecutionErrorCode.TRANSCRIPT_INVALID
            ) from None
        if not isinstance(event, Mapping) or type(event.get("type")) is not str:
            raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.TRANSCRIPT_INVALID)
        event_type = str(event["type"])
        if event_type == "thread.started":
            thread_started = True
        elif event_type == "turn.started":
            turn_started = True
        elif event_type in {"item.started", "item.updated", "item.completed"}:
            item = event.get("item")
            if not isinstance(item, Mapping) or type(item.get("type")) is not str:
                raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.TRANSCRIPT_INVALID)
            item_type = str(item["type"]).casefold()
            if item_type not in _ALLOWED_ITEM_TYPES:
                raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.TRANSCRIPT_INVALID)
            if item_type == "todo_list":
                valid_item = _is_valid_todo_list_item(
                    item,
                    event_type=event_type,
                )
            else:
                valid_item = _is_valid_text_item(
                    item,
                    event_type=event_type,
                )
            if not valid_item:
                raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.TRANSCRIPT_INVALID)
            if event_type == "item.completed" and item_type == "agent_message":
                text = item.get("text")
                if type(text) is str and text.strip():
                    responses.append(text)
        elif event_type == "turn.completed":
            raw_usage = event.get("usage")
            usage_keys = set(raw_usage) if isinstance(raw_usage, Mapping) else set()
            if (
                not isinstance(raw_usage, Mapping)
                or usage_keys
                not in (
                    set(_TOKEN_FIELDS),
                    set((*_TOKEN_FIELDS, *_OPTIONAL_TOKEN_FIELDS)),
                )
                or not all(
                    type(raw_usage[field]) is int and raw_usage[field] >= 0
                    for field in usage_keys
                )
            ):
                raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.TRANSCRIPT_INVALID)
            usage = {field: int(raw_usage[field]) for field in usage_keys}
            turn_completed = True
        elif event_type == "error":
            if not _is_recoverable_transport_error_event(event):
                raise LocalCodexExecutionError(
                    LocalCodexExecutionErrorCode.CLI_FAILED,
                    taskwise_failure_code="EXECUTOR_MODEL_TRANSPORT_FAILED",
                    executor_stage="MODEL_TRANSPORT",
                )
            recoverable_transport_error_count += 1
            if recoverable_transport_error_count > _MAX_RECOVERABLE_TRANSPORT_ERRORS:
                raise LocalCodexExecutionError(
                    LocalCodexExecutionErrorCode.CLI_FAILED,
                    taskwise_failure_code="EXECUTOR_MODEL_TRANSPORT_FAILED",
                    executor_stage="MODEL_TRANSPORT",
                )
        elif event_type == "turn.failed":
            raise LocalCodexExecutionError(
                LocalCodexExecutionErrorCode.CLI_FAILED,
                taskwise_failure_code="EXECUTOR_MODEL_TRANSPORT_FAILED",
                executor_stage="MODEL_TRANSPORT",
            )
        else:
            raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.TRANSCRIPT_INVALID)

    if recoverable_transport_error_count and (not turn_completed or not responses):
        raise LocalCodexExecutionError(
            LocalCodexExecutionErrorCode.CLI_FAILED,
            taskwise_failure_code="EXECUTOR_MODEL_TRANSPORT_FAILED",
            executor_stage="MODEL_TRANSPORT",
        )
    if not thread_started or not turn_started or not turn_completed:
        raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.TRANSCRIPT_INVALID)
    if not responses:
        raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.RESPONSE_MISSING)
    return responses[-1], usage, digest


def _is_recoverable_transport_error_event(event: Mapping[str, object]) -> bool:
    """Recognize Codex's bounded reconnect notice, never arbitrary error payloads."""

    if set(event) != _RECOVERABLE_TRANSPORT_ERROR_KEYS or type(event.get("message")) is not str:
        return False
    message = str(event["message"])
    encoded = message.encode("utf-8")
    if not encoded or len(encoded) > _MAX_TRANSPORT_ERROR_MESSAGE_BYTES:
        return False
    normalized = message.casefold()
    return "reconnect" in normalized and "stream" in normalized and "disconnect" in normalized


def _is_valid_todo_list_item(
    item: Mapping[str, object],
    *,
    event_type: str,
) -> bool:
    """Accept only Codex's inert planning lifecycle schema, never arbitrary item payloads."""

    if (
        event_type not in {"item.started", "item.completed"}
        or set(item) != _TODO_LIST_ITEM_KEYS
        or type(item.get("id")) is not str
        or not str(item["id"]).strip()
        or type(item.get("items")) is not list
        or len(item["items"]) > _MAX_TODO_LIST_ITEMS
    ):
        return False
    for entry in item["items"]:
        if (
            not isinstance(entry, Mapping)
            or set(entry) != _TODO_LIST_ENTRY_KEYS
            or type(entry.get("completed")) is not bool
            or type(entry.get("text")) is not str
            or len(str(entry["text"]).encode("utf-8")) > _MAX_TODO_LIST_TEXT_BYTES
        ):
            return False
    return True


def _is_valid_text_item(
    item: Mapping[str, object],
    *,
    event_type: str,
) -> bool:
    """Accept only the observed inert Codex 0.144.6 text-item schema."""

    return (
        event_type == "item.completed"
        and set(item).issubset(_TEXT_ITEM_KEYS)
        and {"text", "type"}.issubset(item)
        and item.get("type") in {"agent_message", "reasoning"}
        and type(item.get("text")) is str
        and ("id" not in item or (type(item.get("id")) is str and bool(str(item["id"]).strip())))
    )


def _run_local_command(
    command: Sequence[str],
    input_text: str | None,
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
) -> LocalCommandResult:
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=dict(environment),
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=(os.name == "posix"),
    )
    process_group_id = process.pid if os.name == "posix" else None
    try:
        stdout, stderr = process.communicate(
            input=input_text,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        quiescent = _terminate_invocation_processes(
            process,
            process_group_id=process_group_id,
        )
        try:
            final_stdout, final_stderr = process.communicate(timeout=2.0)
        except subprocess.TimeoutExpired:
            _close_process_pipes(process)
            if process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired as termination_error:
                    raise _ProcessTerminationError from termination_error
            raise _ProcessTerminationError from exc
        if not quiescent:
            raise _ProcessTerminationError from exc
        raise subprocess.TimeoutExpired(
            command,
            timeout_seconds,
            output=final_stdout,
            stderr=final_stderr,
        ) from exc
    except BaseException as exc:
        quiescent = _terminate_invocation_processes(
            process,
            process_group_id=process_group_id,
        )
        _close_process_pipes(process)
        if not quiescent:
            raise _ProcessTerminationError from exc
        raise
    else:
        quiescent = _terminate_invocation_processes(
            process,
            process_group_id=process_group_id,
        )
        if not quiescent:
            raise _ProcessTerminationError
    output_exists, output_text, output_invalid = _read_output_last_message(
        command,
        cwd=cwd,
    )
    return LocalCommandResult(
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
        output_last_message_exists=output_exists,
        output_last_message=output_text,
        output_last_message_invalid=output_invalid,
    )


def _read_output_last_message(
    command: Sequence[str],
    *,
    cwd: Path,
) -> tuple[bool | None, str | None, bool]:
    try:
        index = tuple(command).index("--output-last-message")
        raw_path = tuple(command)[index + 1]
    except (ValueError, IndexError):
        return None, None, False
    target = Path(raw_path)
    expected = cwd.parent / "tmp" / "output_last_message.txt"
    if target != expected:
        return True, None, True
    try:
        metadata = target.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            return True, None, True
        return True, target.read_text(encoding="utf-8"), False
    except FileNotFoundError:
        return False, None, False
    except (OSError, UnicodeError):
        return True, None, True


def _terminate_invocation_processes(
    process: subprocess.Popen[str],
    *,
    process_group_id: int | None,
) -> bool:
    if os.name != "posix" or process_group_id is None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    return False
        return process.poll() is not None

    if not _process_group_exists(process_group_id):
        return True
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        process.poll()
        if not _process_group_exists(process_group_id):
            return True
        time.sleep(0.025)
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        process.poll()
        if not _process_group_exists(process_group_id):
            return True
        time.sleep(0.025)
    return not _process_group_exists(process_group_id)


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _close_process_pipes(process: subprocess.Popen[str]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


__all__ = [
    "LocalCodexCLIExecutor",
    "LocalCodexExecutionError",
    "LocalCodexExecutionErrorCode",
    "LocalCommandResult",
    "TaskwiseExecutorSuccessReceiptV1",
    "load_taskwise_success_receipts_v1",
]
