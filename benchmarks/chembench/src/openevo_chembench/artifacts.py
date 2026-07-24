"""Typed evolution artifacts and their validation-gated lifecycle."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import PurePosixPath
from typing import Union


_SHA256 = re.compile(r"[0-9a-f]{64}")


def _require_non_empty_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_sha256(value: object, field_name: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hex digest")


class ArtifactKind(str, Enum):
    """The only evolution artifact kinds allowed by this experiment."""

    TEXT_MEMORY = "text_memory"
    SKILL_BUNDLE = "skill_bundle"
    AGENT_SYSTEM = "agent_system"


@dataclass(frozen=True, slots=True)
class TextMemoryPayload:
    """General strategy memory; content remains untrusted until validation."""

    markdown: str

    def __post_init__(self) -> None:
        _require_non_empty_text(self.markdown, "TextMemoryPayload.markdown")

    @property
    def kind(self) -> ArtifactKind:
        return ArtifactKind.TEXT_MEMORY


@dataclass(frozen=True, slots=True)
class ArtifactFile:
    """One text file in a skill bundle."""

    relative_path: str
    content: str

    def __post_init__(self) -> None:
        _require_non_empty_text(self.relative_path, "ArtifactFile.relative_path")
        _require_non_empty_text(self.content, "ArtifactFile.content")
        path = PurePosixPath(self.relative_path)
        if (
            not path.parts
            or path == PurePosixPath(".")
            or path.is_absolute()
            or ".." in path.parts
            or "." in path.parts
        ):
            raise ValueError("ArtifactFile.relative_path must be a safe relative path")
        if str(path) != self.relative_path:
            raise ValueError("ArtifactFile.relative_path must be normalized POSIX text")


@dataclass(frozen=True, slots=True)
class SkillBundlePayload:
    """A future Codex skill bundle; root SKILL.md is mandatory."""

    files: tuple[ArtifactFile, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.files, tuple) or not self.files:
            raise ValueError(
                "SkillBundlePayload.files must be a non-empty immutable tuple"
            )
        if not all(type(file) is ArtifactFile for file in self.files):
            raise TypeError(
                "SkillBundlePayload.files must contain exact ArtifactFile values"
            )
        paths = tuple(file.relative_path for file in self.files)
        if len(paths) != len(set(paths)):
            raise ValueError("SkillBundlePayload.files must not contain duplicate paths")
        if "SKILL.md" not in paths:
            raise ValueError("SkillBundlePayload.files must contain root SKILL.md")

    @property
    def kind(self) -> ArtifactKind:
        return ArtifactKind.SKILL_BUNDLE


@dataclass(frozen=True, slots=True)
class AgentSystemPayload:
    """General Codex system instruction; only AGENTS.md is in scope."""

    markdown: str
    target_path: str = "AGENTS.md"

    def __post_init__(self) -> None:
        _require_non_empty_text(self.markdown, "AgentSystemPayload.markdown")
        if self.target_path != "AGENTS.md":
            raise ValueError(
                "AgentSystemPayload.target_path must be AGENTS.md for the Codex harness"
            )

    @property
    def kind(self) -> ArtifactKind:
        return ArtifactKind.AGENT_SYSTEM


ArtifactPayload = Union[
    TextMemoryPayload,
    SkillBundlePayload,
    AgentSystemPayload,
]


@dataclass(frozen=True, slots=True)
class ArtifactLineage:
    """Answer-free lineage recorded for each artifact candidate."""

    round_index: int
    safe_signal_hash: str
    parent_artifact_hash: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.round_index, bool) or not isinstance(self.round_index, int):
            raise TypeError("ArtifactLineage.round_index must be an integer")
        if self.round_index < 0:
            raise ValueError("ArtifactLineage.round_index must be non-negative")
        _require_sha256(self.safe_signal_hash, "ArtifactLineage.safe_signal_hash")
        if self.parent_artifact_hash is not None:
            _require_sha256(
                self.parent_artifact_hash,
                "ArtifactLineage.parent_artifact_hash",
            )


@dataclass(frozen=True, slots=True)
class CandidateArtifact:
    """Untrusted reflector output; never injected into a runtime directly."""

    version: int
    payload: ArtifactPayload
    lineage: ArtifactLineage

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise TypeError("CandidateArtifact.version must be an integer")
        if self.version < 1:
            raise ValueError("CandidateArtifact.version must be at least 1")
        if type(self.payload) not in (
            TextMemoryPayload,
            SkillBundlePayload,
            AgentSystemPayload,
        ):
            raise TypeError("CandidateArtifact.payload has an unsupported artifact type")
        if type(self.lineage) is not ArtifactLineage:
            raise TypeError("CandidateArtifact.lineage must be exact ArtifactLineage")

    @property
    def kind(self) -> ArtifactKind:
        return self.payload.kind

    @property
    def artifact_type(self) -> ArtifactKind:
        """Closed artifact type exposed for proposal-stage review."""

        return self.kind

    @property
    def parent_hash(self) -> str | None:
        """Hash of the parent artifact, if this proposal has one."""

        return self.lineage.parent_artifact_hash

    @property
    def source_signal_hash(self) -> str:
        """Hash of the safe taxonomy signal that produced this proposal."""

        return self.lineage.safe_signal_hash

    @property
    def round_index(self) -> int:
        """Evolution round that produced this untrusted proposal."""

        return self.lineage.round_index

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(_canonical_payload_bytes(self.payload)).hexdigest()

    @property
    def candidate_hash(self) -> str:
        """Hash artifact identity, payload content, and complete lineage."""

        return hashlib.sha256(_canonical_candidate_bytes(self)).hexdigest()


class ValidationDecision(str, Enum):
    """Fail-closed artifact validation decision."""

    APPROVED = "approved"
    REJECTED = "rejected"


class ValidationFinding(str, Enum):
    """Closed receipt vocabulary; findings cannot carry benchmark literals."""

    LEAK_ANSWER_LITERAL = "leak_answer_literal"
    LEAK_TARGET_LITERAL = "leak_target_literal"
    LEAK_TARGET_SCORES_LITERAL = "leak_target_scores_literal"
    LEAK_CANARY_LITERAL = "leak_canary_literal"
    LEAK_UUID_LITERAL = "leak_uuid_literal"
    LEAK_CORRECT_ANSWER_LITERAL = "leak_correct_answer_literal"
    LEAK_OPTION_MAPPING = "leak_option_mapping"
    LEAK_QUESTION_SIMILARITY = "leak_question_similarity"
    DISALLOWED_FILE = "disallowed_file"
    INVALID_SKILL_FORMAT = "invalid_skill_format"
    ANSWER_CACHE = "answer_cache"
    LOOKUP_TABLE = "lookup_table"
    BENCHMARK_DATABASE = "benchmark_database"
    ANSWER_LEAKAGE = "answer_leakage"
    QUESTION_LEAKAGE = "question_leakage"
    TARGET_LEAKAGE = "target_leakage"
    UUID_LEAKAGE = "uuid_leakage"
    CANARY_LEAKAGE = "canary_leakage"
    BENCHMARK_SPECIFIC_INSTRUCTION = "benchmark_specific_instruction"
    INVALID_STRUCTURE = "invalid_structure"
    SIMILARITY_MATCH = "similarity_match"
    VALIDATOR_ERROR = "validator_error"


_VALIDATION_RECEIPT_ISSUER = object()
_APPROVED_ARTIFACT_ISSUER = object()


@dataclass(frozen=True, slots=True, init=False)
class ValidationReceipt:
    """Validator-issued result bound to exact candidate content."""

    validator_version: str
    decision: ValidationDecision
    candidate_hash: str
    finding_codes: tuple[ValidationFinding, ...] = ()

    def __init__(
        self,
        *,
        validator_version: str,
        decision: ValidationDecision,
        candidate_hash: str,
        finding_codes: tuple[ValidationFinding, ...] = (),
        _issuer_token: object | None = None,
    ) -> None:
        if _issuer_token is not _VALIDATION_RECEIPT_ISSUER:
            raise TypeError(
                "ValidationReceipt must be issued by EvolutionArtifactValidator"
            )
        object.__setattr__(self, "validator_version", validator_version)
        object.__setattr__(self, "decision", decision)
        object.__setattr__(self, "candidate_hash", candidate_hash)
        object.__setattr__(self, "finding_codes", finding_codes)
        self.__post_init__()

    def __post_init__(self) -> None:
        _require_non_empty_text(
            self.validator_version,
            "ValidationReceipt.validator_version",
        )
        if not isinstance(self.decision, ValidationDecision):
            raise TypeError("ValidationReceipt.decision must be ValidationDecision")
        _require_sha256(self.candidate_hash, "ValidationReceipt.candidate_hash")
        if not isinstance(self.finding_codes, tuple):
            raise TypeError("ValidationReceipt.finding_codes must be an immutable tuple")
        if not all(isinstance(code, ValidationFinding) for code in self.finding_codes):
            raise TypeError(
                "ValidationReceipt.finding_codes must contain ValidationFinding values"
            )
        if len(self.finding_codes) != len(set(self.finding_codes)):
            raise ValueError("ValidationReceipt.finding_codes must be unique")
        if self.decision is ValidationDecision.APPROVED and self.finding_codes:
            raise ValueError("approved ValidationReceipt must not contain findings")
        if self.decision is ValidationDecision.REJECTED and not self.finding_codes:
            raise ValueError("rejected ValidationReceipt must contain findings")

    @property
    def passed(self) -> bool:
        """Whether this validator-issued receipt permits promotion."""

        return self.decision is ValidationDecision.APPROVED

    def to_audit_payload(self) -> dict[str, object]:
        """Serialize only closed, answer-free audit fields."""

        return {
            "candidate_hash": self.candidate_hash,
            "validator_version": self.validator_version,
            "passed": self.passed,
            "finding_codes": [finding.value for finding in self.finding_codes],
        }


def _issue_validation_receipt(
    *,
    validator_version: str,
    decision: ValidationDecision,
    candidate_hash: str,
    finding_codes: tuple[ValidationFinding, ...] = (),
) -> ValidationReceipt:
    """Package-private receipt issuer used only by the security validator."""

    return ValidationReceipt(
        validator_version=validator_version,
        decision=decision,
        candidate_hash=candidate_hash,
        finding_codes=finding_codes,
        _issuer_token=_VALIDATION_RECEIPT_ISSUER,
    )


@dataclass(frozen=True, slots=True, init=False)
class ApprovedArtifact:
    """Validator-approved artifact eligible for next-round injection."""

    version: int
    payload: ArtifactPayload
    lineage: ArtifactLineage
    artifact_hash: str
    validator_receipt: ValidationReceipt

    def __init__(
        self,
        *,
        version: int,
        payload: ArtifactPayload,
        lineage: ArtifactLineage,
        artifact_hash: str,
        validator_receipt: ValidationReceipt,
        _issuer_token: object | None = None,
    ) -> None:
        if _issuer_token is not _APPROVED_ARTIFACT_ISSUER:
            raise TypeError(
                "ApprovedArtifact must be created by EvolutionArtifactValidator"
            )
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "payload", payload)
        object.__setattr__(self, "lineage", lineage)
        object.__setattr__(self, "artifact_hash", artifact_hash)
        object.__setattr__(self, "validator_receipt", validator_receipt)
        self.__post_init__()

    def __post_init__(self) -> None:
        candidate = CandidateArtifact(
            version=self.version,
            payload=self.payload,
            lineage=self.lineage,
        )
        if type(self.validator_receipt) is not ValidationReceipt:
            raise TypeError(
                "ApprovedArtifact requires a validator-issued ValidationReceipt"
            )
        if self.validator_receipt.decision is not ValidationDecision.APPROVED:
            raise ValueError("ApprovedArtifact requires an approved validator receipt")
        _require_sha256(self.artifact_hash, "ApprovedArtifact.artifact_hash")
        expected_hash = hashlib.sha256(_canonical_payload_bytes(self.payload)).hexdigest()
        if self.artifact_hash != expected_hash:
            raise ValueError("ApprovedArtifact.artifact_hash does not match payload")
        if self.validator_receipt.candidate_hash != candidate.candidate_hash:
            raise ValueError(
                "ApprovedArtifact validator receipt candidate hash mismatch"
            )

    @property
    def kind(self) -> ArtifactKind:
        return self.payload.kind


def _promote_candidate(
    candidate: CandidateArtifact,
    receipt: ValidationReceipt,
) -> ApprovedArtifact:
    """Package-private promotion primitive used only by the validator."""

    if type(candidate) is not CandidateArtifact:
        raise TypeError("ApprovedArtifact requires an exact CandidateArtifact")
    if type(receipt) is not ValidationReceipt:
        raise TypeError("ApprovedArtifact requires a validator-issued receipt")
    if receipt.decision is not ValidationDecision.APPROVED:
        raise ValueError("rejected candidate cannot become an ApprovedArtifact")
    if receipt.candidate_hash != candidate.candidate_hash:
        raise ValueError("validator receipt does not match complete candidate hash")
    return ApprovedArtifact(
        version=candidate.version,
        payload=candidate.payload,
        lineage=candidate.lineage,
        artifact_hash=candidate.content_hash,
        validator_receipt=receipt,
        _issuer_token=_APPROVED_ARTIFACT_ISSUER,
    )


@dataclass(frozen=True, slots=True)
class EvolutionArtifactSet:
    """The complete non-parametric OpenEvo agent state for one round."""

    text_memory: ApprovedArtifact | None = None
    skill_bundle: ApprovedArtifact | None = None
    agent_system: ApprovedArtifact | None = None

    def __post_init__(self) -> None:
        self._require_kind(self.text_memory, ArtifactKind.TEXT_MEMORY, "text_memory")
        self._require_kind(self.skill_bundle, ArtifactKind.SKILL_BUNDLE, "skill_bundle")
        self._require_kind(self.agent_system, ArtifactKind.AGENT_SYSTEM, "agent_system")

    @staticmethod
    def _require_kind(
        artifact: ApprovedArtifact | None,
        expected: ArtifactKind,
        field_name: str,
    ) -> None:
        if artifact is not None and type(artifact) is not ApprovedArtifact:
            raise TypeError(
                f"EvolutionArtifactSet.{field_name} must be exact ApprovedArtifact"
            )
        if artifact is not None and artifact.kind is not expected:
            raise ValueError(
                f"EvolutionArtifactSet.{field_name} requires {expected.value}"
            )

    def with_artifact(self, artifact: ApprovedArtifact) -> EvolutionArtifactSet:
        """Return a new set with one approved artifact version replaced."""

        if artifact.kind is ArtifactKind.TEXT_MEMORY:
            return replace(self, text_memory=artifact)
        if artifact.kind is ArtifactKind.SKILL_BUNDLE:
            return replace(self, skill_bundle=artifact)
        if artifact.kind is ArtifactKind.AGENT_SYSTEM:
            return replace(self, agent_system=artifact)
        raise ValueError(f"unsupported artifact kind: {artifact.kind}")


def _canonical_payload_bytes(payload: ArtifactPayload) -> bytes:
    if type(payload) is TextMemoryPayload:
        value: dict[str, object] = {
            "artifact_type": payload.kind.value,
            "markdown": payload.markdown,
        }
    elif type(payload) is SkillBundlePayload:
        value = {
            "artifact_type": payload.kind.value,
            "files": [
                {
                    "relative_path": file.relative_path,
                    "content": file.content,
                }
                for file in sorted(payload.files, key=lambda item: item.relative_path)
            ],
        }
    elif type(payload) is AgentSystemPayload:
        value = {
            "artifact_type": payload.kind.value,
            "markdown": payload.markdown,
            "target_path": payload.target_path,
        }
    else:
        raise TypeError("unsupported artifact payload")
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_candidate_bytes(candidate: CandidateArtifact) -> bytes:
    value = {
        "artifact_type": candidate.artifact_type.value,
        "artifact_version": candidate.version,
        "parent_hash": candidate.parent_hash,
        "payload_hash": candidate.content_hash,
        "round_index": candidate.round_index,
        "source_signal_hash": candidate.source_signal_hash,
    }
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


__all__ = [
    "AgentSystemPayload",
    "ApprovedArtifact",
    "ArtifactFile",
    "ArtifactKind",
    "ArtifactLineage",
    "ArtifactPayload",
    "CandidateArtifact",
    "EvolutionArtifactSet",
    "SkillBundlePayload",
    "TextMemoryPayload",
    "ValidationDecision",
    "ValidationFinding",
    "ValidationReceipt",
]
