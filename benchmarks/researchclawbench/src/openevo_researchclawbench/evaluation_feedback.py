"""Sanitized evaluator-to-evolution feedback for the per-item benchmark.

The evaluator owns hidden checklist data and raw Judge output.  This module is
the only benchmark-side projection boundary allowed to read that private
authority.  It emits a closed, candidate-grounded learning signal; neither the
raw checklist nor Judge reasoning is returned or persisted in the projection.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol

from .baseline_evidence_capsule import (
    admit_baseline_evidence_capsule,
    build_baseline_evidence_capsule,
    build_reflector_capsule_view,
)
from .run_manifest import atomic_write_json
from .training_state_store import canonical_bytes, canonical_sha256

FEEDBACK_CLASS = "sanitized_evaluation_feedback_v1"
FEEDBACK_SCHEMA = "openevo.researchclawbench.sanitized_evaluation_feedback.v1"
ADMISSION_SCHEMA = "openevo.researchclawbench.feedback_admission.v1"
RETENTION_FEEDBACK_CLASS = "preservation_first_evolution_r3"
RETENTION_FEEDBACK_SCHEMA = "openevo.researchclawbench.preservation_first_evolution.r3"

ALLOWED_DIMENSIONS = frozenset(
    {
        "coverage",
        "quantitative_validation",
        "visual_evidence",
        "scientific_consistency",
        "methodology",
        "reproducibility",
        "report_completeness",
        "delivery_quality",
    }
)
_ALLOWED_SEVERITIES = frozenset({"low", "medium", "high"})
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
_JUDGE_RAW_MARKERS = (
    "judge reasoning",
    "judge raw response",
    "raw judge response",
    "private reasoning",
)
_ANSWER_RECONSTRUCTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bcorrect (?:answer|trend|value|relationship|figure)\b",
        r"\bexpected (?:answer|trend|value|relationship|figure)\b",
        r"\b(?:should|must) (?:increase|decrease|peak|decline|rise|fall)\b",
        r"\bcriterion\s*\d+\b",
        r"\bchecklist (?:requires|says|expects)\b",
    )
)
_TOKEN = re.compile(r"[a-z0-9]+(?:[_.-][a-z0-9]+)*", re.IGNORECASE)

# These are adapter-owned method/validation concepts, not task facts.  They
# form the closed vocabulary from which the deterministic projector writes.
_POLICY_LANGUAGE = """
available for evolution sanitized evaluation feedback underperforming competitive
strong candidate observation improvement direction preserve verified strengths
before adding improvements submitted report produced figures outputs code trajectory
complete starting point existing work useful should be retained actual evidence
supporting checks documented remain limited preserve then add independent quantitative
checks using public data connect each conclusion candidate-produced evidence expand
coverage across publicly available inputs verify conclusions more than one analysis
path evidence chain reproducibility methodology report completeness delivery quality
visual scientific consistency focused analysis cross-checks strengthen traceability
between methods results figures maintain valid files incremental evidence-driven
iteration do not merely produce generic research workflow advice use actual trajectory
actual produced report figures code sanitized evaluator diagnoses identify what did well
concrete weaknesses visible own output which validation strategy change next time do not
reconstruct hidden gt high medium low score task source status policy true false
form point improvements relies documents presents
baseline capability forming report-linked accompanying links after cross-check
figure-producing public-data reconstructing replace
keep route public-input comparison extends
independently present
"""
_POLICY_TOKENS = frozenset(token.casefold() for token in _TOKEN.findall(_POLICY_LANGUAGE))
_COMMON_TOKENS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "before",
        "but",
        "by",
        "can",
        "did",
        "do",
        "each",
        "for",
        "from",
        "has",
        "have",
        "in",
        "into",
        "is",
        "it",
        "its",
        "more",
        "not",
        "of",
        "on",
        "only",
        "or",
        "other",
        "own",
        "should",
        "than",
        "that",
        "the",
        "their",
        "then",
        "this",
        "through",
        "to",
        "under",
        "use",
        "using",
        "was",
        "were",
        "what",
        "when",
        "which",
        "while",
        "with",
        "without",
    }
)


class FeedbackProjectionError(RuntimeError):
    """A private evaluation cannot be projected safely."""


class FeedbackAdmissionError(FeedbackProjectionError):
    """A projected feedback object crossed a closed admission boundary."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class PrivateEvaluationReader(Protocol):
    def read_private_evaluation_for_projection(
        self, *, idempotency_key: str, expected_sha256: str
    ) -> dict[str, Any]: ...


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"[a-z0-9]+", normalized))


