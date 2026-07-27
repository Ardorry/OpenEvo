"""Typed skill/agent-system validation and runtime capabilities for supervised transfer."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
from openevo_chembench.frozen_runtime_v2 import CoreResolvedTextMemoryV2
from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[^\x00-\x7f]|\S", re.ASCII)
_H2_RE = re.compile(r"## ([^\r\n]+)\Z", re.ASCII)
_TARGETS = ("skill_bundle", "agent_system")
_LIMITS = {
    "skill_bundle": {"bytes": 24_576, "tokens": 6_144},
    "agent_system": {"bytes": 12_288, "tokens": 3_072},
}
_SECTIONS = {
    "skill_bundle": (
        "Name",
        "Description",
        "Category Scope",
        "When To Use",
        "Workflow",
        "Validation Checks",
        "Failure Recovery",
    ),
    "agent_system": ("Directives", "Output Discipline"),
}


class SupervisedAuxiliaryInspectionV2(BaseModel):
    """Content-free validation result for one model-authored auxiliary target."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["SupervisedAuxiliaryInspectionV2"] = (
        "SupervisedAuxiliaryInspectionV2"
    )
    target_id: Literal["skill_bundle", "agent_system"]
    category: str
    utf8_byte_count: int = Field(ge=0)
    estimated_token_count: int = Field(ge=0)
    section_item_counts: tuple[int, ...]
    source_evidence_digests: tuple[str, ...]
    finding_codes: tuple[str, ...]

    @field_validator("category")
    @classmethod
    def _category(cls, value: str) -> str:
        if value not in CHEMBENCH4K_CATEGORIES:
            raise ValueError("auxiliary category is not frozen")
        return value

    @field_validator("source_evidence_digests")
    @classmethod
    def _evidence(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if (
            not values
            or tuple(sorted(values)) != values
            or len(set(values)) != len(values)
            or any(_SHA256_RE.fullmatch(value) is None for value in values)
        ):
            raise ValueError("auxiliary evidence digests are invalid")
        return values

    @field_validator("finding_codes")
    @classmethod
    def _findings(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(values))) != values:
            raise ValueError("auxiliary findings must be sorted and unique")
        return values

    @model_validator(mode="after")
    def _counts(self) -> SupervisedAuxiliaryInspectionV2:
        if len(self.section_item_counts) != len(_SECTIONS[self.target_id]):
            raise ValueError("auxiliary section counts are incomplete")
        return self

    @property
    def passed(self) -> bool:
        return not self.finding_codes

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            canonical_json_bytes(self.model_dump(mode="json"))
        ).hexdigest()

    def to_public_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "target_id": self.target_id,
            "category": self.category,
            "utf8_byte_count": self.utf8_byte_count,
            "estimated_token_count": self.estimated_token_count,
            "section_item_counts": list(self.section_item_counts),
            "source_evidence_count": len(self.source_evidence_digests),
            "finding_codes": list(self.finding_codes),
            "inspection_sha256": self.digest,
        }


