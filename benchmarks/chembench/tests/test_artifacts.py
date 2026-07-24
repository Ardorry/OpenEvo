from __future__ import annotations

import unittest

from openevo_chembench.artifacts import (
    AgentSystemPayload,
    ApprovedArtifact,
    ArtifactFile,
    ArtifactKind,
    ArtifactLineage,
    CandidateArtifact,
    EvolutionArtifactSet,
    SkillBundlePayload,
    TextMemoryPayload,
    ValidationFinding,
    _promote_candidate,
)
from openevo_chembench.security import EvolutionArtifactValidator


_SIGNAL_HASH = "1" * 64


def _candidate(payload, *, version: int = 1) -> CandidateArtifact:
    return CandidateArtifact(
        version=version,
        payload=payload,
        lineage=ArtifactLineage(
            round_index=0,
            safe_signal_hash=_SIGNAL_HASH,
        ),
    )


def _approve(candidate: CandidateArtifact) -> ApprovedArtifact:
    result = EvolutionArtifactValidator().validate_and_approve(candidate)
    if result.approved_artifact is None:
        raise AssertionError("test candidate was unexpectedly rejected")
    return result.approved_artifact


class ArtifactModelTests(unittest.TestCase):
    def test_candidate_rejects_payload_subclasses_with_unscanned_fields(self) -> None:
        class ExtendedTextMemoryPayload(TextMemoryPayload):
            pass

        with self.assertRaises(TypeError):
            _candidate(
                ExtendedTextMemoryPayload(
                    markdown="Check dimensional consistency."
                )
            )

    def test_three_supported_artifact_payloads_have_closed_kinds(self) -> None:
        text = TextMemoryPayload(markdown="Check dimensional consistency.")
        skill = SkillBundlePayload(
            files=(
                ArtifactFile(
                    relative_path="SKILL.md",
                    content="# Chemistry reasoning\nVerify units.",
                ),
            )
        )
        system = AgentSystemPayload(markdown="Always verify unit consistency.")

        self.assertIs(text.kind, ArtifactKind.TEXT_MEMORY)
        self.assertIs(skill.kind, ArtifactKind.SKILL_BUNDLE)
        self.assertIs(system.kind, ArtifactKind.AGENT_SYSTEM)

    def test_skill_bundle_requires_safe_paths_and_root_skill_file(self) -> None:
        with self.assertRaises(ValueError):
            ArtifactFile(relative_path="../answer-key.txt", content="forbidden")
        with self.assertRaises(ValueError):
            ArtifactFile(relative_path=".", content="forbidden")
        with self.assertRaises(ValueError):
            SkillBundlePayload(
                files=(ArtifactFile(relative_path="notes.md", content="notes"),)
            )

    def test_candidate_hash_is_stable_for_skill_file_order(self) -> None:
        first = _candidate(
            SkillBundlePayload(
                files=(
                    ArtifactFile(relative_path="SKILL.md", content="# Skill"),
                    ArtifactFile(relative_path="references/checks.md", content="checks"),
                )
            )
        )
        second = _candidate(
            SkillBundlePayload(
                files=(
                    ArtifactFile(relative_path="references/checks.md", content="checks"),
                    ArtifactFile(relative_path="SKILL.md", content="# Skill"),
                )
            )
        )

        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.candidate_hash, second.candidate_hash)

    def test_rejected_candidate_cannot_become_approved(self) -> None:
        candidate = _candidate(
            TextMemoryPayload(markdown="The answer is B.")
        )
        receipt = EvolutionArtifactValidator().validate(candidate)

        with self.assertRaises(ValueError):
            _promote_candidate(candidate, receipt)

    def test_receipt_is_bound_to_exact_candidate_hash(self) -> None:
        candidate = _candidate(
            TextMemoryPayload(markdown="Check dimensional consistency.")
        )
        other_candidate = _candidate(
            TextMemoryPayload(markdown="Verify conservation constraints.")
        )
        wrong_receipt = EvolutionArtifactValidator().validate(other_candidate)
        self.assertTrue(wrong_receipt.passed)

        with self.assertRaises(ValueError):
            _promote_candidate(candidate, wrong_receipt)

    def test_receipt_cannot_be_reused_with_different_lineage(self) -> None:
        payload = TextMemoryPayload(markdown="Check dimensional consistency.")
        original = CandidateArtifact(
            version=1,
            payload=payload,
            lineage=ArtifactLineage(
                round_index=1,
                safe_signal_hash="1" * 64,
            ),
        )
        changed_lineage = CandidateArtifact(
            version=1,
            payload=payload,
            lineage=ArtifactLineage(
                round_index=2,
                safe_signal_hash="2" * 64,
            ),
        )
        receipt = EvolutionArtifactValidator().validate(original)

        self.assertTrue(receipt.passed)
        self.assertEqual(original.content_hash, changed_lineage.content_hash)
        self.assertNotEqual(original.candidate_hash, changed_lineage.candidate_hash)
        with self.assertRaises(ValueError):
            _promote_candidate(changed_lineage, receipt)

    def test_approved_artifact_cannot_be_constructed_outside_validator(self) -> None:
        candidate = _candidate(
            TextMemoryPayload(markdown="Check dimensional consistency.")
        )
        receipt = EvolutionArtifactValidator().validate(candidate)
        self.assertTrue(receipt.passed)

        with self.assertRaises(TypeError):
            ApprovedArtifact(
                version=candidate.version,
                payload=candidate.payload,
                lineage=candidate.lineage,
                artifact_hash=candidate.content_hash,
                validator_receipt=receipt,
            )

    def test_validator_receipt_rejects_free_text_findings(self) -> None:
        with self.assertRaises(ValueError):
            ValidationFinding("the answer is B")

    def test_approved_artifact_enters_only_matching_state_slot(self) -> None:
        text = _approve(
            _candidate(TextMemoryPayload(markdown="Check dimensional consistency."))
        )
        state = EvolutionArtifactSet().with_artifact(text)

        self.assertIs(state.text_memory, text)
        self.assertIsNone(state.skill_bundle)
        self.assertIsNone(state.agent_system)
        with self.assertRaises(ValueError):
            EvolutionArtifactSet(skill_bundle=text)


if __name__ == "__main__":
    unittest.main()
