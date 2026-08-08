"""Candidate-grounded retention input for one per-item evolution cycle.

The capsule is deliberately small and deterministic.  It identifies useful
baseline work by *candidate-owned* paths and connects admitted evaluator
dimensions to fresh-workspace reconstruction actions.  It is not a copy of a
Candidate workspace, and it never accepts hidden GT or Judge-private content.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .training_state_store import canonical_sha256

CAPSULE_SCHEMA = "openevo.researchclawbench.baseline_evidence_capsule.v1"
CAPSULE_ADMISSION_SCHEMA = "openevo.researchclawbench.baseline_evidence_capsule_admission.v1"
REFLECTOR_CAPSULE_SCHEMA = (
    "openevo.researchclawbench.baseline_evidence_capsule_reflector_view.v1"
)

_TOKEN = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_PRIVATE_KEY_FRAGMENTS = (
    "checklist",
    "criterion",
    "keywords",
    "weight",
    "target_image",
    "target_figure",
    "judge_reason",
    "judge_raw",
    "raw_response",
    "correct_answer",
    "expected_value",
    "ground_truth_entries",
)
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".svg"})
_CODE_SUFFIXES = frozenset({".py", ".ipynb", ".r", ".jl"})
_OUTPUT_SUFFIXES = frozenset({".csv", ".tsv", ".json"})
_GENERIC_STEM_TOKENS = frozenset(
    {
        "agent",
        "analysis",
        "artifact",
        "data",
        "figure",
        "image",
        "manifest",
        "output",
        "report",
        "result",
        "summary",
    }
)


class BaselineEvidenceCapsuleError(RuntimeError):
    """The candidate-only capsule cannot be safely constructed or admitted."""


class BaselineEvidenceCapsuleAdmissionError(BaselineEvidenceCapsuleError):
    """A capsule contains non-candidate or private evaluation information."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class BaselineEvidenceCapsule:
    """Closed data-only description of baseline work worth reconstructing."""

    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


def build_reflector_capsule_view(capsule: Mapping[str, Any]) -> dict[str, Any]:
    """Build the bounded candidate-specific view rendered into native prompts.

    The durable capsule remains complete for audit.  Core's native reflection
    renderer has a bounded record budget, so this view preserves the essential
    concept/strength/weakness/action chain without adding information.
    """

    _require_schema(capsule)
    concepts = capsule["candidate_concepts"][:2]
    strengths = capsule["successful_work"][:1]
    weaknesses = capsule["observed_weaknesses"][:1]
    actions = capsule["improvement_actions"][:1]
    actions_by_weakness = {
        item["addresses"]: item
        for item in actions
        if isinstance(item, dict) and isinstance(item.get("addresses"), str)
    }
    pairs = []
    for weakness in weaknesses:
        if not isinstance(weakness, dict):
            continue
        action = actions_by_weakness.get(weakness.get("weakness_id"))
        if not isinstance(action, dict):
            continue
        pairs.append(
            {
                "weakness_id": weakness["weakness_id"],
                "dimension": weakness["dimension"],
                "candidate_observation": weakness["candidate_observation"],
                "evidence_refs": weakness["evidence_refs"][:1],
                "next_run_action": action["action"],
                "preserve_refs": action["preserve_refs"][:1],
            }
        )
    if not concepts or not strengths or not pairs:
        raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_REFLECTOR_VIEW_INVALID")
    return {
        "schema_version": REFLECTOR_CAPSULE_SCHEMA,
        "capsule_sha256": canonical_sha256(dict(capsule)),
        "fresh_workspace_requirement": capsule["fresh_workspace_requirement"],
        "candidate_concepts": [
            {
                "text": item["text"],
                "kind": item["kind"],
                "evidence_refs": item["evidence_refs"][:1],
            }
            for item in concepts
        ],
        "successful_work": [
            {
                "summary": item["summary"],
                "evidence_refs": item["evidence_refs"][:1],
            }
            for item in strengths
        ],
        "weakness_to_action": pairs,
    }


def _walk(value: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], Any]]:
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_SCHEMA_INVALID")
            yield from _walk(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, (*path, str(index)))


def _normalize(value: str) -> str:
    return " ".join(_TOKEN.findall(value.casefold()))


def _candidate_files(root: Path) -> list[str]:
    files: list[str] = []
    for path in sorted(root.rglob("*")):
        if len(files) >= 4096:
            raise BaselineEvidenceCapsuleError("CAPSULE_EVIDENCE_INVENTORY_TOO_LARGE")
        if path.is_symlink() or not path.is_file():
            continue
        files.append(path.relative_to(root).as_posix())
    return files


