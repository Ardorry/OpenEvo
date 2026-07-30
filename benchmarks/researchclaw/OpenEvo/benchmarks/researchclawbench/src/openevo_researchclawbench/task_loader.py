"""Public-task loading without target-study access."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import FROZEN_TASKS
from .hashing import UnsafePathError, ensure_within, safe_relative_path, sha256_file

TASK_ID_RE = re.compile(r"^[A-Za-z]+_[0-9]{3}$")


@dataclass(frozen=True)
class PublicTask:
    task_id: str
    task_dir: Path
    task_info: dict[str, Any]
    task_info_sha256: str
    declared_data_files: tuple[Path, ...]


def load_public_task(
    root: str | Path,
    task_id: str,
    *,
    allowed_task_ids: tuple[str, ...] = FROZEN_TASKS,
) -> PublicTask:
    if (
        not allowed_task_ids
        or len(set(allowed_task_ids)) != len(allowed_task_ids)
        or any(TASK_ID_RE.fullmatch(item) is None for item in allowed_task_ids)
        or task_id not in allowed_task_ids
        or TASK_ID_RE.fullmatch(task_id) is None
    ):
        raise ValueError(f"task is not in the exact public-task allowlist: {task_id}")
    repo_root = Path(root).resolve(strict=True)
    task_dir = ensure_within(repo_root / "tasks" / task_id, repo_root)
    info_path = task_dir / "task_info.json"
    info = json.loads(info_path.read_text(encoding="utf-8"))
    if not isinstance(info, dict) or not isinstance(info.get("task"), str):
        raise TypeError("invalid public task_info.json")
    declared: list[Path] = []
    for item in info.get("data", []):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise TypeError("invalid data declaration")
        raw = item["path"].removeprefix("./")
        rel = safe_relative_path(raw)
        if rel.parts[0] != "data" or "target_study" in rel.parts:
            raise UnsafePathError(f"data path is outside public data/: {raw}")
        path = ensure_within(task_dir / rel, task_dir)
        if not path.is_file() or path.is_symlink():
            raise UnsafePathError(f"declared data file is unavailable or unsafe: {raw}")
        declared.append(path)
    return PublicTask(task_id, task_dir, info, sha256_file(info_path), tuple(declared))
