"""Thin current-task GT adapter for native OpenEvo feedback attachments."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from typing import Any

from .config import FROZEN_TASKS, ExperimentConfig

_MAX_GT_BYTES = 1 << 20
_MAX_JSON_DEPTH = 24
_MAX_JSON_NODES = 16_384


def _validate_json_budget(value: Any) -> None:
    pending = [(value, 1)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES or depth > _MAX_JSON_DEPTH:
            raise ValueError("current-task GT exceeds the closed structure budget")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("current-task GT object keys must be strings")
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif item is not None and type(item) not in {str, int, float, bool}:
            raise ValueError("current-task GT contains an unsupported JSON value")


def load_current_task_gt_supervision(
    config: ExperimentConfig,
    *,
    task_id: str,
) -> dict[str, Any]:
    """Load one canonical checklist as a bounded HARD_GT task-local layer."""

    if task_id not in FROZEN_TASKS:
        raise ValueError("current-task GT request is outside the frozen inventory")
    tasks_root = (config.researchclawbench_root / "tasks").resolve(strict=True)
    path = tasks_root / task_id / "target_study" / "checklist.json"
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(tasks_root) or resolved != path:
        raise ValueError("current-task GT authority escapes the canonical task root")
    metadata = os.stat(resolved, follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size <= 0
        or metadata.st_size > _MAX_GT_BYTES
    ):
        raise ValueError("current-task GT authority is unsafe or oversized")
    payload = resolved.read_bytes()
    if len(payload) != metadata.st_size:
        raise ValueError("current-task GT changed while it was read")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("current-task GT is not valid UTF-8 JSON") from exc
    if not isinstance(value, list) or not value:
        raise ValueError("current-task GT must be a non-empty JSON list")
    _validate_json_budget(value)
    digest = hashlib.sha256(payload).hexdigest()
    task_local_feedback = {
        "feedback_source": "current_task_gt",
        "ground_truth_sha256": digest,
        "ground_truth_entries": value,
        "judge_feedback_included": False,
    }
    return {
        "schema_version": "openevo.researchclawbench.current_task_gt.v1",
        "task_id": task_id,
        "ground_truth_sha256": digest,
        "ground_truth_size_bytes": len(payload),
        "feedback_class": "HARD_GT",
        "global_feedback": {},
        "task_local_feedback": task_local_feedback,
        "judge_feedback_included": False,
    }


__all__ = ["load_current_task_gt_supervision"]