def inspect_supervised_auxiliary_artifact_v2(
    payload: bytes,
    *,
    target_id: Literal["skill_bundle", "agent_system"],
    category: str,
    source_evidence_digests: tuple[str, ...],
    allowed_evidence_digests: frozenset[str],
    forbidden_literals: tuple[str, ...],
) -> SupervisedAuxiliaryInspectionV2:
    """Apply structure, capacity, evidence, and token-aligned leakage checks."""

    if type(payload) is not bytes:
        raise TypeError("auxiliary payload must be exact bytes")
    if target_id not in _TARGETS:
        raise ValueError("auxiliary target is invalid")
    if category not in CHEMBENCH4K_CATEGORIES:
        raise ValueError("auxiliary category is not frozen")
    if type(allowed_evidence_digests) is not frozenset or any(
        _SHA256_RE.fullmatch(value) is None for value in allowed_evidence_digests
    ):
        raise TypeError("allowed evidence must be a frozen SHA-256 set")
    findings: set[str] = set()
    limits = _LIMITS[target_id]
    if len(payload) > limits["bytes"]:
        findings.add("auxiliary_bytes_exceeded")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        text = ""
        findings.add("auxiliary_invalid_utf8")
    token_count = len(_TOKEN_RE.findall(text))
    if token_count > limits["tokens"]:
        findings.add("auxiliary_tokens_exceeded")
    if not text.strip() or "\x00" in text or "\r" in text:
        findings.add("auxiliary_structure_invalid")

    expected_h1 = (
        f"# Category Skill: {category}"
        if target_id == "skill_bundle"
        else f"# Category Agent System: {category}"
    )
    lines = text.split("\n")
    first_nonempty = next((line for line in lines if line), "")
    if first_nonempty != expected_h1:
        findings.add("auxiliary_category_heading_invalid")
    expected_sections = _SECTIONS[target_id]
    headings: list[str] = []
    section_counts = {section: 0 for section in expected_sections}
    current: str | None = None
    for line in lines:
        heading = _H2_RE.fullmatch(line)
        if heading is not None:
            current = heading.group(1)
            headings.append(current)
            if current not in section_counts:
                findings.add("auxiliary_unknown_section")
            continue
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")) or re.match(r"\d+[.)]\s+", stripped):
            if current not in section_counts:
                findings.add("auxiliary_structure_invalid")
            else:
                section_counts[current] += 1
        elif stripped and stripped != expected_h1 and current is None:
            findings.add("auxiliary_structure_invalid")
    if tuple(headings) != expected_sections:
        findings.add("auxiliary_required_sections_invalid")
    if any(section_counts[section] == 0 for section in expected_sections):
        findings.add("auxiliary_section_empty")
    if target_id == "skill_bundle":
        if section_counts["Workflow"] > 48:
            findings.add("skill_workflow_steps_exceeded")
        if section_counts["Failure Recovery"] > 24:
            findings.add("skill_failure_rules_exceeded")
    elif sum(section_counts.values()) > 36:
        findings.add("agent_system_instruction_rules_exceeded")

    normalized_evidence = tuple(sorted(set(source_evidence_digests)))
    if (
        not source_evidence_digests
        or normalized_evidence != tuple(source_evidence_digests)
        or any(_SHA256_RE.fullmatch(value) is None for value in source_evidence_digests)
    ):
        findings.add("auxiliary_evidence_invalid")
    elif not set(source_evidence_digests).issubset(allowed_evidence_digests):
        findings.add("auxiliary_evidence_outside_train_chain")

    _add_leakage_findings(
        findings,
        text=text,
        forbidden_literals=forbidden_literals,
    )
    return SupervisedAuxiliaryInspectionV2(
        target_id=target_id,
        category=category,
        utf8_byte_count=len(payload),
        estimated_token_count=token_count,
        section_item_counts=tuple(section_counts[section] for section in expected_sections),
        source_evidence_digests=normalized_evidence or ("0" * 64,),
        finding_codes=tuple(sorted(findings)),
    )


def _add_leakage_findings(
    findings: set[str],
    *,
    text: str,
    forbidden_literals: tuple[str, ...],
) -> None:
    # Imported lazily to avoid a module cycle: the Core bridge owns the shared,
    # already-tested token-aligned leakage primitives.
    from openevo_chembench import taskwise_core_evolution_v1 as core

    normalized_candidate = core._normalize(text)
    for encoded_literal in forbidden_literals:
        source_kind, stripped = core._decode_validator_literal(encoded_literal)
        if core._full_literal_scan_allowed(source_kind, stripped) and core._contains_bounded_literal(
            text,
            stripped,
        ):
            findings.add("auxiliary_leak_forbidden_literal")
        normalized_literal = core._normalize(stripped)
        if core._full_literal_scan_allowed(
            source_kind,
            normalized_literal,
        ) and core._contains_bounded_literal(normalized_candidate, normalized_literal):
            findings.add("auxiliary_leak_normalized_literal")
        if (
            core._source_kind_ngram_match(
                source_kind,
                normalized_literal,
                normalized_candidate,
            )
            is not None
        ):
            findings.add("auxiliary_leak_literal_ngram")
    security_text = core._security_scan_text(text)
    if core._contains_path_or_benchmark_marker(security_text):
        findings.add("auxiliary_leak_path_or_benchmark_marker")
    if core._OPERATIONAL_IDENTIFIER_RE.search(security_text):
        findings.add("auxiliary_leak_operational_identifier")
    if core._contains_answer_map(security_text):
        findings.add("auxiliary_leak_answer_map")


