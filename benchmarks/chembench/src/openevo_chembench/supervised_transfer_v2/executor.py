"""OpenEvo Rollout task boundary for supervised transfer v2.

Candidate calls are submitted as real :class:`openevo.rollout.models.TaskRequest`
objects.  The Rollout service dispatches them to Gateway, which creates the
official ``CodexHarness`` inside the Core-owned ``managed_science`` runtime.
This module never starts Codex, copies subscription credentials, or fabricates
an OpenEvo completion event.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, Protocol, Self

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

from openevo_chembench.models import AgentRuntimeMetadata, RawAttempt, TranscriptReference
from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes
from openevo_chembench.supervised_transfer_v2.artifacts import (
    CoreResolvedSupervisedContextV2,
)
from openevo_chembench.supervised_transfer_v2.context_binding import (
    SupervisedAgentRequestV2,
    SupervisedContextBindingReceiptV2,
    SupervisedSessionContextBindingV2,
    issue_supervised_context_binding_receipt_v2,
)
from openevo_chembench.supervised_transfer_v2.runtime_services import (
    OpenEvoRuntimeServicesIdentityV2,
    RuntimeServicesV2Error,
)

_MODEL = "gpt-5.5"
_REASONING_EFFORT = "medium"
_RUNTIME_HEALTH_RETRY_LIMIT = 120
_RUNTIME_HEALTH_RETRY_SECONDS = 0.5
_TRANSIENT_RUNTIME_HEALTH_FINDINGS = frozenset(
    {"RUNTIME_SERVICE_HEALTH_INVALID", "RUNTIME_SERVICE_UNAVAILABLE"}
)
_REMOTE_SKILLS_ROOT = f"{MANAGED_WORKSPACE}/.openevo-approved-skills"
_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
_TOOL_MARKERS = frozenset(
    {
        "app",
        "browser",
        "command",
        "computer",
        "exec",
        "file",
        "function",
        "image_generation",
        "mcp",
        "network",
        "plugin",
        "search",
        "shell",
        "subagent",
        "tool",
        "unified_exec",
        "web",
    }
)
_SAFE_EVENT_TYPES = frozenset(
    {
        "error",
        "item.completed",
        "item.started",
        "thread.started",
        "turn.completed",
        "turn.started",
    }
)
_SAFE_ITEM_TYPES = frozenset({"agent_message", "reasoning"})
_IDENTIFIER_TOKEN = re.compile(r"[a-z0-9]+", re.ASCII)


class RolloutClientV2(Protocol):
    """Minimal official Rollout API used by the v2 controller."""

    def submit_task(self, payload: dict[str, object]) -> str: ...

    def get_task(self, task_id: str) -> dict[str, object]: ...

    def cancel_task(self, task_id: str) -> dict[str, object]: ...

    def close(self) -> None: ...


class SupervisedTaskExecutionCodeV2(str, Enum):
    INVALID_REQUEST = "SUPERVISED_V2_TASK_INVALID_REQUEST"
    SUBMIT_FAILED = "SUPERVISED_V2_TASK_SUBMIT_FAILED"
    TASK_ID_MISMATCH = "SUPERVISED_V2_TASK_ID_MISMATCH"
    POLL_FAILED = "SUPERVISED_V2_TASK_POLL_FAILED"
    POLL_TIMEOUT = "SUPERVISED_V2_TASK_POLL_TIMEOUT"
    TASK_FAILED = "SUPERVISED_V2_TASK_FAILED"
    SESSION_INVALID = "SUPERVISED_V2_TASK_SESSION_INVALID"
    TRANSCRIPT_INVALID = "SUPERVISED_V2_TASK_TRANSCRIPT_INVALID"
    TOOL_EVENT = "SUPERVISED_V2_TASK_TOOL_EVENT"
    RESPONSE_MISSING = "SUPERVISED_V2_TASK_RESPONSE_MISSING"


class SupervisedTaskExecutionErrorV2(RuntimeError):
    """Content-free task execution failure."""

    def __init__(
        self,
        code: SupervisedTaskExecutionCodeV2,
        *,
        completion_exists: bool | None = None,
        completion_sha256: str | None = None,
    ) -> None:
        if type(code) is not SupervisedTaskExecutionCodeV2:
            raise TypeError("task execution code must be exact")
        if completion_exists not in {None, False, True} or (
            completion_exists is not True and completion_sha256 is not None
        ):
            raise TypeError("completion evidence is invalid")
        if (
            completion_sha256 is not None
            and re.fullmatch(r"[0-9a-f]{64}", completion_sha256) is None
        ):
            raise ValueError("completion digest is invalid")
        self.code = code
        self.completion_exists = completion_exists
        self.completion_sha256 = completion_sha256
        super().__init__(code.value)


class SupervisedManagedCodexExecutorV2:
    """Submit immutable v2 sessions through Rollout/Gateway/CodexHarness."""

    def __init__(
        self,
        *,
        arm: Literal["control", "online"],
        timeout_seconds: int,
        rollout_url: str = "http://127.0.0.1:8080",
        poll_interval_seconds: float = 1.0,
        max_poll_attempts: int = 1800,
        rollout_client: RolloutClientV2 | None = None,
        runtime_services: OpenEvoRuntimeServicesIdentityV2 | None = None,
        protocol_id: str = "chembench_supervised_transfer_v2",
    ) -> None:
        if arm not in ("control", "online"):
            raise ValueError("executor arm must be control or online")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
            raise TypeError("timeout_seconds must be an integer")
        if timeout_seconds != 1200:
            raise ValueError("v2 task timeout is frozen at 1200 seconds")
        if (
            isinstance(poll_interval_seconds, bool)
            or not isinstance(poll_interval_seconds, (int, float))
            or float(poll_interval_seconds) <= 0
        ):
            raise ValueError("poll interval must be positive")
        if (
            isinstance(max_poll_attempts, bool)
            or not isinstance(max_poll_attempts, int)
            or max_poll_attempts < 1
        ):
            raise ValueError("max poll attempts must be positive")
        if type(rollout_url) is not str or not rollout_url.startswith(
            ("http://127.0.0.1:", "http://localhost:")
        ):
            raise ValueError("Rollout URL must be an uncredentialed loopback endpoint")
        if re.fullmatch(r"[a-z][a-z0-9_]{7,95}", protocol_id) is None:
            raise ValueError("protocol_id is invalid")
        self._arm = arm
        self._protocol_id = protocol_id
        self._timeout_seconds = timeout_seconds
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._max_poll_attempts = max_poll_attempts
        self._owns_client = rollout_client is None
        if self._owns_client and runtime_services is None:
            raise ValueError("owned Rollout client requires an attested v2 service identity")
        self._client = RolloutHttpClient(rollout_url) if rollout_client is None else rollout_client
        self._runtime_services = runtime_services
        self._closed = False
        self._sessions: set[str] = set()
        self._receipts: dict[str, SupervisedContextBindingReceiptV2] = {}

    def __enter__(self) -> Self:
        if self._closed:
            raise RuntimeError("v2 executor is closed")
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        if self._owns_client:
            self._client.close()
        self._closed = True

    def build_task_request(
        self,
        request: SupervisedAgentRequestV2,
        *,
        workspace_root: Path | None = None,
    ) -> TaskRequest:
        """Compile a closed request without starting a benchmark or model call."""

        self._validate_request(request)
        context = request.resolved_context
        if context is not None and workspace_root is None:
            raise ValueError("online context requires a candidate workspace")
        if workspace_root is not None and not isinstance(workspace_root, Path):
            raise TypeError("workspace_root must be pathlib.Path or None")

        prepare: list[PrepareAction] = []
        if workspace_root is not None:
            prepare.append(
                PrepareAction(
                    type="upload_dir",
                    source=os.fspath(workspace_root),
                    target=MANAGED_WORKSPACE,
                )
            )
        prepare.append(PrepareAction(type="exec", command=MANAGED_SUBSCRIPTION_PREPARE_COMMAND))
        runtime = RuntimeSpec(
            backend="docker",
            profile="managed_science",
            container_user="host",
            image=MANAGED_RUNTIME_RELEASES["managed_science"].loaded_image_id,
            prepare=prepare,
            env=dict(MANAGED_SUBSCRIPTION_ENV),
            # Subscription transport must reach the provider.  Candidate tool use
            # remains prohibited by the closed instruction and the transcript
            # audit below; any tool event invalidates the immutable session.
            network=None,
            workdir=MANAGED_WORKSPACE,
            allow_internet=True,
        )
        agent = AgentSpec(
            harness="codex",
            model_name=_MODEL,
            settings={
                "auth_mode": "subscription",
                "capture_mode": "transcript",
                "reasoning_effort": _REASONING_EFFORT,
            },
            env={},
            mcp_servers=[],
            skills_path=_REMOTE_SKILLS_ROOT if context is not None else None,
        )
        binding = SupervisedSessionContextBindingV2.from_context(
            session_id=request.session_id,
            context=context,
        )
        return TaskRequest(
            task_id=_runtime_task_id(request, protocol_id=self._protocol_id),
            instruction=_compile_instruction(request),
            num_samples=1,
            timeout_seconds=float(self._timeout_seconds),
            runtime=runtime,
            agent=agent,
            builder=StrategySpec(strategy="agent_transcript"),
            evaluator=None,
            callback_url=None,
            metadata={
                "openevo_chembench": {
                    "schema_version": "SupervisedTaskRequestV2",
                    "protocol_id": self._protocol_id,
                    "arm": request.arm,
                    "task_ordinal": request.task_ordinal,
                    "round_index": request.round_index,
                    "context_binding_sha256": binding.digest,
                    "context_target_ids": [target.target_id for target in binding.targets],
                    "context_artifact_ids": [
                        target.core_artifact_id for target in binding.targets
                    ],
                    "runtime_services_identity_sha256": (
                        None if self._runtime_services is None else self._runtime_services.digest
                    ),
                }
            },
        )

    def execute(self, request: SupervisedAgentRequestV2) -> RawAttempt:
        """Submit one immutable session and consume its official terminal result."""

        self._validate_request(request)
        if self._closed:
            raise RuntimeError("v2 executor is closed")
        try:
            self._require_runtime_services()
        except RuntimeServicesV2Error as exc:
            if exc.finding_code in _TRANSIENT_RUNTIME_HEALTH_FINDINGS:
                raise SupervisedTaskExecutionErrorV2(
                    SupervisedTaskExecutionCodeV2.TASK_FAILED,
                    completion_exists=False,
                ) from None
            raise
        if request.session_id in self._sessions:
            raise SupervisedTaskExecutionErrorV2(SupervisedTaskExecutionCodeV2.INVALID_REQUEST)
        self._sessions.add(request.session_id)
        expected = SupervisedSessionContextBindingV2.from_context(
            session_id=request.session_id,
            context=request.resolved_context,
        )
        with self._prepared_task_request(request) as task_request:
            actual = _binding_from_task_request(
                request=request,
                task_request=task_request,
            )
            receipt = issue_supervised_context_binding_receipt_v2(
                expected=expected,
                actual=actual,
            )
            receipt.require_match()
            payload = task_request.model_dump(
                mode="json",
                exclude_defaults=False,
                exclude_none=False,
                exclude_unset=False,
            )
            try:
                submitted = self._client.submit_task(payload)
            except Exception:  # noqa: BLE001 - external Rollout client boundary
                raise SupervisedTaskExecutionErrorV2(
                    SupervisedTaskExecutionCodeV2.SUBMIT_FAILED
                ) from None
            if submitted != task_request.task_id:
                raise SupervisedTaskExecutionErrorV2(
                    SupervisedTaskExecutionCodeV2.TASK_ID_MISMATCH
                )
            status = self._wait_for_terminal(task_request.task_id)
            self._require_runtime_service_identity()
            attempt = _raw_attempt_from_task_status(status, task_id=task_request.task_id)
            self._receipts[request.session_id] = receipt
            return attempt

    def _require_runtime_services(self) -> None:
        if self._runtime_services is None:
            return
        for attempt_index in range(_RUNTIME_HEALTH_RETRY_LIMIT):
            try:
                self._runtime_services.require_current()
                return
            except RuntimeServicesV2Error as exc:
                if (
                    exc.finding_code not in _TRANSIENT_RUNTIME_HEALTH_FINDINGS
                    or attempt_index == _RUNTIME_HEALTH_RETRY_LIMIT - 1
                ):
                    raise
                time.sleep(_RUNTIME_HEALTH_RETRY_SECONDS)

    def _require_runtime_service_identity(self) -> None:
        if self._runtime_services is not None:
            self._runtime_services.require_identity_current()

    def consume_context_receipt(
        self,
        session_id: str,
    ) -> SupervisedContextBindingReceiptV2:
        try:
            return self._receipts.pop(session_id)
        except KeyError as exc:
            raise RuntimeError("v2 context receipt is unavailable") from exc

    @property
    def run_status(self) -> str | None:
        return None

    @contextmanager
    def _prepared_task_request(
        self,
        request: SupervisedAgentRequestV2,
    ) -> Iterator[TaskRequest]:
        context = request.resolved_context
        if context is None:
            yield self.build_task_request(request)
            return
        staging_root = self._candidate_workspace_staging_root()
        with TemporaryDirectory(
            prefix=f"openevo-chembench-{self._protocol_id[-24:]}-",
            dir=staging_root,
        ) as temporary:
            workspace = Path(temporary) / "workspace"
            workspace.mkdir(mode=0o700)
            _materialize_core_resolved_context(context, workspace)
            yield self.build_task_request(request, workspace_root=workspace)

    def _candidate_workspace_staging_root(self) -> Path | None:
        """Return a Gateway-visible private root for real online sessions."""

        if self._runtime_services is None:
            return None
        root = self._runtime_services.receipt_path.parent / "candidate_workspace_staging"
        root.mkdir(mode=0o700, exist_ok=True)
        metadata = root.lstat()
        resolved = root.resolve(strict=True)
        repository = self._runtime_services.repository_root.resolve(strict=True)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != os.geteuid()
            or not resolved.is_relative_to(repository)
        ):
            raise SupervisedTaskExecutionErrorV2(
                SupervisedTaskExecutionCodeV2.INVALID_REQUEST
            )
        return resolved

    def _wait_for_terminal(self, task_id: str) -> TaskStatus:
        for attempt_index in range(self._max_poll_attempts):
            try:
                status = TaskStatus.model_validate(self._client.get_task(task_id))
            except Exception:  # noqa: BLE001 - external Rollout client boundary
                raise SupervisedTaskExecutionErrorV2(
                    SupervisedTaskExecutionCodeV2.POLL_FAILED
                ) from None
            if status.task_id != task_id:
                raise SupervisedTaskExecutionErrorV2(
                    SupervisedTaskExecutionCodeV2.TASK_ID_MISMATCH
                )
            if status.status != "running":
                return status
            if attempt_index + 1 < self._max_poll_attempts:
                time.sleep(self._poll_interval_seconds)
        try:
            self._client.cancel_task(task_id)
        except Exception:  # noqa: BLE001, S110 - best-effort cancellation only
            pass
        raise SupervisedTaskExecutionErrorV2(SupervisedTaskExecutionCodeV2.POLL_TIMEOUT)

    def _validate_request(self, request: SupervisedAgentRequestV2) -> None:
        if type(request) is not SupervisedAgentRequestV2 or request.arm != self._arm:
            raise SupervisedTaskExecutionErrorV2(SupervisedTaskExecutionCodeV2.INVALID_REQUEST)


def _runtime_task_id(request: SupervisedAgentRequestV2, *, protocol_id: str) -> str:
    protocol_token = protocol_id.replace("_", "-")[-40:]
    return f"chembench-{protocol_token}-{request.session_id}"


def _compile_instruction(request: SupervisedAgentRequestV2) -> str:
    sections = [
        (
            "Runtime policy:\n"
            "- Solve only from the official public prompt and approved same-category "
            "Core context.\n"
            "- Do not use shell, files, network, web, browser, MCP, plugins, apps, "
            "subagents, or external tools.\n"
            "- Reply with exactly one uppercase A, B, C, or D."
        )
    ]
    if request.resolved_context is not None:
        sections.append(
            "Approved Core-resolved category memory:\n" + request.resolved_context.memory.markdown
        )
    sections.append(request.rendered_public_prompt)
    instruction = "\n\n".join(sections)
    if not instruction.endswith(request.rendered_public_prompt):
        raise AssertionError("official rendered prompt was not preserved as the final section")
    return instruction


def _materialize_core_resolved_context(
    context: CoreResolvedSupervisedContextV2,
    workspace: Path,
) -> None:
    if type(context) is not CoreResolvedSupervisedContextV2:
        raise TypeError("context must be Core-issued")
    _write_private_text(workspace / "AGENTS.md", context.agent_system.markdown)
    skill_root = (
        workspace
        / ".openevo-approved-skills"
        / f"artifact-{context.skill.resolved_content_sha256[:16]}"
    )
    skill_root.mkdir(mode=0o700, parents=True)
    _write_private_text(skill_root / "SKILL.md", context.skill.markdown)


def _write_private_text(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _binding_from_task_request(
    *,
    request: SupervisedAgentRequestV2,
    task_request: TaskRequest,
) -> SupervisedSessionContextBindingV2:
    metadata = task_request.metadata.get("openevo_chembench")
    expected = SupervisedSessionContextBindingV2.from_context(
        session_id=request.session_id,
        context=request.resolved_context,
    )
    if not isinstance(metadata, Mapping):
        raise SupervisedTaskExecutionErrorV2(SupervisedTaskExecutionCodeV2.INVALID_REQUEST)
    if (
        metadata.get("context_binding_sha256") != expected.digest
        or metadata.get("context_target_ids") != [target.target_id for target in expected.targets]
        or metadata.get("context_artifact_ids")
        != [target.core_artifact_id for target in expected.targets]
    ):
        raise SupervisedTaskExecutionErrorV2(SupervisedTaskExecutionCodeV2.INVALID_REQUEST)
    return expected


def _raw_attempt_from_task_status(status: TaskStatus, *, task_id: str) -> RawAttempt:
    completion_exists, completion_sha256 = _task_status_completion_evidence(status)
    if status.status != "completed":
        raise SupervisedTaskExecutionErrorV2(
            SupervisedTaskExecutionCodeV2.TASK_FAILED,
            completion_exists=completion_exists,
            completion_sha256=completion_sha256,
        )
    if len(status.results) != 1:
        raise SupervisedTaskExecutionErrorV2(
            SupervisedTaskExecutionCodeV2.SESSION_INVALID,
            completion_exists=completion_exists,
            completion_sha256=completion_sha256,
        )
    session = status.results[0]
    if session.task_id != task_id:
        raise SupervisedTaskExecutionErrorV2(
            SupervisedTaskExecutionCodeV2.SESSION_INVALID,
            completion_exists=completion_exists,
            completion_sha256=completion_sha256,
        )
    if session.status in {SessionStatus.ERROR, SessionStatus.TIMEOUT}:
        raise SupervisedTaskExecutionErrorV2(
            SupervisedTaskExecutionCodeV2.TASK_FAILED,
            completion_exists=completion_exists,
            completion_sha256=completion_sha256,
        )
    if session.status is not SessionStatus.COMPLETED:
        raise SupervisedTaskExecutionErrorV2(
            SupervisedTaskExecutionCodeV2.SESSION_INVALID,
            completion_exists=completion_exists,
            completion_sha256=completion_sha256,
        )
    capture_modes = {session.trajectory.metadata.get("capture_mode")}
    transcripts: list[str] = []
    responses: list[str] = []
    for trace in session.trajectory.traces:
        capture_modes.add(trace.metadata.get("capture_mode"))
        transcript = trace.metadata.get("transcript")
        if isinstance(transcript, str) and transcript.strip():
            transcripts.append(transcript)
        for message in trace.response_messages:
            if (
                isinstance(message, Mapping)
                and message.get("role") == "assistant"
                and isinstance(message.get("content"), str)
                and str(message["content"]).strip()
            ):
                responses.append(str(message["content"]))
    if "transcript" not in capture_modes or not transcripts:
        raise SupervisedTaskExecutionErrorV2(
            SupervisedTaskExecutionCodeV2.TRANSCRIPT_INVALID,
            completion_exists=completion_exists,
            completion_sha256=completion_sha256,
        )
    try:
        usage, transcript_sha256 = _audit_transcripts(transcripts)
    except SupervisedTaskExecutionErrorV2 as exc:
        raise SupervisedTaskExecutionErrorV2(
            exc.code,
            completion_exists=completion_exists,
            completion_sha256=completion_sha256,
        ) from None
    if not responses:
        raise SupervisedTaskExecutionErrorV2(
            SupervisedTaskExecutionCodeV2.RESPONSE_MISSING,
            completion_exists=False,
        )
    duration = session.timing
    return RawAttempt(
        response="\n".join(responses),
        transcript_reference=TranscriptReference(
            reference=f"openevo-rollout-jsonl:sha256:{transcript_sha256}"
        ),
        runtime_metadata=AgentRuntimeMetadata(
            duration_ms=(
                duration.register_to_init_queue_ms
                + duration.init_ms
                + duration.run_ms
                + duration.postrun_ms
            ),
            input_tokens=usage.get("input_tokens"),
            cached_input_tokens=usage.get("cached_input_tokens"),
            output_tokens=usage.get("output_tokens"),
            reasoning_output_tokens=usage.get("reasoning_output_tokens"),
        ),
    )


def _task_status_completion_evidence(status: TaskStatus) -> tuple[bool, str | None]:
    responses = [
        str(message["content"])
        for session in status.results
        for trace in session.trajectory.traces
        for message in trace.response_messages
        if isinstance(message, Mapping)
        and message.get("role") == "assistant"
        and isinstance(message.get("content"), str)
        and str(message["content"]).strip()
    ]
    if not responses:
        return False, None
    return True, hashlib.sha256("\n".join(responses).encode()).hexdigest()


def _audit_transcripts(transcripts: list[str]) -> tuple[dict[str, int], str]:
    combined = "\n".join(transcripts)
    usage: dict[str, int] = {}
    for transcript in transcripts:
        for raw_line in transcript.splitlines():
            line = raw_line.strip()
            if not line or line == "Reading additional input from stdin...":
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, RecursionError):
                raise SupervisedTaskExecutionErrorV2(
                    SupervisedTaskExecutionCodeV2.TRANSCRIPT_INVALID
                ) from None
            if not isinstance(event, dict) or type(event.get("type")) is not str:
                raise SupervisedTaskExecutionErrorV2(
                    SupervisedTaskExecutionCodeV2.TRANSCRIPT_INVALID
                )
            event_type = str(event["type"])
            if event_type not in _SAFE_EVENT_TYPES or _contains_tool_marker(event_type):
                raise SupervisedTaskExecutionErrorV2(SupervisedTaskExecutionCodeV2.TOOL_EVENT)
            item = event.get("item")
            if isinstance(item, Mapping):
                item_type = item.get("type")
                if (
                    type(item_type) is not str
                    or item_type not in _SAFE_ITEM_TYPES
                    or _contains_tool_marker(item_type)
                ):
                    raise SupervisedTaskExecutionErrorV2(SupervisedTaskExecutionCodeV2.TOOL_EVENT)
            if event_type == "turn.completed":
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
                    usage = {field: int(raw_usage[field]) for field in _USAGE_FIELDS}
    return usage, hashlib.sha256(combined.encode("utf-8")).hexdigest()


def _contains_tool_marker(value: str) -> bool:
    tokens = frozenset(_IDENTIFIER_TOKEN.findall(value.casefold()))
    return bool(tokens & _TOOL_MARKERS)


def task_request_digest_v2(task_request: TaskRequest) -> str:
    """Return the canonical immutable TaskRequest identity used by receipts."""

    if type(task_request) is not TaskRequest:
        raise TypeError("task_request must be exact TaskRequest")
    return hashlib.sha256(
        canonical_json_bytes(
            task_request.model_dump(
                mode="json",
                exclude_defaults=False,
                exclude_none=False,
                exclude_unset=False,
            )
        )
    ).hexdigest()


__all__ = [
    "RolloutClientV2",
    "SupervisedManagedCodexExecutorV2",
    "SupervisedTaskExecutionCodeV2",
    "SupervisedTaskExecutionErrorV2",
    "task_request_digest_v2",
]
