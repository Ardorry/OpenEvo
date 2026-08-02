from __future__ import annotations

import hashlib
import json

import pytest

from openevo_chembench.temperature_full_evolve_v1.evidence import (
    BatchReflectionV1,
    RuleEvidenceIndexV1,
    TemperatureEvidenceError,
    apply_batch_reflection,
    parse_batch_reflection,
)


def _uid(index: int) -> str:
    return hashlib.sha256(f"train-{index}".encode()).hexdigest()


def _proposal(
    *,
    support: list[str],
    rule_id: str | None = None,
    operation: str = "add",
    counterexamples: list[str] | None = None,
    confidence: float = 0.8,
    content: str = "Use convergent chemistry evidence to identify a plausible temperature regime.",
) -> dict[str, object]:
    return {
        "operation": operation,
        "rule_id": rule_id,
        "kind": "category_knowledge",
        "content": content,
        "applicability": "When several reaction-condition signals point to the same regime.",
        "validation": "Check units, scale, and conflicting thermal constraints before choosing.",
        "supporting_train_uids": sorted(support),
        "counterexample_train_uids": sorted(counterexamples or []),
        "confidence": confidence,
        "retirement_reason": "Contradictory evidence invalidates the stated scope." if operation == "retire" else None,
    }


def test_canonical_evidence_accumulates_and_requires_two_supports_for_projection() -> None:
    all_uids = frozenset(_uid(index) for index in range(100))
    first = BatchReflectionV1.model_validate(
        {
            "schema_version": "TemperatureBatchReflectionV1",
            "batch_index": 1,
            "prior_evidence_sha256": None,
            "proposals": [_proposal(support=[_uid(0)])],
        }
    )
    index = apply_batch_reflection(
        RuleEvidenceIndexV1.generation_zero(),
        first,
        all_train_uids=all_uids,
        seen_train_uids=frozenset(_uid(index) for index in range(25)),
        current_batch_uids=frozenset(_uid(index) for index in range(25)),
    )
    assert index.rules[0].status == "provisional"
    assert index.rules[0].target_projection == "none"

    second_payload = {
        "schema_version": "TemperatureBatchReflectionV1",
        "batch_index": 2,
        "prior_evidence_sha256": index.digest,
        "proposals": [
            _proposal(
                support=[_uid(25)],
                rule_id=index.rules[0].rule_id,
                operation="update",
            )
        ],
    }
    updated = apply_batch_reflection(
        index,
        BatchReflectionV1.model_validate(second_payload),
        all_train_uids=all_uids,
        seen_train_uids=frozenset(_uid(value) for value in range(50)),
        current_batch_uids=frozenset(_uid(value) for value in range(25, 50)),
    )
    assert updated.rules[0].status == "confirmed"
    assert updated.rules[0].support_count == 2
    assert updated.rules[0].first_seen_batch == 1
    assert updated.rules[0].last_validated_batch == 2


def test_omitted_prior_rules_are_not_recursively_erased() -> None:
    all_uids = frozenset(_uid(index) for index in range(100))
    first = BatchReflectionV1.model_validate(
        {
            "batch_index": 1,
            "prior_evidence_sha256": None,
            "proposals": [_proposal(support=[_uid(0)])],
        }
    )
    index = apply_batch_reflection(
        RuleEvidenceIndexV1.generation_zero(),
        first,
        all_train_uids=all_uids,
        seen_train_uids=frozenset(_uid(value) for value in range(25)),
        current_batch_uids=frozenset(_uid(value) for value in range(25)),
    )
    second = BatchReflectionV1(
        batch_index=2,
        prior_evidence_sha256=index.digest,
        proposals=(),
    )
    retained = apply_batch_reflection(
        index,
        second,
        all_train_uids=all_uids,
        seen_train_uids=frozenset(_uid(value) for value in range(50)),
        current_batch_uids=frozenset(_uid(value) for value in range(25, 50)),
    )
    assert retained.rules == index.rules


