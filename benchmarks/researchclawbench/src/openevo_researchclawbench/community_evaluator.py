"""Evaluator-only scorer subprocess; raw details remain in the private tree."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import FROZEN_TASKS


class EvaluatorError(RuntimeError):
    """Community evaluator failed or returned malformed output."""


def run_community_evaluator(
    *,
    project_root: str | Path,
    workspace: str | Path,
    evaluator_private_root: str | Path,
    task_id: str,
    attempt_id: str,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    if task_id not in FROZEN_TASKS:
        raise EvaluatorError("evaluator task is outside the frozen Community Dev sequence")
    source = Path(workspace).resolve(strict=True)
    private_root = Path(evaluator_private_root).resolve(strict=True)
    destination = private_root / attempt_id / "workspace"
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True)
    shutil.copytree(source, destination, symlinks=False)
    raw_path = destination.parent / "raw_community_score.json"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": str(Path(project_root).resolve(strict=True)),
        "JUDGE_API_KEY": os.environ.get("JUDGE_API_KEY", ""),
        "JUDGE_API_BASE": os.environ.get("JUDGE_API_BASE", ""),
        "JUDGE_MODEL_NAME": os.environ.get("JUDGE_MODEL_NAME", ""),
    }
    if not all(env[name] for name in ("JUDGE_API_KEY", "JUDGE_API_BASE", "JUDGE_MODEL_NAME")):
        raise EvaluatorError("judge environment is incomplete")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "openevo_researchclawbench.community_evaluator",
            "--worker",
            "--project-root",
            str(Path(project_root).resolve(strict=True)),
            "--workspace",
            str(destination),
            "--raw-output",
            str(raw_path),
        ],
        text=True,
        capture_output=True,
        env=env,
        timeout=timeout_seconds,
        check=False,
    )
    (destination.parent / "evaluator_stdout.log").write_text(proc.stdout, encoding="utf-8")
    (destination.parent / "evaluator_stderr.log").write_text(proc.stderr, encoding="utf-8")
    if proc.returncode != 0 or not raw_path.is_file():
        raise EvaluatorError(f"community scorer failed with exit code {proc.returncode}")
    payload = json.loads(raw_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "error" in payload or type(payload.get("total_score")) not in {int, float}:
        raise EvaluatorError("community scorer output is invalid")
    return payload


def _worker(project_root: Path, workspace: Path, raw_output: Path) -> int:
    parent = str(project_root)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    from ResearchClawBench.evaluation.score import score_workspace

    payload = score_workspace(workspace)
    raw_output.parent.mkdir(parents=True, exist_ok=True)
    raw_output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if isinstance(payload, dict) and "error" not in payload else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--raw-output", type=Path)
    args = parser.parse_args(argv)
    if not args.worker or not all((args.project_root, args.workspace, args.raw_output)):
        parser.error("worker arguments are required")
    return _worker(args.project_root.resolve(strict=True), args.workspace.resolve(strict=True), args.raw_output.resolve(strict=False))


if __name__ == "__main__":
    raise SystemExit(main())
