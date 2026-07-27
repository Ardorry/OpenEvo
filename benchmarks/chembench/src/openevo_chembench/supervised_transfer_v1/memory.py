"""Closed category-memory schema and deterministic supervised validator inputs."""

from __future__ import annotations

import re
from typing import Literal

from openevo.evolution.framework import canonical_digest
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes, sha256_bytes

SUPERVISED_MEMORY_REQUIRED_SECTIONS = (
    "Confirmed Principles",
    "Provisional Principles",
    "Common Failure Modes",
    "Option Elimination Checks",
    "Retired Or Contradicted",
    "Output Discipline",
)
# The registered Core method enforces these headings.  They are retained as a
# compatibility projection after the category-specific protocol sections.
CORE_EXPEL_REQUIRED_SECTIONS = (
    "Do",
    "Avoid",
    "Validate",
    "When Applicable",
    "Retired Or Superseded",
)
SUPERVISED_MEMORY_MAX_UTF8_BYTES = 24_576
SUPERVISED_MEMORY_MAX_ESTIMATED_TOKENS = 6_144
SUPERVISED_MEMORY_MAX_CONFIRMED_RULES = 40
SUPERVISED_MEMORY_MAX_PROVISIONAL_RULES = 20
SUPERVISED_MEMORY_MAX_FAILURE_MODES = 24
SUPERVISED_MEMORY_MAX_RETIRED_RULES = 24
SUPERVISED_MEMORY_TOKEN_ESTIMATOR_ID = "ascii_word_or_unicode_codepoint_v1"
SUPERVISED_MEMORY_PARSER_ID = "supervised_category_markdown_v1"

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[^\x00-\x7f]", re.UNICODE)
_H2_RE = re.compile(r"##\s+(.+?)\s*\Z")
_BULLET_RE = re.compile(r"-\s+(.+)\Z")
_EVIDENCE_COUNT_RE = re.compile(
    r"\bEvidence\s+Count\s*[:=]\s*([0-9]+)\b", re.IGNORECASE
)
_STATUS_RE = re.compile(
    r"\bStatus\s*[:=]\s*(provisional|confirmed|retired)\b", re.IGNORECASE
)
_EVIDENCE_DIGESTS_RE = re.compile(
    r"\bEvidence\s+Digests\s*[:=]\s*([^;]+)\s*\Z",
    re.IGNORECASE,
)
_REQUIRED_RULE_FIELDS = (
    "rule id",
    "status",
    "category",
    "trigger",
    "principle",
    "action",
    "validation",
    "evidence count",
    "evidence",
)