def _walk(value: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], Any]]:
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise FeedbackAdmissionError("FEEDBACK_SCHEMA_INVALID")
            yield from _walk(child, (*path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, (*path, str(index)))


def _string_leaves(value: Any) -> list[str]:
    return [child for _path, child in _walk(value) if isinstance(child, str)]


def _hidden_literals(gt_entries: list[dict[str, Any]]) -> tuple[set[str], set[str], set[str]]:
    literals: set[str] = set()
    keyword_literals: set[str] = set()
    target_paths: set[str] = set()
    for entry in gt_entries:
        for path, value in _walk(entry):
            key = path[-1].casefold() if path else ""
            if isinstance(value, str):
                normalized = _normalize(value)
                if len(normalized) >= 12 or len(normalized.split()) >= 3:
                    literals.add(normalized)
                if key == "path" and normalized:
                    target_paths.add(normalized)
                    target_paths.add(_normalize(Path(value).name))
            if key == "keywords" and isinstance(value, list):
                for keyword in value:
                    if isinstance(keyword, str) and _normalize(keyword):
                        keyword_literals.add(_normalize(keyword))
    return literals, keyword_literals, target_paths


def _ngrams(value: str, *, size: int = 5) -> set[tuple[str, ...]]:
    tokens = _normalize(value).split()
    if len(tokens) < size:
        return set()
    return {tuple(tokens[index : index + size]) for index in range(len(tokens) - size + 1)}


def _learning_text(feedback: Mapping[str, Any]) -> str:
    selected = {
        "preserve_strengths": feedback.get("preserve_strengths"),
        "diagnoses": feedback.get("diagnoses"),
        "reflection_guidance": feedback.get("reflection_guidance"),
    }
    return "\n".join(_string_leaves(selected))


def _candidate_files(root: Path) -> list[str]:
    files: list[str] = []
    for path in sorted(root.rglob("*")):
        if len(files) >= 4096:
            raise FeedbackProjectionError("candidate evidence inventory is too large")
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        files.append(relative)
    return files


def _bounded_text_corpus(root: Path, paths: Iterable[str]) -> str:
    chunks: list[str] = []
    total = 0
    for relative in paths:
        suffix = Path(relative).suffix.casefold()
        if suffix not in {".md", ".txt", ".py", ".json", ".jsonl", ".csv", ".yaml", ".yml"}:
            continue
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root) or path.is_symlink():
            raise FeedbackProjectionError("candidate evidence path is unsafe")
        data = path.read_bytes()
        remaining = 1_048_576 - total
        if remaining <= 0:
            break
        data = data[:remaining]
        chunks.append(data.decode("utf-8", errors="replace"))
        total += len(data)
    return "\n".join(chunks)


def _require_evidence_refs(root: Path, refs: Any) -> list[str]:
    if not isinstance(refs, list) or not refs or any(not isinstance(item, str) for item in refs):
        raise FeedbackAdmissionError("FEEDBACK_CANDIDATE_GROUNDING_INVALID")
    closed: list[str] = []
    for relative in refs:
        if relative.startswith("/") or ".." in Path(relative).parts:
            raise FeedbackAdmissionError("FEEDBACK_CANDIDATE_GROUNDING_INVALID")
        candidate = (root / relative).resolve(strict=True)
        if not candidate.is_relative_to(root) or candidate.is_symlink() or not candidate.is_file():
            raise FeedbackAdmissionError("FEEDBACK_CANDIDATE_GROUNDING_INVALID")
        closed.append(relative)
    return closed


