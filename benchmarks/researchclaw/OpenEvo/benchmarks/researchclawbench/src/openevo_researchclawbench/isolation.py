"""Static candidate-view policy; execution isolation belongs to OpenEvo Core."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .hashing import UnsafePathError
from .workspace import CANDIDATE_VISIBLE_ENTRIES, assert_candidate_workspace_shape


FORBIDDEN_VISIBLE_NAMES = {
    "target_study",
    "checklist.json",
    "_score.json",
    "evaluator_private",
    "secrets",
    ".codex",
}


def validate_candidate_view(root: str | Path) -> dict[str, object]:
    workspace = Path(root).resolve(strict=True)
    assert_candidate_workspace_shape(workspace)
    visible = sorted(path.name for path in workspace.iterdir())
    lowered = {name.casefold() for name in visible}
    if lowered & {name.casefold() for name in FORBIDDEN_VISIBLE_NAMES}:
        raise UnsafePathError("candidate workspace exposes a hidden evaluator entry")
    return {
        "visible_entries": visible,
        "allowed_entries": sorted(CANDIDATE_VISIBLE_ENTRIES),
        "target_study_visible": False,
        "project_root_mounted": False,
        "host_home_mounted": False,
        "docker_socket_mounted": False,
        "managed_by": "OpenEvo Core managed runtime",
    }


def reject_forbidden_prepare_sources(paths: Iterable[str | Path]) -> None:
    for raw in paths:
        path = Path(raw).resolve(strict=False)
        lowered = {part.casefold() for part in path.parts}
        if lowered & {name.casefold() for name in FORBIDDEN_VISIBLE_NAMES}:
            raise UnsafePathError(f"hidden evaluator path rejected: {path.name}")
        if any("chembench" in part.casefold() for part in path.parts):
            raise UnsafePathError("unrelated experiment source rejected")
