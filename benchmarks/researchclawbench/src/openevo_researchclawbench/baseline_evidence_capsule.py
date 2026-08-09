"""Candidate-grounded preservation input for one R3 per-item evolution cycle.

This module deliberately projects a bounded success trace and achievement
ledger.  It never transfers a baseline workspace or evaluator-private data to
the fresh evolved Candidate.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .baseline_success_trace import (
    ACHIEVEMENT_LEDGER_SCHEMA,
    SUCCESS_TRACE_SCHEMA,
    BaselineSuccessTraceError,
    build_baseline_achievement_ledger,
    build_baseline_success_trace,
)
from .training_state_store import canonical_sha256

CAPSULE_SCHEMA = "openevo.researchclawbench.baseline_evidence_capsule.v2"
CAPSULE_ADMISSION_SCHEMA = "openevo.researchclawbench.baseline_evidence_capsule_admission.v2"
REFLECTOR_CAPSULE_SCHEMA = "openevo.researchclawbench.baseline_evidence_capsule_reflector_view.v2"

_TOKEN = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_PRIVATE_KEY_FRAGMENTS = (
    "checklist", "criterion", "keywords", "weight", "target_image", "target_figure",
    "judge_reason", "judge_raw", "raw_response", "correct_answer", "expected_value",
    "ground_truth_entries",
)
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".svg"})
_CODE_SUFFIXES = frozenset({".py", ".ipynb", ".r", ".jl"})
_OUTPUT_SUFFIXES = frozenset({".csv", ".tsv", ".json"})
_CANDIDATE_TEXT_SUFFIXES = frozenset(
    {".py", ".ipynb", ".r", ".jl", ".md", ".txt", ".json", ".jsonl", ".csv", ".tsv"}
)
_MAX_CANDIDATE_SOURCE_FILE_BYTES = 1_000_000
_MAX_CANDIDATE_SOURCE_TOTAL_BYTES = 4_000_000
_REPORT_HEADING = re.compile(r"^#{1,3}\s+(.+?)\s*$")
_REPORT_IMAGE = re.compile(r"!\[[^\]]+\]\(([^)]+)\)")
_GENERIC = frozenset({"analysis", "data", "figure", "image", "output", "report", "result", "summary", "validation"})


class BaselineEvidenceCapsuleError(RuntimeError):
    """The candidate-only capsule cannot be safely constructed or admitted."""


class BaselineEvidenceCapsuleAdmissionError(BaselineEvidenceCapsuleError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class BaselineEvidenceCapsule:
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


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
    return {
        Path(value).name.casefold()
        for path, value in _walk(ground_truth_entries)
        if path and path[-1].casefold() == "path" and isinstance(value, str)
    }


def _concepts(root: Path, files: list[str], targets: set[str]) -> list[dict[str, Any]]:
    """Keep small provenance labels for audit; the ledger is the primary input."""

    report = root / "report/report.md"
    report_text = report.read_text(encoding="utf-8", errors="replace")[:131_072]
    concepts: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(text: str, kind: str, refs: list[str]) -> None:
        tokens = [item.casefold() for item in _TOKEN.findall(text)]
        specific = [item for item in tokens if item not in _GENERIC and not item.isdigit()]
        if len(specific) < 2:
            return
        label = " ".join(specific[:6])
        if label in seen:
            return
        seen.add(label)
        concepts.append({
            "concept_id": f"concept_{len(concepts)+1:02d}",
            "text": label,
            "kind": kind,
            "evidence_refs": refs,
            "provenance": "candidate_workspace",
        })

    for line in report_text.splitlines():
        match = _REPORT_HEADING.match(line)
        if match:
            add(match.group(1), "analysis", ["report/report.md"])
    file_set = set(files)
    for match in _REPORT_IMAGE.finditer(report_text):
        ref = (Path("report") / match.group(1)).as_posix()
        if ref in file_set:
            add(Path(ref).stem.replace("_", " "), "figure", [ref, "report/report.md"])
    for kind, suffixes in (("analysis", _CODE_SUFFIXES), ("figure", _IMAGE_SUFFIXES), ("result", _OUTPUT_SUFFIXES)):
        for ref in files:
            path = Path(ref)
            if path.suffix.casefold() not in suffixes or path.name.casefold() in targets:
                continue
            add(path.stem.replace("_", " "), kind, [ref])
            if len(concepts) >= 8:
                return concepts
    return concepts[:8]


def _source_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "session_id": candidate.get("session_id"),
        "core_task_id": candidate.get("core_task_id"),
        "core_attempt_id": candidate.get("core_attempt_id"),
        "artifact_root_sha256": candidate.get("artifact_root_sha256"),
    }


def _weaknesses_and_actions(
    sanitized_feedback: Mapping[str, Any], ledger: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    diagnoses = sanitized_feedback.get("diagnoses")
    achievements = ledger.get("achievements")
    if not isinstance(diagnoses, list) or not diagnoses or not isinstance(achievements, list) or not achievements:
        raise BaselineEvidenceCapsuleError("CAPSULE_SANITIZED_FEEDBACK_INVALID")
    weaknesses: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    for index, diagnosis in enumerate(diagnoses[:6], start=1):
        if not isinstance(diagnosis, dict):
            raise BaselineEvidenceCapsuleError("CAPSULE_SANITIZED_FEEDBACK_INVALID")
        values = {field: diagnosis.get(field) for field in ("dimension", "severity", "candidate_observation", "improvement_direction", "evidence_refs")}
        if (
            not all(isinstance(values[field], str) and _normalize(values[field]) for field in ("dimension", "severity", "candidate_observation", "improvement_direction"))
            or not isinstance(values["evidence_refs"], list)
            or not values["evidence_refs"]
            or not all(isinstance(ref, str) for ref in values["evidence_refs"])
        ):
            raise BaselineEvidenceCapsuleError("CAPSULE_SANITIZED_FEEDBACK_INVALID")
        weakness_id = f"weakness_{index:02d}"
        achievement = achievements[(index - 1) % len(achievements)]
        weaknesses.append({"weakness_id": weakness_id, **values})
        actions.append({
            "action_id": f"action_{index:02d}",
            "addresses": weakness_id,
            "achievement_id": achievement["achievement_id"],
            "action": (
                f"After reconstructing required {achievement['achievement_id']} ({achievement['capability']}), "
                f"add an independent public-data improvement that addresses: {values['improvement_direction']}"
            ),
            "feedback_action_mode": "additive",
            "preserve_refs": achievement["candidate_evidence_refs"],
        })
    return weaknesses, actions


def build_baseline_evidence_capsule(
    *, task_id: str, candidate: Mapping[str, Any], sanitized_feedback: Mapping[str, Any], ground_truth_entries: list[dict[str, Any]]
) -> BaselineEvidenceCapsule:
    root_value = candidate.get("candidate_output_root")
    if not isinstance(root_value, str):
        raise BaselineEvidenceCapsuleError("CAPSULE_CANDIDATE_OUTPUT_AUTHORITY_ABSENT")
    root = Path(root_value).resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise BaselineEvidenceCapsuleError("CAPSULE_CANDIDATE_OUTPUT_AUTHORITY_UNSAFE")
    files = _candidate_files(root)
    if "report/report.md" not in files:
        raise BaselineEvidenceCapsuleError("CAPSULE_REPORT_EVIDENCE_ABSENT")
    try:
        trace = build_baseline_success_trace(candidate_root=root)
        ledger = build_baseline_achievement_ledger(trace=trace, candidate_root=root)
    except BaselineSuccessTraceError as exc:
        raise BaselineEvidenceCapsuleError(str(exc)) from exc
    concepts = _concepts(root, files, _target_names(ground_truth_entries))
    if not concepts:
        # The ledger is detailed enough to construct bounded audit concepts
        # even when the report headings themselves are generic.
        concepts = [{
            "concept_id": f"concept_{index+1:02d}",
            "text": " ".join(item["method_signature"][:6]),
            "kind": "analysis",
            "evidence_refs": item["candidate_evidence_refs"][:1],
            "provenance": "candidate_workspace",
        } for index, item in enumerate(ledger["achievements"][:8])]
    successes = [{
        "achievement_id": item["achievement_id"],
        "summary": item["capability"],
        "evidence_refs": item["candidate_evidence_refs"],
        "why_retain": "Required candidate-produced capability and evidence chain for fresh-workspace reconstruction.",
    } for item in ledger["achievements"]]
    weaknesses, actions = _weaknesses_and_actions(sanitized_feedback, ledger)
    return BaselineEvidenceCapsule({
        "schema_version": CAPSULE_SCHEMA,
        "task_id": task_id,
        "source_candidate": _source_candidate(candidate),
        "fresh_workspace_requirement": "The next Candidate starts without baseline files: RECONSTRUCT required capabilities, PRESERVE them, EXTEND only additively, then VERIFY baseline equivalence.",
        "candidate_concepts": concepts,
        "successful_work": successes,
        "observed_weaknesses": weaknesses,
        "improvement_actions": actions,
        "baseline_success_trace": trace,
        "baseline_achievement_ledger": ledger,
        "preservation_contract": "RECONSTRUCT_PRESERVE_EXTEND_VERIFY",
        "projector_model_calls": 0,
    })


def _require_schema(capsule: Mapping[str, Any]) -> None:
    expected = {
        "schema_version", "task_id", "source_candidate", "fresh_workspace_requirement", "candidate_concepts", "successful_work", "observed_weaknesses", "improvement_actions", "baseline_success_trace", "baseline_achievement_ledger", "preservation_contract", "projector_model_calls",
    }
    if (
        set(capsule) != expected
        or capsule.get("schema_version") != CAPSULE_SCHEMA
        or not isinstance(capsule.get("task_id"), str)
        or not isinstance(capsule.get("source_candidate"), dict)
        or not isinstance(capsule.get("fresh_workspace_requirement"), str)
        or not isinstance(capsule.get("candidate_concepts"), list) or not capsule["candidate_concepts"]
        or not isinstance(capsule.get("successful_work"), list) or not capsule["successful_work"]
        or not isinstance(capsule.get("observed_weaknesses"), list) or not capsule["observed_weaknesses"]
        or not isinstance(capsule.get("improvement_actions"), list) or not capsule["improvement_actions"]
        or not isinstance(capsule.get("baseline_success_trace"), dict)
        or capsule["baseline_success_trace"].get("schema_version") != SUCCESS_TRACE_SCHEMA
        or not isinstance(capsule.get("baseline_achievement_ledger"), dict)
        or capsule["baseline_achievement_ledger"].get("schema_version") != ACHIEVEMENT_LEDGER_SCHEMA
        or capsule.get("preservation_contract") != "RECONSTRUCT_PRESERVE_EXTEND_VERIFY"
        or capsule.get("projector_model_calls") != 0
    ):
        raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_SCHEMA_INVALID")


def _hidden_literals(ground_truth_entries: list[dict[str, Any]]) -> set[str]:
    return {
        _normalize(value)
        for _path, value in _walk(ground_truth_entries)
        if isinstance(value, str) and (len(_normalize(value).split()) >= 3 or len(_normalize(value)) >= 12)
    }


def _candidate_source_corpus(root: Path) -> str:
    """Read the bounded Candidate-owned text that can contribute to a capsule."""

    chunks: list[str] = []
    consumed = 0
    for relative in _candidate_files(root):
        path = root / relative
        if path.suffix.casefold() not in _CANDIDATE_TEXT_SUFFIXES:
            continue
        metadata = path.stat(follow_symlinks=False)
        if metadata.st_size <= 0 or metadata.st_size > _MAX_CANDIDATE_SOURCE_FILE_BYTES:
            continue
        if consumed + metadata.st_size > _MAX_CANDIDATE_SOURCE_TOTAL_BYTES:
            break
        chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        consumed += metadata.st_size
    return _normalize("\n".join(chunks))


def _validate_trace_and_ledger(capsule: Mapping[str, Any], root: Path) -> tuple[set[str], set[str]]:
    trace = capsule["baseline_success_trace"]
    events = trace.get("events")
    if not isinstance(events, list) or not events or trace.get("event_count") != len(events) or trace.get("projector_model_calls") != 0:
        raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_SUCCESS_TRACE_INVALID")
    trace_ids: set[str] = set()
    event_fields = {"trace_id", "decision_context", "action", "public_input_refs", "candidate_artifact_refs", "result_summary", "verification", "status"}
    for event in events:
        if (
            not isinstance(event, dict) or set(event) != event_fields
            or not isinstance(event.get("trace_id"), str) or event["trace_id"] in trace_ids
            or event.get("status") != "successful"
            or not all(isinstance(event.get(key), str) and _normalize(event[key]) for key in ("decision_context", "action", "result_summary", "verification"))
            or not isinstance(event.get("public_input_refs"), list) or not isinstance(event.get("candidate_artifact_refs"), list) or not event["candidate_artifact_refs"]
        ):
            raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_SUCCESS_TRACE_INVALID")
        trace_ids.add(event["trace_id"])
        for ref in [*event["public_input_refs"], *event["candidate_artifact_refs"]]:
            if not isinstance(ref, str):
                raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_SUCCESS_TRACE_INVALID")
            _verify_ref(root, ref)
    ledger = capsule["baseline_achievement_ledger"]
    achievements = ledger.get("achievements")
    required_output_counts = ledger.get("required_output_class_counts")
    if (
        not isinstance(achievements, list) or not achievements or ledger.get("projector_model_calls") != 0
        or ledger.get("achievement_count") != len(achievements) or ledger.get("required_achievement_count") != len(achievements)
        or not isinstance(required_output_counts, dict)
        or set(required_output_counts) != {"script", "numeric_output", "figure", "report_section"}
        or any(type(value) is not int or value < 0 for value in required_output_counts.values())
    ):
        raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_ACHIEVEMENT_LEDGER_INVALID")
    achievement_ids: set[str] = set()
    fields = {"achievement_id", "capability", "scientific_role", "decision_rationale", "public_inputs", "trajectory_refs", "reconstruction_steps", "method_signature", "evidence_role_signature", "evidence_output_class", "required_output_classes", "candidate_evidence_refs", "verification_assertions", "candidate_observations", "preservation_priority"}
    output_classes = {"script", "numeric_output", "figure", "report_section"}
    for item in achievements:
        required_lists = ("public_inputs", "trajectory_refs", "reconstruction_steps", "method_signature", "evidence_role_signature", "required_output_classes", "candidate_evidence_refs", "verification_assertions")
        if (
            not isinstance(item, dict) or set(item) != fields or not isinstance(item.get("achievement_id"), str)
            or item["achievement_id"] in achievement_ids or item.get("preservation_priority") != "required"
            or not all(isinstance(item.get(key), str) and _normalize(item[key]) for key in ("capability", "scientific_role", "decision_rationale"))
            or not all(isinstance(item.get(key), list) and item[key] for key in required_lists)
            or not set(item["trajectory_refs"]).issubset(trace_ids)
            or not set(item["required_output_classes"]).issubset(output_classes)
            or item.get("evidence_output_class") not in output_classes
        ):
            raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_ACHIEVEMENT_LEDGER_INVALID")
        observations = item.get("candidate_observations")
        if not isinstance(observations, list) or not observations or any(not isinstance(observation, dict) or observation.get("provenance") != "candidate_observation" or not isinstance(observation.get("summary"), str) for observation in observations):
            raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_ACHIEVEMENT_LEDGER_INVALID")
        achievement_ids.add(item["achievement_id"])
        for ref in [*item["public_inputs"], *item["candidate_evidence_refs"]]:
            if not isinstance(ref, str):
                raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_ACHIEVEMENT_LEDGER_INVALID")
            _verify_ref(root, ref)
    observed_output_counts = {
        "script": len(
            {
                ref
                for item in achievements
                for ref in item["candidate_evidence_refs"]
                if Path(ref).suffix.casefold() in {".py", ".ipynb", ".r", ".jl"}
            }
        ),
        "numeric_output": sum(
            item["evidence_output_class"] == "numeric_output"
            for item in achievements
        ),
        "figure": sum(
            item["evidence_output_class"] == "figure" for item in achievements
        ),
        "report_section": 1,
    }
    if required_output_counts != observed_output_counts:
        raise BaselineEvidenceCapsuleAdmissionError(
            "CAPSULE_ACHIEVEMENT_LEDGER_INVALID"
        )
    return trace_ids, achievement_ids


def admit_baseline_evidence_capsule(capsule: Mapping[str, Any], *, candidate_root: str | Path, ground_truth_entries: list[dict[str, Any]]) -> dict[str, Any]:
    _require_schema(capsule)
    root = Path(candidate_root).resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_CANDIDATE_GROUNDING_INVALID")
    for path, _value in _walk(capsule):
        if path and any(fragment in path[-1].casefold() for fragment in _PRIVATE_KEY_FRAGMENTS):
            raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_PRIVATE_FIELD_VIOLATION")
    trace_ids, achievement_ids = _validate_trace_and_ledger(capsule, root)
    concept_ids: set[str] = set()
    for concept in capsule["candidate_concepts"]:
        if (
            not isinstance(concept, dict) or set(concept) != {"concept_id", "text", "kind", "evidence_refs", "provenance"}
            or not isinstance(concept.get("concept_id"), str) or concept["concept_id"] in concept_ids
            or not isinstance(concept.get("text"), str) or not _normalize(concept["text"])
            or concept.get("kind") not in {"analysis", "figure", "result"}
            or concept.get("provenance") not in {"candidate_workspace", "public_task"}
            or not isinstance(concept.get("evidence_refs"), list) or not concept["evidence_refs"]
        ):
            raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_SCHEMA_INVALID")
        concept_ids.add(concept["concept_id"])
        for ref in concept["evidence_refs"]:
            if not isinstance(ref, str):
                raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_SCHEMA_INVALID")
            _verify_ref(root, ref)
    for item in capsule["successful_work"]:
        if not isinstance(item, dict) or set(item) != {"achievement_id", "summary", "evidence_refs", "why_retain"} or item.get("achievement_id") not in achievement_ids or not isinstance(item.get("summary"), str) or not isinstance(item.get("why_retain"), str) or not isinstance(item.get("evidence_refs"), list):
            raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_SCHEMA_INVALID")
        for ref in item["evidence_refs"]:
            if not isinstance(ref, str):
                raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_SCHEMA_INVALID")
            _verify_ref(root, ref)
    weakness_ids: set[str] = set()
    for item in capsule["observed_weaknesses"]:
        expected = {"weakness_id", "dimension", "severity", "candidate_observation", "improvement_direction", "evidence_refs"}
        if not isinstance(item, dict) or set(item) != expected or not isinstance(item.get("weakness_id"), str) or item["weakness_id"] in weakness_ids or not all(isinstance(item.get(key), str) and _normalize(item[key]) for key in ("dimension", "severity", "candidate_observation", "improvement_direction")) or not isinstance(item.get("evidence_refs"), list) or not item["evidence_refs"]:
            raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_SCHEMA_INVALID")
        weakness_ids.add(item["weakness_id"])
        for ref in item["evidence_refs"]:
            if not isinstance(ref, str):
                raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_SCHEMA_INVALID")
            _verify_ref(root, ref)
    mapped: set[str] = set()
    for item in capsule["improvement_actions"]:
        expected = {"action_id", "addresses", "achievement_id", "action", "feedback_action_mode", "preserve_refs"}
        if not isinstance(item, dict) or set(item) != expected or not isinstance(item.get("action_id"), str) or item.get("addresses") not in weakness_ids or item.get("achievement_id") not in achievement_ids or not isinstance(item.get("action"), str) or "reconstruct" not in item["action"].casefold() or item.get("feedback_action_mode") != "additive" or not isinstance(item.get("preserve_refs"), list):
            raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_FRESH_WORKSPACE_SEMANTICS_INVALID")
        mapped.add(item["addresses"])
        for ref in item["preserve_refs"]:
            if not isinstance(ref, str):
                raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_SCHEMA_INVALID")
            _verify_ref(root, ref)
    if mapped != weakness_ids:
        raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_WEAKNESS_ACTION_MISSING")
    serialized = _normalize("\n".join(value for _path, value in _walk(capsule) if isinstance(value, str)))
    hidden_literals = _hidden_literals(ground_truth_entries)
    candidate_source = _candidate_source_corpus(root)
    candidate_owned_literals = {
        hidden for hidden in hidden_literals if hidden and hidden in candidate_source
    }
    if any(
        hidden
        and hidden in serialized
        and hidden not in candidate_owned_literals
        for hidden in hidden_literals
    ):
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
        "success_trace_event_count": len(trace_ids),
        "required_achievement_count": len(achievement_ids),
        "successful_work_count": len(capsule["successful_work"]),
        "observed_weakness_count": len(weakness_ids),
        "weakness_to_action_mapping_count": len(mapped),
        "projector_model_calls": 0,
        "exact_gt_literal_scan": "PASS",
        "candidate_provenance_scan": "PASS",
        "fresh_workspace_semantics": "PASS",
        "preservation_first_semantics": "PASS",
    }
    return {**body, "content_sha256": canonical_sha256(body)}


def build_reflector_capsule_view(capsule: Mapping[str, Any]) -> dict[str, Any]:
    _require_schema(capsule)
    ledger = capsule["baseline_achievement_ledger"]
    actions = {item["addresses"]: item for item in capsule["improvement_actions"]}
    mappings: list[dict[str, Any]] = []
    for weakness in capsule["observed_weaknesses"]:
        action = actions.get(weakness["weakness_id"])
        if not isinstance(action, dict):
            raise BaselineEvidenceCapsuleAdmissionError("CAPSULE_REFLECTOR_VIEW_INVALID")
        mappings.append({
            "weakness_id": weakness["weakness_id"],
            "dimension": weakness["dimension"],
            "severity": weakness["severity"],
            "candidate_observation": weakness["candidate_observation"],
            "improvement_direction": weakness["improvement_direction"],
            "evidence_refs": weakness["evidence_refs"][:3],
            "achievement_id": action["achievement_id"],
            "next_run_action": action["action"],
            "feedback_action_mode": "additive",
        })
    trace_events = capsule["baseline_success_trace"]["events"]

    # Preserve a bounded *high-information* trace in the reflector view.  The
    # first raw transcript entries can be navigation or inspection, while the
    # candidate-created script/report/output chain is the evidence an evolved
    # Candidate must be able to reconstruct.  Keep the original trace IDs and
    # use only candidate-owned path classes for this stable ranking.
    def trace_rank(event: Mapping[str, Any]) -> tuple[int, int, str]:
        refs = [
            ref for ref in event.get("candidate_artifact_refs", []) if isinstance(ref, str)
        ]
        has_script = any(ref.endswith((".py", ".ipynb", ".r", ".jl")) for ref in refs)
        output_count = sum(ref.endswith((".csv", ".tsv", ".json", ".png", ".jpg", ".jpeg", ".svg")) for ref in refs)
        route = str(event.get("action", "")).startswith(
            "Reconstruct and execute candidate-created"
        )
        return (0 if route else (1 if has_script else 2), -output_count, str(event.get("trace_id", "")))

    selected_trace = sorted(trace_events, key=trace_rank)[:12]
    return {
        "schema_version": REFLECTOR_CAPSULE_SCHEMA,
        "capsule_sha256": canonical_sha256(dict(capsule)),
        "fresh_workspace_requirement": capsule["fresh_workspace_requirement"],
        "preservation_contract": "RECONSTRUCT_PRESERVE_EXTEND_VERIFY",
        "baseline_success_trace": {**capsule["baseline_success_trace"], "events": selected_trace},
        "baseline_achievement_ledger": {
            **ledger,
            "achievements": ledger["achievements"][:12],
        },
        "weakness_to_action": mappings,
        "artifact_role_contract": {
            "text_memory": "WHAT MUST NOT BE LOST: cover every required achievement with capability, evidence role, and preservation instruction.",
            "skill_bundle": "HOW TO RECONSTRUCT AND EXTEND: Phase 1 reconstruct, Phase 2 verify, Phase 3 add each additive action, Phase 4 integrate without deletion.",
            "agent_system": "WHAT MUST BE TRUE BEFORE SUBMISSION: RECONSTRUCT, PRESERVE, EXTEND, VERIFY; baseline-equivalence is required before submission.",
        },
    }


__all__ = [
    "CAPSULE_ADMISSION_SCHEMA", "CAPSULE_SCHEMA", "REFLECTOR_CAPSULE_SCHEMA",
    "BaselineEvidenceCapsule", "BaselineEvidenceCapsuleAdmissionError", "BaselineEvidenceCapsuleError",
    "admit_baseline_evidence_capsule", "build_baseline_evidence_capsule", "build_reflector_capsule_view",
]