def test_test_or_future_train_reference_is_rejected() -> None:
    all_uids = frozenset(_uid(index) for index in range(100))
    reflection = BatchReflectionV1.model_validate(
        {
            "batch_index": 1,
            "prior_evidence_sha256": None,
            "proposals": [_proposal(support=[_uid(99)])],
        }
    )
    with pytest.raises(
        TemperatureEvidenceError,
        match="EVIDENCE_REFERENCE_OUTSIDE_VISIBLE_TRAIN",
    ):
        apply_batch_reflection(
            RuleEvidenceIndexV1.generation_zero(),
            reflection,
            all_train_uids=all_uids,
            seen_train_uids=frozenset(_uid(value) for value in range(25)),
            current_batch_uids=frozenset(_uid(value) for value in range(25)),
        )


def test_reflector_parser_rejects_duplicate_keys_and_public_summary_is_aggregate() -> None:
    response = '{"batch_index":1,"batch_index":1,"prior_evidence_sha256":null,"proposals":[]}'
    with pytest.raises(TemperatureEvidenceError, match="REFLECTOR_RESPONSE_DUPLICATE_KEY"):
        parse_batch_reflection(response)

    summary = RuleEvidenceIndexV1.generation_zero().to_public_summary()
    encoded = json.dumps(summary, sort_keys=True)
    assert "train_uid" not in encoded
    assert _uid(0) not in encoded


def test_retire_preserves_prior_rule_text_and_records_reason() -> None:
    all_uids = frozenset(_uid(index) for index in range(100))
    seen_one = frozenset(_uid(index) for index in range(25))
    first = BatchReflectionV1.model_validate(
        {
            "batch_index": 1,
            "prior_evidence_sha256": None,
            "proposals": [_proposal(support=[_uid(0), _uid(1)])],
        }
    )
    index = apply_batch_reflection(
        RuleEvidenceIndexV1.generation_zero(),
        first,
        all_train_uids=all_uids,
        seen_train_uids=seen_one,
        current_batch_uids=seen_one,
    )
    rule = index.rules[0]
    retire = _proposal(
        support=[_uid(25)],
        counterexamples=[_uid(26)],
        rule_id=rule.rule_id,
        operation="retire",
    )
    retire["content"] = rule.content
    reflection = BatchReflectionV1.model_validate(
        {
            "batch_index": 2,
            "prior_evidence_sha256": index.digest,
            "proposals": [retire],
        }
    )
    retired = apply_batch_reflection(
        index,
        reflection,
        all_train_uids=all_uids,
        seen_train_uids=frozenset(_uid(value) for value in range(50)),
        current_batch_uids=frozenset(_uid(value) for value in range(25, 50)),
    ).rules[0]
    assert retired.content == rule.content
    assert retired.applicability == rule.applicability
    assert retired.validation == rule.validation
    assert retired.status == "retired"
    assert retired.target_projection == "none"
    assert retired.retirement_reason


def test_retire_cannot_rewrite_prior_semantics() -> None:
    all_uids = frozenset(_uid(index) for index in range(100))
    first_batch = frozenset(_uid(index) for index in range(25))
    first = BatchReflectionV1.model_validate(
        {
            "batch_index": 1,
            "prior_evidence_sha256": None,
            "proposals": [_proposal(support=[_uid(0), _uid(1)])],
        }
    )
    index = apply_batch_reflection(
        RuleEvidenceIndexV1.generation_zero(),
        first,
        all_train_uids=all_uids,
        seen_train_uids=first_batch,
        current_batch_uids=first_batch,
    )
    proposal = _proposal(
        support=[_uid(25)],
        rule_id=index.rules[0].rule_id,
        operation="retire",
        content="A rewritten rule that must not replace the audited predecessor.",
    )
    reflection = BatchReflectionV1.model_validate(
        {
            "batch_index": 2,
            "prior_evidence_sha256": index.digest,
            "proposals": [proposal],
        }
    )
    with pytest.raises(TemperatureEvidenceError, match="EVIDENCE_RETIRE_REWRITE_FORBIDDEN"):
        apply_batch_reflection(
            index,
            reflection,
            all_train_uids=all_uids,
            seen_train_uids=frozenset(_uid(value) for value in range(50)),
            current_batch_uids=frozenset(_uid(value) for value in range(25, 50)),
        )


