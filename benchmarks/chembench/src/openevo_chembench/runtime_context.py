"""Closed runtime projection for public prompts and approved artifacts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from openevo_chembench.artifacts import (
    AgentSystemPayload,
    ApprovedArtifact,
    ArtifactKind,
    SkillBundlePayload,
    TextMemoryPayload,
)
from openevo_chembench.models import PublicPrompt, RawAttempt


_SHA256 = re.compile(r"[0-9a-f]{64}")
_RUNTIME_ID = re.compile(r"(?:run|episode)_[0-9a-f]{24}")
_AGENT_CONTEXT_ISSUER = object()


def _require_non_empty_text(value: object, field_name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_sha256(value: object, field_name: str) -> None:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _require_runtime_id(value: object, field_name: str) -> None:
    if type(value) is not str or _RUNTIME_ID.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a controller-generated runtime id")


def _require_non_negative_integer(value: object, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class AgentContextFile:
    """One validator-approved skill file exposed to the Agent Runtime."""

    relative_path: str
    content: str

    def __post_init__(self) -> None:
        _require_non_empty_text(self.relative_path, "AgentContextFile.relative_path")
        _require_non_empty_text(self.content, "AgentContextFile.content")

    def to_agent_payload(self) -> dict[str, str]:
        return {
            "relative_path": self.relative_path,
            "content": self.content,
        }


@dataclass(frozen=True, slots=True, init=False)
class AgentArtifactContext:
    """Closed runtime projection of exactly one ``ApprovedArtifact``."""

    artifact_type: ArtifactKind
    artifact_hash: str
    artifact_version: int
    source_round_index: int
    markdown: str | None = None
    files: tuple[AgentContextFile, ...] = ()
    target_path: str | None = None

    def __init__(
        self,
        *,
        artifact_type: ArtifactKind,
        artifact_hash: str,
        artifact_version: int,
        source_round_index: int,
        markdown: str | None = None,
        files: tuple[AgentContextFile, ...] = (),
        target_path: str | None = None,
        _issuer_token: object | None = None,
    ) -> None:
        if _issuer_token is not _AGENT_CONTEXT_ISSUER:
            raise TypeError(
                "AgentArtifactContext must be issued by ArtifactContextResolver"
            )
        object.__setattr__(self, "artifact_type", artifact_type)
        object.__setattr__(self, "artifact_hash", artifact_hash)
        object.__setattr__(self, "artifact_version", artifact_version)
        object.__setattr__(self, "source_round_index", source_round_index)
        object.__setattr__(self, "markdown", markdown)
        object.__setattr__(self, "files", files)
        object.__setattr__(self, "target_path", target_path)
        self.__post_init__()

    def __post_init__(self) -> None:
        if type(self.artifact_type) is not ArtifactKind:
            raise TypeError("AgentArtifactContext.artifact_type must be ArtifactKind")
        _require_sha256(self.artifact_hash, "AgentArtifactContext.artifact_hash")
        if (
            isinstance(self.artifact_version, bool)
            or not isinstance(self.artifact_version, int)
            or self.artifact_version < 1
        ):
            raise ValueError(
                "AgentArtifactContext.artifact_version must be a positive integer"
            )
        _require_non_negative_integer(
            self.source_round_index,
            "AgentArtifactContext.source_round_index",
        )
        if not isinstance(self.files, tuple) or not all(
            type(file) is AgentContextFile for file in self.files
        ):
            raise TypeError(
                "AgentArtifactContext.files must contain exact AgentContextFile values"
            )

        if self.artifact_type is ArtifactKind.TEXT_MEMORY:
            _require_non_empty_text(self.markdown, "AgentArtifactContext.markdown")
            if self.files or self.target_path is not None:
                raise ValueError("text_memory context has invalid extra fields")
        elif self.artifact_type is ArtifactKind.SKILL_BUNDLE:
            if not self.files or self.markdown is not None or self.target_path is not None:
                raise ValueError("skill_bundle context has invalid content fields")
        elif self.artifact_type is ArtifactKind.AGENT_SYSTEM:
            _require_non_empty_text(self.markdown, "AgentArtifactContext.markdown")
            if self.files or self.target_path != "AGENTS.md":
                raise ValueError("agent_system context must target AGENTS.md")
        else:
            raise TypeError("unsupported AgentArtifactContext artifact type")

    def to_agent_payload(self) -> dict[str, object]:
        """Serialize only approved content and answer-free artifact metadata."""

        result: dict[str, object] = {
            "artifact_type": self.artifact_type.value,
            "artifact_hash": self.artifact_hash,
            "artifact_version": self.artifact_version,
            "source_round_index": self.source_round_index,
        }
        if self.artifact_type is ArtifactKind.SKILL_BUNDLE:
            result["files"] = [file.to_agent_payload() for file in self.files]
        else:
            result["markdown"] = self.markdown
            if self.artifact_type is ArtifactKind.AGENT_SYSTEM:
                result["target_path"] = self.target_path
        return result


class ArtifactContextResolver:
    """Project only validator-issued artifacts into the Agent Runtime."""

    __slots__ = ()

    def resolve(self, artifact: ApprovedArtifact) -> AgentArtifactContext:
        if type(artifact) is not ApprovedArtifact:
            raise TypeError(
                "ArtifactContextResolver.resolve requires an exact ApprovedArtifact"
            )
        payload = artifact.payload
        if type(payload) is TextMemoryPayload:
            return AgentArtifactContext(
                artifact_type=artifact.kind,
                artifact_hash=artifact.artifact_hash,
                artifact_version=artifact.version,
                source_round_index=artifact.lineage.round_index,
                markdown=payload.markdown,
                _issuer_token=_AGENT_CONTEXT_ISSUER,
            )
        if type(payload) is SkillBundlePayload:
            return AgentArtifactContext(
                artifact_type=artifact.kind,
                artifact_hash=artifact.artifact_hash,
                artifact_version=artifact.version,
                source_round_index=artifact.lineage.round_index,
                files=tuple(
                    AgentContextFile(
                        relative_path=file.relative_path,
                        content=file.content,
                    )
                    for file in payload.files
                ),
                _issuer_token=_AGENT_CONTEXT_ISSUER,
            )
        if type(payload) is AgentSystemPayload:
            return AgentArtifactContext(
                artifact_type=artifact.kind,
                artifact_hash=artifact.artifact_hash,
                artifact_version=artifact.version,
                source_round_index=artifact.lineage.round_index,
                markdown=payload.markdown,
                target_path=payload.target_path,
                _issuer_token=_AGENT_CONTEXT_ISSUER,
            )
        raise TypeError("unsupported approved artifact payload")


@dataclass(frozen=True, slots=True)
class AgentRoundRequest:
    """The complete request visible to the future Codex executor adapter."""

    run_id: str
    episode_id: str
    round_index: int
    public_prompt: PublicPrompt
    artifact_context: tuple[AgentArtifactContext, ...] = ()

    def __post_init__(self) -> None:
        _require_runtime_id(self.run_id, "AgentRoundRequest.run_id")
        _require_runtime_id(self.episode_id, "AgentRoundRequest.episode_id")
        _require_non_negative_integer(
            self.round_index,
            "AgentRoundRequest.round_index",
        )
        if type(self.public_prompt) is not PublicPrompt:
            raise TypeError("AgentRoundRequest.public_prompt must be exact PublicPrompt")
        if not isinstance(self.artifact_context, tuple) or not all(
            type(context) is AgentArtifactContext
            for context in self.artifact_context
        ):
            raise TypeError(
                "AgentRoundRequest.artifact_context must contain exact contexts"
            )
        artifact_types = tuple(
            context.artifact_type for context in self.artifact_context
        )
        if len(artifact_types) != len(set(artifact_types)):
            raise ValueError("AgentRoundRequest cannot contain duplicate artifact types")

    def to_runtime_payload(self) -> dict[str, object]:
        """Return the strict request payload permitted to cross into Agent Runtime."""

        return {
            "run_id": self.run_id,
            "episode_id": self.episode_id,
            "round": self.round_index,
            "public_prompt": self.public_prompt.to_agent_payload(),
            "artifact_context": [
                context.to_agent_payload() for context in self.artifact_context
            ],
        }


class AgentExecutor(Protocol):
    """One-round Agent Runtime boundary implemented by the real Codex adapter."""

    def execute(self, request: AgentRoundRequest) -> RawAttempt:
        """Execute exactly one isolated agent round."""


__all__ = [
    "AgentArtifactContext",
    "AgentContextFile",
    "AgentExecutor",
    "AgentRoundRequest",
    "ArtifactContextResolver",
]