class SupervisedMemoryLimitsV1(BaseModel):
    """Protocol-fixed capacity limits for category memory artifacts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_utf8_bytes: Literal[24576] = SUPERVISED_MEMORY_MAX_UTF8_BYTES
    max_estimated_tokens: Literal[6144] = SUPERVISED_MEMORY_MAX_ESTIMATED_TOKENS
    max_confirmed_rules: Literal[40] = SUPERVISED_MEMORY_MAX_CONFIRMED_RULES
    max_provisional_rules: Literal[20] = SUPERVISED_MEMORY_MAX_PROVISIONAL_RULES
    max_failure_modes: Literal[24] = SUPERVISED_MEMORY_MAX_FAILURE_MODES
    max_retired_rules: Literal[24] = SUPERVISED_MEMORY_MAX_RETIRED_RULES
    required_sections: tuple[str, ...] = SUPERVISED_MEMORY_REQUIRED_SECTIONS
    core_compatibility_sections: tuple[str, ...] = CORE_EXPEL_REQUIRED_SECTIONS
    token_estimator_id: Literal["ascii_word_or_unicode_codepoint_v1"] = (
        SUPERVISED_MEMORY_TOKEN_ESTIMATOR_ID
    )
    parser_id: Literal["supervised_category_markdown_v1"] = SUPERVISED_MEMORY_PARSER_ID

    @model_validator(mode="after")
    def _fixed(self) -> SupervisedMemoryLimitsV1:
        if (
            self.required_sections != SUPERVISED_MEMORY_REQUIRED_SECTIONS
            or self.core_compatibility_sections != CORE_EXPEL_REQUIRED_SECTIONS
        ):
            raise ValueError("supervised memory section contract is fixed")
        return self

    def to_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json")

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.to_payload()))


SUPERVISED_MEMORY_LIMITS_V1 = SupervisedMemoryLimitsV1()


class SupervisedMemoryInspectionV1(BaseModel):
    """Content-free measurements and schema findings for one category memory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["chembench_supervised_memory_inspection_v1"] = (
        "chembench_supervised_memory_inspection_v1"
    )
    memory_limits_sha256: str
    category: str
    token_estimator_id: Literal["ascii_word_or_unicode_codepoint_v1"]
    parser_id: Literal["supervised_category_markdown_v1"]
    utf8_byte_count: int = Field(ge=0)
    estimated_token_count: int = Field(ge=0)
    section_item_counts: tuple[int, ...]
    total_section_items: int = Field(ge=0)
    max_section_items: int = Field(ge=0)
    confirmed_rule_count: int = Field(ge=0)
    provisional_rule_count: int = Field(ge=0)
    failure_mode_count: int = Field(ge=0)
    retired_rule_count: int = Field(ge=0)
    finding_codes: tuple[str, ...]

    @field_validator("memory_limits_sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("memory limits digest must be SHA-256")
        return value

    @field_validator("category")
    @classmethod
    def _category(cls, value: str) -> str:
        if value not in CHEMBENCH4K_CATEGORIES:
            raise ValueError("memory category is not frozen")
        return value

    @field_validator("finding_codes")
    @classmethod
    def _findings(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))) or any(
            re.fullmatch(r"[a-z][a-z0-9_]{0,95}", value) is None for value in values
        ):
            raise ValueError("memory findings must be sorted closed codes")
        return values

    @model_validator(mode="after")
    def _counts(self) -> SupervisedMemoryInspectionV1:
        if (
            len(self.section_item_counts)
            != len(SUPERVISED_MEMORY_REQUIRED_SECTIONS) + len(CORE_EXPEL_REQUIRED_SECTIONS)
            or sum(self.section_item_counts) != self.total_section_items
            or max(self.section_item_counts, default=0) != self.max_section_items
        ):
            raise ValueError("memory section measurements are inconsistent")
        return self

    @property
    def passed(self) -> bool:
        return not self.finding_codes

    @property
    def digest(self) -> str:
        return canonical_digest(self)

    def to_public_payload(self) -> dict[str, object]:
        return {
            "memory_limits_sha256": self.memory_limits_sha256,
            "category": self.category,
            "token_estimator_id": self.token_estimator_id,
            "parser_id": self.parser_id,
            "utf8_byte_count": self.utf8_byte_count,
            "estimated_token_count": self.estimated_token_count,
            "section_item_counts": list(self.section_item_counts),
            "total_section_items": self.total_section_items,
            "max_section_items": self.max_section_items,
            "confirmed_rule_count": self.confirmed_rule_count,
            "provisional_rule_count": self.provisional_rule_count,
            "failure_mode_count": self.failure_mode_count,
            "retired_rule_count": self.retired_rule_count,
            "inspection_sha256": self.digest,
        }


