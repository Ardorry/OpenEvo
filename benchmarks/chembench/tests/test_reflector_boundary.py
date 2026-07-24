from __future__ import annotations

import json
import unittest

from openevo_chembench.artifacts import (
    AgentSystemPayload,
    ApprovedArtifact,
    ArtifactKind,
    CandidateArtifact,
    SkillBundlePayload,
    TextMemoryPayload,
)
from openevo_chembench.models import (
    FailureCategory,
    SafeEvolutionSignal,
    SignalOutcome,
    SignalSeverity,
)
from openevo_chembench.reflector import (
    SafeEvolutionReflector,
    compute_source_signal_hash,
)


_PARENT_HASH = "a" * 64


def _safe_signal(
    *categories: FailureCategory,
) -> SafeEvolutionSignal:
    return SafeEvolutionSignal(
        outcome=SignalOutcome.NEEDS_IMPROVEMENT,
        failure_categories=categories,
        severity=SignalSeverity.MEDIUM,
    )


def _artifact_text(candidate: CandidateArtifact) -> str:
    payload = candidate.payload
    if isinstance(payload, TextMemoryPayload):
        return payload.markdown
    if isinstance(payload, SkillBundlePayload):
        return "\n".join(file.content for file in payload.files)
    if isinstance(payload, AgentSystemPayload):
        return payload.markdown
    raise AssertionError("unexpected artifact payload")


def _candidate_record(candidate: CandidateArtifact) -> dict[str, object]:
    return {
        "artifact_type": candidate.artifact_type.value,
        "payload": _artifact_text(candidate),
        "parent_hash": candidate.parent_hash,
        "source_signal_hash": candidate.source_signal_hash,
        "round_index": candidate.round_index,
        "artifact_version": candidate.version,
    }


class ReflectorBoundaryTests(unittest.TestCase):
    def test_safe_signal_generates_untrusted_text_memory_candidate(self) -> None:
        signal = _safe_signal(FailureCategory.CALCULATION_VERIFICATION)

        candidate = SafeEvolutionReflector().reflect(
            signal,
            artifact_type=ArtifactKind.TEXT_MEMORY,
            round_index=2,
            parent_hash=_PARENT_HASH,
            artifact_version=3,
        )

        self.assertIsInstance(candidate, CandidateArtifact)
        self.assertNotIsInstance(candidate, ApprovedArtifact)
        self.assertIs(candidate.artifact_type, ArtifactKind.TEXT_MEMORY)
        self.assertIsInstance(candidate.payload, TextMemoryPayload)
        self.assertEqual(candidate.parent_hash, _PARENT_HASH)
        self.assertEqual(candidate.round_index, 2)
        self.assertEqual(candidate.version, 3)
        self.assertEqual(
            candidate.source_signal_hash,
            compute_source_signal_hash(signal),
        )
        self.assertIn(
            "Add numerical verification before finalizing a calculation.",
            candidate.payload.markdown,
        )
        self.assertNotIn(
            FailureCategory.CALCULATION_VERIFICATION.value,
            candidate.payload.markdown,
        )
        self.assertFalse(hasattr(candidate, "to_agent_payload"))

    def test_reflector_supports_all_three_artifact_types(self) -> None:
        signal = _safe_signal(FailureCategory.FORMAT_FAILURE)
        expected_payloads = {
            ArtifactKind.TEXT_MEMORY: TextMemoryPayload,
            ArtifactKind.SKILL_BUNDLE: SkillBundlePayload,
            ArtifactKind.AGENT_SYSTEM: AgentSystemPayload,
        }

        for artifact_type, expected_payload_type in expected_payloads.items():
            with self.subTest(artifact_type=artifact_type.value):
                candidate = SafeEvolutionReflector().reflect(
                    signal,
                    artifact_type=artifact_type,
                    round_index=1,
                )
                self.assertIsInstance(candidate.payload, expected_payload_type)
                self.assertIn(
                    "Always follow the required answer format exactly",
                    _artifact_text(candidate),
                )

        skill_candidate = SafeEvolutionReflector().reflect(
            signal,
            artifact_type=ArtifactKind.SKILL_BUNDLE,
            round_index=1,
        )
        assert isinstance(skill_candidate.payload, SkillBundlePayload)
        self.assertEqual(
            tuple(file.relative_path for file in skill_candidate.payload.files),
            ("SKILL.md",),
        )
        skill_text = skill_candidate.payload.files[0].content
        self.assertTrue(skill_text.startswith("---\nname: "))
        frontmatter = skill_text.split("---", maxsplit=2)[1]
        self.assertEqual(
            {
                line.split(":", maxsplit=1)[0]
                for line in frontmatter.strip().splitlines()
            },
            {"name", "description"},
        )

    def test_mapping_with_correct_answer_is_rejected_as_non_signal(self) -> None:
        signal = _safe_signal(FailureCategory.INCORRECT_SELECTION)
        malicious_payload = {
            **signal.to_evolution_payload(),
            "correct_answer": "B",
        }

        with self.assertRaises(TypeError) as raised:
            SafeEvolutionReflector().reflect(
                malicious_payload,  # type: ignore[arg-type]
                artifact_type=ArtifactKind.TEXT_MEMORY,
                round_index=1,
            )

        self.assertNotIn("B", str(raised.exception))

    def test_candidate_record_contains_no_private_boundary_fields(self) -> None:
        for artifact_type in ArtifactKind:
            with self.subTest(artifact_type=artifact_type.value):
                candidate = SafeEvolutionReflector().reflect(
                    _safe_signal(FailureCategory.FORMAT_FAILURE),
                    artifact_type=artifact_type,
                    round_index=1,
                    parent_hash=_PARENT_HASH,
                )

                record = _candidate_record(candidate)
                serialized = json.dumps(record, sort_keys=True)

                self.assertEqual(
                    set(record),
                    {
                        "artifact_type",
                        "payload",
                        "parent_hash",
                        "source_signal_hash",
                        "round_index",
                        "artifact_version",
                    },
                )
                for forbidden in (
                    "question",
                    "target",
                    "target_scores",
                    "uuid",
                    "canary",
                    "correct_answer",
                    "transcript",
                ):
                    self.assertNotIn(forbidden, serialized.lower())
                for private_literal in (
                    "private-question-literal",
                    "private-target-literal",
                    "private-uuid-literal",
                    "private-canary-literal",
                ):
                    self.assertNotIn(private_literal, serialized)

    def test_signal_hash_and_strategy_order_are_canonical(self) -> None:
        first = _safe_signal(
            FailureCategory.FORMAT_FAILURE,
            FailureCategory.CALCULATION_VERIFICATION,
        )
        second = _safe_signal(
            FailureCategory.CALCULATION_VERIFICATION,
            FailureCategory.FORMAT_FAILURE,
        )

        first_candidate = SafeEvolutionReflector().reflect(
            first,
            artifact_type=ArtifactKind.AGENT_SYSTEM,
            round_index=1,
        )
        second_candidate = SafeEvolutionReflector().reflect(
            second,
            artifact_type=ArtifactKind.AGENT_SYSTEM,
            round_index=1,
        )

        self.assertEqual(
            compute_source_signal_hash(first),
            compute_source_signal_hash(second),
        )
        self.assertEqual(first_candidate.content_hash, second_candidate.content_hash)
        self.assertEqual(first_candidate.payload, second_candidate.payload)


if __name__ == "__main__":
    unittest.main()
