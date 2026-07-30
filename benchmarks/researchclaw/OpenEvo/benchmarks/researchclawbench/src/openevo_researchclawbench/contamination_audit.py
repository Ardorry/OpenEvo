"""Trusted offline Community/official contamination audit.

Target papers are treated as opaque byte strings.  The audit hashes them but
does not open, parse, render, OCR, or expose their contents to candidate or
reflector code.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import FROZEN_TASKS
from .hashing import canonical_json_sha256, sha256_file


_WORDS = re.compile(r"[A-Za-z0-9]+")
_TEXT_DATA_SUFFIXES = {".csv", ".tsv", ".txt", ".json", ".jsonl", ".yaml", ".yml"}
_SAMPLE_BYTES = 64 * 1024


@dataclass(frozen=True)
class AuditFile:
    relative_path: str
    size_bytes: int
    sha256: str
    suffix: str
    normalized_text_sha256: str | None
    sample_hashes: tuple[str, ...]


@dataclass(frozen=True)
class AuditTask:
    task_id: str
    cohort: str
    description_sha256: str
    description_tokens: frozenset[str]
    data_files: tuple[AuditFile, ...]
    related_work_files: tuple[AuditFile, ...]
    target_paper_sha256: str


def _files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return ()
    files = sorted(path for path in root.rglob("*") if path.is_file() and not path.is_symlink())
    return tuple(files)


def _audit_file(path: Path, root: Path) -> AuditFile:
    size = path.stat().st_size
    offsets = sorted({0, max(0, (size - _SAMPLE_BYTES) // 2), max(0, size - _SAMPLE_BYTES)})
    samples: list[str] = []
    with path.open("rb") as handle:
        for offset in offsets:
            handle.seek(offset)
            samples.append(canonical_json_sha256({"chunk": handle.read(_SAMPLE_BYTES).hex()}))
    normalized_text_sha256: str | None = None
    if path.suffix.casefold() in _TEXT_DATA_SUFFIXES:
        try:
            normalized = re.sub(r"\s+", "", path.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            normalized = ""
        if normalized:
            normalized_text_sha256 = canonical_json_sha256({"normalized_text": normalized})
    return AuditFile(
        relative_path=path.relative_to(root).as_posix(),
        size_bytes=size,
        sha256=sha256_file(path),
        suffix=path.suffix.casefold(),
        normalized_text_sha256=normalized_text_sha256,
        sample_hashes=tuple(samples),
    )


def _audited_files(root: Path) -> tuple[AuditFile, ...]:
    return tuple(_audit_file(path, root) for path in _files(root))


def _load_task(task_dir: Path, cohort: str) -> AuditTask:
    info = json.loads((task_dir / "task_info.json").read_text(encoding="utf-8"))
    description = str(info.get("task") or "")
    paper = task_dir / "target_study" / "paper.pdf"
    if not paper.is_file() or paper.is_symlink():
        raise ValueError(f"target paper is unavailable for opaque hashing: {task_dir.name}")
    return AuditTask(
        task_id=task_dir.name,
        cohort=cohort,
        description_sha256=canonical_json_sha256({"task": description}),
        description_tokens=frozenset(word.casefold() for word in _WORDS.findall(description)),
        data_files=_audited_files(task_dir / "data"),
        related_work_files=_audited_files(task_dir / "related_work"),
        target_paper_sha256=sha256_file(paper),
    )


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    union = left | right
    return 0.0 if not union else len(left & right) / len(union)


def _near_data_match(left: AuditFile, right: AuditFile) -> tuple[str, float] | None:
    if left.sha256 == right.sha256 or left.suffix != right.suffix:
        return None
    if (
        left.normalized_text_sha256 is not None
        and left.normalized_text_sha256 == right.normalized_text_sha256
    ):
        return "normalized_text_identity", 1.0
    if not left.sample_hashes or len(left.sample_hashes) != len(right.sample_hashes):
        return None
    size_ratio = min(left.size_bytes, right.size_bytes) / max(left.size_bytes, right.size_bytes, 1)
    matching = sum(a == b for a, b in zip(left.sample_hashes, right.sample_hashes, strict=True))
    similarity = matching / len(left.sample_hashes)
    if size_ratio >= 0.9 and len(left.sample_hashes) >= 3 and similarity >= (2 / 3):
        return "three_window_byte_similarity", round(similarity, 6)
    return None


def run_offline_contamination_audit(tasks_root: str | Path) -> dict[str, Any]:
    root = Path(tasks_root).resolve(strict=True)
    community_ids = set(FROZEN_TASKS)
    task_dirs = sorted(
        path for path in root.iterdir()
        if path.is_dir() and (path / "task_info.json").is_file()
    )
    tasks = [
        _load_task(path, "community" if path.name in community_ids else "official")
        for path in task_dirs
    ]
    community = [task for task in tasks if task.cohort == "community"]
    official = [task for task in tasks if task.cohort == "official"]
    if {task.task_id for task in community} != community_ids:
        raise ValueError("the frozen 17 Community tasks are not all present")
    if len(official) != 40:
        raise ValueError("official cohort is not exactly 40 tasks")

    paper_index: dict[str, list[AuditTask]] = defaultdict(list)
    data_index: dict[str, list[tuple[AuditTask, str, int]]] = defaultdict(list)
    related_index: dict[str, list[tuple[AuditTask, str, int]]] = defaultdict(list)
    for task in tasks:
        paper_index[task.target_paper_sha256].append(task)
        for file in task.data_files:
            data_index[file.sha256].append((task, file.relative_path, file.size_bytes))
        for file in task.related_work_files:
            related_index[file.sha256].append((task, file.relative_path, file.size_bytes))

    def cross_pairs(index: dict[str, list[Any]], *, task_getter) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        for digest, values in sorted(index.items()):
            left = sorted({task_getter(value).task_id for value in values if task_getter(value).cohort == "community"})
            right = sorted({task_getter(value).task_id for value in values if task_getter(value).cohort == "official"})
            if left and right:
                findings.append({"sha256": digest, "community_tasks": left, "official_tasks": right})
        return findings

    paper_matches = cross_pairs(paper_index, task_getter=lambda value: value)
    data_matches = cross_pairs(data_index, task_getter=lambda value: value[0])
    related_matches = cross_pairs(related_index, task_getter=lambda value: value[0])
    near_data_matches: list[dict[str, Any]] = []
    for left in community:
        for right in official:
            for left_file in left.data_files:
                for right_file in right.data_files:
                    match = _near_data_match(left_file, right_file)
                    if match is not None:
                        method, similarity = match
                        near_data_matches.append(
                            {
                                "community_task": left.task_id,
                                "community_path": left_file.relative_path,
                                "official_task": right.task_id,
                                "official_path": right_file.relative_path,
                                "method": method,
                                "similarity": similarity,
                            }
                        )
    description_matches = [
        {
            "community_task": left.task_id,
            "official_task": right.task_id,
            "token_jaccard": round(_jaccard(left.description_tokens, right.description_tokens), 6),
        }
        for left in community
        for right in official
        if _jaccard(left.description_tokens, right.description_tokens) >= 0.9
    ]
    # Exact target-paper or data reuse is a hard block. Related-work reuse is
    # evidence to inspect, not by itself target leakage.
    blocking = bool(paper_matches or data_matches or near_data_matches or description_matches)
    payload = {
        "schema_version": "1.0.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "community_task_count": len(community),
            "official_task_count": len(official),
            "community_task_ids": [task.task_id for task in community],
        },
        "target_study_handling": {
            "paper_pdf_byte_hash_only": True,
            "paper_content_opened_or_parsed": False,
            "checklist_opened": False,
            "target_images_opened": False,
            "doi_title_source": "PUBLIC_TASK_INFO_ONLY",
            "doi_title_available": False,
        },
        "findings": {
            "exact_target_paper_matches": paper_matches,
            "exact_data_file_matches": data_matches,
            "conservative_near_data_file_matches": near_data_matches,
            "near_duplicate_task_descriptions": description_matches,
            "cross_cohort_related_work_matches": related_matches,
        },
        "status": "COMMUNITY_OFFICIAL_CONTAMINATION_RISK" if blocking else "PASS",
        "model_execution_allowed": not blocking,
    }
    payload["receipt_sha256"] = canonical_json_sha256(payload)
    return payload
