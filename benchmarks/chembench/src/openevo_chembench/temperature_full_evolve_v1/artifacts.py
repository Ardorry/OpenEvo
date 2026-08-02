"""Deterministic, role-separated projections of canonical Temperature evidence."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from openevo_chembench.chembench4k_dataset import normalize_benchmark_text
from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes, sha256_bytes
from openevo_chembench.temperature_full_evolve_v1.evidence import (
    CanonicalRuleV1,
    RuleEvidenceIndexV1,
)

TARGET_ORDER = ("text_memory", "skill_bundle", "agent_system")
TARGET_MAX_BYTES = {
    "text_memory": 4096,
    "skill_bundle": 1536,
    "agent_system": 1024,
}
# Core's built-in ``text_memory`` method adds a small, deterministic provenance
# frame around the projected summary.  Reserve enough space before the formal
# job is created so the Core-owned payload can still satisfy the 4096-byte
# target limit without truncation or an orphaned over-budget job.
PROJECTED_TARGET_MAX_BYTES = {
    **TARGET_MAX_BYTES,
    "text_memory": 3400,
}
TARGET_TOTAL_BYTES = 6144
HARD_TOTAL_BYTES = 8192
_UID = re.compile(r"\b[0-9a-f]{64}\b", re.ASCII)
_ANSWER_MAP = re.compile(
    r"\b(?:answer|option|choice|prediction)\s*(?:is|=|:|->|maps?\s+to)?\s*[ABCD]\b",
    re.IGNORECASE | re.ASCII,
)
_STANDALONE_CHOICE = re.compile(r"(?m)^\s*(?:[-*]\s*)?[ABCD][.)]?\s*$", re.ASCII)
_RULE_LINE_PREFIX = re.compile(r"^rule-[0-9a-f]{24}:\s*", re.ASCII)


class TemperatureArtifactError(RuntimeError):
    """Closed target projection or validation error."""


@dataclass(frozen=True, slots=True, repr=False)
class ProjectedArtifactV1:
    target_id: Literal["text_memory", "skill_bundle", "agent_system"]
    content: str
    source_rule_ids: tuple[str, ...]
    dropped_rule_count: int

    def __post_init__(self) -> None:
        if self.target_id not in TARGET_ORDER:
            raise ValueError("target ID is invalid")
        if type(self.content) is not str or not self.content.endswith("\n"):
            raise ValueError("artifact content must end in one LF")
        if len(self.content.encode("utf-8")) > PROJECTED_TARGET_MAX_BYTES[self.target_id]:
            raise ValueError("artifact exceeds target byte budget")
        if tuple(sorted(set(self.source_rule_ids))) != self.source_rule_ids:
            raise ValueError("source rule IDs must be sorted and unique")
        if self.dropped_rule_count < 0:
            raise ValueError("dropped rule count must be non-negative")

    def __repr__(self) -> str:
        return f"ProjectedArtifactV1(target_id={self.target_id!r}, <private-content>)"

    @property
    def utf8_byte_count(self) -> int:
        return len(self.content.encode("utf-8"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    def to_private_payload(self) -> dict[str, object]:
        return {
            "target_id": self.target_id,
            "content": self.content,
            "source_rule_ids": list(self.source_rule_ids),
            "dropped_rule_count": self.dropped_rule_count,
            "utf8_byte_count": self.utf8_byte_count,
            "sha256": self.sha256,
        }

    def to_public_receipt(self) -> dict[str, object]:
        return {
            "target_id": self.target_id,
            "source_rule_count": len(self.source_rule_ids),
            "dropped_rule_count": self.dropped_rule_count,
            "utf8_byte_count": self.utf8_byte_count,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True, repr=False)
class ProjectedArtifactSetV1:
    batch_index: int
    evidence_sha256: str
    memory: ProjectedArtifactV1
    skill: ProjectedArtifactV1
    agent_system: ProjectedArtifactV1
    compression_applied: bool

    def __post_init__(self) -> None:
        if not 1 <= self.batch_index <= 8:
            raise ValueError("batch index is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", self.evidence_sha256) is None:
            raise ValueError("evidence digest is invalid")
        if (
            self.memory.target_id,
            self.skill.target_id,
            self.agent_system.target_id,
        ) != TARGET_ORDER:
            raise ValueError("artifact set is incomplete or unordered")
        if self.total_utf8_bytes > TARGET_TOTAL_BYTES or self.total_utf8_bytes > HARD_TOTAL_BYTES:
            raise ValueError("combined artifact budget exceeded")

    def __repr__(self) -> str:
        return f"ProjectedArtifactSetV1(batch_index={self.batch_index}, <private-content>)"

    @property
    def artifacts(self) -> tuple[ProjectedArtifactV1, ...]:
        return (self.memory, self.skill, self.agent_system)

    @property
    def total_utf8_bytes(self) -> int:
        return sum(artifact.utf8_byte_count for artifact in self.artifacts)

    @property
    def digest(self) -> str:
        return sha256_bytes(
            canonical_json_bytes(
                {
                    "batch_index": self.batch_index,
                    "evidence_sha256": self.evidence_sha256,
                    "artifacts": [artifact.to_private_payload() for artifact in self.artifacts],
                    "compression_applied": self.compression_applied,
                }
            )
        )

    def to_private_payload(self) -> dict[str, object]:
        return {
            "schema_version": "TemperatureProjectedArtifactSetV1",
            "batch_index": self.batch_index,
            "evidence_sha256": self.evidence_sha256,
            "artifacts": [artifact.to_private_payload() for artifact in self.artifacts],
            "total_utf8_bytes": self.total_utf8_bytes,
            "compression_applied": self.compression_applied,
            "artifact_set_sha256": self.digest,
        }

    def to_public_receipt(self) -> dict[str, object]:
        return {
            "schema_version": "TemperatureProjectedArtifactSetReceiptV1",
            "batch_index": self.batch_index,
            "evidence_sha256": self.evidence_sha256,
            "artifacts": [artifact.to_public_receipt() for artifact in self.artifacts],
            "total_utf8_bytes": self.total_utf8_bytes,
            "target_total_utf8_bytes": TARGET_TOTAL_BYTES,
            "hard_total_utf8_bytes": HARD_TOTAL_BYTES,
            "compression_applied": self.compression_applied,
            "artifact_set_sha256": self.digest,
        }


def project_artifacts(
    evidence: RuleEvidenceIndexV1,
    *,
    forbidden_questions: tuple[str, ...],
) -> ProjectedArtifactSetV1:
    """Render a complete replacement target set and deterministically compress it."""

    if type(evidence) is not RuleEvidenceIndexV1 or evidence.batch_index < 1:
        raise TypeError("projection requires a non-zero exact evidence index")
    if not isinstance(forbidden_questions, tuple) or any(
        type(value) is not str or not value.strip() for value in forbidden_questions
    ):
        raise TypeError("forbidden questions must be a text tuple")

    ranked = {
        target: sorted(
            (
                rule
                for rule in evidence.rules
                if rule.status == "confirmed" and rule.target_projection == target
            ),
            key=_rule_rank,
        )
        for target in TARGET_ORDER
    }
    initial_counts = {target: len(rules) for target, rules in ranked.items()}
    compression = False
    while True:
        contents = {
            "text_memory": _render_memory(ranked["text_memory"], evidence),
            "skill_bundle": _render_skill(ranked["skill_bundle"]),
            "agent_system": _render_agent_system(ranked["agent_system"]),
        }
        sizes = {target: len(content.encode("utf-8")) for target, content in contents.items()}
        oversized = [
            target
            for target in TARGET_ORDER
            if sizes[target] > PROJECTED_TARGET_MAX_BYTES[target]
        ]
        total_oversized = sum(sizes.values()) > TARGET_TOTAL_BYTES
        if not oversized and not total_oversized:
            break
        removable = [target for target in TARGET_ORDER if ranked[target]]
        if not removable:
            raise TemperatureArtifactError("ARTIFACT_FIXED_FRAMING_BUDGET_EXCEEDED")
        if oversized:
            selected = max(
                oversized,
                key=lambda target: sizes[target] / PROJECTED_TARGET_MAX_BYTES[target],
            )
        else:
            selected = max(removable, key=lambda target: sizes[target])
        ranked[selected].pop()
        compression = True

    artifacts = {
        target: ProjectedArtifactV1(
            target_id=target,  # type: ignore[arg-type]
            content=contents[target],
            source_rule_ids=tuple(sorted(rule.rule_id for rule in ranked[target])),
            dropped_rule_count=initial_counts[target] - len(ranked[target]),
        )
        for target in TARGET_ORDER
    }
    result = ProjectedArtifactSetV1(
        batch_index=evidence.batch_index,
        evidence_sha256=evidence.digest,
        memory=artifacts["text_memory"],
        skill=artifacts["skill_bundle"],
        agent_system=artifacts["agent_system"],
        compression_applied=compression,
    )
    _validate_artifact_set(result, forbidden_questions=forbidden_questions)
    return result


def _rule_rank(rule: CanonicalRuleV1) -> tuple[int, float, int, str]:
    return (-rule.support_count, -rule.confidence, rule.contradiction_count, rule.rule_id)


def _render_memory(rules: list[CanonicalRuleV1], evidence: RuleEvidenceIndexV1) -> str:
    do_lines = [f"- {rule.rule_id}: {rule.content}" for rule in rules]
    avoid_lines = [
        (
            f"- {rule.rule_id}: keep the stated applicability boundary"
            + (
                f"; counterexamples observed={rule.contradiction_count}."
                if rule.contradiction_count
                else "."
            )
        )
        for rule in rules
    ]
    validate_lines = [f"- {rule.rule_id}: {rule.validation}" for rule in rules]
    applicable_lines = [f"- {rule.rule_id}: {rule.applicability}" for rule in rules]
    retired = [
        rule for rule in evidence.rules if rule.kind == "category_knowledge" and rule.status == "retired"
    ]
    retired_lines = [
        f"- {rule.rule_id}: retired; support={rule.support_count}; contradictions={rule.contradiction_count}."
        for rule in sorted(retired, key=_rule_rank)[:8]
    ]
    return _sections(
        "# Temperature Prediction Memory",
        (
            ("Do", do_lines),
            ("Avoid", avoid_lines),
            ("Validate", validate_lines),
            ("When Applicable", applicable_lines),
            ("Retired Or Superseded", retired_lines),
        ),
    )


def _render_skill(rules: list[CanonicalRuleV1]) -> str:
    workflow = [
        "1. Identify the reaction class and the reagents that control thermal requirements.",
        "2. Extract explicit cooling, heating, reflux, atmosphere, solvent, and addition-order signals.",
        "3. Translate those signals into a plausible temperature regime before inspecting choices.",
        "4. Check units, sign, scale, and whether a value denotes temperature rather than another quantity.",
        "5. Compare choices by chemical plausibility and eliminate incompatible regimes.",
        "6. Resolve conflicts by prioritizing explicit operational constraints over weak defaults.",
        "7. Perform a final regime, unit, and output-format consistency check.",
    ]
    learned = [f"- {rule.rule_id}: {rule.content} Trigger: {rule.applicability}" for rule in rules]
    checks = [f"- {rule.rule_id}: {rule.validation}" for rule in rules]
    return _sections(
        "# Temperature Prediction Skill",
        (
            ("Workflow", workflow),
            ("Learned Procedure Refinements", learned),
            ("Validation Checks", checks),
        ),
    )


def _render_agent_system(rules: list[CanonicalRuleV1]) -> str:
    directives = [
        "- Use only the provided problem, choices, and approved context; do not invent experimental conditions.",
        "- Extract operational constraints and check units before committing to a choice.",
        "- When evidence conflicts or is incomplete, make a calibrated best-supported judgment.",
        *[f"- {rule.rule_id}: {rule.content}" for rule in rules],
    ]
    output = [
        "- Return exactly one uppercase choice letter: A, B, C, or D.",
        "- Do not include explanations, hidden information, tool calls, or additional text.",
    ]
    return _sections(
        "# Temperature Prediction Agent System",
        (("Directives", directives), ("Output Discipline", output)),
    )


def _sections(title: str, sections: tuple[tuple[str, list[str]], ...]) -> str:
    lines = [title, ""]
    for heading, values in sections:
        lines.extend((f"## {heading}", ""))
        lines.extend(values or ["- No validated rule in this section."])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _validate_artifact_set(
    artifacts: ProjectedArtifactSetV1,
    *,
    forbidden_questions: tuple[str, ...],
) -> None:
    normalized_questions = tuple(
        value
        for question in forbidden_questions
        if len(value := normalize_benchmark_text(question)) >= 24
    )
    line_owners: dict[str, str] = {}
    for artifact in artifacts.artifacts:
        text = artifact.content
        if _UID.search(text) or _ANSWER_MAP.search(text) or _STANDALONE_CHOICE.search(text):
            raise TemperatureArtifactError("ARTIFACT_TASK_ID_OR_ANSWER_LEAKAGE")
        normalized = normalize_benchmark_text(text)
        if any(question in normalized for question in normalized_questions):
            raise TemperatureArtifactError("ARTIFACT_QUESTION_TEXT_LEAKAGE")
        for line in text.splitlines():
            candidate = line.removeprefix("- ")
            candidate = _RULE_LINE_PREFIX.sub("", candidate)
            candidate = normalize_benchmark_text(candidate)
            if len(candidate) < 24 or candidate.startswith("no validated"):
                continue
            owner = line_owners.setdefault(candidate, artifact.target_id)
            if owner != artifact.target_id:
                raise TemperatureArtifactError("ARTIFACT_CROSS_TARGET_DUPLICATION")
    if artifacts.total_utf8_bytes > HARD_TOTAL_BYTES:
        raise TemperatureArtifactError("ARTIFACT_HARD_BUDGET_EXCEEDED")


__all__ = [
    "HARD_TOTAL_BYTES",
    "PROJECTED_TARGET_MAX_BYTES",
    "TARGET_MAX_BYTES",
    "TARGET_ORDER",
    "TARGET_TOTAL_BYTES",
    "ProjectedArtifactSetV1",
    "ProjectedArtifactV1",
    "TemperatureArtifactError",
    "project_artifacts",
]
