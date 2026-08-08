"""Benchmark-local quality gate for native per-item evolution artifacts.

This is an adapter admission check, not a replacement for OpenEvo's native
artifact registry/admission.  It consumes read-only text snapshots of the
already registered three artifacts and never creates, modifies, or promotes an
artifact.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from .training_state_store import canonical_sha256

QUALITY_SCHEMA = "openevo.researchclawbench.task_specific_artifact_quality.v1"
QUALITY_FAILURE = "TASK_SPECIFIC_ARTIFACT_QUALITY_FAILED"
_ARTIFACT_TYPES = ("text_memory", "skill_bundle", "agent_system")
_PRIVATE_MARKERS = (
    "ground_truth_entries",
    "criterion.content",
    "criterion.keywords",
    "criterion.path",
    "criterion.weight",
    "target image",
    "judge reasoning",
    "judge raw response",
)
_GENERIC_PATTERNS = (
    "inspect the task",
    "inventory workspace",
    "verify outputs",
    "ensure reproducibility",
    "validate claims",
    "check results",
    "follow instructions",
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+(?=[#*-]|\S)")


class TaskSpecificArtifactQualityError(RuntimeError):
    """A native artifact triple is unsafe or too generic for fresh rerun use."""

    def __init__(self, reason_code: str, report: dict[str, Any] | None = None) -> None:
        self.reason_code = reason_code
        self.report = report
        super().__init__(reason_code)


def _normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _string_leaves(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _string_leaves(child)
    elif isinstance(value, list):
        for child in value:
            yield from _string_leaves(child)


def _concepts(capsule: Mapping[str, Any]) -> list[str]:
    raw = capsule.get("candidate_concepts")
    if not isinstance(raw, list) or not raw:
        raise TaskSpecificArtifactQualityError("ARTIFACT_QUALITY_CAPSULE_INVALID")
    concepts: list[str] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("provenance") not in {
            "candidate_workspace",
            "public_task",
        }:
            raise TaskSpecificArtifactQualityError("ARTIFACT_QUALITY_CAPSULE_INVALID")
        text = item.get("text")
        if not isinstance(text, str) or not _normalize(text):
            raise TaskSpecificArtifactQualityError("ARTIFACT_QUALITY_CAPSULE_INVALID")
        normalized = _normalize(text)
        if normalized not in concepts:
            concepts.append(normalized)
    return concepts


def _hidden_literals(ground_truth_entries: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for value in _string_leaves(ground_truth_entries):
        normalized = _normalize(value)
        if len(normalized) >= 12 or len(normalized.split()) >= 3:
            values.append(normalized)
    return values


def _semantic_units(text: str) -> list[str]:
    return [unit.strip() for unit in _SENTENCE.split(text) if unit.strip()]


def _concept_units(text: str, concepts: list[str]) -> dict[str, list[str]]:
    units = _semantic_units(text)
    result: dict[str, list[str]] = {}
    for concept in concepts:
        matches = [unit for unit in units if concept in _normalize(unit)]
        if matches:
            result[concept] = matches
    return result


def _contains_any(value: str, terms: Iterable[str]) -> bool:
    normalized = _normalize(value)
    return any(_normalize(term) in normalized for term in terms)


def _artifact_metrics(
    artifact_type: str,
    text: str,
    concepts: list[str],
    weakness_dimensions: list[str],
) -> dict[str, Any]:
    units = _semantic_units(text)
    concept_units = _concept_units(text, concepts)
    retained = sorted(concept_units)
    candidate_specific_units = [
        unit
        for matches in concept_units.values()
        for unit in matches
        if not _contains_any(unit, ("source files", "evidence refs", "references:"))
    ]
    strength_units = [
        unit
        for unit in candidate_specific_units
        if _contains_any(unit, ("reconstruct", "preserve", "baseline", "prior", "successful"))
    ]
    action_units = [
        unit
        for unit in candidate_specific_units
        if _contains_any(unit, ("reconstruct", "add", "generate", "build", "repeat", "calculate"))
    ]
    weakness_units = [
        unit
        for unit in units
        if _contains_any(unit, [dimension.replace("_", " ") for dimension in weakness_dimensions])
    ]
    validation_units = [
        unit
        for unit in units
        if _contains_any(unit, ("validate", "validation", "cross check", "cross-check", "quantitative", "numeric", "aggregation"))
    ]
    generic_units = [
        unit
        for unit in units
        if _contains_any(unit, _GENERIC_PATTERNS)
        and not any(concept in _normalize(unit) for concept in concepts)
    ]
    procedure_units = [
        unit
        for unit in action_units
        if _contains_any(unit, ("reconstruct", "add", "generate", "build", "repeat", "calculate"))
    ]
    return {
        "artifact_type": artifact_type,
        "retained_concepts": retained,
        "candidate_specific_reference_count": len(candidate_specific_units),
        "baseline_strength_count": len(strength_units),
        "observed_weakness_count": len(weakness_units),
        "weakness_to_action_mapping_count": int(bool(weakness_units and action_units)),
        "executable_task_local_step_count": len(procedure_units),
        "validation_step_count": len(validation_units),
        "fresh_workspace_reconstruction_present": _contains_any(
            text, ("fresh workspace", "fresh run", "reconstruct")
        ),
        "preserve_baseline_strategy_present": _contains_any(
            text, ("preserve", "reconstruct", "baseline strategy", "prior strategy")
        ),
        "weakness_driven_improvement_present": bool(weakness_units and action_units),
        "generic_advice_ratio": (
            0.0 if not units else round(len(generic_units) / len(units), 6)
        ),
    }


def _leakage_findings(texts: Mapping[str, str], ground_truth_entries: list[dict[str, Any]]) -> list[str]:
    combined = _normalize("\n".join(texts.values()))
    findings: list[str] = []
    for marker in _PRIVATE_MARKERS:
        if _normalize(marker) in combined:
            findings.append("PRIVATE_MARKER")
    for literal in _hidden_literals(ground_truth_entries):
        if literal and literal in combined:
            findings.append("GT_LITERAL")
    return sorted(set(findings))


def assess_task_specific_artifact_quality(
    *,
    capsule: Mapping[str, Any],
    artifact_texts: Mapping[str, str],
    ground_truth_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return a deterministic pre-injection quality report for native outputs."""

    if set(artifact_texts) != set(_ARTIFACT_TYPES) or not all(
        isinstance(value, str) and value.strip() for value in artifact_texts.values()
    ):
        raise TaskSpecificArtifactQualityError("ARTIFACT_QUALITY_ARTIFACT_INVENTORY_INVALID")
    concepts = _concepts(capsule)
    raw_weaknesses = capsule.get("observed_weaknesses")
    if not isinstance(raw_weaknesses, list) or not raw_weaknesses:
        raise TaskSpecificArtifactQualityError("ARTIFACT_QUALITY_CAPSULE_INVALID")
    dimensions = [
        item.get("dimension")
        for item in raw_weaknesses
        if isinstance(item, dict) and isinstance(item.get("dimension"), str)
    ]
    if not dimensions:
        raise TaskSpecificArtifactQualityError("ARTIFACT_QUALITY_CAPSULE_INVALID")
    metrics = {
        artifact_type: _artifact_metrics(artifact_type, artifact_texts[artifact_type], concepts, dimensions)
        for artifact_type in _ARTIFACT_TYPES
    }
    leakage = _leakage_findings(artifact_texts, ground_truth_entries)
    overall_retained = sorted(
        {
            concept
            for metric in metrics.values()
            for concept in metric["retained_concepts"]
        }
    )
    aggregate = {
        "candidate_specific_reference_count": sum(
            metric["candidate_specific_reference_count"] for metric in metrics.values()
        ),
        "baseline_strength_count": sum(
            metric["baseline_strength_count"] for metric in metrics.values()
        ),
        "observed_weakness_count": sum(
            metric["observed_weakness_count"] for metric in metrics.values()
        ),
        "weakness_to_action_mapping_count": sum(
            metric["weakness_to_action_mapping_count"] for metric in metrics.values()
        ),
        "overall_retention_ratio": round(len(overall_retained) / len(concepts), 6),
    }
    memory = metrics["text_memory"]
    skill = metrics["skill_bundle"]
    agent = metrics["agent_system"]
    passed = (
        not leakage
        and aggregate["candidate_specific_reference_count"] >= 2
        and aggregate["baseline_strength_count"] >= 1
        and aggregate["observed_weakness_count"] >= 1
        and aggregate["weakness_to_action_mapping_count"] >= 1
        and bool(memory["retained_concepts"])
        and memory["baseline_strength_count"] >= 1
        and memory["observed_weakness_count"] >= 1
        and memory["weakness_to_action_mapping_count"] >= 1
        and bool(skill["retained_concepts"])
        and skill["executable_task_local_step_count"] >= 2
        and skill["validation_step_count"] >= 1
        and agent["fresh_workspace_reconstruction_present"] is True
        and agent["preserve_baseline_strategy_present"] is True
        and agent["weakness_driven_improvement_present"] is True
    )
    body = {
        "schema_version": QUALITY_SCHEMA,
        "status": "PASS" if passed else QUALITY_FAILURE,
        "candidate_concepts": concepts,
        "artifact_metrics": metrics,
        "aggregate": aggregate,
        "gt_leakage_findings": leakage,
        "provenance_violations": [],
        "artifact_text_sha256": {
            artifact_type: canonical_sha256(text)
            for artifact_type, text in artifact_texts.items()
        },
    }
    return {**body, "content_sha256": canonical_sha256(body)}


def require_task_specific_artifact_quality(**kwargs: Any) -> dict[str, Any]:
    report = assess_task_specific_artifact_quality(**kwargs)
    if report["status"] != "PASS":
        raise TaskSpecificArtifactQualityError(QUALITY_FAILURE, report)
    return report


__all__ = [
    "QUALITY_FAILURE",
    "QUALITY_SCHEMA",
    "TaskSpecificArtifactQualityError",
    "assess_task_specific_artifact_quality",
    "require_task_specific_artifact_quality",
]
