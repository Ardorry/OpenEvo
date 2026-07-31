#!/usr/bin/env python3
"""Download mirrored ResearchClawBench tasks from Hugging Face."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path, PurePosixPath
from typing import Any


DEFAULT_REPO_ID = "InternScience/ResearchClawBench"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download mirrored ResearchClawBench tasks from the Hugging Face "
            "dataset repo while preserving the tasks/<TaskID>/... directory layout."
        )
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--all",
        action="store_true",
        help="Download every mirrored task currently available on Hugging Face.",
    )
    mode.add_argument(
        "--task",
        action="append",
        dest="tasks",
        metavar="TASK_ID",
        help="Download a specific task. Repeat this flag to download multiple tasks.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help=(
            "Destination tasks directory. Files are written under "
            "<output-dir>/<TaskID>/..."
        ),
    )
    parser.add_argument(
        "--repo-id",
        default=DEFAULT_REPO_ID,
        help=f"Hugging Face dataset repo id. Default: {DEFAULT_REPO_ID}",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="Optional Hugging Face cache directory if the default cache path is not writable.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN"),
        help="Optional Hugging Face token for private or rate-limited access.",
    )
    return parser.parse_args()


def collect_task_files(api: Any, repo_id: str, token: str | None) -> dict[str, list[str]]:
    task_files: dict[str, list[str]] = {}
    for remote_path in api.list_repo_files(repo_id=repo_id, repo_type="dataset", token=token):
        parts = PurePosixPath(remote_path).parts
        if len(parts) < 2 or parts[0] != "tasks":
            continue
        task_files.setdefault(parts[1], []).append(remote_path)
    for task_id in task_files:
        task_files[task_id].sort()
    return task_files


def resolve_requested_tasks(
    available: dict[str, list[str]], all_tasks: bool, requested: list[str] | None
) -> list[str]:
    if all_tasks:
        return sorted(available)

    assert requested is not None
    unique_requested = []
    seen = set()
    for task_id in requested:
        if task_id not in seen:
            unique_requested.append(task_id)
            seen.add(task_id)

    missing = [task_id for task_id in unique_requested if task_id not in available]
    if missing:
        available_text = ", ".join(sorted(available)) or "(none)"
        raise SystemExit(
            "Unknown task(s): "
            + ", ".join(missing)
            + "\nAvailable tasks: "
            + available_text
        )
    return unique_requested


def copy_repo_file(
    repo_id: str,
    token: str | None,
    remote_path: str,
    output_dir: Path,
    cache_dir: str | None,
) -> None:
    from huggingface_hub import hf_hub_download

    cached_path = Path(
        hf_hub_download(
            repo_id=repo_id,
            repo_type="dataset",
            filename=remote_path,
            token=token,
            cache_dir=cache_dir,
        )
    )
    destination = output_dir / Path(*PurePosixPath(remote_path).parts[1:])
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(cached_path, destination)


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.cache_dir:
        cache_dir = str(Path(args.cache_dir).expanduser().resolve())
        os.environ.setdefault("HF_HOME", cache_dir)
        os.environ.setdefault("HF_HUB_CACHE", str(Path(cache_dir) / "hub"))
        os.environ.setdefault("HF_XET_CACHE", str(Path(cache_dir) / "xet"))
        os.environ.setdefault("HF_ASSETS_CACHE", str(Path(cache_dir) / "assets"))

    from huggingface_hub import HfApi

    api = HfApi(token=args.token)
    available = collect_task_files(api, args.repo_id, args.token)
    if not available:
        raise SystemExit(
            f"No mirrored tasks were found in dataset repo {args.repo_id!r}."
        )

    selected_tasks = resolve_requested_tasks(available, args.all, args.tasks)

    total_files = sum(len(available[task_id]) for task_id in selected_tasks)
    print(f"Repo: {args.repo_id}")
    print(f"Tasks directory: {output_dir}")
    print(f"Tasks: {', '.join(selected_tasks)}")
    print(f"Files to download: {total_files}")

    downloaded = 0
    for task_id in selected_tasks:
        task_files = available[task_id]
        print(f"\n[{task_id}] {len(task_files)} file(s)")
        for remote_path in task_files:
            downloaded += 1
            print(f"  [{downloaded}/{total_files}] {remote_path}")
            copy_repo_file(
                args.repo_id,
                args.token,
                remote_path,
                output_dir,
                args.cache_dir,
            )

    print(f"\nDone. Task files were written under {output_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
