"""Judge-precondition check for preservation-first evolved workspaces.

This adapter-local gate asks only whether the fresh evolved Candidate retained
the baseline Candidate's own capability/evidence chain.  It receives neither
ground truth nor a Judge response.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .training_state_store import canonical_sha256

EQUIVALENCE_SCHEMA = "openevo.researchclawbench.baseline_equivalence.v1"
EQUIVALENCE_FAILURE = "BASELINE_EQUIVALENCE_FAILED"
_TOKEN = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_CODE = frozenset({".py", ".ipynb", ".r", ".jl"})
_IMAGE = frozenset({".png", ".jpg", ".jpeg", ".svg"})
_NUMERIC = frozenset({".csv", ".tsv", ".json"})
_LOW_SIGNAL = frozenset({"analysis", "artifact", "baseline", "candidate", "capability", "data", "evidence", "figure", "fresh", "image", "method", "output", "reconstruct", "report", "required", "result", "route", "summary", "the", "validation", "with"})
_ACHIEVEMENT_ID = re.compile(r"\bachievement\s+[a-z0-9]+\b")
_PUBLIC_INPUT_PREFIXES = ("data/", "related_work/", "inputs/")
_INSTRUCTION_FILES = frozenset(
    {"AGENTS.md", "agents.md", "CLAUDE.md", "GEMINI.md", "INSTRUCTIONS.md"}
)


class BaselineEquivalenceError(RuntimeError):
    pass


def _normalize(value: str) -> str:
    return " ".join(_TOKEN.findall(value.casefold()))


def _terms(value: str) -> set[str]:
    return {token for token in _normalize(value).split() if token not in _LOW_SIGNAL and len(token) > 2 and not token.isdigit()}


def _files(root: Path) -> list[str]:
    result: list[str] = []
    for path in sorted(root.rglob("*")):
        if len(result) >= 4096:
            raise BaselineEquivalenceError("BASELINE_EQUIVALENCE_FILE_INVENTORY_TOO_LARGE")
        if path.is_symlink() or not path.is_file():
            continue
        result.append(path.relative_to(root).as_posix())
    return result


def _candidate_evidence_files(files: list[str]) -> list[str]:
    return [
        ref
        for ref in files
        if not ref.startswith(_PUBLIC_INPUT_PREFIXES)
        and Path(ref).name not in _INSTRUCTION_FILES
        and not Path(ref).name.startswith("_")
        and (
            ref.startswith(("code/", "outputs/", "report/"))
            or Path(ref).suffix.casefold() in _CODE
        )
    ]


def _text_corpus(root: Path, files: list[str]) -> str:
    chunks: list[str] = []
    budget = 786_432
    for ref in files:
        if budget <= 0:
            break
        path = root / ref
        if path.suffix.casefold() not in {".md", ".txt", ".py", ".r", ".jl", ".csv", ".tsv", ".json"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")[:min(budget, 131_072)]
        chunks.append(text)
        budget -= len(text.encode("utf-8"))
    return "\n".join(chunks)


def _outputs_present(files: list[str], required: list[str]) -> dict[str, bool]:
    scripts = [
        ref
        for ref in files
        if Path(ref).suffix.casefold() in _CODE
        and not ref.startswith(_PUBLIC_INPUT_PREFIXES)
        and Path(ref).name not in _INSTRUCTION_FILES
    ]
    numeric_outputs = [
        ref
        for ref in files
        if ref.startswith("outputs/") and Path(ref).suffix.casefold() in _NUMERIC
    ]
    figures = [
        ref
        for ref in files
        if ref.startswith(("outputs/", "report/"))
        and Path(ref).suffix.casefold() in _IMAGE
    ]
    return {
        "script": bool(scripts) if "script" in required else True,
        "numeric_output": bool(numeric_outputs) if "numeric_output" in required else True,
        "figure": bool(figures) if "figure" in required else True,
        "report_section": "report/report.md" in files if "report_section" in required else True,
    }


def _output_counts(files: list[str]) -> dict[str, int]:
    return {
        "script": sum(
            Path(ref).suffix.casefold() in _CODE
            and not ref.startswith(_PUBLIC_INPUT_PREFIXES)
            and Path(ref).name not in _INSTRUCTION_FILES
            for ref in files
        ),
        "numeric_output": sum(
            ref.startswith("outputs/")
            and Path(ref).suffix.casefold() in _NUMERIC
            for ref in files
        ),
        "figure": sum(
            ref.startswith(("outputs/", "report/"))
            and Path(ref).suffix.casefold() in _IMAGE
            for ref in files
        ),
        "report_section": int("report/report.md" in files),
    }


def _method_reconstructed(achievement: Mapping[str, Any], corpus: str, files: list[str]) -> bool:
    normalized = _normalize(corpus + " " + " ".join(files))
    identifier = _normalize(str(achievement["achievement_id"]))
    mentioned = set(_ACHIEVEMENT_ID.findall(normalized))
    signature = _terms(" ".join(str(item) for item in achievement.get("method_signature", [])) + " " + str(achievement.get("capability", "")))
    required = min(3, len(signature))
    signature_present = (
        required >= 2
        and len(signature.intersection(_terms(corpus + " " + " ".join(files))))
        >= required
    )
    return signature_present and (not mentioned or identifier in mentioned)


def _report_discusses(achievement: Mapping[str, Any], report_text: str) -> bool:
    normalized = _normalize(report_text)
    identifier = _normalize(str(achievement["achievement_id"]))
    mentioned = set(_ACHIEVEMENT_ID.findall(normalized))
    signature = _terms(" ".join(str(item) for item in achievement.get("method_signature", [])))
    required = min(2, len(signature))
    signature_present = (
        required >= 2
        and len(signature.intersection(_terms(report_text))) >= required
    )
    return signature_present and (not mentioned or identifier in mentioned)


def _report_output_roles_present(report_text: str, required: list[str]) -> bool:
    """Confirm that the report links every required output role.

    A scientific report normally separates method, results, figures, and
    reproducibility into different sections.  Requiring every role word in one
    sentence or paragraph rejects that valid structure, so role linkage is
    aggregated across the report after the achievement-specific discussion has
    independently been established.
    """

    normalized = _normalize(report_text)
    vocabulary = {
        "script": ("script", "code", "program"),
        "numeric_output": ("numeric", "table", "csv", "metric", "value"),
        "figure": ("figure", "plot", "chart", "image"),
        "report_section": ("report", "discussion", "section", "evidence"),
    }
    return all(
        any(marker in normalized for marker in vocabulary[item])
        for item in required
        if item in vocabulary
    )


def _verification_assertion_present(report_text: str) -> bool:
    normalized = _normalize(report_text)
    return any(
        marker in normalized
        for marker in ("verify", "validation", "check", "compare", "reproduc")
    )


def _evidence_refs_for_class(
    files: list[str], output_class: str
) -> list[str]:
    if output_class == "script":
        return [ref for ref in files if Path(ref).suffix.casefold() in _CODE]
    if output_class == "numeric_output":
        return [
            ref
            for ref in files
            if ref.startswith("outputs/")
            and Path(ref).suffix.casefold() in _NUMERIC
        ]
    if output_class == "figure":
        return [
            ref
            for ref in files
            if ref.startswith(("outputs/", "report/"))
            and Path(ref).suffix.casefold() in _IMAGE
        ]
    if output_class == "report_section":
        return ["report/report.md"] if "report/report.md" in files else []
    return []


def _linked_report_units(report_text: str, ref: str) -> str:
    name = Path(ref).name.casefold()
    normalized_ref = ref.casefold()
    units = re.split(r"\n\s*\n|(?<=[.!?])\s+", report_text)
    return "\n".join(
        unit
        for unit in units
        if name in unit.casefold() or normalized_ref in unit.casefold()
    )


def _evidence_role_present(
    achievement: Mapping[str, Any],
    report_text: str,
    evidence_files: list[str],
    root: Path,
) -> bool:
    role_terms = _terms(
        " ".join(
            str(item) for item in achievement.get("evidence_role_signature", [])
        )
    )
    if not role_terms:
        return False
    output_class = str(achievement.get("evidence_output_class", ""))
    refs = _evidence_refs_for_class(evidence_files, output_class)
    required = min(2, len(role_terms))
    for ref in refs:
        linked_report = _linked_report_units(report_text, ref)
        if not linked_report:
            continue
        evidence_text = ref
        path = root / ref
        if path.suffix.casefold() in {".md", ".txt", ".py", ".r", ".jl", ".csv", ".tsv", ".json"}:
            try:
                evidence_text += " " + path.read_text(
                    encoding="utf-8", errors="replace"
                )[:65_536]
            except OSError:
                continue
        observed = _terms(evidence_text + " " + linked_report)
        if len(role_terms.intersection(observed)) >= required:
            return True
    return False


def assess_baseline_equivalence(*, ledger: Mapping[str, Any], evolved_candidate_root: str | Path, validation: Mapping[str, Any]) -> dict[str, Any]:
    if ledger.get("schema_version") != "openevo.researchclawbench.baseline_achievement_ledger.v2":
        raise BaselineEquivalenceError("BASELINE_EQUIVALENCE_LEDGER_INVALID")
    achievements = ledger.get("achievements")
    if not isinstance(achievements, list) or not achievements:
        raise BaselineEquivalenceError("BASELINE_EQUIVALENCE_LEDGER_INVALID")
    output_floor = ledger.get("required_output_class_counts")
    if (
        not isinstance(output_floor, dict)
        or set(output_floor) != {"script", "numeric_output", "figure", "report_section"}
        or any(type(value) is not int or value < 0 for value in output_floor.values())
    ):
        raise BaselineEquivalenceError("BASELINE_EQUIVALENCE_LEDGER_INVALID")
    if validation.get("artifact_valid") is not True:
        raise BaselineEquivalenceError("BASELINE_EQUIVALENCE_EVOLVED_VALIDATION_INVALID")
    root = Path(evolved_candidate_root).resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise BaselineEquivalenceError("BASELINE_EQUIVALENCE_EVOLVED_ROOT_UNSAFE")
    files = _files(root)
    if "report/report.md" not in files:
        raise BaselineEquivalenceError("BASELINE_EQUIVALENCE_REPORT_ABSENT")
    report_text = (root / "report/report.md").read_text(encoding="utf-8", errors="replace")[:262_144]
    evidence_files = _candidate_evidence_files(files)
    corpus = _text_corpus(root, evidence_files)
    output_counts = _output_counts(files)
    output_floor_passed = all(
        output_counts[key] >= value for key, value in output_floor.items()
    )
    findings: list[dict[str, Any]] = []
    for achievement in achievements:
        if not isinstance(achievement, dict) or achievement.get("preservation_priority") != "required":
            continue
        required = achievement.get("required_output_classes")
        if not isinstance(required, list):
            raise BaselineEquivalenceError("BASELINE_EQUIVALENCE_LEDGER_INVALID")
        if (
            not isinstance(achievement.get("evidence_role_signature"), list)
            or not achievement["evidence_role_signature"]
            or achievement.get("evidence_output_class")
            not in {"script", "numeric_output", "figure", "report_section"}
        ):
            raise BaselineEquivalenceError("BASELINE_EQUIVALENCE_LEDGER_INVALID")
        output_presence = _outputs_present(files, required)
        method = _method_reconstructed(achievement, corpus, evidence_files)
        report_discussion = _report_discusses(achievement, report_text)
        evidence_role = _evidence_role_present(
            achievement, report_text, evidence_files, root
        )
        evidence_roles = report_discussion and _report_output_roles_present(
            report_text, required
        )
        verification_present = (
            validation.get("artifact_valid") is True
            and report_discussion
            and _verification_assertion_present(report_text)
        )
        conclusion_revised = any(
            marker in _normalize(report_text)
            for marker in ("revised", "corrected", "updated conclusion", "different conclusion")
        )
        corresponding_evidence = (
            method
            and all(output_presence.values())
            and report_discussion
            and evidence_roles
            and evidence_role
        )
        passed = corresponding_evidence and verification_present
        findings.append({
            "achievement_id": achievement.get("achievement_id"),
            "method_reconstructed": method,
            "required_output_classes_present": output_presence,
            "corresponding_evidence_present": corresponding_evidence,
            "report_output_roles_present": evidence_roles,
            "evidence_role_signature_present": evidence_role,
            "report_discussion_present": report_discussion,
            "verification_assertion_present": verification_present,
            "candidate_conclusion_revised": conclusion_revised,
            "status": "PASS" if passed else "FAIL",
        })
    if not findings:
        raise BaselineEquivalenceError("BASELINE_EQUIVALENCE_REQUIRED_ACHIEVEMENTS_ABSENT")
    passed = output_floor_passed and all(item["status"] == "PASS" for item in findings)
    body = {
        "schema_version": EQUIVALENCE_SCHEMA,
        "status": "PASS" if passed else EQUIVALENCE_FAILURE,
        "required_achievement_count": len(findings),
        "reconstructed_required_achievement_count": sum(item["status"] == "PASS" for item in findings),
        "dropped_required_achievement_ids": [item["achievement_id"] for item in findings if item["status"] != "PASS"],
        "required_output_class_counts": dict(output_floor),
        "evolved_output_class_counts": output_counts,
        "output_class_floor_passed": output_floor_passed,
        "findings": findings,
        "evolved_workspace_sha256": canonical_sha256({"files": files}),
        "raw_gt_visible": False,
        "judge_reasoning_visible": False,
        "target_image_visible": False,
        "provider_calls": 0,
    }
    return {**body, "content_sha256": canonical_sha256(body)}


def require_baseline_equivalence(**kwargs: Any) -> dict[str, Any]:
    report = assess_baseline_equivalence(**kwargs)
    if report["status"] != "PASS":
        raise BaselineEquivalenceError(EQUIVALENCE_FAILURE)
    return report


__all__ = ["EQUIVALENCE_FAILURE", "EQUIVALENCE_SCHEMA", "BaselineEquivalenceError", "assess_baseline_equivalence", "require_baseline_equivalence"]
