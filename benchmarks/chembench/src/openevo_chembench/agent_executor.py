"""Real OpenEvo Rollout adapter for the managed Codex subscription harness."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from enum import Enum
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
import time
from typing import Protocol

from openevo.experiments.clients import RolloutHttpClient
from openevo.harness.models import AgentSpec
from openevo.rollout.models import SessionStatus, TaskRequest, TaskStatus
from openevo.runtime.managed import (
    MANAGED_RUNTIME_RELEASES,
    MANAGED_SUBSCRIPTION_ENV,
    MANAGED_SUBSCRIPTION_PREPARE_COMMAND,
    MANAGED_WORKSPACE,
)
from openevo.runtime.models import PrepareAction, RuntimeSpec
from openevo.trajectory.models import StrategySpec

from openevo_chembench.artifacts import ArtifactKind
from openevo_chembench.config import ExperimentConfig
from openevo_chembench.models import (
    AgentRuntimeMetadata,
    RawAttempt,
    TranscriptReference,
)
from openevo_chembench.runtime_context import (
    AgentArtifactContext,
    AgentRoundRequest,
)


_REMOTE_SKILLS_ROOT = f"{MANAGED_WORKSPACE}/.openevo-approved-skills"
_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
_DISALLOWED_TRANSCRIPT_ITEM_MARKERS = (
    "browser",
    "computer",
    "mcp",
    "web_search",
)


class RolloutClient(Protocol):
    """Minimal OpenEvo Rollout contract used by the executor."""

    def submit_task(self, payload: dict[str, object]) -> str: ...

    def get_task(self, task_id: str) -> dict[str, object]: ...

    def cancel_task(self, task_id: str) -> dict[str, object]: ...

    def close(self) -> None: ...


class AgentExecutionErrorCode(str, Enum):
    """Closed failure vocabulary safe for benchmark-controller logs."""

    INVALID_REQUEST = "invalid_request"
    SUBMIT_FAILED = "submit_failed"
    TASK_ID_MISMATCH = "task_id_mismatch"
    POLL_FAILED = "poll_failed"
    POLL_TIMEOUT = "poll_timeout"
    TASK_FAILED = "task_failed"
    SESSION_COUNT_INVALID = "session_count_invalid"
    SESSION_FAILED = "session_failed"
    TRANSCRIPT_INVALID = "transcript_invalid"
    DISALLOWED_TOOL_EVENT = "disallowed_tool_event"
    RESPONSE_MISSING = "response_missing"


class AgentExecutionError(RuntimeError):
    """Sanitized real-execution failure without prompt or response content."""

    __slots__ = ("code", "runtime_task_id")

    def __init__(
        self,
        code: AgentExecutionErrorCode,
        *,
        runtime_task_id: str | None = None,
    ) -> None:
        if type(code) is not AgentExecutionErrorCode:
            raise TypeError("AgentExecutionError.code must be AgentExecutionErrorCode")
        if runtime_task_id is not None and (
            type(runtime_task_id) is not str or not runtime_task_id
        ):
            raise ValueError("AgentExecutionError.runtime_task_id must be non-empty")
        self.code = code
        self.runtime_task_id = runtime_task_id
        message = f"OpenEvo agent execution failed: error_type={code.value}"
        if runtime_task_id is not None:
            message += f" runtime_task_id={runtime_task_id}"
        super().__init__(message)

    def to_log_fields(self) -> dict[str, str]:
        fields = {"error_type": self.code.value}
        if self.runtime_task_id is not None:
            fields["runtime_task_id"] = self.runtime_task_id
        return fields


class OpenEvoCodexExecutor:
    """Execute one public ChemBench round through OpenEvo's CodexHarness."""

    __slots__ = (
        "_client",
        "_closed",
        "_config",
        "_max_poll_attempts",
        "_owns_client",
        "_poll_interval_seconds",
        "_task_timeout_seconds",
    )

    def __init__(
        self,
        *,
        config: ExperimentConfig,
        rollout_url: str = "http://127.0.0.1:8080",
        task_timeout_seconds: float = 600.0,
        poll_interval_seconds: float = 1.0,
        max_poll_attempts: int = 900,
        rollout_client: RolloutClient | None = None,
    ) -> None:
        if type(config) is not ExperimentConfig:
            raise TypeError("OpenEvoCodexExecutor.config must be exact ExperimentConfig")
        if type(rollout_url) is not str or not rollout_url.strip():
            raise ValueError("rollout_url must be a non-empty string")
        for value, field_name in (
            (task_timeout_seconds, "task_timeout_seconds"),
            (poll_interval_seconds, "poll_interval_seconds"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or float(value) <= 0
            ):
                raise ValueError(f"{field_name} must be positive")
        if (
            isinstance(max_poll_attempts, bool)
            or not isinstance(max_poll_attempts, int)
            or max_poll_attempts < 1
        ):
            raise ValueError("max_poll_attempts must be a positive integer")
        if config.runtime.network_enabled:
            raise ValueError("ChemBench agent task networking must remain disabled")

        self._config = config
        self._task_timeout_seconds = float(task_timeout_seconds)
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._max_poll_attempts = max_poll_attempts
        self._owns_client = rollout_client is None
        self._client = (
            RolloutHttpClient(rollout_url)
            if rollout_client is None
            else rollout_client
        )
        self._closed = False

    def __enter__(self) -> OpenEvoCodexExecutor:
        if self._closed:
            raise RuntimeError("OpenEvoCodexExecutor is closed")
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        if self._owns_client:
            self._client.close()
        self._closed = True

    def execute(self, request: AgentRoundRequest) -> RawAttempt:
        """Run exactly one round and project the terminal OpenEvo result."""

        if type(request) is not AgentRoundRequest:
            raise TypeError(
                "OpenEvoCodexExecutor.execute requires an exact AgentRoundRequest"
            )
        if self._closed:
            raise RuntimeError("OpenEvoCodexExecutor is closed")

        with self._prepared_task_request(request) as task_request:
            runtime_task_id = task_request.task_id
            payload = task_request.model_dump(
                mode="json",
                exclude_defaults=False,
                exclude_none=False,
                exclude_unset=False,
            )
            try:
                submitted_task_id = self._client.submit_task(payload)
            except Exception:
                raise AgentExecutionError(
                    AgentExecutionErrorCode.SUBMIT_FAILED,
                    runtime_task_id=runtime_task_id,
                ) from None
            if submitted_task_id != runtime_task_id:
                raise AgentExecutionError(
                    AgentExecutionErrorCode.TASK_ID_MISMATCH,
                    runtime_task_id=runtime_task_id,
                )

            task_status = self._wait_for_terminal(runtime_task_id)
            return _raw_attempt_from_task_status(
                task_status,
                runtime_task_id=runtime_task_id,
            )

    def build_task_request(
        self,
        request: AgentRoundRequest,
        *,
        workspace_root: Path | None = None,
    ) -> TaskRequest:
        """Compile a closed OpenEvo request without submitting it."""

        if type(request) is not AgentRoundRequest:
            raise TypeError(
                "OpenEvoCodexExecutor.build_task_request requires AgentRoundRequest"
            )
        if workspace_root is not None and not isinstance(workspace_root, Path):
            raise TypeError("workspace_root must be pathlib.Path or None")

        needs_workspace = any(
            context.artifact_type
            in {ArtifactKind.SKILL_BUNDLE, ArtifactKind.AGENT_SYSTEM}
            for context in request.artifact_context
        )
        if needs_workspace and workspace_root is None:
            raise ValueError(
                "approved skill_bundle or agent_system context requires workspace_root"
            )
        skills_enabled = any(
            context.artifact_type is ArtifactKind.SKILL_BUNDLE
            for context in request.artifact_context
        )

        prepare: list[PrepareAction] = []
        if workspace_root is not None:
            prepare.append(
                PrepareAction(
                    type="upload_dir",
                    source=str(workspace_root),
                    target=MANAGED_WORKSPACE,
                )
            )
        prepare.append(
            PrepareAction(
                type="exec",
                command=MANAGED_SUBSCRIPTION_PREPARE_COMMAND,
            )
        )

        runtime = RuntimeSpec(
            backend="docker",
            profile="managed_science",
            container_user="host",
            # The official offline release archive loads this exact OCI index
            # without registry RepoDigests. Core explicitly admits its pinned
            # image ID as immutable managed-runtime authority.
            image=MANAGED_RUNTIME_RELEASES["managed_science"].loaded_image_id,
            prepare=prepare,
            env=dict(MANAGED_SUBSCRIPTION_ENV),
            network="none",
            workdir=MANAGED_WORKSPACE,
            allow_internet=False,
        )
        agent = AgentSpec(
            harness=self._config.agent.harness,
            model_name=self._config.agent.model,
            settings={
                "auth_mode": self._config.agent.auth_mode,
                "capture_mode": self._config.agent.capture_mode,
            },
            env={},
            mcp_servers=[],
            skills_path=_REMOTE_SKILLS_ROOT if skills_enabled else None,
        )
        return TaskRequest(
            task_id=_runtime_task_id(request),
            instruction=_compile_instruction(request),
            num_samples=self._config.runtime.num_samples,
            timeout_seconds=self._task_timeout_seconds,
            runtime=runtime,
            agent=agent,
            builder=StrategySpec(strategy="agent_transcript"),
            evaluator=None,
            callback_url=None,
            metadata={
                "openevo_chembench": {
                    "schema_version": 1,
                    "run_id": request.run_id,
                    "episode_id": request.episode_id,
                    "round": request.round_index,
                }
            },
        )

    @contextmanager
    def _prepared_task_request(
        self,
        request: AgentRoundRequest,
    ) -> Iterator[TaskRequest]:
        needs_workspace = any(
            context.artifact_type
            in {ArtifactKind.SKILL_BUNDLE, ArtifactKind.AGENT_SYSTEM}
            for context in request.artifact_context
        )
        if not needs_workspace:
            yield self.build_task_request(request)
            return

        with TemporaryDirectory(prefix="openevo-chembench-approved-") as temporary:
            workspace_root = Path(temporary) / "workspace"
            workspace_root.mkdir(mode=0o700)
            _materialize_approved_context(request.artifact_context, workspace_root)
            yield self.build_task_request(
                request,
                workspace_root=workspace_root,
            )

    def _wait_for_terminal(self, runtime_task_id: str) -> TaskStatus:
        for attempt_index in range(self._max_poll_attempts):
            try:
                payload = self._client.get_task(runtime_task_id)
                status = TaskStatus.model_validate(payload)
            except Exception:
                raise AgentExecutionError(
                    AgentExecutionErrorCode.POLL_FAILED,
                    runtime_task_id=runtime_task_id,
                ) from None
            if status.task_id != runtime_task_id:
                raise AgentExecutionError(
                    AgentExecutionErrorCode.TASK_ID_MISMATCH,
                    runtime_task_id=runtime_task_id,
                )
            if status.status != "running":
                return status
            if attempt_index + 1 < self._max_poll_attempts:
                time.sleep(self._poll_interval_seconds)

        try:
            self._client.cancel_task(runtime_task_id)
        except Exception:
            pass
        raise AgentExecutionError(
            AgentExecutionErrorCode.POLL_TIMEOUT,
            runtime_task_id=runtime_task_id,
        )


def _runtime_task_id(request: AgentRoundRequest) -> str:
    run_token = request.run_id.removeprefix("run_")
    episode_token = request.episode_id.removeprefix("episode_")
    return f"chembench-{run_token}-{episode_token}-r{request.round_index}"


def _compile_instruction(request: AgentRoundRequest) -> str:
    sections: list[str] = []
    for context in request.artifact_context:
        if context.artifact_type is ArtifactKind.AGENT_SYSTEM:
            sections.append(
                "Use the following validator-approved evolved agent system "
                f"instructions for this task:\n{context.markdown}"
            )
        elif context.artifact_type is ArtifactKind.TEXT_MEMORY:
            sections.append(
                "Use the following validator-approved long-term strategy memory "
                f"for this task:\n{context.markdown}"
            )
        elif context.artifact_type is not ArtifactKind.SKILL_BUNDLE:
            raise TypeError("unsupported approved artifact context")

    prompt = request.public_prompt
    public_parts = ["Question:", prompt.question]
    if prompt.choices:
        public_parts.extend(
            [
                "",
                "Choices:",
                *(
                    f"{choice.label}. {choice.text}"
                    for choice in prompt.choices
                ),
            ]
        )
    public_parts.extend(["", "Answer format:", prompt.answer_format])
    sections.append("\n".join(public_parts))
    return "\n\n".join(sections)


def _materialize_approved_context(
    contexts: tuple[AgentArtifactContext, ...],
    workspace_root: Path,
) -> None:
    for context in contexts:
        if type(context) is not AgentArtifactContext:
            raise TypeError("runtime context must contain exact AgentArtifactContext values")
        if context.artifact_type is ArtifactKind.AGENT_SYSTEM:
            if context.target_path != "AGENTS.md" or context.markdown is None:
                raise ValueError("approved agent_system context is invalid")
            _write_private_text(workspace_root / "AGENTS.md", context.markdown)
        elif context.artifact_type is ArtifactKind.SKILL_BUNDLE:
            artifact_root = (
                workspace_root
                / ".openevo-approved-skills"
                / f"artifact-{context.artifact_hash[:16]}"
            )
            artifact_root.mkdir(mode=0o700, parents=True, exist_ok=False)
            for file in context.files:
                relative = _safe_relative_path(file.relative_path)
                target = artifact_root.joinpath(*relative.parts)
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _write_private_text(target, file.content)
        elif context.artifact_type is not ArtifactKind.TEXT_MEMORY:
            raise TypeError("unsupported approved artifact context")


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or str(path) != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("approved skill path must be canonical and relative")
    return path


def _write_private_text(path: Path, content: str) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def _raw_attempt_from_task_status(
    task_status: TaskStatus,
    *,
    runtime_task_id: str,
) -> RawAttempt:
    if task_status.status != "completed":
        raise AgentExecutionError(
            AgentExecutionErrorCode.TASK_FAILED,
            runtime_task_id=runtime_task_id,
        )
    if len(task_status.results) != 1:
        raise AgentExecutionError(
            AgentExecutionErrorCode.SESSION_COUNT_INVALID,
            runtime_task_id=runtime_task_id,
        )
    session = task_status.results[0]
    if session.task_id != runtime_task_id or session.status is not SessionStatus.COMPLETED:
        raise AgentExecutionError(
            AgentExecutionErrorCode.SESSION_FAILED,
            runtime_task_id=runtime_task_id,
        )

    response_parts: list[str] = []
    transcript_texts: list[str] = []
    capture_modes = {session.trajectory.metadata.get("capture_mode")}
    for trace in session.trajectory.traces:
        capture_modes.add(trace.metadata.get("capture_mode"))
        transcript = trace.metadata.get("transcript")
        if isinstance(transcript, str) and transcript.strip():
            transcript_texts.append(transcript)
        for message in trace.response_messages:
            if (
                isinstance(message, Mapping)
                and message.get("role") == "assistant"
                and isinstance(message.get("content"), str)
                and str(message["content"]).strip()
            ):
                response_parts.append(str(message["content"]))

    if "transcript" not in capture_modes or not transcript_texts:
        raise AgentExecutionError(
            AgentExecutionErrorCode.TRANSCRIPT_INVALID,
            runtime_task_id=runtime_task_id,
        )
    usage = _audit_transcripts(transcript_texts, runtime_task_id=runtime_task_id)
    if not response_parts:
        raise AgentExecutionError(
            AgentExecutionErrorCode.RESPONSE_MISSING,
            runtime_task_id=runtime_task_id,
        )

    timing = session.timing
    duration_ms = (
        timing.register_to_init_queue_ms
        + timing.init_ms
        + timing.run_ms
        + timing.postrun_ms
    )
    return RawAttempt(
        response="\n".join(response_parts),
        transcript_reference=TranscriptReference(
            reference=(
                f"openevo-rollout:{runtime_task_id}:{session.session_id}"
            )
        ),
        runtime_metadata=AgentRuntimeMetadata(
            duration_ms=duration_ms,
            input_tokens=usage.get("input_tokens"),
            cached_input_tokens=usage.get("cached_input_tokens"),
            output_tokens=usage.get("output_tokens"),
            reasoning_output_tokens=usage.get("reasoning_output_tokens"),
        ),
    )


def _audit_transcripts(
    transcripts: list[str],
    *,
    runtime_task_id: str,
) -> dict[str, int]:
    usage: dict[str, int] = {}
    for transcript in transcripts:
        for raw_line in transcript.splitlines():
            line = raw_line.strip()
            if not line or line == "Reading additional input from stdin...":
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, RecursionError):
                raise AgentExecutionError(
                    AgentExecutionErrorCode.TRANSCRIPT_INVALID,
                    runtime_task_id=runtime_task_id,
                ) from None
            if not isinstance(event, dict):
                raise AgentExecutionError(
                    AgentExecutionErrorCode.TRANSCRIPT_INVALID,
                    runtime_task_id=runtime_task_id,
                )
            item = event.get("item")
            item_type = (
                str(item.get("type") or "").casefold()
                if isinstance(item, Mapping)
                else ""
            )
            if any(marker in item_type for marker in _DISALLOWED_TRANSCRIPT_ITEM_MARKERS):
                raise AgentExecutionError(
                    AgentExecutionErrorCode.DISALLOWED_TOOL_EVENT,
                    runtime_task_id=runtime_task_id,
                )
            if event.get("type") == "turn.completed":
                raw_usage = event.get("usage")
                if (
                    isinstance(raw_usage, Mapping)
                    and set(raw_usage) == set(_USAGE_FIELDS)
                    and all(
                        not isinstance(raw_usage[field], bool)
                        and isinstance(raw_usage[field], int)
                        and raw_usage[field] >= 0
                        for field in _USAGE_FIELDS
                    )
                ):
                    usage = {
                        field: int(raw_usage[field])
                        for field in _USAGE_FIELDS
                    }
    return usage


__all__ = [
    "AgentExecutionError",
    "AgentExecutionErrorCode",
    "OpenEvoCodexExecutor",
    "RolloutClient",
]
