"""Fail-closed removal of task-specific cross-task artifact content."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .artifact_loader import ABSOLUTE_PATH, EVALUATOR_TERMS, LONG_RESULT_LITERAL, TASK_LITERAL


@dataclass(frozen=True)
class SanitizationResult:
    accepted: bool
    sanitized_text: str
    removed: tuple[str, ...]
    reasons: tuple[str, ...]
    diff: str


def sanitize_text(
    text: str,
    *,
    source_file_names: Iterable[str] = (),
    source_entities: Iterable[str] = (),
    previous_report_sentences: Iterable[str] = (),
) -> SanitizationResult:
    forbidden = {
        value.strip().casefold()
        for value in (*source_file_names, *source_entities, *previous_report_sentences)
        if len(value.strip()) >= 8
    }
    kept: list[str] = []
    removed: list[str] = []
    for line in text.splitlines():
        lowered = line.casefold()
        suspicious = (
            bool(TASK_LITERAL.search(line))
            or bool(EVALUATOR_TERMS.search(line))
            or bool(ABSOLUTE_PATH.search(line))
            or bool(LONG_RESULT_LITERAL.search(line))
            or any(value in lowered for value in forbidden)
        )
        if suspicious:
            removed.append(line)
        else:
            kept.append(line)
    sanitized = "\n".join(kept).strip()
    if sanitized:
        sanitized += "\n"
    residual: list[str] = []
    for label, pattern in (
        ("TASK_ID_LITERAL", TASK_LITERAL),
        ("EVALUATOR_PRIVATE_TERM", EVALUATOR_TERMS),
        ("ABSOLUTE_PATH", ABSOLUTE_PATH),
        ("LONG_NUMERIC_LITERAL", LONG_RESULT_LITERAL),
    ):
        if pattern.search(sanitized):
            residual.append(label)
    diff = "".join(
        difflib.unified_diff(
            text.splitlines(keepends=True),
            sanitized.splitlines(keepends=True),
            fromfile="before_sanitization",
            tofile="after_sanitization",
        )
    )
    return SanitizationResult(not residual, sanitized, tuple(removed), tuple(residual), diff)


def public_source_file_names(task_info: dict) -> tuple[str, ...]:
    names: list[str] = []
    for item in task_info.get("data", []):
        if isinstance(item, dict):
            for key in ("name", "path"):
                if isinstance(item.get(key), str):
                    names.append(Path(item[key]).name)
    return tuple(sorted(set(names)))
