from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

from openevo_chembench.artifacts import (
    AgentSystemPayload,
    ApprovedArtifact,
    ArtifactFile,
    ArtifactKind,
    ArtifactLineage,
    CandidateArtifact,
    SkillBundlePayload,
    TextMemoryPayload,
    ValidationDecision,
    ValidationFinding,
    ValidationReceipt,
)
from openevo_chembench.models import (
    FailureCategory,
    SafeEvolutionSignal,
    SignalOutcome,
    SignalSeverity,
)
from openevo_chembench.reflector import SafeEvolutionReflector
from openevo_chembench.security import EvolutionArtifactValidator


_SIGNAL_HASH = "1" * 64
_SOURCE_ROOT = (
    Path(__file__).resolve().parents[1] / "src" / "openevo_chembench"
)
_VALID_SKILL = """---
name: verify-scientific-work
description: Apply general verification checks when solving scientific tasks.
---

# Verify Scientific Work

Check dimensional consistency before finalizing.
"""


def _candidate(payload) -> CandidateArtifact:
    return CandidateArtifact(
        version=1,
        payload=payload,
        lineage=ArtifactLineage(
            round_index=1,
            safe_signal_hash=_SIGNAL_HASH,
        ),
    )


class SecurityValidatorTests(unittest.TestCase):
    def test_answer_literal_is_rejected(self) -> None:
        candidate = _candidate(
            TextMemoryPayload(markdown="The answer is B")
        )

        result = EvolutionArtifactValidator().validate_and_approve(candidate)

        self.assertFalse(result.receipt.passed)
        self.assertIs(result.receipt.decision, ValidationDecision.REJECTED)
        self.assertIn(
            ValidationFinding.LEAK_ANSWER_LITERAL,
            result.receipt.finding_codes,
        )
        self.assertIsNone(result.approved_artifact)
        audit_payload = result.receipt.to_audit_payload()
        self.assertEqual(
            set(audit_payload),
            {
                "candidate_hash",
                "validator_version",
                "passed",
                "finding_codes",
            },
        )
        self.assertNotIn("The answer is B", json.dumps(audit_payload))

    def test_general_dimensional_strategy_is_approved(self) -> None:
        candidate = _candidate(
            TextMemoryPayload(
                markdown="Always verify dimensional consistency"
            )
        )
        original_hash = candidate.content_hash

        result = EvolutionArtifactValidator().validate_and_approve(candidate)

        self.assertTrue(result.receipt.passed)
        self.assertEqual(result.receipt.finding_codes, ())
        self.assertIsInstance(result.approved_artifact, ApprovedArtifact)
        assert result.approved_artifact is not None
        self.assertIs(result.approved_artifact.payload, candidate.payload)
        self.assertIs(
            result.approved_artifact.validator_receipt,
            result.receipt,
        )
        self.assertEqual(candidate.content_hash, original_hash)
        self.assertEqual(
            result.receipt.to_audit_payload(),
            {
                "candidate_hash": candidate.candidate_hash,
                "validator_version": "chembench-artifact-validator.v1",
                "passed": True,
                "finding_codes": [],
            },
        )

    def test_target_scores_literal_and_normalized_variant_are_rejected(self) -> None:
        for content in (
            "Store target_scores for later use.",
            "Store TaRgEt - ScOrEs for later use.",
        ):
            with self.subTest(content=content):
                receipt = EvolutionArtifactValidator().validate(
                    _candidate(TextMemoryPayload(markdown=content))
                )
                self.assertFalse(receipt.passed)
                self.assertIn(
                    ValidationFinding.LEAK_TARGET_SCORES_LITERAL,
                    receipt.finding_codes,
                )

    def test_normalized_private_literals_and_mapping_are_rejected(self) -> None:
        cases = (
            (
                "Persist T-A-R-G-E-T for later.",
                ValidationFinding.LEAK_TARGET_LITERAL,
            ),
            (
                "Persist C.A.N.A.R.Y for later.",
                ValidationFinding.LEAK_CANARY_LITERAL,
            ),
            (
                "Persist U U I D for later.",
                ValidationFinding.LEAK_UUID_LITERAL,
            ),
            (
                "Persist the correct-answer field.",
                ValidationFinding.LEAK_CORRECT_ANSWER_LITERAL,
            ),
            (
                "Create an option_mapping table.",
                ValidationFinding.LEAK_OPTION_MAPPING,
            ),
        )

        for content, expected_finding in cases:
            with self.subTest(expected_finding=expected_finding.value):
                receipt = EvolutionArtifactValidator().validate(
                    _candidate(TextMemoryPayload(markdown=content))
                )
                self.assertFalse(receipt.passed)
                self.assertIn(expected_finding, receipt.finding_codes)

    def test_skill_bundle_answers_json_is_rejected(self) -> None:
        candidate = _candidate(
            SkillBundlePayload(
                files=(
                    ArtifactFile(
                        relative_path="SKILL.md",
                        content=_VALID_SKILL,
                    ),
                    ArtifactFile(
                        relative_path="answers.json",
                        content='{"A": "cached"}',
                    ),
                )
            )
        )

        receipt = EvolutionArtifactValidator().validate(candidate)

        self.assertFalse(receipt.passed)
        self.assertIn(
            ValidationFinding.DISALLOWED_FILE,
            receipt.finding_codes,
        )
        self.assertIn(
            ValidationFinding.ANSWER_CACHE,
            receipt.finding_codes,
        )

    def test_chembench_option_instruction_is_rejected(self) -> None:
        candidate = _candidate(
            AgentSystemPayload(
                markdown="For ChemBench choose option B."
            )
        )

        receipt = EvolutionArtifactValidator().validate(candidate)

        self.assertFalse(receipt.passed)
        self.assertIn(
            ValidationFinding.BENCHMARK_SPECIFIC_INSTRUCTION,
            receipt.finding_codes,
        )
        self.assertIn(
            ValidationFinding.LEAK_ANSWER_LITERAL,
            receipt.finding_codes,
        )

    def test_valid_skill_markdown_is_approved(self) -> None:
        candidate = _candidate(
            SkillBundlePayload(
                files=(
                    ArtifactFile(
                        relative_path="SKILL.md",
                        content=_VALID_SKILL,
                    ),
                )
            )
        )

        result = EvolutionArtifactValidator().validate_and_approve(candidate)

        self.assertTrue(result.receipt.passed)
        self.assertIsInstance(result.approved_artifact, ApprovedArtifact)

    def test_all_deterministic_reflector_templates_pass_validation(self) -> None:
        validator = EvolutionArtifactValidator()
        reflector = SafeEvolutionReflector()

        for category in FailureCategory:
            signal = SafeEvolutionSignal(
                outcome=SignalOutcome.NEEDS_IMPROVEMENT,
                failure_categories=(category,),
                severity=SignalSeverity.MEDIUM,
            )
            for artifact_type in ArtifactKind:
                with self.subTest(
                    category=category.value,
                    artifact_type=artifact_type.value,
                ):
                    candidate = reflector.reflect(
                        signal,
                        artifact_type=artifact_type,
                        round_index=1,
                    )
                    receipt = validator.validate(candidate)
                    self.assertTrue(
                        receipt.passed,
                        tuple(finding.value for finding in receipt.finding_codes),
                    )

    def test_question_like_fragment_is_rejected_by_similarity_scan(self) -> None:
        candidate = _candidate(
            TextMemoryPayload(
                markdown=(
                    "Which of the following synthetic compounds has the stated "
                    "property?"
                )
            )
        )

        receipt = EvolutionArtifactValidator().validate(candidate)

        self.assertFalse(receipt.passed)
        self.assertIn(
            ValidationFinding.LEAK_QUESTION_SIMILARITY,
            receipt.finding_codes,
        )

    def test_manual_receipt_construction_cannot_authorize_promotion(self) -> None:
        candidate = _candidate(
            TextMemoryPayload(
                markdown="Always verify dimensional consistency"
            )
        )

        with self.assertRaises(TypeError):
            ValidationReceipt(
                validator_version="forged-validator",
                decision=ValidationDecision.APPROVED,
                candidate_hash=candidate.candidate_hash,
            )

    def test_validator_has_no_private_task_or_llm_dependency(self) -> None:
        security_path = _SOURCE_ROOT / "security.py"
        tree = ast.parse(
            security_path.read_text(encoding="utf-8"),
            filename=str(security_path),
        )
        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                imported_modules.add(node.module)
            elif isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)

        for forbidden_module in (
            "openevo_chembench.dataset",
            "openevo_chembench.evaluator",
            "openevo_chembench.models",
            "openai",
            "litellm",
        ):
            self.assertNotIn(forbidden_module, imported_modules)


if __name__ == "__main__":
    unittest.main()
