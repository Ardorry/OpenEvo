"""Low-interference Candidate-grounded context for R5 evolution.

The adapter selects bounded, high-information excerpts from the baseline
Candidate's own transcript, code, outputs, and report.  It deliberately does
not translate those excerpts into an achievement or capability taxonomy.
Sanitized evaluator feedback is supplied unchanged by the frozen projector.
No model is called here.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .run_manifest import atomic_write_json
from .training_state_store import canonical_sha256

MINIMAL_TRACE_SCHEMA = "openevo.researchclawbench.selected_baseline_trajectory.v1"
MINIMAL_CONTEXT_SCHEMA = "openevo.researchclawbench.minimal_interference_evolution.r5"
MINIMAL_QUALITY_SCHEMA = "openevo.researchclawbench.minimal_interference_observation.v1"
RETENTION_FEEDBACK_CLASS = "minimal_interference_evolution_r5"
RETENTION_FEEDBACK_SCHEMA = MINIMAL_CONTEXT_SCHEMA

_ARTIFACT_TYPES = ("text_memory", "skill_bundle", "agent_system")
_CODE_SUFFIXES = frozenset({".py", ".r", ".jl"})
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
_SCIENTIFIC_TERMS = frozenset(
    {
        "auc",
        "brier",
        "calibration",
        "candidate",
        "catalog",
        "classification",
        "compare",
        "confusion",
        "correlation",
        "descriptor",
        "distribution",
        "evidence",
        "experiment",
        "extract",
        "figure",
        "fit",
        "hypothesis",
        "metric",
        "model",
        "onset",
        "parameter",
        "pdf",
        "probability",
        "ranking",
        "regression",
        "result",
        "score",
        "selection",
        "sensitivity",
        "threshold",
        "validate",
        "validation",
    }
)
_DECISION_TERMS = frozenset(
    {
        "because",
        "choose",
        "define",
        "explicit",
        "instead",
        "preserve",
        "reference",
        "retain",
        "silver",
        "target",
    }
)
_OPERATIONAL_NOISE = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^pwd(?:\s|$)",
        r"^ls(?:\s|$)",
        r"^find\s+[^|;]+(?:\||$)",
        r"pip (?:--version|cache|install)",
        r"which (?:python|pdftotext|pdfinfo)",
        r"__pycache__|\.pytest_cache|\.ruff_cache|node_modules",
    )
)
_TERM = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
_NUMBER = re.compile(r"(?<![A-Za-z_])[-+]?(?:\d+\.\d+|\d+)(?![A-Za-z_])")


class MinimalInterferenceEvolutionError(RuntimeError):
    """The sealed Candidate evidence cannot provide selected excerpts."""


def _compact(value: str, *, limit: int) -> str:
    return " ".join(value.split())[:limit].rstrip()


def _terms(value: str) -> set[str]:
    return {token.casefold() for token in _TERM.findall(value)}


def _signal_score(value: str) -> int:
    terms = _terms(value)
    return (
        3 * len(terms.intersection(_SCIENTIFIC_TERMS))
        + 2 * len(terms.intersection(_DECISION_TERMS))
        + min(8, len(_NUMBER.findall(value)))
        + 2 * bool(re.search(r"(?i)\.(?:csv|json|png|pdf|py)\b", value))
        + 2 * bool(re.search(r"(?:>=|<=|==|\bAUC\b|\bBrier\b|\bECE\b)", value))
    )


def _candidate_files(root: Path) -> list[str]:
    files: list[str] = []
    for path in sorted(root.rglob("*")):
        if len(files) >= 4096:
            raise MinimalInterferenceEvolutionError("SELECTED_TRAJECTORY_INVENTORY_TOO_LARGE")
        if path.is_symlink() or not path.is_file():
            continue
        files.append(path.relative_to(root).as_posix())
    return files


def _transcript_items(root: Path) -> list[dict[str, Any]]:
    path = root / "_agent_output.jsonl"
    if not path.is_file() or path.is_symlink():
        return []
    try:
        outer = json.loads(path.read_text(encoding="utf-8", errors="replace")[:1_500_000].splitlines()[0])
    except (IndexError, OSError, json.JSONDecodeError):
        return []
    nested = outer.get("metadata", {}).get("transcript") if isinstance(outer, dict) else None
    if not isinstance(nested, str):
        return []
    items: list[dict[str, Any]] = []
    for line in nested.splitlines()[:4096]:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = record.get("item") if isinstance(record.get("item"), dict) else record
        if isinstance(item, dict):
            items.append(item)
    return items


def _selected_candidate_messages(items: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    ranked: list[tuple[int, int, dict[str, str]]] = []
    for index, item in enumerate(items):
        if item.get("type") != "agent_message" or not isinstance(item.get("text"), str):
            continue
        text = _compact(item["text"], limit=1_800)
        score = _signal_score(text)
        if score < 5:
            continue
        ranked.append(
            (-score, index, {"source": "candidate_message", "reference": f"transcript:{index}", "text": text})
        )
    return [item for _score, _index, item in sorted(ranked)[:10]]


def _selected_commands(items: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, str]], set[str]]:
    ranked: list[tuple[int, int, dict[str, str]]] = []
    executed_scripts: set[str] = set()
    for index, item in enumerate(items):
        if (
            item.get("type") != "command_execution"
            or item.get("status") != "completed"
            or item.get("exit_code") not in {0, "0"}
            or not isinstance(item.get("command"), str)
        ):
            continue
        command = item["command"]
        for match in re.findall(r"(?<![\w./-])([\w./-]+\.(?:py|r|jl))(?![\w.-])", command, re.IGNORECASE):
            executed_scripts.add(match.replace("\\", "/").lstrip("./"))
        output = item.get("aggregated_output")
        output = output if isinstance(output, str) else ""
        combined = command + "\n" + output
        score = _signal_score(combined)
        if re.search(r"(?i)python\s+(?:-u\s+)?(?:\./)?code/", command):
            score += 12
        if re.search(r"(?i)outputs/(?:summary|metrics|model|result|descriptor)", combined):
            score += 8
        if any(pattern.search(command.strip()) for pattern in _OPERATIONAL_NOISE) and score < 14:
            continue
        if score < 8:
            continue
        text = "COMMAND:\n" + _compact(command, limit=1_000)
        if output.strip():
            text += "\nOUTPUT:\n" + _compact(output, limit=2_400)
        ranked.append(
            (-score, index, {"source": "successful_command", "reference": f"transcript:{index}", "text": text})
        )
    return [item for _score, _index, item in sorted(ranked)[:8]], executed_scripts


def _selected_code(root: Path, files: Iterable[str], executed_scripts: set[str]) -> list[dict[str, str]]:
    scripts = [ref for ref in files if Path(ref).suffix.casefold() in _CODE_SUFFIXES]
    selected = [ref for ref in scripts if ref in executed_scripts or Path(ref).name in {Path(item).name for item in executed_scripts}]
    selected = selected or scripts
    ranked: list[tuple[int, str, int, dict[str, str]]] = []
    for script in selected[:4]:
        source = (root / script).read_text(encoding="utf-8", errors="replace")[:1_000_000]
        try:
            tree = ast.parse(source, filename=script)
        except SyntaxError:
            continue
        main_calls: set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main":
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        if isinstance(child.func, ast.Name):
                            main_calls.add(child.func.id)
                        elif isinstance(child.func, ast.Attribute):
                            main_calls.add(child.func.attr)
        for order, node in enumerate(tree.body):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign)):
                continue
            raw = ast.get_source_segment(source, node) or ""
            if not raw.strip():
                continue
            name = getattr(node, "name", "module decision table")
            score = _signal_score(raw)
            if name == "main":
                score += 16
            if name in main_calls:
                score += 10
            if isinstance(node, ast.ClassDef):
                score += 4
            if _NUMBER.search(raw):
                score += 4
            if score < 8:
                continue
            ranked.append(
                (
                    -score,
                    script,
                    order,
                    {
                        "source": "candidate_code",
                        "reference": f"{script}:{getattr(node, 'lineno', 1)}",
                        "text": raw[:4_000].rstrip(),
                    },
                )
            )
    return [item for _score, _script, _order, item in sorted(ranked)[:12]]


def _selected_report(root: Path) -> list[dict[str, str]]:
    report_path = root / "report/report.md"
    if not report_path.is_file() or report_path.is_symlink():
        raise MinimalInterferenceEvolutionError("SELECTED_TRAJECTORY_REPORT_ABSENT")
    report = report_path.read_text(encoding="utf-8", errors="replace")[:262_144]
    ranked: list[tuple[int, int, dict[str, str]]] = []
    for index, paragraph in enumerate(re.split(r"\n\s*\n", report)):
        text = paragraph.strip()
        if not text or text.startswith("!["):
            continue
        score = _signal_score(text)
        if score < 7:
            continue
        ranked.append(
            (-score, index, {"source": "candidate_report", "reference": f"report/report.md:paragraph:{index}", "text": text[:2_400].rstrip()})
        )
    return [item for _score, _index, item in sorted(ranked)[:8]]


def build_selected_baseline_trajectory(*, candidate_root: str | Path) -> dict[str, Any]:
    """Select original scientific excerpts without capability re-interpretation."""

    root = Path(candidate_root).resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise MinimalInterferenceEvolutionError("SELECTED_TRAJECTORY_CANDIDATE_ROOT_UNSAFE")
    files = _candidate_files(root)
    items = _transcript_items(root)
    commands, executed_scripts = _selected_commands(items)
    candidates = [
        *_selected_candidate_messages(items),
        *_selected_code(root, files, executed_scripts),
        *commands,
        *_selected_report(root),
    ]
    excerpts: list[dict[str, str]] = []
    seen: set[str] = set()
    total_bytes = 0
    for item in candidates:
        digest = hashlib.sha256(item["text"].encode("utf-8")).hexdigest()
        size = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
        if digest in seen or total_bytes + size > 58_000:
            continue
        seen.add(digest)
        excerpts.append(item)
        total_bytes += size
    if not excerpts or not any(item["source"] == "candidate_code" for item in excerpts):
        raise MinimalInterferenceEvolutionError("SELECTED_TRAJECTORY_SCIENTIFIC_EXCERPTS_ABSENT")
    return {
        "schema_version": MINIMAL_TRACE_SCHEMA,
        "selected_trajectory_excerpts": excerpts,
        "projector_model_calls": 0,
    }


def admit_selected_baseline_trajectory(
    trace: Mapping[str, Any], *, candidate_root: str | Path
) -> dict[str, Any]:
    Path(candidate_root).resolve(strict=True)
    excerpts = trace.get("selected_trajectory_excerpts")
    if (
        set(trace) != {"schema_version", "selected_trajectory_excerpts", "projector_model_calls"}
        or trace.get("schema_version") != MINIMAL_TRACE_SCHEMA
        or trace.get("projector_model_calls") != 0
        or not isinstance(excerpts, list)
        or not excerpts
        or len(excerpts) > 38
    ):
        raise MinimalInterferenceEvolutionError("SELECTED_TRAJECTORY_SCHEMA_INVALID")
    expected = {"source", "reference", "text"}
    for excerpt in excerpts:
        if (
            not isinstance(excerpt, dict)
            or set(excerpt) != expected
            or excerpt.get("source") not in {"candidate_message", "successful_command", "candidate_code", "candidate_report"}
            or any(not isinstance(excerpt.get(key), str) or not excerpt[key].strip() for key in expected)
        ):
            raise MinimalInterferenceEvolutionError("SELECTED_TRAJECTORY_SCHEMA_INVALID")
    serialized = " ".join(str(item["text"]) for item in excerpts).casefold()
    if any(marker.casefold() in serialized for marker in _PRIVATE_MARKERS):
        raise MinimalInterferenceEvolutionError("SELECTED_TRAJECTORY_PRIVATE_MARKER")
    body = {
        "schema_version": "openevo.researchclawbench.selected_baseline_trajectory_admission.v1",
        "status": "ADMITTED",
        "trace_sha256": canonical_sha256(dict(trace)),
        "excerpt_count": len(excerpts),
        "candidate_grounded": True,
        "raw_gt_projected": False,
        "judge_reasoning_projected": False,
        "target_image_projected": False,
        "provider_calls": 0,
    }
    return {**body, "content_sha256": canonical_sha256(body)}


def build_minimal_interference_context(
    *, trace: Mapping[str, Any], sanitized_feedback: Mapping[str, Any]
) -> dict[str, Any]:
    excerpts = trace.get("selected_trajectory_excerpts")
    diagnoses = sanitized_feedback.get("diagnoses")
    if not isinstance(excerpts, list) or not excerpts or not isinstance(diagnoses, list) or not diagnoses:
        raise MinimalInterferenceEvolutionError("MINIMAL_INTERFERENCE_CONTEXT_INCOMPLETE")
    return {
        "schema_version": MINIMAL_CONTEXT_SCHEMA,
        "baseline_scientific_work": {"selected_trajectory_excerpts": excerpts},
        "sanitized_evaluator_feedback": {
            "feedback_class": sanitized_feedback.get("feedback_class"),
            "preserve_strengths": sanitized_feedback.get("preserve_strengths", []),
            "diagnoses": diagnoses,
        },
        "evolution_objective": (
            "Learn from the baseline. Preserve useful work. Use feedback to improve weak "
            "areas. Produce a delta over the baseline rather than a replacement research "
            "plan. Do not silently replace baseline scientific definitions. If a baseline "
            "choice is changed, require public evidence and make the change explicit."
        ),
        "artifact_roles": {
            "text_memory": "Remember what the baseline did and learned.",
            "skill_bundle": "Reproduce the baseline work, then add the feedback-driven delta.",
            "agent_system": "Do not discard useful baseline work while improving it.",
        },
    }


def assess_minimal_interference_artifacts(
    *,
    trace: Mapping[str, Any],
    sanitized_feedback: Mapping[str, Any],
    artifact_texts: Mapping[str, str],
    content_admission_findings: list[str],
    provenance_violations: list[str],
) -> dict[str, Any]:
    excerpts = trace.get("selected_trajectory_excerpts")
    diagnoses = sanitized_feedback.get("diagnoses")
    if (
        trace.get("schema_version") != MINIMAL_TRACE_SCHEMA
        or not isinstance(excerpts, list)
        or not excerpts
        or not isinstance(diagnoses, list)
        or not diagnoses
        or set(artifact_texts) != set(_ARTIFACT_TYPES)
        or any(not isinstance(value, str) or not value.strip() for value in artifact_texts.values())
    ):
        raise MinimalInterferenceEvolutionError("MINIMAL_INTERFERENCE_OBSERVATION_INPUT_INVALID")
    baseline_text = "\n".join(str(item.get("text", "")) for item in excerpts if isinstance(item, dict))
    artifact_text = "\n".join(artifact_texts.values())
    baseline_terms = _terms(baseline_text).difference({"candidate", "baseline", "report", "result"})
    artifact_terms = _terms(artifact_text)
    baseline_numbers = list(dict.fromkeys(_NUMBER.findall(baseline_text)))
    artifact_numbers = set(_NUMBER.findall(artifact_text))
    feedback_findings = []
    for diagnosis in diagnoses:
        if not isinstance(diagnosis, dict):
            continue
        terms = _terms(str(diagnosis.get("dimension", "")) + " " + str(diagnosis.get("improvement_direction", "")))
        feedback_findings.append(
            {
                "dimension": diagnosis.get("dimension"),
                "matched_term_count": len(terms.intersection(artifact_terms)),
            }
        )
    warnings = [
        *[f"content_admission:{item}" for item in sorted(set(content_admission_findings))],
        *[f"provenance:{item}" for item in sorted(set(provenance_violations))],
    ]
    body = {
        "schema_version": MINIMAL_QUALITY_SCHEMA,
        "status": "PASS",
        "dispatch_authority": {
            "pass": True,
            "basis": "registered_readable_native_artifact_triple",
            "semantic_heuristics_hard_blocking": False,
        },
        "integrity_observations": {
            "authority": "diagnostic_only",
            "findings": warnings,
        },
        "diagnostics": {
            "authority": "diagnostic_only",
            "baseline_term_retention_ratio": round(len(baseline_terms.intersection(artifact_terms)) / max(1, len(baseline_terms)), 6),
            "numeric_literal_retention": {
                "matched": sum(value in artifact_numbers for value in baseline_numbers),
                "total": len(baseline_numbers),
            },
            "feedback_findings": feedback_findings,
            "warnings": warnings,
        },
        "gt_leakage_findings": sorted(set(content_admission_findings)),
        "provenance_violations": sorted(set(provenance_violations)),
        "artifact_text_sha256": {kind: canonical_sha256(text) for kind, text in artifact_texts.items()},
        "provider_calls": 0,
    }
    return {**body, "content_sha256": canonical_sha256(body)}


class MinimalInterferenceFeedbackProjectionPort:
    """Add selected Candidate excerpts beside frozen sanitized feedback."""

    def __init__(self, *, root: str | Path, frozen_projector: Any) -> None:
        self.root = Path(os.path.abspath(root))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.frozen_projector = frozen_projector

    def _path(self, idempotency_key: str) -> Path:
        return self.root / f"{hashlib.sha256(idempotency_key.encode()).hexdigest()}.json"

    def recover(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any] | None:
        path = self._path(idempotency_key)
        if not path.is_file() or path.is_symlink():
            return None
        stored = json.loads(path.read_text(encoding="utf-8"))
        if stored.get("request_sha256") != canonical_sha256(request):
            raise MinimalInterferenceEvolutionError("R5_PROJECTION_REQUEST_DRIFT")
        result = stored.get("result")
        if not isinstance(result, dict):
            raise MinimalInterferenceEvolutionError("R5_PROJECTION_RECEIPT_INVALID")
        return result

    def execute(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        prior = self.recover(request, idempotency_key)
        if prior is not None:
            return prior
        frozen = self.frozen_projector.execute(request, idempotency_key)
        candidate = request.get("candidate")
        root_value = candidate.get("candidate_output_root") if isinstance(candidate, dict) else None
        sanitized = frozen.get("reflector_sanitized_feedback")
        if not isinstance(root_value, str) or not isinstance(sanitized, dict):
            raise MinimalInterferenceEvolutionError("R5_PROJECTION_AUTHORITY_INCOMPLETE")
        trace = build_selected_baseline_trajectory(candidate_root=root_value)
        admission = admit_selected_baseline_trajectory(trace, candidate_root=root_value)
        context = build_minimal_interference_context(trace=trace, sanitized_feedback=sanitized)
        reflector_feedback = {
            "a00_minimal_interference_context": context,
            "schema_version": RETENTION_FEEDBACK_SCHEMA,
            "status": "available_for_evolution",
            "feedback_class": RETENTION_FEEDBACK_CLASS,
            "task_id": request.get("task_id"),
            "policy": {
                "candidate_grounded": True,
                "answer_reconstruction_allowed": False,
            },
        }
        result_body = {
            **frozen,
            "schema_version": "openevo.researchclawbench.feedback_projection_receipt.r5",
            "feedback_projection_id": "feedback-projection-r5-" + canonical_sha256(reflector_feedback)[:24],
            "selected_baseline_trajectory": trace,
            "selected_baseline_trajectory_sha256": canonical_sha256(trace),
            "selected_baseline_trajectory_admission": admission,
            "reflector_feedback": reflector_feedback,
            "reflector_feedback_sha256": canonical_sha256(reflector_feedback),
            "structured_r4_diagnostic_reflector_feedback_sha256": frozen.get("reflector_feedback_sha256"),
            "quality_contract": {
                "selected_baseline_trajectory_present": True,
                "all_sanitized_diagnoses_visible": True,
                "semantic_quality_diagnostic_only": True,
                "baseline_equivalence_diagnostic_only": True,
            },
        }
        result_body.pop("content_sha256", None)
        result = {**result_body, "content_sha256": canonical_sha256(result_body)}
        atomic_write_json(self._path(idempotency_key), {"request_sha256": canonical_sha256(request), "result": result})
        return result


__all__ = [
    "MINIMAL_CONTEXT_SCHEMA",
    "MINIMAL_QUALITY_SCHEMA",
    "MINIMAL_TRACE_SCHEMA",
    "RETENTION_FEEDBACK_CLASS",
    "RETENTION_FEEDBACK_SCHEMA",
    "MinimalInterferenceEvolutionError",
    "MinimalInterferenceFeedbackProjectionPort",
    "admit_selected_baseline_trajectory",
    "assess_minimal_interference_artifacts",
    "build_minimal_interference_context",
    "build_selected_baseline_trajectory",
]
