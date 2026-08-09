"""Per-artifact preservation gate for R3 native evolution outputs.

The gate reads bounded snapshots of already registered artifacts.  It is an
adapter admission control, never a replacement for Core artifact admission.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from .training_state_store import canonical_sha256

QUALITY_SCHEMA = "openevo.researchclawbench.preservation_artifact_quality.v3"
QUALITY_FAILURE = "PRESERVATION_ARTIFACT_QUALITY_FAILED"
_ARTIFACT_TYPES = ("text_memory", "skill_bundle", "agent_system")
_PRIVATE_MARKERS = (
    "ground_truth_entries", "criterion.content", "criterion.keywords", "criterion.path",
    "criterion.weight", "target image", "judge reasoning", "judge raw response",
)
_GENERIC_PATTERNS = (
    "inspect the task", "inventory workspace", "ensure reproducibility", "validate outputs",
    "verify outputs", "check results", "follow instructions", "link evidence", "run scripts",
    "verify paths", "review files", "check files",
)
_LOW_SIGNAL = frozenset({"analysis", "artifact", "candidate", "data", "figure", "image", "output", "report", "result", "summary", "validation", "visual", "evidence", "the", "with"})
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+(?=[#*\-\d]|\S)")


class TaskSpecificArtifactQualityError(RuntimeError):
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


def _units(text: str) -> list[str]:
    return [unit.strip() for unit in _SENTENCE.split(text) if unit.strip()]


def _terms(value: str) -> set[str]:
    return {
        token for token in _normalize(value).split()
        if token not in _LOW_SIGNAL and len(token) > 2 and not token.isdigit()
    }


def _required_achievements(capsule: Mapping[str, Any]) -> list[dict[str, Any]]:
    ledger = capsule.get("baseline_achievement_ledger")
    if not isinstance(ledger, dict) or ledger.get("schema_version") != "openevo.researchclawbench.baseline_achievement_ledger.v2":
        raise TaskSpecificArtifactQualityError("ARTIFACT_QUALITY_CAPSULE_INVALID")
    raw = ledger.get("achievements")
    if not isinstance(raw, list) or not raw:
        raise TaskSpecificArtifactQualityError("ARTIFACT_QUALITY_CAPSULE_INVALID")
    achievements: list[dict[str, Any]] = []
    ids: set[str] = set()
    for item in raw:
        if (
            not isinstance(item, dict)
            or item.get("preservation_priority") != "required"
            or not isinstance(item.get("achievement_id"), str)
            or item["achievement_id"] in ids
            or not isinstance(item.get("method_signature"), list)
            or not isinstance(item.get("required_output_classes"), list)
            or not isinstance(item.get("capability"), str)
        ):
            raise TaskSpecificArtifactQualityError("ARTIFACT_QUALITY_CAPSULE_INVALID")
        ids.add(item["achievement_id"])
        achievements.append(item)
    return achievements


def _weaknesses(capsule: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = capsule.get("observed_weaknesses")
    if not isinstance(raw, list) or not raw:
        raise TaskSpecificArtifactQualityError("ARTIFACT_QUALITY_CAPSULE_INVALID")
    return [item for item in raw if isinstance(item, dict) and isinstance(item.get("weakness_id"), str)]


def _hidden_literals(entries: list[dict[str, Any]]) -> list[str]:
    return list({
        _normalize(value)
        for value in _string_leaves(entries)
        if len(_normalize(value)) >= 12 or len(_normalize(value).split()) >= 3
    })


def _leakage(texts: Mapping[str, str], entries: list[dict[str, Any]]) -> list[str]:
    combined = _normalize("\n".join(texts.values()))
    findings = ["PRIVATE_MARKER" for marker in _PRIVATE_MARKERS if _normalize(marker) in combined]
    if any(literal and literal in combined for literal in _hidden_literals(entries)):
        findings.append("GT_LITERAL")
    return sorted(set(findings))


def _achievement_terms(achievement: Mapping[str, Any]) -> set[str]:
    signature = " ".join(str(value) for value in achievement.get("method_signature", []))
    return _terms(signature + " " + str(achievement.get("capability", "")))


def _matches_achievement(unit: str, achievement: Mapping[str, Any]) -> bool:
    normalized = _normalize(unit)
    identifier = _normalize(str(achievement["achievement_id"]))
    # An explicit achievement reference is a structured identity claim.  Do
    # not let generic output-role words in the same sentence make that claim
    # accidentally cover a different required achievement.
    mentioned = set(re.findall(r"\bachievement\s+[a-z0-9]+\b", normalized))
    if mentioned:
        return identifier in mentioned
    terms = _achievement_terms(achievement)
    if len(terms) < 2:
        return False
    overlap = len(terms.intersection(_terms(unit)))
    return overlap >= min(3, len(terms))


def _has_output_role(unit: str, achievement: Mapping[str, Any]) -> bool:
    classes = set(achievement.get("required_output_classes", []))
    vocabulary = {
        "script": ("script", "code", "execute"),
        "numeric_output": ("numeric", "table", "csv", "summary", "metric"),
        "figure": ("figure", "plot", "chart", "image"),
        "report_section": ("report", "discussion", "section"),
    }
    return any(any(word in _normalize(unit) for word in vocabulary[item]) for item in classes if item in vocabulary)


def _matches_memory(unit: str, achievement: Mapping[str, Any]) -> bool:
    normalized = _normalize(unit)
    return _matches_achievement(unit, achievement) and _has_output_role(unit, achievement) and any(
        marker in normalized for marker in ("preserve", "retain", "must not lose", "do not drop")
    )


def _matches_skill(unit: str, achievement: Mapping[str, Any]) -> bool:
    normalized = _normalize(unit)
    return _matches_achievement(unit, achievement) and _has_output_role(unit, achievement) and "reconstruct" in normalized and any(
        marker in normalized for marker in ("verify", "validation", "check", "confirm")
    )


def _feedback_mapping(unit: str, weakness: Mapping[str, Any], achievements: list[dict[str, Any]]) -> bool:
    normalized = _normalize(unit)
    weakness_id = _normalize(str(weakness["weakness_id"]))
    weakness_terms = _terms(str(weakness.get("dimension", "")) + " " + str(weakness.get("candidate_observation", "")))
    weakness_present = weakness_id in normalized or len(weakness_terms.intersection(_terms(unit))) >= min(2, len(weakness_terms))
    return weakness_present and any(_matches_achievement(unit, achievement) for achievement in achievements) and any(
        marker in normalized for marker in ("add", "extend", "cross check", "crosscheck", "validate", "compare")
    ) and not any(marker in normalized for marker in ("replace", "discard", "restart"))


def _generic_ratio(units: list[str], achievements: list[dict[str, Any]]) -> float:
    generic = [
        unit for unit in units
        if any(_normalize(pattern) in _normalize(unit) for pattern in _GENERIC_PATTERNS)
        and not any(_matches_achievement(unit, achievement) for achievement in achievements)
    ]
    return 0.0 if not units else round(len(generic) / len(units), 6)


def _coverage(units: list[str], achievements: list[dict[str, Any]], matcher) -> tuple[list[str], float]:
    matched = [
        achievement["achievement_id"] for achievement in achievements
        if any(matcher(unit, achievement) for unit in units)
    ]
    return matched, round(len(matched) / len(achievements), 6)


def assess_task_specific_artifact_quality(*, capsule: Mapping[str, Any], artifact_texts: Mapping[str, str], ground_truth_entries: list[dict[str, Any]]) -> dict[str, Any]:
    if set(artifact_texts) != set(_ARTIFACT_TYPES) or not all(isinstance(value, str) and value.strip() for value in artifact_texts.values()):
        raise TaskSpecificArtifactQualityError("ARTIFACT_QUALITY_ARTIFACT_INVENTORY_INVALID")
    achievements = _required_achievements(capsule)
    weaknesses = _weaknesses(capsule)
    by_type = {artifact_type: _units(artifact_texts[artifact_type]) for artifact_type in _ARTIFACT_TYPES}
    memory_ids, memory_coverage = _coverage(by_type["text_memory"], achievements, _matches_memory)
    skill_ids, skill_coverage = _coverage(by_type["skill_bundle"], achievements, _matches_skill)
    skill_feedback_ids = [
        weakness["weakness_id"] for weakness in weaknesses
        if any(_feedback_mapping(unit, weakness, achievements) for unit in by_type["skill_bundle"])
    ]
    agent_normalized = _normalize(artifact_texts["agent_system"])
    agent_semantics = {
        key: key in agent_normalized
        for key in ("reconstruct", "preserve", "extend", "verify")
    }
    agent_equivalence = "baseline equivalence" in agent_normalized or "baseline-equivalence" in agent_normalized
    metrics = {
        "text_memory": {
            "role": "what_must_not_be_lost",
            "unique_required_achievements": memory_ids,
            "required_achievement_memory_coverage": memory_coverage,
            "generic_advice_ratio": _generic_ratio(by_type["text_memory"], achievements),
        },
        "skill_bundle": {
            "role": "how_to_reconstruct_and_extend",
            "unique_required_achievements": skill_ids,
            "required_achievement_reconstruction_coverage": skill_coverage,
            "feedback_action_coverage": round(len(skill_feedback_ids) / len(weaknesses), 6),
            "feedback_action_weakness_ids": skill_feedback_ids,
            "generic_advice_ratio": _generic_ratio(by_type["skill_bundle"], achievements),
        },
        "agent_system": {
            "role": "what_must_be_true_before_submission",
            "reconstruct_preserve_extend_verify": agent_semantics,
            "baseline_equivalence_before_submission": agent_equivalence,
            "generic_advice_ratio": _generic_ratio(by_type["agent_system"], achievements),
        },
    }
    leakage = _leakage(artifact_texts, ground_truth_entries)
    threshold = 0.80
    passed = (
        not leakage
        and memory_coverage >= threshold
        and skill_coverage >= threshold
        and len(skill_feedback_ids) == len(weaknesses)
        and all(agent_semantics.values())
        and agent_equivalence
    )
    aggregate = {
        "required_achievement_count": len(achievements),
        "memory_required_achievement_coverage": memory_coverage,
        "skill_required_achievement_reconstruction_coverage": skill_coverage,
        "feedback_action_coverage": round(len(skill_feedback_ids) / len(weaknesses), 6),
        "candidate_specific_reference_count": len(set(memory_ids) | set(skill_ids)),
        "baseline_strength_count": len(memory_ids),
        "observed_weakness_count": len(skill_feedback_ids),
        "weakness_to_action_mapping_count": len(skill_feedback_ids),
        "overall_retention_ratio": round((memory_coverage + skill_coverage) / 2, 6),
        "generic_advice_ratio": round(sum(item["generic_advice_ratio"] for item in metrics.values()) / 3, 6),
    }
    body = {
        "schema_version": QUALITY_SCHEMA,
        "status": "PASS" if passed else QUALITY_FAILURE,
        "required_achievement_ids": [item["achievement_id"] for item in achievements],
        "artifact_metrics": metrics,
        "aggregate": aggregate,
        "artifact_role_contract": {
            "memory": "required achievement preservation",
            "skill": "required reconstruction plus one additive mapping per weakness",
            "agent_system": "RECONSTRUCT PRESERVE EXTEND VERIFY plus baseline equivalence",
            "duplicate_mentions_count_once": True,
            "two_stem_overlap_is_not_sufficient": True,
        },
        "gt_leakage_findings": leakage,
        "provenance_violations": [],
        "artifact_text_sha256": {kind: canonical_sha256(text) for kind, text in artifact_texts.items()},
    }
    return {**body, "content_sha256": canonical_sha256(body)}


def require_task_specific_artifact_quality(**kwargs: Any) -> dict[str, Any]:
    report = assess_task_specific_artifact_quality(**kwargs)
    if report["status"] != "PASS":
        raise TaskSpecificArtifactQualityError(QUALITY_FAILURE, report)
    return report


__all__ = ["QUALITY_FAILURE", "QUALITY_SCHEMA", "TaskSpecificArtifactQualityError", "assess_task_specific_artifact_quality", "require_task_specific_artifact_quality"]
