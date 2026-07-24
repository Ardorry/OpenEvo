"""Deterministic safe-signal-only evolution proposal generation."""

from __future__ import annotations

import hashlib
import json
from types import MappingProxyType
from typing import Final, Mapping

from openevo_chembench.artifacts import (
    AgentSystemPayload,
    ArtifactFile,
    ArtifactKind,
    ArtifactLineage,
    ArtifactPayload,
    CandidateArtifact,
    SkillBundlePayload,
    TextMemoryPayload,
)
from openevo_chembench.models import (
    FailureCategory,
    SafeEvolutionSignal,
    SignalOutcome,
)


_SATISFACTORY_STRATEGY: Final = (
    "Retain a deliberate scientific reasoning workflow with a final consistency check."
)
_CATEGORY_STRATEGIES: Final[Mapping[FailureCategory, str]] = MappingProxyType(
    {
        FailureCategory.INCORRECT_SELECTION: (
            "Independently verify a tentative selection against governing scientific "
            "principles and eliminate inconsistent alternatives before finalizing."
        ),
        FailureCategory.FORMAT_FAILURE: (
            "Always follow the required answer format exactly and perform a final "
            "format-only check before submission."
        ),
        FailureCategory.INSUFFICIENT_VERIFICATION: (
            "Add an independent verification step before finalizing a result."
        ),
        FailureCategory.CALCULATION_VERIFICATION: (
            "Add numerical verification before finalizing a calculation."
        ),
        FailureCategory.REASONING_CHECK: (
            "Cross-check each conclusion against the stated assumptions and governing "
            "scientific principles."
        ),
        FailureCategory.FORMAT_ISSUE: (
            "Always follow the required answer format exactly and perform a final "
            "format-only check before submission."
        ),
        FailureCategory.UNIT_CONSISTENCY: (
            "Check unit and dimensional consistency before finalizing quantitative work."
        ),
        FailureCategory.UNCERTAINTY_MANAGEMENT: (
            "Resolve material uncertainty explicitly before committing to a final result."
        ),
        FailureCategory.TOOL_USAGE: (
            "Use tools only when they improve verification, and independently check "
            "their outputs."
        ),
        FailureCategory.TIMEOUT_OR_INCOMPLETE: (
            "Prioritize a complete solution path and reserve time for the required "
            "final response."
        ),
        FailureCategory.INCORRECT_RESULT_UNSPECIFIED: (
            "Perform an independent final verification before submitting a result."
        ),
    }
)

if frozenset(_CATEGORY_STRATEGIES) != frozenset(FailureCategory):
    raise RuntimeError("reflector strategy mapping must cover every failure category")


class SafeEvolutionReflector:
    """Convert one exact safe taxonomy signal into an untrusted candidate."""

    __slots__ = ()

    def reflect(
        self,
        signal: SafeEvolutionSignal,
        *,
        artifact_type: ArtifactKind,
        round_index: int,
        parent_hash: str | None = None,
        artifact_version: int = 1,
    ) -> CandidateArtifact:
        """Create a deterministic proposal without task, answer, or transcript input."""

        if type(signal) is not SafeEvolutionSignal:
            raise TypeError(
                "SafeEvolutionReflector.reflect requires an exact SafeEvolutionSignal"
            )
        if type(artifact_type) is not ArtifactKind:
            raise TypeError("artifact_type must be an ArtifactKind")

        strategies = _strategies_for_signal(signal)
        payload = _render_payload(artifact_type, strategies)
        lineage = ArtifactLineage(
            round_index=round_index,
            safe_signal_hash=compute_source_signal_hash(signal),
            parent_artifact_hash=parent_hash,
        )
        return CandidateArtifact(
            version=artifact_version,
            payload=payload,
            lineage=lineage,
        )


def reflect_safe_signal(
    signal: SafeEvolutionSignal,
    *,
    artifact_type: ArtifactKind,
    round_index: int,
    parent_hash: str | None = None,
    artifact_version: int = 1,
) -> CandidateArtifact:
    """Convenience entrypoint retaining the exact safe-signal boundary."""

    return SafeEvolutionReflector().reflect(
        signal,
        artifact_type=artifact_type,
        round_index=round_index,
        parent_hash=parent_hash,
        artifact_version=artifact_version,
    )


def compute_source_signal_hash(signal: SafeEvolutionSignal) -> str:
    """Hash only canonical, closed safe-signal fields."""

    if type(signal) is not SafeEvolutionSignal:
        raise TypeError(
            "compute_source_signal_hash requires an exact SafeEvolutionSignal"
        )
    canonical = {
        "schema_version": signal.schema_version,
        "outcome": signal.outcome.value,
        "failure_categories": sorted(
            category.value for category in signal.failure_categories
        ),
        "severity": signal.severity.value,
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _strategies_for_signal(signal: SafeEvolutionSignal) -> tuple[str, ...]:
    if signal.outcome is SignalOutcome.SATISFACTORY:
        return (_SATISFACTORY_STRATEGY,)

    strategies: list[str] = []
    for category in sorted(signal.failure_categories, key=lambda item: item.value):
        strategy = _CATEGORY_STRATEGIES[category]
        if strategy not in strategies:
            strategies.append(strategy)
    if not strategies:
        raise RuntimeError("non-satisfactory safe signal produced no strategy")
    return tuple(strategies)


def _render_payload(
    artifact_type: ArtifactKind,
    strategies: tuple[str, ...],
) -> ArtifactPayload:
    if artifact_type is ArtifactKind.TEXT_MEMORY:
        return TextMemoryPayload(markdown=_render_text_memory(strategies))
    if artifact_type is ArtifactKind.SKILL_BUNDLE:
        return SkillBundlePayload(
            files=(
                ArtifactFile(
                    relative_path="SKILL.md",
                    content=_render_skill(strategies),
                ),
            )
        )
    if artifact_type is ArtifactKind.AGENT_SYSTEM:
        return AgentSystemPayload(markdown=_render_agent_system(strategies))
    raise TypeError("unsupported artifact type")


def _render_text_memory(strategies: tuple[str, ...]) -> str:
    bullets = "\n".join(f"- {strategy}" for strategy in strategies)
    return f"# General scientific strategies\n\n{bullets}\n"


def _render_skill(strategies: tuple[str, ...]) -> str:
    steps = "\n".join(
        f"{index}. {strategy}" for index, strategy in enumerate(strategies, start=1)
    )
    return (
        "---\n"
        "name: improve-scientific-reasoning\n"
        "description: Apply general reasoning, verification, tool-use, and "
        "response-format checks when solving scientific tasks.\n"
        "---\n\n"
        "# Improve Scientific Reasoning\n\n"
        "Apply these checks before finalizing:\n\n"
        f"{steps}\n"
    )


def _render_agent_system(strategies: tuple[str, ...]) -> str:
    bullets = "\n".join(f"- {strategy}" for strategy in strategies)
    return (
        "# General scientific problem-solving instructions\n\n"
        "Apply the following instructions consistently:\n\n"
        f"{bullets}\n"
    )


__all__ = [
    "SafeEvolutionReflector",
    "compute_source_signal_hash",
    "reflect_safe_signal",
]
