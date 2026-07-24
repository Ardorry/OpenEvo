"""Deterministic, candidate-only evolution artifact security validation."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Final

from openevo_chembench.artifacts import (
    AgentSystemPayload,
    ApprovedArtifact,
    ArtifactKind,
    CandidateArtifact,
    SkillBundlePayload,
    TextMemoryPayload,
    ValidationDecision,
    ValidationFinding,
    ValidationReceipt,
    _issue_validation_receipt,
    _promote_candidate,
)


VALIDATOR_VERSION: Final = "chembench-artifact-validator.v1"
_MAX_PAYLOAD_CHARACTERS: Final = 32_768
_SKILL_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_ANSWER_SELECTION = re.compile(
    r"\b(?:the )?answer (?:(?:is|equals|equal to) )?(?:option )?[a-z]{1,2}\b"
)
_OPTION_SELECTION = re.compile(
    r"\b(?:choose|select) option [a-z]{1,2}\b"
    r"|\boption [a-z]{1,2} (?:is )?correct\b"
)
_OPTION_MAPPING_LINE = re.compile(
    r"(?im)^\s*[\"']?(?:\([A-Z]\)|[A-Z])[\"']?\s*"
    r"(?::|=|-|\.)?\s+\S"
)
_BENCHMARK_INSTRUCTION = re.compile(
    r"\b(?:this |the )?benchmark "
    r"(?:question|item|answer|score|option|task|database|lookup)\b"
)
_QUESTION_STEMS: Final = (
    "which of the following",
    "what is the",
    "what are the",
    "calculate the",
    "determine the",
    "select the correct",
    "choose the correct",
    "the question asks",
    "previous question",
)


@dataclass(frozen=True, slots=True)
class ArtifactValidationResult:
    """Receipt plus an optional validator-approved artifact."""

    receipt: ValidationReceipt
    approved_artifact: ApprovedArtifact | None

    def __post_init__(self) -> None:
        if type(self.receipt) is not ValidationReceipt:
            raise TypeError(
                "ArtifactValidationResult.receipt must be ValidationReceipt"
            )
        if self.receipt.passed:
            if type(self.approved_artifact) is not ApprovedArtifact:
                raise ValueError(
                    "passed validation requires an ApprovedArtifact"
                )
            if self.approved_artifact.validator_receipt is not self.receipt:
                raise ValueError(
                    "ApprovedArtifact must retain the exact validation receipt"
                )
        elif self.approved_artifact is not None:
            raise ValueError(
                "rejected validation cannot contain an ApprovedArtifact"
            )


@dataclass(frozen=True, slots=True)
class _ArtifactSurface:
    """Candidate-only text and path surface used by deterministic checks."""

    text_blobs: tuple[str, ...]
    file_paths: tuple[str, ...] = ()


class EvolutionArtifactValidator:
    """The only component that issues receipts and promotes candidates."""

    __slots__ = ()

    def validate(self, candidate_artifact: CandidateArtifact) -> ValidationReceipt:
        """Return a closed receipt; any validator error rejects the candidate."""

        if type(candidate_artifact) is not CandidateArtifact:
            raise TypeError(
                "EvolutionArtifactValidator.validate requires an exact "
                "CandidateArtifact"
            )
        candidate_hash = candidate_artifact.candidate_hash
        try:
            surface = _extract_surface(candidate_artifact)
            findings: set[ValidationFinding] = set()
            findings.update(_structure_findings(candidate_artifact, surface))
            findings.update(_literal_findings(surface))
            findings.update(_similarity_findings(surface))
            findings.update(_benchmark_specific_findings(surface))
        except Exception:
            findings = {ValidationFinding.VALIDATOR_ERROR}

        ordered_findings = tuple(sorted(findings, key=lambda finding: finding.value))
        return _issue_validation_receipt(
            validator_version=VALIDATOR_VERSION,
            decision=(
                ValidationDecision.REJECTED
                if ordered_findings
                else ValidationDecision.APPROVED
            ),
            candidate_hash=candidate_hash,
            finding_codes=ordered_findings,
        )

    def validate_and_approve(
        self,
        candidate_artifact: CandidateArtifact,
    ) -> ArtifactValidationResult:
        """Promote only a candidate with this validator's passing receipt."""

        receipt = self.validate(candidate_artifact)
        if not receipt.passed:
            return ArtifactValidationResult(
                receipt=receipt,
                approved_artifact=None,
            )
        try:
            approved = _promote_candidate(
                candidate_artifact,
                receipt,
            )
        except Exception:
            failed_receipt = _issue_validation_receipt(
                validator_version=VALIDATOR_VERSION,
                decision=ValidationDecision.REJECTED,
                candidate_hash=candidate_artifact.candidate_hash,
                finding_codes=(ValidationFinding.VALIDATOR_ERROR,),
            )
            return ArtifactValidationResult(
                receipt=failed_receipt,
                approved_artifact=None,
            )
        return ArtifactValidationResult(
            receipt=receipt,
            approved_artifact=approved,
        )


def _extract_surface(candidate: CandidateArtifact) -> _ArtifactSurface:
    payload = candidate.payload
    if type(payload) is TextMemoryPayload:
        return _ArtifactSurface(text_blobs=(payload.markdown,))
    if type(payload) is SkillBundlePayload:
        return _ArtifactSurface(
            text_blobs=tuple(file.content for file in payload.files),
            file_paths=tuple(file.relative_path for file in payload.files),
        )
    if type(payload) is AgentSystemPayload:
        return _ArtifactSurface(text_blobs=(payload.markdown,))
    raise TypeError("unsupported candidate payload")


