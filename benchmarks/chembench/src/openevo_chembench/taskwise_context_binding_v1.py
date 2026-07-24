"""Exact per-session context evidence for taskwise online execution.

The binding contains only runtime identifiers and digests.  It never contains
the prompt, completion, benchmark target, or memory payload.  Both the runner
and executor derive their views independently and the runner must reject any
receipt whose expected and actual bindings differ.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from openevo_chembench.frozen_runtime_v2 import CoreResolvedTextMemoryV2


CONTEXT_BINDING_VIOLATION = "TASKWISE_CONTEXT_BINDING_VIOLATION"
_RUNTIME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}\Z", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


class TaskwiseContextBindingError(RuntimeError):
    """Closed context-binding failure carrying no prompt or memory content."""

    finding_code = CONTEXT_BINDING_VIOLATION

    def __init__(self) -> None:
        super().__init__(CONTEXT_BINDING_VIOLATION)


@dataclass(frozen=True, slots=True)
class TaskwiseSessionContextBindingV1:
    """Content-free identity of the context used by one immutable session."""

    session_id: str
    core_artifact_id: str | None
    artifact_payload_sha256: str | None
    resolved_memory_sha256: str | None
    context_resolution_digest: str | None

    def __post_init__(self) -> None:
        if type(self.session_id) is not str or _RUNTIME_ID.fullmatch(self.session_id) is None:
            raise ValueError("session_id must be a bounded runtime identifier")
        memory_fields = (
            self.core_artifact_id,
            self.artifact_payload_sha256,
            self.resolved_memory_sha256,
            self.context_resolution_digest,
        )
        if all(value is None for value in memory_fields):
            return
        if any(value is None for value in memory_fields):
            raise ValueError("context memory identity must be entirely present or absent")
        if (
            type(self.core_artifact_id) is not str
            or not self.core_artifact_id
            or "/" in self.core_artifact_id
            or "\\" in self.core_artifact_id
        ):
            raise ValueError("core_artifact_id must be a non-path identifier")
        for value in (
            self.artifact_payload_sha256,
            self.resolved_memory_sha256,
            self.context_resolution_digest,
        ):
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise ValueError("context memory digests must be lowercase SHA-256 values")

    @classmethod
    def from_memory(
        cls,
        *,
        session_id: str,
        memory: CoreResolvedTextMemoryV2 | None,
    ) -> TaskwiseSessionContextBindingV1:
        """Derive one view without accepting caller-supplied digest booleans."""

        if memory is None:
            return cls(
                session_id=session_id,
                core_artifact_id=None,
                artifact_payload_sha256=None,
                resolved_memory_sha256=None,
                context_resolution_digest=None,
            )
        if type(memory) is not CoreResolvedTextMemoryV2:
            raise TypeError("memory must be exact CoreResolvedTextMemoryV2 or None")
        return cls(
            session_id=session_id,
            core_artifact_id=memory.core_artifact_id,
            artifact_payload_sha256=memory.artifact_payload_sha256,
            resolved_memory_sha256=memory.resolved_memory_sha256,
            context_resolution_digest=memory.context_resolution_digest,
        )

    @classmethod
    def from_injected_markdown(
        cls,
        *,
        session_id: str,
        resolved_artifact_id: str | None,
        artifact_payload_sha256: str | None,
        context_resolution_digest: str | None,
        injected_markdown: str | None,
    ) -> TaskwiseSessionContextBindingV1:
        """Compute the actual injected-memory digest from final prompt bytes."""

        if injected_markdown is None:
            if any(
                value is not None
                for value in (
                    resolved_artifact_id,
                    artifact_payload_sha256,
                    context_resolution_digest,
                )
            ):
                raise ValueError("absent injected markdown cannot claim memory identity")
            resolved_memory_sha256 = None
        else:
            if type(injected_markdown) is not str:
                raise TypeError("injected_markdown must be text or None")
            resolved_memory_sha256 = hashlib.sha256(injected_markdown.encode("utf-8")).hexdigest()
        return cls(
            session_id=session_id,
            core_artifact_id=resolved_artifact_id,
            artifact_payload_sha256=artifact_payload_sha256,
            resolved_memory_sha256=resolved_memory_sha256,
            context_resolution_digest=context_resolution_digest,
        )

    @property
    def memory_present(self) -> bool:
        return self.core_artifact_id is not None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "session_id": self.session_id,
            "core_artifact_id": self.core_artifact_id,
            "artifact_payload_sha256": self.artifact_payload_sha256,
            "resolved_memory_sha256": self.resolved_memory_sha256,
            "context_resolution_digest": self.context_resolution_digest,
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical_bytes(self.to_dict())).hexdigest()


@dataclass(frozen=True, slots=True)
class TaskwiseContextBindingReceiptV1:
    """Expected/actual evidence whose decision is recomputed from both views."""

    expected: TaskwiseSessionContextBindingV1
    actual: TaskwiseSessionContextBindingV1
    schema_version: str = "taskwise_context_binding_receipt_v1"

    def __post_init__(self) -> None:
        if self.schema_version != "taskwise_context_binding_receipt_v1":
            raise ValueError("unexpected context-binding receipt schema")
        if (
            type(self.expected) is not TaskwiseSessionContextBindingV1
            or type(self.actual) is not TaskwiseSessionContextBindingV1
        ):
            raise TypeError("receipt bindings must use exact taskwise binding DTOs")

    @property
    def passed(self) -> bool:
        return self.expected == self.actual

    @property
    def finding_codes(self) -> tuple[str, ...]:
        return () if self.passed else (CONTEXT_BINDING_VIOLATION,)

    @property
    def expected_memory_artifact_id(self) -> str | None:
        return self.expected.core_artifact_id

    @property
    def actual_resolved_artifact_id(self) -> str | None:
        return self.actual.core_artifact_id

    @property
    def expected_memory_sha256(self) -> str | None:
        return self.expected.resolved_memory_sha256

    @property
    def actual_injected_memory_sha256(self) -> str | None:
        return self.actual.resolved_memory_sha256

    @property
    def context_resolution_digest(self) -> str | None:
        return self.actual.context_resolution_digest

    @property
    def session_id(self) -> str:
        return self.actual.session_id

    def require_match(self) -> None:
        if not self.passed:
            raise TaskwiseContextBindingError

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "expected_session_id": self.expected.session_id,
            "session_id": self.session_id,
            "expected_memory_artifact_id": self.expected_memory_artifact_id,
            "actual_resolved_artifact_id": self.actual_resolved_artifact_id,
            "expected_artifact_payload_sha256": (self.expected.artifact_payload_sha256),
            "actual_artifact_payload_sha256": self.actual.artifact_payload_sha256,
            "expected_memory_sha256": self.expected_memory_sha256,
            "actual_injected_memory_sha256": (self.actual_injected_memory_sha256),
            "expected_context_resolution_digest": (self.expected.context_resolution_digest),
            "context_resolution_digest": self.context_resolution_digest,
            "passed": self.passed,
            "finding_codes": list(self.finding_codes),
            "receipt_sha256": self.digest,
        }

    @property
    def digest(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "expected_session_id": self.expected.session_id,
            "session_id": self.session_id,
            "expected_memory_artifact_id": self.expected_memory_artifact_id,
            "actual_resolved_artifact_id": self.actual_resolved_artifact_id,
            "expected_artifact_payload_sha256": (self.expected.artifact_payload_sha256),
            "actual_artifact_payload_sha256": self.actual.artifact_payload_sha256,
            "expected_memory_sha256": self.expected_memory_sha256,
            "actual_injected_memory_sha256": (self.actual_injected_memory_sha256),
            "expected_context_resolution_digest": (self.expected.context_resolution_digest),
            "context_resolution_digest": self.context_resolution_digest,
            "finding_codes": list(self.finding_codes),
        }
        return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def issue_taskwise_context_binding_receipt_v1(
    *,
    expected: TaskwiseSessionContextBindingV1,
    actual: TaskwiseSessionContextBindingV1,
) -> TaskwiseContextBindingReceiptV1:
    """Issue a receipt while recomputing, rather than accepting, the decision."""

    return TaskwiseContextBindingReceiptV1(expected=expected, actual=actual)


def taskwise_context_binding_receipt_from_public_dict(
    payload: object,
) -> TaskwiseContextBindingReceiptV1:
    """Recompute a persisted receipt through an exact public schema."""

    if type(payload) is not dict or set(payload) != {
        "schema_version",
        "expected_session_id",
        "session_id",
        "expected_memory_artifact_id",
        "actual_resolved_artifact_id",
        "expected_artifact_payload_sha256",
        "actual_artifact_payload_sha256",
        "expected_memory_sha256",
        "actual_injected_memory_sha256",
        "expected_context_resolution_digest",
        "context_resolution_digest",
        "passed",
        "finding_codes",
        "receipt_sha256",
    }:
        raise ValueError("persisted context-binding receipt schema is invalid")
    expected = TaskwiseSessionContextBindingV1(
        session_id=payload["expected_session_id"],
        core_artifact_id=payload["expected_memory_artifact_id"],
        artifact_payload_sha256=payload["expected_artifact_payload_sha256"],
        resolved_memory_sha256=payload["expected_memory_sha256"],
        context_resolution_digest=payload["expected_context_resolution_digest"],
    )
    actual = TaskwiseSessionContextBindingV1(
        session_id=payload["session_id"],
        core_artifact_id=payload["actual_resolved_artifact_id"],
        artifact_payload_sha256=payload["actual_artifact_payload_sha256"],
        resolved_memory_sha256=payload["actual_injected_memory_sha256"],
        context_resolution_digest=payload["context_resolution_digest"],
    )
    receipt = issue_taskwise_context_binding_receipt_v1(
        expected=expected,
        actual=actual,
    )
    if receipt.to_public_dict() != payload:
        raise ValueError("persisted context-binding receipt content changed")
    return receipt


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
    ).encode("utf-8")


__all__ = [
    "CONTEXT_BINDING_VIOLATION",
    "TaskwiseContextBindingError",
    "TaskwiseContextBindingReceiptV1",
    "TaskwiseSessionContextBindingV1",
    "issue_taskwise_context_binding_receipt_v1",
    "taskwise_context_binding_receipt_from_public_dict",
]
