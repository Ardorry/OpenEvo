from __future__ import annotations

import hashlib

import pytest

from openevo_chembench.temperature_full_evolve_v1.artifacts import (
    HARD_TOTAL_BYTES,
    PROJECTED_TARGET_MAX_BYTES,
    TARGET_MAX_BYTES,
    TARGET_TOTAL_BYTES,
    TemperatureArtifactError,
    project_artifacts,
)
from openevo_chembench.temperature_full_evolve_v1.evidence import (
    CanonicalRuleV1,
    RuleEvidenceIndexV1,
)


def _uid(index: int) -> str:
    return hashlib.sha256(f"train-{index}".encode()).hexdigest()


def _rule(index: int, kind: str, *, status: str = "confirmed", content: str | None = None) -> CanonicalRuleV1:
    support = (_uid(index),) if status == "provisional" else tuple(sorted((_uid(index), _uid(index + 1))))
    target = {
        "category_knowledge": "text_memory",
        "workflow": "skill_bundle",
        "behavior": "agent_system",
    }[kind]
    if status != "confirmed":
        target = "none"
    return CanonicalRuleV1(
        rule_id=f"rule-{index:024x}",
        kind=kind,
        content=content or f"General transferable principle number {index} with conditional evidence.",
        applicability=f"Apply only when condition family {index} is present.",
        validation=f"Verify independent signal family {index} before use.",
        supporting_train_uids=support,
        counterexample_train_uids=(),
        support_count=len(support),
        contradiction_count=0,
        first_seen_batch=1,
        last_validated_batch=1,
        confidence=0.8,
        status=status,
        target_projection=target,
    )


def test_projection_is_role_separated_bounded_and_contains_no_evidence_uids() -> None:
    rules = tuple(
        sorted(
            (
                _rule(1, "category_knowledge"),
                _rule(3, "workflow"),
                _rule(5, "behavior"),
                _rule(7, "category_knowledge", status="provisional"),
            ),
            key=lambda item: item.rule_id,
        )
    )
    evidence = RuleEvidenceIndexV1(batch_index=1, rules=rules)
    projected = project_artifacts(evidence, forbidden_questions=("A private synthetic question",))

    assert projected.memory.source_rule_ids == (rules[0].rule_id,)
    assert rules[1].rule_id in projected.skill.source_rule_ids
    assert rules[2].rule_id in projected.agent_system.source_rule_ids
    assert rules[3].rule_id not in {
        value for artifact in projected.artifacts for value in artifact.source_rule_ids
    }
    assert projected.total_utf8_bytes <= TARGET_TOTAL_BYTES <= HARD_TOTAL_BYTES
    for artifact in projected.artifacts:
        assert artifact.utf8_byte_count <= TARGET_MAX_BYTES[artifact.target_id]
        assert not any(uid in artifact.content for uid in (_uid(1), _uid(2)))
    receipt = projected.to_public_receipt()
    assert "content" not in str(receipt)


def test_projection_deterministically_compresses_without_truncating_schema() -> None:
    rules = tuple(
        _rule(
            index,
            "category_knowledge",
            content=("Transferable thermal constraint with corroborated chemistry. " * 8) + str(index),
        )
        for index in range(0, 40, 2)
    )
    evidence = RuleEvidenceIndexV1(batch_index=1, rules=rules)
    projected = project_artifacts(evidence, forbidden_questions=("Private question text",))

    assert projected.compression_applied is True
    assert projected.memory.dropped_rule_count > 0
    assert projected.memory.utf8_byte_count <= PROJECTED_TARGET_MAX_BYTES["text_memory"]
    assert projected.memory.content.endswith("\n")
    assert all(
        heading in projected.memory.content
        for heading in ("## Do", "## Avoid", "## Validate", "## When Applicable", "## Retired Or Superseded")
    )


def test_projection_rejects_train_question_copy() -> None:
    question = "Which temperature should be used for the private synthetic conversion under these conditions?"
    evidence = RuleEvidenceIndexV1(
        batch_index=1,
        rules=(_rule(1, "category_knowledge", content=question),),
    )
    with pytest.raises(TemperatureArtifactError, match="ARTIFACT_QUESTION_TEXT_LEAKAGE"):
        project_artifacts(evidence, forbidden_questions=(question,))


def test_projection_rejects_cross_target_duplicate_rule_content() -> None:
    shared = "Use this sufficiently long duplicated instruction only under its explicit boundary."
    evidence = RuleEvidenceIndexV1(
        batch_index=1,
        rules=tuple(
            sorted(
                (
                    _rule(1, "category_knowledge", content=shared),
                    _rule(3, "behavior", content=shared),
                ),
                key=lambda rule: rule.rule_id,
            )
        ),
    )
    with pytest.raises(TemperatureArtifactError, match="ARTIFACT_CROSS_TARGET_DUPLICATION"):
        project_artifacts(evidence, forbidden_questions=("Private question text",))