def _structure_findings(
    candidate: CandidateArtifact,
    surface: _ArtifactSurface,
) -> set[ValidationFinding]:
    findings: set[ValidationFinding] = set()
    total_characters = sum(len(text) for text in surface.text_blobs)
    total_characters += sum(len(path) for path in surface.file_paths)
    if total_characters > _MAX_PAYLOAD_CHARACTERS:
        findings.add(ValidationFinding.INVALID_STRUCTURE)

    if candidate.kind is ArtifactKind.TEXT_MEMORY:
        if type(candidate.payload) is not TextMemoryPayload:
            findings.add(ValidationFinding.INVALID_STRUCTURE)
        return findings

    if candidate.kind is ArtifactKind.AGENT_SYSTEM:
        payload = candidate.payload
        if (
            type(payload) is not AgentSystemPayload
            or payload.target_path != "AGENTS.md"
        ):
            findings.add(ValidationFinding.INVALID_STRUCTURE)
        return findings

    if candidate.kind is not ArtifactKind.SKILL_BUNDLE:
        findings.add(ValidationFinding.INVALID_STRUCTURE)
        return findings

    payload = candidate.payload
    if type(payload) is not SkillBundlePayload:
        findings.add(ValidationFinding.INVALID_STRUCTURE)
        return findings
    if tuple(file.relative_path for file in payload.files) != ("SKILL.md",):
        findings.add(ValidationFinding.DISALLOWED_FILE)
    for path in surface.file_paths:
        normalized_path, compact_path = _normalize(path)
        if "answer" in normalized_path or "answer" in compact_path:
            findings.add(ValidationFinding.ANSWER_CACHE)
        if "lookup" in normalized_path or "lookup" in compact_path:
            findings.add(ValidationFinding.LOOKUP_TABLE)
        if "database" in normalized_path or "database" in compact_path:
            findings.add(ValidationFinding.BENCHMARK_DATABASE)
    if not payload.files or not _valid_skill_markdown(payload.files[0].content):
        findings.add(ValidationFinding.INVALID_SKILL_FORMAT)
    return findings


def _literal_findings(surface: _ArtifactSurface) -> set[ValidationFinding]:
    findings: set[ValidationFinding] = set()
    for value in (*surface.file_paths, *surface.text_blobs):
        normalized, compact = _normalize(value)

        has_target_scores = "targetscores" in compact
        if has_target_scores:
            findings.add(ValidationFinding.LEAK_TARGET_SCORES_LITERAL)
        elif "target" in compact:
            findings.add(ValidationFinding.LEAK_TARGET_LITERAL)
        if "correctanswer" in compact:
            findings.add(ValidationFinding.LEAK_CORRECT_ANSWER_LITERAL)
        if "canary" in compact:
            findings.add(ValidationFinding.LEAK_CANARY_LITERAL)
        if "uuid" in compact:
            findings.add(ValidationFinding.LEAK_UUID_LITERAL)
        if "optionmapping" in compact or "answermapping" in compact:
            findings.add(ValidationFinding.LEAK_OPTION_MAPPING)
        if "answercache" in compact:
            findings.add(ValidationFinding.ANSWER_CACHE)
        if "lookuptable" in compact:
            findings.add(ValidationFinding.LOOKUP_TABLE)
        if "benchmarkdatabase" in compact or "answerdatabase" in compact:
            findings.add(ValidationFinding.BENCHMARK_DATABASE)
        if _ANSWER_SELECTION.search(normalized) or _OPTION_SELECTION.search(
            normalized
        ):
            findings.add(ValidationFinding.LEAK_ANSWER_LITERAL)
        if len(_OPTION_MAPPING_LINE.findall(value)) >= 2:
            findings.add(ValidationFinding.LEAK_OPTION_MAPPING)
    return findings


def _similarity_findings(surface: _ArtifactSurface) -> set[ValidationFinding]:
    for text in surface.text_blobs:
        normalized, _ = _normalize(text)
        if any(stem in normalized for stem in _QUESTION_STEMS):
            return {ValidationFinding.LEAK_QUESTION_SIMILARITY}
        for line in text.splitlines():
            if "?" in line and sum(character.isalnum() for character in line) >= 30:
                return {ValidationFinding.LEAK_QUESTION_SIMILARITY}
    return set()


def _benchmark_specific_findings(
    surface: _ArtifactSurface,
) -> set[ValidationFinding]:
    for value in (*surface.file_paths, *surface.text_blobs):
        normalized, compact = _normalize(value)
        if "chembench" in compact or _BENCHMARK_INSTRUCTION.search(normalized):
            return {ValidationFinding.BENCHMARK_SPECIFIC_INSTRUCTION}
    return set()


def _valid_skill_markdown(content: str) -> bool:
    lines = content.splitlines()
    if len(lines) < 5 or lines[0] != "---":
        return False
    try:
        closing_index = lines.index("---", 1)
    except ValueError:
        return False
    frontmatter_lines = lines[1:closing_index]
    if len(frontmatter_lines) != 2:
        return False

    frontmatter: dict[str, str] = {}
    for line in frontmatter_lines:
        if ":" not in line:
            return False
        key, value = line.split(":", maxsplit=1)
        key = key.strip()
        value = value.strip()
        if not key or not value or key in frontmatter:
            return False
        frontmatter[key] = value
    if set(frontmatter) != {"name", "description"}:
        return False
    if (
        _SKILL_NAME.fullmatch(frontmatter["name"]) is None
        or len(frontmatter["name"]) > 64
        or len(frontmatter["description"]) > 1_024
    ):
        return False
    return any(line.strip() for line in lines[closing_index + 1 :])


def _normalize(value: str) -> tuple[str, str]:
    normalized_unicode = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized_unicode).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    compact = normalized.replace(" ", "")
    return normalized, compact


__all__ = [
    "VALIDATOR_VERSION",
    "ArtifactValidationResult",
    "EvolutionArtifactValidator",
]
