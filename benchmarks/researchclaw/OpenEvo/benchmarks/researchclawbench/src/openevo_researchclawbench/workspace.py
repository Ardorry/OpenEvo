"""Trusted official workspace construction and safety checks."""

from __future__ import annotations

import importlib
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import FROZEN_TASKS
from .hashing import UnsafePathError, ensure_within, iter_regular_files
from .task_loader import load_public_task


@dataclass(frozen=True)
class WorkspaceReceipt:
    task_id: str
    run_id: str
    workspace: Path
    candidate_workspace: Path
    instructions: Path
    data: Path
    related_work: Path
    code: Path
    outputs: Path
    report: Path


CANDIDATE_VISIBLE_ENTRIES = {
    "INSTRUCTIONS.md",
    "data",
    "related_work",
    "code",
    "outputs",
    "report",
    "artifacts",
    "skills",
}


def assert_candidate_workspace_shape(root: str | Path) -> None:
    workspace = Path(root).resolve(strict=True)
    actual = {path.name for path in workspace.iterdir()}
    if actual != CANDIDATE_VISIBLE_ENTRIES:
        raise UnsafePathError(
            "candidate workspace differs from the closed visible set: "
            f"missing={sorted(CANDIDATE_VISIBLE_ENTRIES - actual)}, "
            f"extra={sorted(actual - CANDIDATE_VISIBLE_ENTRIES)}"
        )
    for path in workspace.rglob("*"):
        if path.is_symlink():
            raise UnsafePathError(f"candidate workspace contains a symlink: {path}")
        lowered = {part.casefold() for part in path.parts}
        if lowered & {"target_study", "evaluator_private", "secrets", ".codex"}:
            raise UnsafePathError(f"candidate workspace contains hidden content: {path}")


def _build_sanitized_template(workspace: Path, template: Path) -> Path:
    """Create one immutable-input template per task, never a host bind mount."""

    if template.exists():
        assert_candidate_workspace_shape(template)
        return template
    temporary = template.with_name(f".{template.name}.building")
    if temporary.exists():
        raise FileExistsError(f"unfinished candidate template exists: {temporary}")
    temporary.mkdir(parents=True)
    # Any failure deliberately leaves the named .building directory as
    # fail-closed audit evidence.
    shutil.copy2(workspace / "INSTRUCTIONS.md", temporary / "INSTRUCTIONS.md")
    for name in ("data", "related_work"):
        shutil.copytree(workspace / name, temporary / name, symlinks=False)
    for name in ("code", "outputs", "artifacts", "skills"):
        (temporary / name).mkdir()
    (temporary / "report" / "images").mkdir(parents=True)
    assert_candidate_workspace_shape(temporary)
    template.parent.mkdir(parents=True, exist_ok=True)
    temporary.rename(template)
    return template


def _load_official_task_runner(rcb_root: Path):
    parent = str(rcb_root.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    module = importlib.import_module(f"{rcb_root.name}.evaluation.run_task")
    origin = Path(module.__file__).resolve(strict=True)
    ensure_within(origin, rcb_root)
    return module.TaskRunner


def build_official_workspace(
    rcb_root: str | Path,
    task_id: str,
    run_root: str | Path,
    run_id: str,
    *,
    allowed_task_ids: tuple[str, ...] = tuple(FROZEN_TASKS),
) -> WorkspaceReceipt:
    if not run_id.startswith(f"{task_id}_a") or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for char in run_id):
        raise ValueError("unsafe or mismatched run_id")
    repo_root = Path(rcb_root).resolve(strict=True)
    public_task = load_public_task(
        repo_root,
        task_id,
        allowed_task_ids=allowed_task_ids,
    )
    root = Path(run_root).resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    workspace = ensure_within(root / run_id, root)
    if workspace.exists() and any(workspace.iterdir()):
        raise FileExistsError(f"run workspace is not fresh: {workspace}")

    TaskRunner = _load_official_task_runner(repo_root)
    runner = TaskRunner(task_id, agent_cmd="ADAPTER_ISOLATED_RUNTIME", agent_name="OpenEvo-RCB-v0")
    runner.task_dir = public_task.task_dir
    runner.run_id = run_id
    runner.timestamp = run_id.rsplit("_", 1)[-1]
    runner.workspace = workspace
    runner.meta_path = workspace / "_meta.json"
    runner.output_path = workspace / "_agent_output.jsonl"
    runner.instructions_path = workspace / "INSTRUCTIONS.md"
    runner.setup_workspace()

    expected = {
        "INSTRUCTIONS.md", "_meta.json", "data", "related_work", "code", "outputs", "report"
    }
    actual = {path.name for path in workspace.iterdir()}
    if not actual.issubset(expected) or "target_study" in actual:
        raise UnsafePathError(f"unexpected official workspace entries: {sorted(actual - expected)}")
    for directory in (workspace / "data", workspace / "related_work"):
        if directory.exists():
            list(iter_regular_files(directory))
    meta = json.loads((workspace / "_meta.json").read_text(encoding="utf-8"))
    if meta.get("run_id") != run_id or meta.get("task_id") != task_id:
        raise ValueError("official workspace metadata mismatch")
    candidate_workspace = _build_sanitized_template(
        workspace,
        root.parent / "sanitized_workspaces" / task_id,
    )
    return WorkspaceReceipt(
        task_id=task_id,
        run_id=run_id,
        workspace=workspace,
        candidate_workspace=candidate_workspace,
        instructions=workspace / "INSTRUCTIONS.md",
        data=workspace / "data",
        related_work=workspace / "related_work",
        code=workspace / "code",
        outputs=workspace / "outputs",
        report=workspace / "report",
    )
