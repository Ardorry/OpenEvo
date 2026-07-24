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
from openevo_chembench.frozen_runtime_v2 import FrozenAgentRequestV2
from openevo_chembench.models import (
    AgentRuntimeMetadata,
    RawAttempt,
    TranscriptReference,
)
from openevo_chembench.runtime_context import (
    AgentArtifactContext,
    AgentRoundRequest,
)
from openevo_chembench.v2_config import FrozenExperimentConfigV2


_CODEX_VERSION = re.compile(r"codex-cli ([0-9A-Za-z.+-]+)")
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
_ALLOWED_ITEM_TYPES = frozenset({"agent_message", "reasoning"})
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
_SAFE_ENV_KEYS = (
    "ALL_PROXY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LC_ALL",
    "NO_PROXY",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "all_proxy",
    "https_proxy",
    "http_proxy",
    "no_proxy",
)
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
_WORKSPACE_ROOT = _REPOSITORY_ROOT.parent
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


class _ProcessTerminationError(RuntimeError):
    """Internal marker for a process group that did not reach quiescence."""


class LocalCodexExecutionError(RuntimeError):
    """Sanitized error that never embeds prompt, response, transcript, or stderr."""

    __slots__ = (
        "code",
        "diagnostic_receipt",
        "event_counts",
        "event_digest",
        "private_event_reference",
        "replacement_completion_allowed",
        "resume_allowed",
        "retry_allowed",
        "run_status",
    )

    def __init__(
        self,
        code: LocalCodexExecutionErrorCode,
        *,
        diagnostic_receipt: str | None = None,
        event_counts: Mapping[str, int] | None = None,
        event_digest: str | None = None,
        private_event_reference: str | None = None,
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
        self.code = code
        self.diagnostic_receipt = diagnostic_receipt
        self.event_counts = dict(sorted(normalized_event_counts.items()))
        self.event_digest = event_digest
        self.private_event_reference = private_event_reference
        is_security_violation = code is LocalCodexExecutionErrorCode.SECURITY_TOOL_USE_VIOLATION
        self.run_status = _SECURITY_TOOL_USE_VIOLATION if is_security_violation else None
        self.retry_allowed = not is_security_violation
        self.resume_allowed = not is_security_violation
        self.replacement_completion_allowed = not is_security_violation
        super().__init__(f"local Codex execution failed: error_type={code.value}")

    def to_log_fields(self) -> dict[str, object]:
        """Return a public-safe receipt; raw event payloads are never included."""

        fields: dict[str, object] = {"error_type": self.code.value}
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
        "_codex_version",
        "_command_runner",
        "_config",
        "_diagnostic_root",
        "_isolation_parent",
        "_model",
        "_pending_cleanup",
        "_reasoning_effort",
        "_security_violation",
        "_task_timeout_seconds",
        "_v2_mode",
    )

    def __init__(
        self,
        *,
        config: ExperimentConfig | FrozenExperimentConfigV2,
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
        elif type(config) is FrozenExperimentConfigV2:
            model = config.model
            expected_codex_version = config.codex_cli_version
            reasoning_effort = config.reasoning_effort
            v2_mode = True
            if float(task_timeout_seconds) != float(config.executor.timeout_seconds):
                raise ValueError("task_timeout_seconds must match the frozen v2 executor policy")
        else:
            raise TypeError("config must be exact ExperimentConfig or FrozenExperimentConfigV2")
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
        self._reasoning_effort = reasoning_effort
        self._v2_mode = v2_mode
        self._task_timeout_seconds = float(task_timeout_seconds)
        self._codex_executable = resolved_executable
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
                phase="cleanup",
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

        if self._v2_mode:
            raise TypeError("v2 executor mode requires execute_frozen")
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

    def _execute_prompt_once(self, prompt: str) -> RawAttempt:
        if type(prompt) is not str or not prompt:
            raise TypeError("prompt must be non-empty text")
        if self._closed:
            raise RuntimeError("LocalCodexCLIExecutor is closed")
        if self._security_violation is not None:
            raise _copy_security_violation(self._security_violation)
        pending_outcome = self._drain_pending_cleanup()
        if pending_outcome is not None and not pending_outcome.complete:
            diagnostic_reference = self._write_diagnostic_receipt(
                phase="cleanup",
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
        failure_phase = "setup"
        failure_errno: int | None = None
        isolation_root: Path | None = None
        isolated_auth: Path | None = None
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
            _attest_invocation(layout, environment=environment)
            command = _codex_command(
                executable=self._codex_executable,
                model=self._model,
                reasoning_effort=self._reasoning_effort,
                workspace=layout.work,
            )
            failure_phase = "command"
            result = self._command_runner(
                command,
                prompt,
                layout.work,
                environment,
                self._task_timeout_seconds,
            )
            if type(result) is not LocalCommandResult:
                raise TypeError("command runner must return LocalCommandResult")
            _write_invocation_event_log(
                layout.private_events,
                result.stdout,
            )
            security_violation = _find_security_tool_use(result.stdout)
            if security_violation is not None:
                raise security_violation
            if result.returncode != 0:
                failure = LocalCodexExecutionError(LocalCodexExecutionErrorCode.CLI_FAILED)
            else:
                failure_phase = "transcript"
                response, usage, transcript_digest = _parse_jsonl_transcript(result.stdout)
        except subprocess.TimeoutExpired as exc:
            result = LocalCommandResult(
                returncode=-signal.SIGKILL,
                stdout=_subprocess_stream_text(exc.output),
                stderr=_subprocess_stream_text(exc.stderr),
            )
            failure = LocalCodexExecutionError(LocalCodexExecutionErrorCode.CLI_TIMEOUT)
        except LocalCodexExecutionError as exc:
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
            failure_errno = exc.errno
            code = (
                LocalCodexExecutionErrorCode.CODEX_UNAVAILABLE
                if failure_phase == "command"
                else LocalCodexExecutionErrorCode.ISOLATION_SETUP_FAILED
            )
            failure = LocalCodexExecutionError(code)
        except _ProcessTerminationError:
            failure = LocalCodexExecutionError(
                LocalCodexExecutionErrorCode.PROCESS_TERMINATION_FAILED
            )
        except BaseException as exc:
            unexpected = exc
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

        if plugin_activity and failure is None and unexpected is None:
            failure_phase = "cleanup"
            failure = LocalCodexExecutionError(
                LocalCodexExecutionErrorCode.DISALLOWED_PLUGIN_ACTIVITY
            )
        if not cleanup_outcome.complete and failure is None and unexpected is None:
            failure_phase = "cleanup"
            failure_errno = cleanup_outcome.last_errno
            failure = LocalCodexExecutionError(LocalCodexExecutionErrorCode.CLEANUP_FAILED)

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
            )

        if unexpected is not None:
            raise unexpected
        if failure is not None:
            if diagnostic_reference is not None:
                failure.diagnostic_receipt = diagnostic_reference
            if failure.code is LocalCodexExecutionErrorCode.SECURITY_TOOL_USE_VIOLATION:
                self._security_violation = _copy_security_violation(failure)
            raise failure
        if response is None or usage is None or transcript_digest is None:
            raise AssertionError("successful local Codex execution is incomplete")
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
        payload = {
            "schema_version": "2",
            "phase": phase,
            "error_code": None if error_code is None else error_code.value,
            "run_status": (
                _SECURITY_TOOL_USE_VIOLATION
                if error_code is LocalCodexExecutionErrorCode.SECURITY_TOOL_USE_VIOLATION
                else None
            ),
            "returncode": None if result is None else result.returncode,
            "errno": error_errno,
            "errno_name": (None if error_errno is None else errno.errorcode.get(error_errno)),
            "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
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
            "retry_allowed": (None if security_violation is None else False),
            "resume_allowed": (None if security_violation is None else False),
            "replacement_completion_allowed": (None if security_violation is None else False),
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


def _base_environment() -> dict[str, str]:
    environment = {key: value for key in _SAFE_ENV_KEYS if (value := os.environ.get(key))}
    environment.setdefault("PATH", os.defpath)
    environment.setdefault("LANG", "C.UTF-8")
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
            if exc.errno == errno.ENOTEMPTY:
                disallowed_activity_seen = True
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


def _codex_command(
    *,
    executable: Path,
    model: str,
    reasoning_effort: str | None,
    workspace: Path,
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
            "--config",
            "agents.enabled=false",
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
        or error.code is not LocalCodexExecutionErrorCode.SECURITY_TOOL_USE_VIOLATION
    ):
        raise TypeError("error must be a security tool-use violation")
    return LocalCodexExecutionError(
        LocalCodexExecutionErrorCode.SECURITY_TOOL_USE_VIOLATION,
        diagnostic_receipt=error.diagnostic_receipt,
        event_counts=error.event_counts,
        event_digest=error.event_digest,
        private_event_reference=error.private_event_reference,
    )


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
        event_type = str(event["type"])
        category: str | None = None
        if event_type in {"item.started", "item.updated", "item.completed"}:
            item = event.get("item")
            if isinstance(item, Mapping) and type(item.get("type")) is str:
                item_type = str(item["type"]).casefold()
                if item_type not in _ALLOWED_ITEM_TYPES:
                    category = _security_event_category(item_type)
        elif event_type not in {
            "thread.started",
            "turn.started",
            "turn.completed",
            "error",
            "turn.failed",
        }:
            category = _security_event_category(event_type.casefold())
        if category is not None:
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
        "file" in tokens
        and {
            "write",
            "edit",
            "patch",
            "change",
            "create",
            "delete",
        }
        & tokens
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
    if "tool" in tokens or normalized:
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
                raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.DISALLOWED_TOOL_EVENT)
            if event_type == "item.completed" and item_type == "agent_message":
                text = item.get("text")
                if type(text) is str and text.strip():
                    responses.append(text)
        elif event_type == "turn.completed":
            raw_usage = event.get("usage")
            if (
                not isinstance(raw_usage, Mapping)
                or set(raw_usage) != set(_TOKEN_FIELDS)
                or not all(
                    type(raw_usage[field]) is int and raw_usage[field] >= 0
                    for field in _TOKEN_FIELDS
                )
            ):
                raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.TRANSCRIPT_INVALID)
            usage = {field: int(raw_usage[field]) for field in _TOKEN_FIELDS}
            turn_completed = True
        elif event_type in {"error", "turn.failed"}:
            raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.CLI_FAILED)
        else:
            raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.TRANSCRIPT_INVALID)

    if not thread_started or not turn_started or not turn_completed:
        raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.TRANSCRIPT_INVALID)
    if not responses:
        raise LocalCodexExecutionError(LocalCodexExecutionErrorCode.RESPONSE_MISSING)
    return responses[-1], usage, digest


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
    else:
        quiescent = _terminate_invocation_processes(
            process,
            process_group_id=process_group_id,
        )
        if not quiescent:
            raise _ProcessTerminationError
    return LocalCommandResult(
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
    )


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
]
