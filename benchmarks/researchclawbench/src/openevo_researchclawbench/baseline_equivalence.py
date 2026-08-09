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
    suffixes = [Path(ref).suffix.casefold() for ref in files]
    return {
        "script": any(suffix in _CODE for suffix in suffixes) if "script" in required else True,
        "numeric_output": any(suffix in _NUMERIC for suffix in suffixes) if "numeric_output" in required else True,
        "figure": any(suffix in _IMAGE for suffix in suffixes) if "figure" in required else True,
        "report_section": "report/report.md" in files if "report_section" in required else True,
    }


def _method_reconstructed(achievement: Mapping[str, Any], corpus: str, files: list[str]) -> bool:
    normalized = _normalize(corpus + " " + " ".join(files))
    identifier = _normalize(str(achievement["achievement_id"]))
    mentioned = set(_ACHIEVEMENT_ID.findall(normalized))
    if mentioned:
        return identifier in mentioned
    signature = _terms(" ".join(str(item) for item in achievement.get("method_signature", [])) + " " + str(achievement.get("capability", "")))
    return len(signature.intersection(_terms(corpus + " " + " ".join(files)))) >= min(3, len(signature))


def _report_discusses(achievement: Mapping[str, Any], report_text: str) -> bool:
    normalized = _normalize(report_text)
    identifier = _normalize(str(achievement["achievement_id"]))
    mentioned = set(_ACHIEVEMENT_ID.findall(normalized))
    if mentioned:
        return identifier in mentioned
    signature = _terms(" ".join(str(item) for item in achievement.get("method_signature", [])))
    return len(signature.intersection(_terms(report_text))) >= min(2, len(signature))


def _report_evidence_unit(
    achievement: Mapping[str, Any], report_text: str
) -> str:
    units = [item.strip() for item in re.split(r"\n\s*\n|(?<=[.!?])\s+", report_text) if item.strip()]
    identifier = _normalize(str(achievement["achievement_id"]))
    for unit in units:
        if identifier in _normalize(unit):
            return unit
    signature = _terms(" ".join(str(item) for item in achievement.get("method_signature", [])))
    ranked = sorted(
        units,
        key=lambda unit: len(signature.intersection(_terms(unit))),
        reverse=True,
    )
    return ranked[0] if ranked and len(signature.intersection(_terms(ranked[0]))) >= min(2, len(signature)) else ""


def _report_output_roles_present(unit: str, required: list[str]) -> bool:
    normalized = _normalize(unit)
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


def assess_baseline_equivalence(*, ledger: Mapping[str, Any], evolved_candidate_root: str | Path, validation: Mapping[str, Any]) -> dict[str, Any]:
    if ledger.get("schema_version") != "openevo.researchclawbench.baseline_achievement_ledger.v2":
        raise BaselineEquivalenceError("BASELINE_EQUIVALENCE_LEDGER_INVALID")
    achievements = ledger.get("achievements")
    if not isinstance(achievements, list) or not achievements:
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
    corpus = _text_corpus(root, files)
    findings: list[dict[str, Any]] = []
    for achievement in achievements:
        if not isinstance(achievement, dict) or achievement.get("preservation_priority") != "required":
            continue
        required = achievement.get("required_output_classes")
        if not isinstance(required, list):
            raise BaselineEquivalenceError("BASELINE_EQUIVALENCE_LEDGER_INVALID")
        output_presence = _outputs_present(files, required)
        method = _method_reconstructed(achievement, corpus, files)
        report_discussion = _report_discusses(achievement, report_text)
        evidence_unit = _report_evidence_unit(achievement, report_text)
        evidence_roles = bool(evidence_unit) and _report_output_roles_present(
            evidence_unit, required
        )
        verification_present = validation.get("artifact_valid") is True and any(
            marker in _normalize(evidence_unit)
            for marker in ("verify", "validation", "check", "compare", "reproduc")
        )
        conclusion_revised = any(
            marker in _normalize(evidence_unit)
            for marker in ("revised", "corrected", "updated conclusion", "different conclusion")
        )
        corresponding_evidence = (
            method
            and all(output_presence.values())
            and report_discussion
            and evidence_roles
        )
        passed = corresponding_evidence and verification_present
        findings.append({
            "achievement_id": achievement.get("achievement_id"),
            "method_reconstructed": method,
            "required_output_classes_present": output_presence,
            "corresponding_evidence_present": corresponding_evidence,
            "report_output_roles_present": evidence_roles,
            "report_discussion_present": report_discussion,
            "verification_assertion_present": verification_present,
            "candidate_conclusion_revised": conclusion_revised,
            "status": "PASS" if passed else "FAIL",
        })
    if not findings:
        raise BaselineEquivalenceError("BASELINE_EQUIVALENCE_REQUIRED_ACHIEVEMENTS_ABSENT")
    passed = all(item["status"] == "PASS" for item in findings)
    body = {
        "schema_version": EQUIVALENCE_SCHEMA,
        "status": "PASS" if passed else EQUIVALENCE_FAILURE,
        "required_achievement_count": len(findings),
        "reconstructed_required_achievement_count": sum(item["status"] == "PASS" for item in findings),
        "dropped_required_achievement_ids": [item["achievement_id"] for item in findings if item["status"] != "PASS"],
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
