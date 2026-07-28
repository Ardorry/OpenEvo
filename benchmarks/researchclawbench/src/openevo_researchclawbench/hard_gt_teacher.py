"""Evaluator-only hard-GT teacher and task-local overlay boundaries."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

from .artifact_loader import ABSOLUTE_PATH, EVALUATOR_TERMS, TASK_LITERAL
from .hashing import sha256_bytes


TeacherMode = Literal["HARD_GT", "SOFT_JUDGE", "MIXED"]
_DOI = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.I)
_HIGH_PRECISION_NUMBER = re.compile(r"(?<![A-Za-z0-9])[-+]?\d+\.\d{5,}(?:[eE][-+]?\d+)?")


@dataclass(frozen=True)
class TeacherPrivateResult:
    task_id: str
    attempt_id: str
    mode: TeacherMode
    corrections: tuple[str, ...]
    correct_values: tuple[str, ...]
    tolerances: tuple[str, ...]
    expected_structure: tuple[str, ...]
    failed_tests: tuple[str, ...]
    missing_fields: tuple[str, ...]

    @classmethod
    def from_private_payload(cls, payload: dict[str, Any]) -> "TeacherPrivateResult":
        expected = {
            "task_id", "attempt_id", "mode", "corrections", "correct_values",
            "tolerances", "expected_structure", "failed_tests", "missing_fields",
        }
        if set(payload) != expected or payload.get("mode") not in {"HARD_GT", "SOFT_JUDGE", "MIXED"}:
            raise ValueError("teacher private payload is not closed")
        values: dict[str, tuple[str, ...]] = {}
        for name in expected - {"task_id", "attempt_id", "mode"}:
            raw = payload[name]
            if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
                raise ValueError(f"teacher field {name} must contain strings")
            values[name] = tuple(raw)
        return cls(
            task_id=str(payload["task_id"]),
            attempt_id=str(payload["attempt_id"]),
            mode=payload["mode"],
            **values,
        )


@dataclass(frozen=True)
class TaskLocalOverlay:
    task_id: str
    source_attempt_id: str
    text: str
    sha256: str


def build_task_local_overlay(teacher: TeacherPrivateResult) -> TaskLocalOverlay:
    """Render private training truth for the next attempt of the same task only."""

    sections = (
        ("Corrections", teacher.corrections),
        ("Correct values", teacher.correct_values),
        ("Tolerances", teacher.tolerances),
        ("Expected structure", teacher.expected_structure),
        ("Failed tests", teacher.failed_tests),
        ("Missing fields", teacher.missing_fields),
    )
    lines = [f"Teacher mode: {teacher.mode}"]
    for heading, values in sections:
        if values:
            lines.extend([f"### {heading}", *(f"- {value}" for value in values)])
    text = "\n".join(lines).rstrip() + "\n"
    return TaskLocalOverlay(
        task_id=teacher.task_id,
        source_attempt_id=teacher.attempt_id,
        text=text,
        sha256=sha256_bytes(text.encode("utf-8")),
    )


@dataclass(frozen=True)
class GlobalLeakScan:
    passed: bool
    reasons: tuple[str, ...]


def scan_global_artifact(
    text: str,
    *,
    public_file_names: Iterable[str] = (),
    correct_answer_literals: Iterable[str] = (),
    target_entities: Iterable[str] = (),
    prior_report_sentences: Iterable[str] = (),
) -> GlobalLeakScan:
    """Reject, rather than redact, any task-local/evaluator-derived revision."""

    reasons: list[str] = []
    if TASK_LITERAL.search(text):
        reasons.append("TASK_ID_LITERAL")
    if EVALUATOR_TERMS.search(text):
        reasons.append("EVALUATOR_TERM")
    if ABSOLUTE_PATH.search(text):
        reasons.append("ABSOLUTE_PATH")
    if _DOI.search(text):
        reasons.append("TARGET_DOI")
    if _HIGH_PRECISION_NUMBER.search(text):
        reasons.append("HIGH_PRECISION_VALUE")
    lowered = text.casefold()
    literal_groups = (
        ("PUBLIC_FILE_NAME", public_file_names),
        ("CORRECT_ANSWER_LITERAL", correct_answer_literals),
        ("TARGET_ENTITY", target_entities),
        ("PRIOR_REPORT_SENTENCE", prior_report_sentences),
    )
    for reason, values in literal_groups:
        if any(value.strip() and value.casefold() in lowered for value in values):
            reasons.append(reason)
    return GlobalLeakScan(not reasons, tuple(sorted(set(reasons))))


def require_global_artifact_clean(scan: GlobalLeakScan) -> None:
    if not scan.passed:
        raise ValueError("GLOBAL_ARTIFACT_TEACHER_LEAK:" + ",".join(scan.reasons))


def write_overlay_receipt(path: str | Path, overlay: TaskLocalOverlay) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(
            {
                "task_id": overlay.task_id,
                "source_attempt_id": overlay.source_attempt_id,
                "overlay_sha256": overlay.sha256,
                "lifecycle": "CURRENT_TASK_ONLY",
                "global_artifact_eligible": False,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def assert_overlay_transition(overlay: TaskLocalOverlay | None, next_task_id: str) -> None:
    if overlay is not None and overlay.task_id != next_task_id:
        raise RuntimeError("TASK_LOCAL_OVERLAY_CROSS_TASK_TRANSFER_REJECTED")


def native_teacher_attachment_capability() -> dict[str, Any]:
    return {
        "hard_gt_classification": True,
        "task_local_overlay": True,
        "global_leak_scanner": True,
        "post_run_feedback_attachment_to_native_dataset": True,
        "session_completed_immutable": True,
        "evaluator_authority": "CORE_PROCESS_LOCAL_CAPABILITY",
        "production_evolution_http_transport": False,
        "production_science_successor_hook": False,
        "status": "PROCESS_LOCAL_ATTACHMENT_AVAILABLE_PRODUCTION_TRANSPORT_GAP",
    }
