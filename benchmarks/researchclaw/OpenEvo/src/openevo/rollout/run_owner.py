"""Public Core owner for one already-bound native rollout task."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import threading
import time
from typing import Any, Protocol

from openevo.rollout.models import (
    CanonicalTaskRequest,
    SessionResult,
    SessionStatus,
    TaskStatus,
    canonicalize_task_request,
)


class BoundTaskRolloutClient(Protocol):
    def submit_task(self, payload: dict[str, Any]) -> str: ...

    def get_task(self, task_id: str) -> dict[str, Any]: ...

    def cancel_task(self, task_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class OwnedNativeTaskResult:
    task_id: str
    request_sha256: str
    session_result: SessionResult
    submitted_at: str
    completed_at: str
    poll_count: int


class NativeTaskRunOwner:
    """Submit, poll, cancel and verify exactly one Core-bound TaskRequest."""

    def __init__(
        self,
        client: BoundTaskRolloutClient,
        *,
        poll_interval_seconds: float = 1.0,
        cancellation: threading.Event | None = None,
        clock=time.monotonic,
        utc_clock=lambda: datetime.now(UTC),
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("run-owner poll interval must be positive")
        self._client = client
        self._poll_interval = poll_interval_seconds
        self._cancellation = cancellation or threading.Event()
        self._clock = clock
        self._utc_clock = utc_clock
        self._owned_task_id: str | None = None

    def execute(self, request: CanonicalTaskRequest | dict[str, Any]) -> OwnedNativeTaskResult:
        canonical = (
            request
            if isinstance(request, CanonicalTaskRequest)
            else canonicalize_task_request(request)
        )
        self._validate_binding(canonical)
        if self._owned_task_id is not None:
            raise RuntimeError("run owner is single-use")
        task_id = canonical.request.task_id
        submitted_at = self._utc_clock().isoformat()
        submitted = self._client.submit_task(dict(canonical.payload))
        if submitted != task_id:
            raise RuntimeError("rollout service changed the bound task identity")
        self._owned_task_id = task_id
        deadline = self._clock() + canonical.request.timeout_seconds
        polls = 0
        try:
            while self._clock() <= deadline:
                if self._cancellation.is_set():
                    self.cancel_owned_task()
                    raise RuntimeError("owned rollout task was cancelled")
                status = TaskStatus.model_validate(self._client.get_task(task_id))
                polls += 1
                if status.task_id != task_id:
                    raise RuntimeError("rollout status changed the bound task identity")
                if status.status == "running":
                    self._cancellation.wait(self._poll_interval)
                    continue
                result = self._terminal_result(status, canonical)
                return OwnedNativeTaskResult(
                    task_id=task_id,
                    request_sha256=canonical.payload_sha256,
                    session_result=result,
                    submitted_at=submitted_at,
                    completed_at=self._utc_clock().isoformat(),
                    poll_count=polls,
                )
            self.cancel_owned_task()
            raise TimeoutError("owned rollout task exceeded its bound timeout")
        except BaseException:
            if self._owned_task_id is not None:
                try:
                    self.cancel_owned_task()
                except Exception:
                    pass
            raise

    def cancel_owned_task(self) -> None:
        task_id = self._owned_task_id
        if task_id is None:
            return
        response = self._client.cancel_task(task_id)
        if response.get("task_id") != task_id or response.get("status") != "cancelled":
            raise RuntimeError("rollout cancellation did not prove owned-task termination")
        self._owned_task_id = None

    @staticmethod
    def _validate_binding(canonical: CanonicalTaskRequest) -> None:
        request = canonical.request
        handoff = request.workspace_handoff
        context = request.runtime_context_binding
        if handoff is None or context is None:
            raise ValueError("native run owner requires workspace and runtime bindings")
        if (
            handoff.task_id != request.task_id
            or handoff.project_id != context.project_head.project_id
            or handoff.service_generation_sha256 != context.service_generation_sha256
            or handoff.framework_lock_sha256 != context.framework_lock_sha256
        ):
            raise ValueError("native TaskRequest binding authorities do not agree")

    @staticmethod
    def _terminal_result(
        status: TaskStatus,
        canonical: CanonicalTaskRequest,
    ) -> SessionResult:
        if (
            status.status != "completed"
            or status.total_sessions != 1
            or status.completed_sessions != 1
            or len(status.results) != 1
        ):
            raise RuntimeError("native rollout did not produce one completed session")
        result = SessionResult.model_validate(status.results[0])
        request = canonical.request
        if result.task_id != request.task_id or result.status is not SessionStatus.COMPLETED:
            raise RuntimeError("native session result does not match the owned task")
        if result.trajectory.metadata.get("capture_mode") != "transcript":
            raise RuntimeError("native session was not captured as an OpenEvo transcript")
        workspace = result.workspace_result
        handoff = request.workspace_handoff
        if workspace is None or handoff is None or workspace.handoff_id != handoff.handoff_id:
            raise RuntimeError("native session lacks the owned workspace result")
        return result


__all__ = ["BoundTaskRolloutClient", "NativeTaskRunOwner", "OwnedNativeTaskResult"]
