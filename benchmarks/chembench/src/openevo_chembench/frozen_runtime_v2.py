"""Strong runtime DTOs for the frozen ChemBench4K v2 test protocol."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_RENDERED_PROMPT_BYTES = 256 * 1024
_MAX_RESOLVED_MEMORY_BYTES = 64 * 1024


def _require_sha256(value: object, field_name: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _require_text(
    value: object,
    field_name: str,
    *,
    maximum_bytes: int,
) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
    encoded = value.encode("utf-8")
    if len(encoded) > maximum_bytes:
        raise ValueError(f"{field_name} exceeds its byte limit")
    if "\x00" in value:
        raise ValueError(f"{field_name} contains a NUL byte")


@dataclass(frozen=True, slots=True, init=False)
class CoreResolvedTextMemoryV2:
    """Text memory issued only after the benchmark-local Core bridge resolves it."""

    core_artifact_id: str
    artifact_payload_sha256: str
    context_resolution_digest: str
    resolved_memory_sha256: str
    markdown: str

    def __init__(
        self,
        *,
        core_artifact_id: str,
        artifact_payload_sha256: str,
        context_resolution_digest: str,
        resolved_memory_sha256: str,
        markdown: str,
        _issuer_token: object | None = None,
    ) -> None:
        if not _core_memory_token_is_valid(_issuer_token):
            raise TypeError("CoreResolvedTextMemoryV2 must be issued by the Core context bridge")
        object.__setattr__(self, "core_artifact_id", core_artifact_id)
        object.__setattr__(self, "artifact_payload_sha256", artifact_payload_sha256)
        object.__setattr__(
            self,
            "context_resolution_digest",
            context_resolution_digest,
        )
        object.__setattr__(
            self,
            "resolved_memory_sha256",
            resolved_memory_sha256,
        )
        object.__setattr__(self, "markdown", markdown)
        self.__post_init__()

    def __post_init__(self) -> None:
        _require_text(
            self.core_artifact_id,
            "core_artifact_id",
            maximum_bytes=512,
        )
        if "/" in self.core_artifact_id or "\\" in self.core_artifact_id:
            raise ValueError("core_artifact_id must not be a path")
        _require_sha256(
            self.artifact_payload_sha256,
            "artifact_payload_sha256",
        )
        _require_sha256(
            self.context_resolution_digest,
            "context_resolution_digest",
        )
        _require_sha256(
            self.resolved_memory_sha256,
            "resolved_memory_sha256",
        )
        _require_text(
            self.markdown,
            "markdown",
            maximum_bytes=_MAX_RESOLVED_MEMORY_BYTES,
        )
        actual = hashlib.sha256(self.markdown.encode("utf-8")).hexdigest()
        if actual != self.resolved_memory_sha256:
            raise ValueError("resolved_memory_sha256 does not match markdown")

    def to_runtime_payload(self) -> dict[str, str]:
        return {
            "artifact_type": "text_memory",
            "core_artifact_id": self.core_artifact_id,
            "artifact_payload_sha256": self.artifact_payload_sha256,
            "context_resolution_digest": self.context_resolution_digest,
            "resolved_memory_sha256": self.resolved_memory_sha256,
            "markdown": self.markdown,
        }


def _make_core_memory_issuer():
    """Keep the raw Core-context capability out of module globals."""

    issuer_token = object()

    def token_is_valid(candidate: object | None) -> bool:
        return candidate is issuer_token

    def issue(
        *,
        core_artifact_id: str,
        artifact_payload_sha256: str,
        context_resolution_digest: str,
        resolved_memory_sha256: str,
        markdown: str,
    ) -> CoreResolvedTextMemoryV2:
        return CoreResolvedTextMemoryV2(
            core_artifact_id=core_artifact_id,
            artifact_payload_sha256=artifact_payload_sha256,
            context_resolution_digest=context_resolution_digest,
            resolved_memory_sha256=resolved_memory_sha256,
            markdown=markdown,
            _issuer_token=issuer_token,
        )

    return token_is_valid, issue


(
    _core_memory_token_is_valid,
    _issue_core_resolved_text_memory_v2,
) = _make_core_memory_issuer()


@dataclass(frozen=True, slots=True)
class FrozenAgentRequestV2:
    """Only the official rendered prompt and optional Core-resolved memory."""

    rendered_public_prompt: str
    resolved_text_memory: CoreResolvedTextMemoryV2 | None = None

    def __post_init__(self) -> None:
        _require_text(
            self.rendered_public_prompt,
            "rendered_public_prompt",
            maximum_bytes=_MAX_RENDERED_PROMPT_BYTES,
        )
        if (
            self.resolved_text_memory is not None
            and type(self.resolved_text_memory) is not CoreResolvedTextMemoryV2
        ):
            raise TypeError("resolved_text_memory must be exact CoreResolvedTextMemoryV2 or None")

    def to_runtime_payload(self) -> dict[str, object]:
        return {
            "rendered_public_prompt": self.rendered_public_prompt,
            "resolved_text_memory": (
                None
                if self.resolved_text_memory is None
                else self.resolved_text_memory.to_runtime_payload()
            ),
        }


__all__ = [
    "CoreResolvedTextMemoryV2",
    "FrozenAgentRequestV2",
]