def inspect_supervised_category_memory_v1(
    payload: bytes,
    *,
    category: str,
    limits: SupervisedMemoryLimitsV1 = SUPERVISED_MEMORY_LIMITS_V1,
    allowed_evidence_digests: frozenset[str] | None = None,
) -> SupervisedMemoryInspectionV1:
    """Validate exact headings, rule evidence states, and artifact capacity."""

    if type(payload) is not bytes:
        raise TypeError("memory payload must be exact bytes")
    if category not in CHEMBENCH4K_CATEGORIES:
        raise ValueError("memory category is not frozen")
    if type(limits) is not SupervisedMemoryLimitsV1:
        raise TypeError("limits must be exact SupervisedMemoryLimitsV1")
    if allowed_evidence_digests is not None and (
        type(allowed_evidence_digests) is not frozenset
        or any(
            re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in allowed_evidence_digests
        )
    ):
        raise TypeError("allowed evidence must be a frozen SHA-256 set or None")
    findings: set[str] = set()
    byte_count = len(payload)
    if byte_count > limits.max_utf8_bytes:
        findings.add("memory_bytes_exceeded")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        text = ""
        findings.add("memory_invalid_utf8")
    token_count = len(_TOKEN_RE.findall(text))
    if token_count > limits.max_estimated_tokens:
        findings.add("memory_tokens_exceeded")
    if not text.strip() or "\x00" in text or "\r" in text:
        findings.add("memory_structure_invalid")

    expected_h1 = f"# Category Memory: {category}"
    lines = text.split("\n")
    first_nonempty = next((line for line in lines if line), "")
    if first_nonempty != expected_h1:
        findings.add("memory_category_heading_invalid")

    expected_sections = (*limits.required_sections, *limits.core_compatibility_sections)
    items: dict[str, list[str]] = {section: [] for section in expected_sections}
    headings: list[str] = []
    current: str | None = None
    for line in lines:
        heading = _H2_RE.fullmatch(line)
        if heading is not None:
            current = heading.group(1)
            headings.append(current)
            if current not in items:
                findings.add("memory_unknown_section")
            continue
        bullet = _BULLET_RE.fullmatch(line)
        if bullet is not None:
            if current not in items:
                findings.add("memory_structure_invalid")
            else:
                items[current].append(bullet.group(1).strip())
        elif line and line != expected_h1 and current is None:
            findings.add("memory_structure_invalid")
    if tuple(headings) != expected_sections:
        findings.add("memory_required_sections_invalid")
    if any(not items[section] for section in expected_sections):
        findings.add("memory_section_empty")

    rule_sections = (
        "Confirmed Principles",
        "Provisional Principles",
        "Retired Or Contradicted",
    )
    for section in rule_sections:
        for item in items[section]:
            if item.casefold() in {"none", "none.", "no rules", "no rules."}:
                continue
            normalized = item.casefold()
            if any(field not in normalized for field in _REQUIRED_RULE_FIELDS):
                findings.add("memory_rule_schema_invalid")
                continue
            status_match = _STATUS_RE.search(item)
            evidence_match = _EVIDENCE_COUNT_RE.search(item)
            if status_match is None or evidence_match is None:
                findings.add("memory_rule_schema_invalid")
                continue
            status = status_match.group(1).casefold()
            evidence_count = int(evidence_match.group(1))
            evidence_digests_match = _EVIDENCE_DIGESTS_RE.search(item)
            expected_status = {
                "Confirmed Principles": "confirmed",
                "Provisional Principles": "provisional",
                "Retired Or Contradicted": "retired",
            }[section]
            if status != expected_status:
                findings.add("memory_rule_status_invalid")
            if status == "confirmed" and evidence_count < 2:
                findings.add("memory_confirmed_evidence_insufficient")
            if status == "provisional" and evidence_count != 1:
                findings.add("memory_provisional_evidence_invalid")
            if allowed_evidence_digests is not None:
                if evidence_digests_match is None:
                    findings.add("memory_evidence_digest_schema_invalid")
                else:
                    encoded_digests = evidence_digests_match.group(1).strip()
                    digests = (
                        ()
                        if encoded_digests.casefold() == "none"
                        else tuple(
                            value.strip() for value in encoded_digests.split(",")
                        )
                    )
                    if (
                        len(digests) != evidence_count
                        or len(set(digests)) != len(digests)
                        or any(
                            re.fullmatch(r"[0-9a-f]{64}", value) is None
                            for value in digests
                        )
                    ):
                        findings.add("memory_evidence_digest_schema_invalid")
                    elif not set(digests).issubset(allowed_evidence_digests):
                        findings.add("memory_evidence_outside_train_chain")

    def substantive_count(section: str) -> int:
        return sum(
            item.casefold() not in {"none", "none.", "no rules", "no rules."}
            for item in items[section]
        )

    confirmed = substantive_count("Confirmed Principles")
    provisional = substantive_count("Provisional Principles")
    failure_modes = substantive_count("Common Failure Modes")
    retired = substantive_count("Retired Or Contradicted")
    if confirmed > limits.max_confirmed_rules:
        findings.add("memory_confirmed_rules_exceeded")
    if provisional > limits.max_provisional_rules:
        findings.add("memory_provisional_rules_exceeded")
    if failure_modes > limits.max_failure_modes:
        findings.add("memory_failure_modes_exceeded")
    if retired > limits.max_retired_rules:
        findings.add("memory_retired_rules_exceeded")

    counts = tuple(len(items[section]) for section in expected_sections)
    return SupervisedMemoryInspectionV1(
        memory_limits_sha256=limits.digest,
        category=category,
        token_estimator_id=limits.token_estimator_id,
        parser_id=limits.parser_id,
        utf8_byte_count=byte_count,
        estimated_token_count=token_count,
        section_item_counts=counts,
        total_section_items=sum(counts),
        max_section_items=max(counts, default=0),
        confirmed_rule_count=confirmed,
        provisional_rule_count=provisional,
        failure_mode_count=failure_modes,
        retired_rule_count=retired,
        finding_codes=tuple(sorted(findings)),
    )


__all__ = [
    "CORE_EXPEL_REQUIRED_SECTIONS",
    "SUPERVISED_MEMORY_LIMITS_V1",
    "SUPERVISED_MEMORY_REQUIRED_SECTIONS",
    "SupervisedMemoryInspectionV1",
    "SupervisedMemoryLimitsV1",
    "inspect_supervised_category_memory_v1",
]