def test_contradiction_heavy_or_low_confidence_rule_is_not_projected() -> None:
    all_uids = frozenset(_uid(index) for index in range(100))
    current = frozenset(_uid(index) for index in range(25))
    for proposal in (
        _proposal(
            support=[_uid(0), _uid(1)],
            counterexamples=[_uid(2), _uid(3)],
        ),
        _proposal(
            support=[_uid(4), _uid(5)],
            confidence=0.49,
            content="A second conditional principle with insufficient calibrated confidence.",
        ),
    ):
        result = apply_batch_reflection(
            RuleEvidenceIndexV1.generation_zero(),
            BatchReflectionV1.model_validate(
                {
                    "batch_index": 1,
                    "prior_evidence_sha256": None,
                    "proposals": [proposal],
                }
            ),
            all_train_uids=all_uids,
            seen_train_uids=current,
            current_batch_uids=current,
        )
        assert result.rules[0].status == "provisional"
        assert result.rules[0].target_projection == "none"


def test_parser_rejects_nonfinite_and_oversize_payloads() -> None:
    with pytest.raises(TemperatureEvidenceError, match="REFLECTOR_RESPONSE_NONFINITE"):
        parse_batch_reflection(
            '{"batch_index":1,"prior_evidence_sha256":null,"proposals":[],"x":NaN}'
        )
    with pytest.raises(TemperatureEvidenceError, match="REFLECTOR_RESPONSE_SIZE_INVALID"):
        parse_batch_reflection("{}" + ("x" * 100), maximum_utf8_bytes=32)


def test_parser_canonicalizes_uid_order_but_rejects_duplicates() -> None:
    first = _uid(1)
    second = _uid(2)
    response = json.dumps(
        {
            "schema_version": "TemperatureBatchReflectionV1",
            "batch_index": 1,
            "prior_evidence_sha256": None,
            "proposals": [_proposal(support=[second, first])],
        },
        separators=(",", ":"),
    )
    # Deliberately undo the helper's sorting to reproduce a harmless model
    # transport-order variation.
    payload = json.loads(response)
    payload["proposals"][0]["supporting_train_uids"] = [second, first]
    parsed = parse_batch_reflection(json.dumps(payload, separators=(",", ":")))
    assert parsed.proposals[0].supporting_train_uids == tuple(sorted((first, second)))

    payload["proposals"][0]["supporting_train_uids"] = [first, first]
    with pytest.raises(
        TemperatureEvidenceError,
        match="REFLECTOR_RESPONSE_DUPLICATE_EVIDENCE_UID",
    ):
        parse_batch_reflection(json.dumps(payload, separators=(",", ":")))


def test_same_semantics_with_different_model_rule_id_is_rejected() -> None:
    all_uids = frozenset(_uid(index) for index in range(100))
    current = frozenset(_uid(index) for index in range(25))
    proposal = _proposal(support=[_uid(0)])
    reflection = BatchReflectionV1.model_validate(
        {
            "batch_index": 1,
            "prior_evidence_sha256": None,
            "proposals": [proposal, {**proposal, "supporting_train_uids": [_uid(1)]}],
        }
    )
    with pytest.raises(TemperatureEvidenceError, match="EVIDENCE_RULE_ID_COLLISION"):
        apply_batch_reflection(
            RuleEvidenceIndexV1.generation_zero(),
            reflection,
            all_train_uids=all_uids,
            seen_train_uids=current,
            current_batch_uids=current,
        )