def _require_closed_schema(feedback: Mapping[str, Any]) -> None:
    expected = {
        "schema_version",
        "status",
        "feedback_class",
        "task_id",
        "source_candidate",
        "evaluation_summary",
        "preserve_before_improve",
        "preserve_strengths",
        "diagnoses",
        "reflection_guidance",
        "policy",
    }
    if set(feedback) != expected:
        raise FeedbackAdmissionError("FEEDBACK_SCHEMA_INVALID")
    if (
        feedback.get("schema_version") != FEEDBACK_SCHEMA
        or feedback.get("status") != "available_for_evolution"
        or feedback.get("feedback_class") != FEEDBACK_CLASS
        or not isinstance(feedback.get("task_id"), str)
        or not isinstance(feedback.get("source_candidate"), dict)
        or not isinstance(feedback.get("evaluation_summary"), dict)
        or feedback.get("preserve_before_improve")
        != "PRESERVE VERIFIED STRENGTHS BEFORE ADDING IMPROVEMENTS"
        or not isinstance(feedback.get("preserve_strengths"), list)
        or not feedback["preserve_strengths"]
        or not isinstance(feedback.get("diagnoses"), list)
        or not feedback["diagnoses"]
        or not isinstance(feedback.get("reflection_guidance"), list)
        or not isinstance(feedback.get("policy"), dict)
    ):
        raise FeedbackAdmissionError("FEEDBACK_SCHEMA_INVALID")
    expected_evaluation = {"overall_outcome", "score"}
    summary = feedback["evaluation_summary"]
    if (
        set(summary) != expected_evaluation
        or summary.get("overall_outcome") not in {"underperforming", "competitive", "strong"}
        or type(summary.get("score")) not in {int, float}
        or not 0 <= float(summary["score"]) <= 100
    ):
        raise FeedbackAdmissionError("FEEDBACK_SCHEMA_INVALID")
    expected_item = {
        "dimension",
        "severity",
        "candidate_grounded",
        "candidate_observation",
        "improvement_direction",
        "evidence_refs",
    }
    for item in [*feedback["preserve_strengths"], *feedback["diagnoses"]]:
        if (
            not isinstance(item, dict)
            or set(item) != expected_item
            or item.get("dimension") not in ALLOWED_DIMENSIONS
            or item.get("severity") not in _ALLOWED_SEVERITIES
            or item.get("candidate_grounded") is not True
            or not isinstance(item.get("candidate_observation"), str)
            or not item["candidate_observation"].strip()
            or not isinstance(item.get("improvement_direction"), str)
            or not item["improvement_direction"].strip()
        ):
            raise FeedbackAdmissionError("FEEDBACK_SCHEMA_INVALID")
    expected_policy = {
        "candidate_grounded",
        "public_provenance_required",
        "answer_reconstruction_allowed",
    }
    policy = feedback["policy"]
    if set(policy) != expected_policy or policy != {
        "candidate_grounded": True,
        "public_provenance_required": True,
        "answer_reconstruction_allowed": False,
    }:
        raise FeedbackAdmissionError("FEEDBACK_SCHEMA_INVALID")