@dataclass(frozen=True, slots=True, init=False)
class CoreResolvedAuxiliaryArtifactV2:
    """Capability-issued text projection of one verified Core auxiliary artifact."""

    target_id: Literal["skill_bundle", "agent_system"]
    core_artifact_id: str
    artifact_payload_sha256: str
    context_resolution_digest: str
    resolved_content_sha256: str
    markdown: str

    def __init__(
        self,
        *,
        target_id: Literal["skill_bundle", "agent_system"],
        core_artifact_id: str,
        artifact_payload_sha256: str,
        context_resolution_digest: str,
        resolved_content_sha256: str,
        markdown: str,
        _issuer_token: object | None = None,
    ) -> None:
        if not _auxiliary_token_is_valid(_issuer_token):
            raise TypeError("auxiliary context must be issued by the Core bridge")
        object.__setattr__(self, "target_id", target_id)
        object.__setattr__(self, "core_artifact_id", core_artifact_id)
        object.__setattr__(self, "artifact_payload_sha256", artifact_payload_sha256)
        object.__setattr__(self, "context_resolution_digest", context_resolution_digest)
        object.__setattr__(self, "resolved_content_sha256", resolved_content_sha256)
        object.__setattr__(self, "markdown", markdown)
        self.__post_init__()

    def __post_init__(self) -> None:
        if self.target_id not in _TARGETS:
            raise ValueError("auxiliary target is invalid")
        if (
            not self.core_artifact_id
            or "/" in self.core_artifact_id
            or "\\" in self.core_artifact_id
        ):
            raise ValueError("auxiliary artifact ID is invalid")
        for value in (
            self.artifact_payload_sha256,
            self.context_resolution_digest,
            self.resolved_content_sha256,
        ):
            if _SHA256_RE.fullmatch(value) is None:
                raise ValueError("auxiliary digest is invalid")
        if (
            not self.markdown.strip()
            or len(self.markdown.encode("utf-8")) > _LIMITS[self.target_id]["bytes"]
            or hashlib.sha256(self.markdown.encode("utf-8")).hexdigest()
            != self.resolved_content_sha256
        ):
            raise ValueError("auxiliary content binding is invalid")

    def to_runtime_payload(self) -> dict[str, str]:
        return {
            "target_id": self.target_id,
            "core_artifact_id": self.core_artifact_id,
            "artifact_payload_sha256": self.artifact_payload_sha256,
            "context_resolution_digest": self.context_resolution_digest,
            "resolved_content_sha256": self.resolved_content_sha256,
            "markdown": self.markdown,
        }


@dataclass(frozen=True, slots=True)
class CoreResolvedSupervisedContextV2:
    """Exact three-target context injected into one supervised online session."""

    memory: CoreResolvedTextMemoryV2
    skill: CoreResolvedAuxiliaryArtifactV2
    agent_system: CoreResolvedAuxiliaryArtifactV2

    def __post_init__(self) -> None:
        if (
            type(self.memory) is not CoreResolvedTextMemoryV2
            or type(self.skill) is not CoreResolvedAuxiliaryArtifactV2
            or self.skill.target_id != "skill_bundle"
            or type(self.agent_system) is not CoreResolvedAuxiliaryArtifactV2
            or self.agent_system.target_id != "agent_system"
        ):
            raise TypeError("supervised context requires exact memory/skill/system targets")

    def to_runtime_payload(self) -> dict[str, object]:
        return {
            "memory": self.memory.to_runtime_payload(),
            "skill": self.skill.to_runtime_payload(),
            "agent_system": self.agent_system.to_runtime_payload(),
        }


def _make_auxiliary_issuer():
    issuer_token = object()

    def token_is_valid(candidate: object | None) -> bool:
        return candidate is issuer_token

    def issue(**values: object) -> CoreResolvedAuxiliaryArtifactV2:
        return CoreResolvedAuxiliaryArtifactV2(
            **values,  # type: ignore[arg-type]
            _issuer_token=issuer_token,
        )

    return token_is_valid, issue


(_auxiliary_token_is_valid, _issue_core_resolved_auxiliary_v2) = _make_auxiliary_issuer()


__all__ = [
    "CoreResolvedAuxiliaryArtifactV2",
    "CoreResolvedSupervisedContextV2",
    "SupervisedAuxiliaryInspectionV2",
    "inspect_supervised_auxiliary_artifact_v2",
]
