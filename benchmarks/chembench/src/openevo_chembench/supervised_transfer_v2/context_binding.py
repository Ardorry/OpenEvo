"""Three-target session request and content-free context-binding evidence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Literal

from openevo_chembench.supervised_transfer_v2.artifacts import (
    CoreResolvedAuxiliaryArtifactV2,
    CoreResolvedSupervisedContextV2,
)

CONTEXT_BINDING_VIOLATION = "SUPERVISED_V2_CONTEXT_BINDING_VIOLATION"
_RUNTIME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}\Z", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_TARGET_ORDER = ("text_memory", "skill_bundle", "agent_system")


class SupervisedContextBindingError(RuntimeError):
    """Closed failure that carries no prompt or evolved artifact content."""

    finding_code = CONTEXT_BINDING_VIOLATION

    def __init__(self) -> None:
        super().__init__(CONTEXT_BINDING_VIOLATION)


@dataclass(frozen=True, slots=True)
class SupervisedAgentRequestV2:
    """Official public prompt plus an optional complete three-target context."""

    rendered_public_prompt: str
    resolved_context: CoreResolvedSupervisedContextV2 | None
    session_id: str
    arm: Literal["control", "online"]
    task_ordinal: int
    round_index: Literal[0, 1, 2, 3]
    run_id: str | None = None
    task_uid: str | None = None

    def __post_init__(self) -> None:
        if type(self.rendered_public_prompt) is not str or not self.rendered_public_prompt:
            raise ValueError("rendered public prompt must be non-empty")
        if self.resolved_context is not None and type(self.resolved_context) is not CoreResolvedSupervisedContextV2:
            raise TypeError("resolved context must be exact or None")
        if type(self.session_id) is not str or _RUNTIME_ID.fullmatch(self.session_id) is None:
            raise ValueError("session ID must be a bounded runtime identifier")
        if self.arm not in ("control", "online"):
            raise ValueError("arm must be control or online")
        if isinstance(self.task_ordinal, bool) or not isinstance(self.task_ordinal, int) or self.task_ordinal < 0:
            raise ValueError("task ordinal must be non-negative")
        if self.round_index not in (0, 1, 2, 3):
            raise ValueError("round index must be 0, 1, 2, or 3")
        if self.run_id is not None and (
            type(self.run_id) is not str or _RUNTIME_ID.fullmatch(self.run_id) is None
        ):
            raise ValueError("run ID must be a bounded runtime identifier or None")
        if self.task_uid is not None and (
            type(self.task_uid) is not str or _SHA256.fullmatch(self.task_uid) is None
        ):
            raise ValueError("task UID must be a lowercase SHA-256 or None")
        if self.arm == "control" and self.resolved_context is not None:
            raise ValueError("control request must not contain evolved context")

    def to_runtime_payload(self) -> dict[str, object]:
        return {
            "rendered_public_prompt": self.rendered_public_prompt,
            "resolved_context": (
                None
                if self.resolved_context is None
                else self.resolved_context.to_runtime_payload()
            ),
        }


@dataclass(frozen=True, slots=True)
class SupervisedTargetBindingV2:
    """Content-free identity for one injected target."""

    target_id: Literal["text_memory", "skill_bundle", "agent_system"]
    core_artifact_id: str
    artifact_payload_sha256: str
    resolved_content_sha256: str
    context_resolution_digest: str

    def __post_init__(self) -> None:
        if self.target_id not in _TARGET_ORDER:
            raise ValueError("supervised target is not frozen")
        if not self.core_artifact_id or "/" in self.core_artifact_id or "\\" in self.core_artifact_id:
            raise ValueError("Core artifact ID must be a non-path identifier")
        for value in (
            self.artifact_payload_sha256,
            self.resolved_content_sha256,
            self.context_resolution_digest,
        ):
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise ValueError("target binding digest must be lowercase SHA-256")

    def to_dict(self) -> dict[str, str]:
        return {
            "target_id": self.target_id,
            "core_artifact_id": self.core_artifact_id,
            "artifact_payload_sha256": self.artifact_payload_sha256,
            "resolved_content_sha256": self.resolved_content_sha256,
            "context_resolution_digest": self.context_resolution_digest,
        }


@dataclass(frozen=True, slots=True)
class SupervisedSessionContextBindingV2:
    """Exact target identities used by one immutable task session."""

    session_id: str
    targets: tuple[SupervisedTargetBindingV2, ...]

    def __post_init__(self) -> None:
        if type(self.session_id) is not str or _RUNTIME_ID.fullmatch(self.session_id) is None:
            raise ValueError("session ID must be a bounded runtime identifier")
        if type(self.targets) is not tuple or any(
            type(value) is not SupervisedTargetBindingV2 for value in self.targets
        ):
            raise TypeError("supervised context targets must use exact binding DTOs")
        if self.targets and tuple(value.target_id for value in self.targets) != _TARGET_ORDER:
            raise ValueError("supervised context must contain all targets in frozen order")

    @classmethod
    def from_context(
        cls,
        *,
        session_id: str,
        context: CoreResolvedSupervisedContextV2 | None,
    ) -> SupervisedSessionContextBindingV2:
        if context is None:
            return cls(session_id=session_id, targets=())
        if type(context) is not CoreResolvedSupervisedContextV2:
            raise TypeError("context must be exact CoreResolvedSupervisedContextV2 or None")
        return cls(
            session_id=session_id,
            targets=(
                SupervisedTargetBindingV2(
                    target_id="text_memory",
                    core_artifact_id=context.memory.core_artifact_id,
                    artifact_payload_sha256=context.memory.artifact_payload_sha256,
                    resolved_content_sha256=context.memory.resolved_memory_sha256,
                    context_resolution_digest=context.memory.context_resolution_digest,
                ),
                _auxiliary_binding(context.skill),
                _auxiliary_binding(context.agent_system),
            ),
        )

    @classmethod
    def from_injected_context(
        cls,
        *,
        session_id: str,
        context: CoreResolvedSupervisedContextV2 | None,
    ) -> SupervisedSessionContextBindingV2:
        """Re-hash final injected bytes independently from capability digest fields."""

        if context is None:
            return cls(session_id=session_id, targets=())
        if type(context) is not CoreResolvedSupervisedContextV2:
            raise TypeError("context must be exact CoreResolvedSupervisedContextV2 or None")
        memory = context.memory
        return cls(
            session_id=session_id,
            targets=(
                SupervisedTargetBindingV2(
                    target_id="text_memory",
                    core_artifact_id=memory.core_artifact_id,
                    artifact_payload_sha256=memory.artifact_payload_sha256,
                    resolved_content_sha256=_sha256_text(memory.markdown),
                    context_resolution_digest=memory.context_resolution_digest,
                ),
                _injected_auxiliary_binding(context.skill),
                _injected_auxiliary_binding(context.agent_system),
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "targets": [value.to_dict() for value in self.targets],
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical_bytes(self.to_dict())).hexdigest()


@dataclass(frozen=True, slots=True)
class SupervisedContextBindingReceiptV2:
    """Expected/actual binding whose decision is always recomputed."""

    expected: SupervisedSessionContextBindingV2
    actual: SupervisedSessionContextBindingV2
    schema_version: str = "supervised_context_binding_receipt_v2"

    def __post_init__(self) -> None:
        if self.schema_version != "supervised_context_binding_receipt_v2":
            raise ValueError("unexpected supervised context receipt schema")
        if (
            type(self.expected) is not SupervisedSessionContextBindingV2
            or type(self.actual) is not SupervisedSessionContextBindingV2
        ):
            raise TypeError("receipt bindings must use exact supervised DTOs")

    @property
    def passed(self) -> bool:
        return self.expected == self.actual

    @property
    def finding_codes(self) -> tuple[str, ...]:
        return () if self.passed else (CONTEXT_BINDING_VIOLATION,)

    def require_match(self) -> None:
        if not self.passed:
            raise SupervisedContextBindingError

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            _canonical_bytes(
                {
                    "schema_version": self.schema_version,
                    "expected": self.expected.to_dict(),
                    "actual": self.actual.to_dict(),
                }
            )
        ).hexdigest()

    def to_public_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.actual.session_id,
            "expected_targets": [value.to_dict() for value in self.expected.targets],
            "actual_targets": [value.to_dict() for value in self.actual.targets],
            "passed": self.passed,
            "finding_codes": list(self.finding_codes),
            "receipt_sha256": self.digest,
        }


def issue_supervised_context_binding_receipt_v2(
    *,
    expected: SupervisedSessionContextBindingV2,
    actual: SupervisedSessionContextBindingV2,
) -> SupervisedContextBindingReceiptV2:
    return SupervisedContextBindingReceiptV2(expected=expected, actual=actual)


def _auxiliary_binding(
    artifact: CoreResolvedAuxiliaryArtifactV2,
) -> SupervisedTargetBindingV2:
    return SupervisedTargetBindingV2(
        target_id=artifact.target_id,
        core_artifact_id=artifact.core_artifact_id,
        artifact_payload_sha256=artifact.artifact_payload_sha256,
        resolved_content_sha256=artifact.resolved_content_sha256,
        context_resolution_digest=artifact.context_resolution_digest,
    )


def _injected_auxiliary_binding(
    artifact: CoreResolvedAuxiliaryArtifactV2,
) -> SupervisedTargetBindingV2:
    return SupervisedTargetBindingV2(
        target_id=artifact.target_id,
        core_artifact_id=artifact.core_artifact_id,
        artifact_payload_sha256=artifact.artifact_payload_sha256,
        resolved_content_sha256=_sha256_text(artifact.markdown),
        context_resolution_digest=artifact.context_resolution_digest,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


__all__ = [
    "CONTEXT_BINDING_VIOLATION",
    "SupervisedAgentRequestV2",
    "SupervisedContextBindingError",
    "SupervisedContextBindingReceiptV2",
    "SupervisedSessionContextBindingV2",
    "SupervisedTargetBindingV2",
    "issue_supervised_context_binding_receipt_v2",
]