def admit_sanitized_feedback(
    feedback: Mapping[str, Any],
    *,
    candidate_root: str | Path,
    public_corpus: str,
    ground_truth_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Fail closed unless feedback is grounded, provenance-safe, and GT-free."""

    _require_closed_schema(feedback)
    root = Path(candidate_root).resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise FeedbackAdmissionError("FEEDBACK_CANDIDATE_GROUNDING_INVALID")
    for path, _value in _walk(feedback):
        if path:
            if path[0] == "policy":
                continue
            key = path[-1].casefold().replace("-", "_")
            if any(fragment in key for fragment in _PRIVATE_KEY_FRAGMENTS):
                raise FeedbackAdmissionError("FEEDBACK_PRIVATE_FIELD_VIOLATION")
    for item in [*feedback["preserve_strengths"], *feedback["diagnoses"]]:
        _require_evidence_refs(root, item["evidence_refs"])
        direction = item["improvement_direction"]
        observation = item["candidate_observation"]
        if any(
            pattern.search(direction) or pattern.search(observation)
            for pattern in _ANSWER_RECONSTRUCTION_PATTERNS
        ):
            raise FeedbackAdmissionError("FEEDBACK_ANSWER_RECONSTRUCTION_VIOLATION")

    learning_text = _learning_text(feedback)
    normalized_learning = _normalize(learning_text)
    hidden_literals, keyword_literals, target_paths = _hidden_literals(ground_truth_entries)
    raw_hidden_literals = [
        value
        for value in _string_leaves(ground_truth_entries)
        if len(value.strip()) >= 12 or len(value.split()) >= 3
    ]
    for marker in _JUDGE_RAW_MARKERS:
        if _normalize(marker) in normalized_learning:
            raise FeedbackAdmissionError("FEEDBACK_JUDGE_PRIVATE_VIOLATION")
    learning_casefold = learning_text.casefold()
    if any(value.casefold() in learning_casefold for value in raw_hidden_literals):
        raise FeedbackAdmissionError("FEEDBACK_GT_LITERAL_VIOLATION")
    if any(literal and literal in normalized_learning for literal in hidden_literals):
        raise FeedbackAdmissionError("FEEDBACK_GT_LITERAL_VIOLATION")
    hidden_ngrams = {ngram for value in raw_hidden_literals for ngram in _ngrams(value)}
    if hidden_ngrams.intersection(_ngrams(learning_text)):
        raise FeedbackAdmissionError("FEEDBACK_GT_NGRAM_VIOLATION")
    if any(
        keyword and re.search(rf"\b{re.escape(keyword)}\b", normalized_learning)
        for keyword in keyword_literals
    ):
        raise FeedbackAdmissionError("FEEDBACK_CHECKLIST_KEYWORD_VIOLATION")
    if any(target and target in normalized_learning for target in target_paths):
        raise FeedbackAdmissionError("FEEDBACK_TARGET_IMAGE_VIOLATION")

    public_tokens = (
        {token.casefold() for token in _TOKEN.findall(public_corpus)}
        | _POLICY_TOKENS
        | _COMMON_TOKENS
    )
    for field in ("candidate_observation", "improvement_direction"):
        for item in [*feedback["preserve_strengths"], *feedback["diagnoses"]]:
            text = item[field]
            # Numeric claims in free-form learning text are disallowed.  The
            # only admitted score is the typed evaluation_summary.score.
            if any(character.isdigit() for character in text):
                raise FeedbackAdmissionError("FEEDBACK_PROVENANCE_VIOLATION")
            unknown = {
                token.casefold()
                for token in _TOKEN.findall(text)
                if len(token) >= 4 and token.casefold() not in public_tokens
            }
            if unknown:
                raise FeedbackAdmissionError("FEEDBACK_PROVENANCE_VIOLATION")

    refs = sorted(
        {
            ref
            for item in [*feedback["preserve_strengths"], *feedback["diagnoses"]]
            for ref in item["evidence_refs"]
        }
    )
    dimensions = sorted({item["dimension"] for item in feedback["diagnoses"]})
    feedback_sha256 = canonical_sha256(dict(feedback))
    body = {
        "schema_version": ADMISSION_SCHEMA,
        "status": "ADMITTED",
        "feedback_class": FEEDBACK_CLASS,
        "feedback_sha256": feedback_sha256,
        "dimension_tags": dimensions,
        "candidate_evidence_refs": refs,
        "candidate_grounded": True,
        "public_provenance_valid": True,
        "exact_gt_literal_scan": "PASS",
        "normalized_text_scan": "PASS",
        "significant_ngram_scan": "PASS",
        "checklist_keyword_scan": "PASS",
        "criterion_metadata_scan": "PASS",
        "target_image_path_scan": "PASS",
        "judge_raw_marker_scan": "PASS",
        "raw_gt_projected": False,
        "judge_reasoning_projected": False,
        "target_image_projected": False,
        "secret_recorded": False,
    }
    return {**body, "content_sha256": canonical_sha256(body)}


def objective_outcome_signal(*, prediction: str, expected: str) -> dict[str, bool]:
    """Return correctness supervision without ever returning the answer."""

    return {"incorrect": prediction != expected}


def _score_profile(raw_evaluation: Mapping[str, Any]) -> tuple[float, dict[str, float]]:
    score = raw_evaluation.get("total_score")
    if type(score) not in {int, float} or not 0 <= float(score) <= 100:
        raise FeedbackProjectionError("private evaluation total score is invalid")
    groups: dict[str, list[float]] = defaultdict(list)
    items = raw_evaluation.get("items")
    if not isinstance(items, list) or not items:
        raise FeedbackProjectionError("private evaluation item inventory is invalid")
    for item in items:
        if not isinstance(item, dict) or item.get("score_valid") is not True:
            raise FeedbackProjectionError("private evaluation contains an invalid item")
        item_score = item.get("score")
        item_type = item.get("type")
        if type(item_score) not in {int, float} or not isinstance(item_type, str):
            raise FeedbackProjectionError("private evaluation item summary is invalid")
        groups[item_type.casefold()].append(float(item_score))
    return float(score), {
        item_type: sum(values) / len(values) for item_type, values in groups.items()
    }


def _feedback_item(
    dimension: str,
    severity: str,
    observation: str,
    direction: str,
    refs: list[str],
) -> dict[str, Any]:
    return {
        "dimension": dimension,
        "severity": severity,
        "candidate_grounded": True,
        "candidate_observation": observation,
        "improvement_direction": direction,
        "evidence_refs": refs,
    }


def project_sanitized_evaluation_feedback(
    *,
    task_id: str,
    candidate: Mapping[str, Any],
    raw_evaluation: Mapping[str, Any],
    public_task_info: Mapping[str, Any],
    ground_truth_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    """Deterministically project private evaluation into safe learning advice."""

    root_value = candidate.get("candidate_output_root")
    if not isinstance(root_value, str):
        raise FeedbackProjectionError("candidate output authority is absent")
    root = Path(root_value).resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise FeedbackProjectionError("candidate output authority is unsafe")
    files = _candidate_files(root)
    report_ref = "report/report.md"
    if report_ref not in files:
        raise FeedbackProjectionError("candidate report evidence is absent")
    target_names = {
        Path(value).name.casefold()
        for entry in ground_truth_entries
        for path, value in _walk(entry)
        if path and path[-1].casefold() == "path" and isinstance(value, str)
    }
    image_refs = [
        item
        for item in files
        if Path(item).suffix.casefold() in {".png", ".jpg", ".jpeg", ".svg"}
        and Path(item).name.casefold() not in target_names
    ]
    code_refs = [
        item for item in files if Path(item).suffix.casefold() in {".py", ".ipynb", ".r", ".jl"}
    ]
    trajectory_refs = [item for item in files if item == "_agent_output.jsonl"]
    public_corpus = json.dumps(public_task_info, ensure_ascii=False, sort_keys=True)
    public_corpus += "\n" + _bounded_text_corpus(root, files)
    public_corpus += "\n" + "\n".join(files)
    score, type_scores = _score_profile(raw_evaluation)
    outcome = "underperforming" if score < 50 else "competitive" if score < 75 else "strong"
    source_refs = [report_ref, *image_refs[:1], *code_refs[:1], *trajectory_refs[:1]]
    source_refs = list(dict.fromkeys(source_refs))
    image_reference = image_refs[0] if image_refs else report_ref
    code_reference = code_refs[0] if code_refs else (trajectory_refs[0] if trajectory_refs else report_ref)
    preserve = [
        _feedback_item(
            "delivery_quality",
            "low",
            (
                f"The Candidate produced {code_reference} and report-linked evidence "
                f"at {image_reference}, forming a concrete baseline analysis path."
            ),
            "Preserve the baseline capability and evidence chain before adding any new validation.",
            source_refs,
        )
    ]
    diagnoses: list[dict[str, Any]] = []
    image_score = type_scores.get("image")
    if image_score is not None and image_refs:
        diagnoses.append(
            _feedback_item(
                "visual_evidence",
                "high" if image_score < 50 else "medium",
                (
                    f"The Candidate report links {image_reference} as evidence while "
                    f"the accompanying candidate path {code_reference} documents limited "
                    "independent supporting checks."
                ),
                "After reconstructing the existing figure-producing path, add an independent public-data quantitative cross-check; do not replace the baseline path.",
                [report_ref, image_refs[0]],
            )
        )
    if score < 75 or not diagnoses:
        diagnoses.append(
            _feedback_item(
                "quantitative_validation",
                "high" if score < 50 else "medium",
                (
                    f"The Candidate report and {code_reference} present one focused "
                    "analysis route with limited independently documented cross-checks."
                ),
                "Keep the existing route, then add one independent public-input validation or comparison that extends its evidence chain.",
                [report_ref, *(code_refs[:1] or trajectory_refs[:1])],
            )
        )
    feedback = {
        "schema_version": FEEDBACK_SCHEMA,
        "status": "available_for_evolution",
        "feedback_class": FEEDBACK_CLASS,
        "task_id": task_id,
        "source_candidate": {
            "session_id": candidate.get("session_id"),
            "core_task_id": candidate.get("core_task_id"),
            "core_attempt_id": candidate.get("core_attempt_id"),
            "artifact_root_sha256": candidate.get("artifact_root_sha256"),
        },
        "evaluation_summary": {"overall_outcome": outcome, "score": score},
        "preserve_before_improve": ("PRESERVE VERIFIED STRENGTHS BEFORE ADDING IMPROVEMENTS"),
        "preserve_strengths": preserve,
        "diagnoses": diagnoses,
        "reflection_guidance": [
            "Do not merely produce generic research workflow advice.",
            "Use the Candidate's actual trajectory, actual produced report, figures, code, and sanitized evaluator diagnoses.",
            "Identify what the Candidate already did well, concrete weaknesses visible in its own output, and which validation strategy should change next time.",
            "Do not reconstruct hidden GT.",
        ],
        "policy": {
            "candidate_grounded": True,
            "public_provenance_required": True,
            "answer_reconstruction_allowed": False,
        },
    }
    admission = admit_sanitized_feedback(
        feedback,
        candidate_root=root,
        public_corpus=public_corpus,
        ground_truth_entries=ground_truth_entries,
    )
    capsule = build_baseline_evidence_capsule(
        task_id=task_id,
        candidate=candidate,
        sanitized_feedback=feedback,
        ground_truth_entries=ground_truth_entries,
    ).to_dict()
    capsule_admission = admit_baseline_evidence_capsule(
        capsule,
        candidate_root=root,
        ground_truth_entries=ground_truth_entries,
    )
    reflector_sanitized_feedback = {
        "feedback_class": feedback["feedback_class"],
        "evaluation_summary": feedback["evaluation_summary"],
        "preserve_before_improve": feedback["preserve_before_improve"],
        "diagnoses": [
            {
                key: diagnosis[key]
                for key in (
                    "dimension",
                    "severity",
                    "candidate_observation",
                    "improvement_direction",
                    "evidence_refs",
                )
            }
            for diagnosis in feedback["diagnoses"]
        ],
    }
    reflector_capsule = build_reflector_capsule_view(capsule)
    actionable_candidate_evolution = {
        "preservation_contract": "RECONSTRUCT_PRESERVE_EXTEND_VERIFY",
        "required_achievement_ids": [
            item["achievement_id"]
            for item in reflector_capsule["baseline_achievement_ledger"]["achievements"]
        ],
        "all_feedback_actions": reflector_capsule["weakness_to_action"],
        "fresh_workspace": reflector_capsule["fresh_workspace_requirement"],
        "requirement": (
            "RECONSTRUCT every required baseline achievement before PRESERVE it. "
            "EXTEND only through each additive feedback action, then VERIFY baseline "
            "equivalence. Do not replace baseline paths, restart the research plan, "
            "or reconstruct hidden targets."
        ),
    }
    # Core's native reflector renderer correctly bounds feedback records.  Put
    # the complete preservation contract first in a compact, data-only view so
    # that all three native reflector prompts receive it before any verbose
    # trajectory JSON can consume that bounded rendering budget.  The full
    # typed capsule remains attached below for durable provenance and adapter
    # admission; this compact view is not a second source of authority.
    prompt_contract = _build_reflector_prompt_contract(reflector_capsule)
    reflector_feedback = {
        "a00_preservation_first_prompt_contract": prompt_contract,
        "schema_version": RETENTION_FEEDBACK_SCHEMA,
        "status": "available_for_evolution",
        "feedback_class": RETENTION_FEEDBACK_CLASS,
        "task_id": task_id,
        "actionable_candidate_evolution": actionable_candidate_evolution,
        "sanitized_evaluation_feedback": reflector_sanitized_feedback,
        "baseline_evidence_capsule": reflector_capsule,
        "fresh_workspace_guidance": [
            "The next Candidate will run in a fresh workspace without baseline files.",
            (
                "For memory write WHAT MUST NOT BE LOST; for skill write HOW TO "
                "RECONSTRUCT AND EXTEND; for agent-system write WHAT MUST BE TRUE "
                "BEFORE SUBMISSION. Keep all required achievements and all admitted "
                "diagnoses visible in the final artifacts."
            ),
            "Do not reconstruct hidden evaluation targets.",
        ],
        "policy": {
            "candidate_grounded": True,
            "public_or_candidate_provenance_required": True,
            "answer_reconstruction_allowed": False,
        },
    }
    return {
        "sanitized_feedback": feedback,
        "sanitized_feedback_sha256": canonical_sha256(feedback),
        "admission": admission,
        "baseline_evidence_capsule": capsule,
        "baseline_evidence_capsule_sha256": canonical_sha256(capsule),
        "baseline_evidence_capsule_admission": capsule_admission,
        "reflector_sanitized_feedback": reflector_sanitized_feedback,
        "reflector_sanitized_feedback_sha256": canonical_sha256(
            reflector_sanitized_feedback
        ),
        "reflector_baseline_evidence_capsule": reflector_capsule,
        "reflector_baseline_evidence_capsule_sha256": canonical_sha256(
            reflector_capsule
        ),
        "reflector_feedback": reflector_feedback,
        "reflector_feedback_sha256": canonical_sha256(reflector_feedback),
        "quality_contract": {
            "generic_only_advice": False,
            "candidate_specific_references_present": True,
            "candidate_evidence_refs_present": True,
            "preserve_strength_guidance_present": True,
            "targeted_improvement_guidance_present": True,
            "baseline_evidence_capsule_present": True,
            "fresh_workspace_reconstruction_required": True,
            "weakness_to_action_mapping_present": True,
            "all_sanitized_diagnoses_visible": True,
            "baseline_success_trace_present": True,
            "baseline_achievement_ledger_present": True,
            "preservation_first_contract": True,
            "gt_leakage": False,
        },
    }


def _build_reflector_prompt_contract(reflector_capsule: Mapping[str, Any]) -> dict[str, Any]:
    """Return the compact, ordered R3 view consumed by native reflectors.

    Values stay below Core's per-leaf feedback rendering bound.  The ordered
    keys deliberately place the semantic rule, every admitted diagnosis, the
    required-achievement ledger, and successful trace evidence ahead of the
    verbose durable capsule fields.
    """

    def compact(value: Any, *, limit: int) -> str:
        return " ".join(str(value).split())[:limit].rstrip()

    mappings = reflector_capsule["weakness_to_action"]
    diagnoses = [
        (
            f"{item['weakness_id']} -> {item['achievement_id']}: {item['dimension']}; "
            f"candidate={compact(item['candidate_observation'], limit=32)}; ADD "
            f"{compact(item['next_run_action'], limit=48)}"
        )
        for item in mappings
    ]
    achievements = [
        (
            f"{item['achievement_id']} REQUIRED: method={' '.join(item['method_signature'][:7])}; "
            f"outputs={','.join(item['required_output_classes'])}; evidence role="
            f"{compact(item['scientific_role'], limit=28)}"
        )
        for item in reflector_capsule["baseline_achievement_ledger"]["achievements"]
    ]
    trace_events = [
        (
            f"{item['trace_id']} successful: action={compact(item['action'], limit=48)}; "
            f"evidence={','.join(item['candidate_artifact_refs'][:2])}; "
            f"verify={compact(item['verification'], limit=24)}"
        )
        for item in reflector_capsule["baseline_success_trace"]["events"][:3]
    ]
    return {
        "00_task_local_final_artifact_contract": (
            "R3 TASK-LOCAL; NOT generic SOP. RECONSTRUCT/PRESERVE all achievements; EXTEND "
            "only additive weaknesses in a fresh workspace; VERIFY baseline equivalence. Final artifact must use "
            "required achievement IDs, methods, and evidence roles."
        ),
        "00a_destination_role_contract": (
            "Memory: PRESERVE each ID with method+evidence. Skill: reconstruct+verify each ID; "
            "map every weakness additively. Agent-system: RECONSTRUCT, PRESERVE, EXTEND, VERIFY, "
            "and baseline equivalence."
        ),
        "01_required_baseline_achievements": achievements,
        "02_all_sanitized_diagnoses": diagnoses,
        "03_baseline_success_trace": trace_events,
        "04_artifact_roles": (
            "memory=what must not be lost; skill=how to reconstruct then extend; "
            "agent-system=what must be true before submission."
        ),
    }


class EvaluationFeedbackProjectionPort:
    """Durable, zero-model projection authority outside OpenEvo Core."""

    def __init__(
        self,
        *,
        root: str | Path,
        researchclawbench_root: str | Path,
        evaluator: PrivateEvaluationReader,
    ) -> None:
        self.root = Path(os.path.abspath(root))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.researchclawbench_root = Path(researchclawbench_root).resolve(strict=True)
        self.evaluator = evaluator

    def _path(self, idempotency_key: str) -> Path:
        return self.root / f"{hashlib.sha256(idempotency_key.encode()).hexdigest()}.json"

    def recover(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any] | None:
        path = self._path(idempotency_key)
        if not path.is_file() or path.is_symlink():
            return None
        stored = json.loads(path.read_text(encoding="utf-8"))
        if stored.get("request_sha256") != canonical_sha256(request):
            raise FeedbackProjectionError("feedback projection request authority drifted")
        return stored.get("result")

    def execute(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        prior = self.recover(request, idempotency_key)
        if prior is not None:
            return prior
        candidate = request.get("candidate")
        evaluation = request.get("evaluation")
        gt = request.get("gt_supervision")
        if (
            not isinstance(candidate, dict)
            or not isinstance(evaluation, dict)
            or not isinstance(gt, dict)
        ):
            raise FeedbackProjectionError("feedback projection request is incomplete")
        task_id = request.get("task_id")
        if (
            task_id != gt.get("task_id")
            or candidate.get("run_id", "").split("_a", 1)[0] != task_id
        ):
            raise FeedbackProjectionError("feedback projection task binding differs")
        operation_key = evaluation.get("idempotency_key")
        raw_sha256 = evaluation.get("raw_response_sha256")
        if not isinstance(operation_key, str) or not isinstance(raw_sha256, str):
            raise FeedbackProjectionError("private evaluation authority is absent")
        raw = self.evaluator.read_private_evaluation_for_projection(
            idempotency_key=operation_key,
            expected_sha256=raw_sha256,
        )
        task_info_path = self.researchclawbench_root / "tasks" / str(task_id) / "task_info.json"
        if task_info_path.is_symlink() or not task_info_path.is_file():
            raise FeedbackProjectionError("public task authority is absent")
        task_info = json.loads(task_info_path.read_text(encoding="utf-8"))
        task_local = gt.get("task_local_feedback")
        entries = task_local.get("ground_truth_entries") if isinstance(task_local, dict) else None
        if (
            not isinstance(entries, list)
            or not entries
            or any(not isinstance(item, dict) for item in entries)
        ):
            raise FeedbackProjectionError("GT leakage authority is incomplete")
        candidate_for_projection = {
            **candidate,
            "artifact_root_sha256": request.get("validation", {}).get("artifact_root_sha256"),
        }
        projection = project_sanitized_evaluation_feedback(
            task_id=str(task_id),
            candidate=candidate_for_projection,
            raw_evaluation=raw,
            public_task_info=task_info,
            ground_truth_entries=entries,
        )
        capsule = projection["baseline_evidence_capsule"]
        capsule_path = (
            self.root
            / "capsules"
            / str(task_id)
            / f"{projection['baseline_evidence_capsule_sha256']}.baseline_evidence_capsule.json"
        )
        # Candidate-derived only; this is intentionally separate from private
        # evaluator output and contains neither raw GT nor Judge reasoning.
        atomic_write_json(capsule_path, capsule)
        body = {
            "schema_version": "openevo.researchclawbench.feedback_projection_receipt.v2",
            "feedback_projection_id": (
                "feedback-projection-" + projection["reflector_feedback_sha256"][:24]
            ),
            "task_id": task_id,
            "source_evaluation_receipt_id": evaluation.get("evaluation_receipt_id"),
            "source_candidate_session_id": candidate.get("session_id"),
            "source_candidate_core_task_id": candidate.get("core_task_id"),
            "ground_truth_sha256": gt.get("ground_truth_sha256"),
            "baseline_evidence_capsule_path": os.fspath(capsule_path),
            **projection,
            "feedback_projector_model_calls": 0,
            "openevo_state_mutations": 0,
            "evaluation_frozen": True,
            "raw_gt_projected": False,
            "raw_checklist_projected": False,
            "target_image_projected": False,
            "judge_reasoning_projected": False,
            "judge_raw_response_projected": False,
            "secret_recorded": False,
        }
        result = {**body, "content_sha256": canonical_sha256(body)}
        stored = {
            "request_sha256": canonical_sha256(request),
            "result": result,
        }
        path = self._path(idempotency_key)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            data = canonical_bytes(stored) + b"\n"
            os.write(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        return result


__all__ = [
    "ADMISSION_SCHEMA",
    "ALLOWED_DIMENSIONS",
    "FEEDBACK_CLASS",
    "FEEDBACK_SCHEMA",
    "RETENTION_FEEDBACK_CLASS",
    "RETENTION_FEEDBACK_SCHEMA",
    "EvaluationFeedbackProjectionPort",
    "FeedbackAdmissionError",
    "FeedbackProjectionError",
    "admit_sanitized_feedback",
    "objective_outcome_signal",
    "project_sanitized_evaluation_feedback",
]