def _verify_ref(root: Path, relative: str) -> None:
    if relative.startswith("/") or ".." in Path(relative).parts:
        raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_CANDIDATE_GROUNDING_INVALID")
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root) or path.is_symlink() or not path.is_file():
        raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_CANDIDATE_GROUNDING_INVALID")


def _target_names(ground_truth_entries: list[dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    for path, value in _walk(ground_truth_entries):
        if path and path[-1].casefold() == "path" and isinstance(value, str):
            result.add(Path(value).name.casefold())
    return result


def _concept_from_path(relative: str) -> str | None:
    path = Path(relative)
    tokens = [token.casefold() for token in _TOKEN.findall(path.stem)]
    tokens = [token for token in tokens if not token.isdigit()]
    specific = [token for token in tokens if token not in _GENERIC_STEM_TOKENS]
    selected = specific or tokens
    if not selected:
        return None
    return " ".join(selected[:6])


def _concepts(root: Path, files: list[str], targets: set[str]) -> list[dict[str, Any]]:
    concepts: list[dict[str, Any]] = []
    seen: set[str] = set()
    kinds = (
        ("analysis", _CODE_SUFFIXES),
        ("figure", _IMAGE_SUFFIXES),
        ("result", _OUTPUT_SUFFIXES),
    )
    for kind, suffixes in kinds:
        for relative in files:
            path = Path(relative)
            if path.suffix.casefold() not in suffixes or path.name.casefold() in targets:
                continue
            concept = _concept_from_path(relative)
            if concept is None or concept in seen:
                continue
            seen.add(concept)
            concepts.append(
                {
                    "concept_id": f"concept_{len(concepts) + 1:02d}",
                    "text": concept,
                    "kind": kind,
                    "evidence_refs": [relative],
                    "provenance": "candidate_workspace",
                }
            )
            if len(concepts) >= 8:
                return concepts
    return concepts


def _source_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "session_id": candidate.get("session_id"),
        "core_task_id": candidate.get("core_task_id"),
        "core_attempt_id": candidate.get("core_attempt_id"),
        "artifact_root_sha256": candidate.get("artifact_root_sha256"),
    }


def _successes(concepts: list[dict[str, Any]], report_ref: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for concept in concepts[:3]:
        kind = concept["kind"]
        if kind == "analysis":
            summary = (
                f"Reconstruct the candidate-produced {concept['text']} analysis route "
                "in the fresh workspace."
            )
        elif kind == "figure":
            summary = (
                f"Rebuild the candidate-produced {concept['text']} visual-evidence route "
                "and retain its role in the report."
            )
        else:
            summary = (
                f"Reproduce the candidate-produced {concept['text']} result summary "
                "as a checkable intermediate output."
            )
        result.append(
            {
                "kind": kind,
                "concept_id": concept["concept_id"],
                "summary": summary,
                "evidence_refs": [*concept["evidence_refs"], report_ref],
                "why_retain": (
                    "This is candidate-produced evidence that provides a concrete "
                    "starting analysis path for a fresh rerun."
                ),
            }
        )
    return result


def _weaknesses_and_actions(
    sanitized_feedback: Mapping[str, Any],
    concepts: list[dict[str, Any]],
    report_ref: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    diagnoses = sanitized_feedback.get("diagnoses")
    if not isinstance(diagnoses, list) or not diagnoses:
        raise BaselineEvidenceCapsuleError("CAPSULE_SANITIZED_FEEDBACK_INVALID")
    primary = concepts[0] if concepts else None
    primary_text = "the candidate-produced analysis route"
    primary_refs = [report_ref]
    if primary is not None:
        primary_text = f"the {primary['text']} route"
        primary_refs = [*primary["evidence_refs"], report_ref]
    weaknesses: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    for index, diagnosis in enumerate(diagnoses[:3], start=1):
        if not isinstance(diagnosis, dict):
            raise BaselineEvidenceCapsuleError("CAPSULE_SANITIZED_FEEDBACK_INVALID")
        dimension = diagnosis.get("dimension")
        observation = diagnosis.get("candidate_observation")
        refs = diagnosis.get("evidence_refs")
        if (
            not isinstance(dimension, str)
            or not isinstance(observation, str)
            or not isinstance(refs, list)
            or not all(isinstance(item, str) for item in refs)
        ):
            raise BaselineEvidenceCapsuleError("CAPSULE_SANITIZED_FEEDBACK_INVALID")
        weakness_id = f"weakness_{index:02d}"
        weaknesses.append(
            {
                "weakness_id": weakness_id,
                "dimension": dimension,
                "candidate_observation": observation,
                "evidence_refs": list(dict.fromkeys([*refs, *primary_refs])),
            }
        )
        if dimension == "visual_evidence":
            action = (
                f"Reconstruct {primary_text} in the fresh workspace, then add an "
                "independent numerical summary that checks the visual evidence with public data."
            )
        elif dimension == "quantitative_validation":
            action = (
                f"Reconstruct {primary_text} first, then add an independent aggregation "
                "or cross-check that tests the same conclusion."
            )
        else:
            action = (
                f"Reconstruct {primary_text} before adding one documented validation "
                f"step that addresses the observed {dimension.replace('_', ' ')} weakness."
            )
        actions.append(
            {
                "action_id": f"action_{index:02d}",
                "addresses": weakness_id,
                "action": action,
                "preserve_refs": primary_refs,
            }
        )
    return weaknesses, actions


def build_baseline_evidence_capsule(
    *,
    task_id: str,
    candidate: Mapping[str, Any],
    sanitized_feedback: Mapping[str, Any],
    ground_truth_entries: list[dict[str, Any]],
) -> BaselineEvidenceCapsule:
    """Build a bounded candidate-only capsule without new model inference."""

    root_value = candidate.get("candidate_output_root")
    if not isinstance(root_value, str):
        raise BaselineEvidenceCapsuleError("CAPSULE_CANDIDATE_OUTPUT_AUTHORITY_ABSENT")
    root = Path(root_value).resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise BaselineEvidenceCapsuleError("CAPSULE_CANDIDATE_OUTPUT_AUTHORITY_UNSAFE")
    files = _candidate_files(root)
    report_ref = "report/report.md"
    if report_ref not in files:
        raise BaselineEvidenceCapsuleError("CAPSULE_REPORT_EVIDENCE_ABSENT")
    concepts = _concepts(root, files, _target_names(ground_truth_entries))
    if not concepts:
        raise BaselineEvidenceCapsuleError("CAPSULE_NO_CANDIDATE_SPECIFIC_CONCEPTS")
    successes = _successes(concepts, report_ref)
    weaknesses, actions = _weaknesses_and_actions(sanitized_feedback, concepts, report_ref)
    payload = {
        "schema_version": CAPSULE_SCHEMA,
        "task_id": task_id,
        "source_candidate": _source_candidate(candidate),
        "fresh_workspace_requirement": (
            "The next Candidate starts without baseline files and must reconstruct "
            "candidate-proven strategies before adding improvements."
        ),
        "candidate_concepts": concepts,
        "successful_work": successes,
        "observed_weaknesses": weaknesses,
        "improvement_actions": actions,
        "projector_model_calls": 0,
    }
    return BaselineEvidenceCapsule(payload)


def _require_schema(capsule: Mapping[str, Any]) -> None:
    expected = {
        "schema_version",
        "task_id",
        "source_candidate",
        "fresh_workspace_requirement",
        "candidate_concepts",
        "successful_work",
        "observed_weaknesses",
        "improvement_actions",
        "projector_model_calls",
    }
    if (
        set(capsule) != expected
        or capsule.get("schema_version") != CAPSULE_SCHEMA
        or not isinstance(capsule.get("task_id"), str)
        or not isinstance(capsule.get("source_candidate"), dict)
        or not isinstance(capsule.get("fresh_workspace_requirement"), str)
        or not isinstance(capsule.get("candidate_concepts"), list)
        or not 1 <= len(capsule["candidate_concepts"]) <= 8
        or not isinstance(capsule.get("successful_work"), list)
        or not capsule["successful_work"]
        or not isinstance(capsule.get("observed_weaknesses"), list)
        or not capsule["observed_weaknesses"]
        or not isinstance(capsule.get("improvement_actions"), list)
        or not capsule["improvement_actions"]
        or capsule.get("projector_model_calls") != 0
    ):
        raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_SCHEMA_INVALID")


def _hidden_literals(ground_truth_entries: list[dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    for _path, value in _walk(ground_truth_entries):
        if isinstance(value, str):
            normalized = _normalize(value)
            if len(normalized.split()) >= 3 or len(normalized) >= 12:
                result.add(normalized)
    return result


def admit_baseline_evidence_capsule(
    capsule: Mapping[str, Any],
    *,
    candidate_root: str | Path,
    ground_truth_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Fail closed unless every task-specific fact has Candidate provenance."""

    _require_schema(capsule)
    root = Path(candidate_root).resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_CANDIDATE_GROUNDING_INVALID")
    for path, _value in _walk(capsule):
        if path and any(fragment in path[-1].casefold() for fragment in _PRIVATE_KEY_FRAGMENTS):
            raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_PRIVATE_FIELD_VIOLATION")

    concept_ids: set[str] = set()
    concept_text: set[str] = set()
    for concept in capsule["candidate_concepts"]:
        if (
            not isinstance(concept, dict)
            or set(concept) != {"concept_id", "text", "kind", "evidence_refs", "provenance"}
            or not isinstance(concept.get("concept_id"), str)
            or not isinstance(concept.get("text"), str)
            or concept.get("kind") not in {"analysis", "figure", "result"}
            or concept.get("provenance") not in {"candidate_workspace", "public_task"}
            or not isinstance(concept.get("evidence_refs"), list)
            or not concept["evidence_refs"]
        ):
            raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_SCHEMA_INVALID")
        if concept["concept_id"] in concept_ids or not _normalize(concept["text"]):
            raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_SCHEMA_INVALID")
        concept_ids.add(concept["concept_id"])
        concept_text.add(_normalize(concept["text"]))
        for ref in concept["evidence_refs"]:
            if not isinstance(ref, str):
                raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_SCHEMA_INVALID")
            _verify_ref(root, ref)
            path_tokens = set(_TOKEN.findall(Path(ref).stem.casefold()))
            text_tokens = set(_TOKEN.findall(concept["text"].casefold()))
            if not text_tokens.intersection(path_tokens):
                raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_PROVENANCE_VIOLATION")

    strengths = capsule["successful_work"]
    for item in strengths:
        if (
            not isinstance(item, dict)
            or set(item) != {"kind", "concept_id", "summary", "evidence_refs", "why_retain"}
            or item.get("concept_id") not in concept_ids
            or not isinstance(item.get("summary"), str)
            or not isinstance(item.get("why_retain"), str)
            or not isinstance(item.get("evidence_refs"), list)
        ):
            raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_SCHEMA_INVALID")
        for ref in item["evidence_refs"]:
            if not isinstance(ref, str):
                raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_SCHEMA_INVALID")
            _verify_ref(root, ref)

    weakness_ids: set[str] = set()
    for item in capsule["observed_weaknesses"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"weakness_id", "dimension", "candidate_observation", "evidence_refs"}
            or not isinstance(item.get("weakness_id"), str)
            or not isinstance(item.get("dimension"), str)
            or not isinstance(item.get("candidate_observation"), str)
            or not isinstance(item.get("evidence_refs"), list)
            or item["weakness_id"] in weakness_ids
        ):
            raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_SCHEMA_INVALID")
        weakness_ids.add(item["weakness_id"])
        for ref in item["evidence_refs"]:
            if not isinstance(ref, str):
                raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_SCHEMA_INVALID")
            _verify_ref(root, ref)

    mapped_weaknesses: set[str] = set()
    for item in capsule["improvement_actions"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"action_id", "addresses", "action", "preserve_refs"}
            or not isinstance(item.get("action_id"), str)
            or item.get("addresses") not in weakness_ids
            or not isinstance(item.get("action"), str)
            or "reconstruct" not in item["action"].casefold()
            or not isinstance(item.get("preserve_refs"), list)
        ):
            raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_FRESH_WORKSPACE_SEMANTICS_INVALID")
        mapped_weaknesses.add(item["addresses"])
        for ref in item["preserve_refs"]:
            if not isinstance(ref, str):
                raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_SCHEMA_INVALID")
            _verify_ref(root, ref)
    if not mapped_weaknesses:
        raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_WEAKNESS_ACTION_MISSING")

    serialized = "\n".join(value for _path, value in _walk(capsule) if isinstance(value, str))
    normalized = _normalize(serialized)
    for hidden in _hidden_literals(ground_truth_entries):
        if hidden and hidden in normalized:
            raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_GT_LITERAL_VIOLATION")
    body = {
        "schema_version": CAPSULE_ADMISSION_SCHEMA,
        "status": "ADMITTED",
        "capsule_sha256": canonical_sha256(dict(capsule)),
        "candidate_grounded": True,
        "raw_gt_projected": False,
        "judge_reasoning_projected": False,
        "target_image_projected": False,
        "candidate_concept_count": len(concept_ids),
        "successful_work_count": len(strengths),
        "observed_weakness_count": len(weakness_ids),
        "weakness_to_action_mapping_count": len(mapped_weaknesses),
        "projector_model_calls": 0,
        "exact_gt_literal_scan": "PASS",
        "candidate_provenance_scan": "PASS",
        "fresh_workspace_semantics": "PASS",
    }
    return {**body, "content_sha256": canonical_sha256(body)}


__all__ = [
    "CAPSULE_ADMISSION_SCHEMA",
    "CAPSULE_SCHEMA",
    "REFLECTOR_CAPSULE_SCHEMA",
    "BaselineEvidenceCapsule",
    "BaselineEvidenceCapsuleAdmissionError",
    "BaselineEvidenceCapsuleError",
    "admit_baseline_evidence_capsule",
    "build_baseline_evidence_capsule",
    "build_reflector_capsule_view",
]
