"""Clean duplicated task inputs from CLI evaluation batches."""

from __future__ import annotations

import argparse
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .config import WORKSPACES_DIR


CLEAN_TARGETS = ("data", "related_work")
CLI_WORKSPACE_GROUP = "cli_runs"


@dataclass
class PathStats:
    bytes: int = 0
    files: int = 0
    dirs: int = 0

    def add(self, other: "PathStats") -> None:
        self.bytes += other.bytes
        self.files += other.files
        self.dirs += other.dirs


@dataclass
class CleanupSummary:
    cli_root: Path
    before_bytes: int
    after_bytes: int
    reclaimed_bytes: int
    batches: int
    runs: int
    runs_with_targets: int
    targets: int
    files: int
    dirs: int
    dry_run: bool


def _format_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(max(size, 0))
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def _path_stats(path: Path) -> PathStats:
    if not path.exists() and not path.is_symlink():
        return PathStats()
    try:
        root_stat = os.lstat(path)
    except OSError:
        return PathStats()
    if path.is_symlink() or path.is_file():
        return PathStats(bytes=root_stat.st_size, files=1)

    stats = PathStats(bytes=root_stat.st_size, dirs=1)
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stats.bytes += entry_stat.st_size
                        stats.dirs += 1
                        stack.append(entry.path)
                    else:
                        stats.bytes += entry_stat.st_size
                        stats.files += 1
        except OSError:
            continue
    return stats


def _is_cli_batch_dir(path: Path) -> bool:
    return path.is_dir() and path.name.startswith("cli_")


def _is_cli_run_dir(path: Path) -> bool:
    return path.is_dir() and path.name.startswith("cli_") and (path / "_meta.json").is_file()


def _iter_batch_dirs(cli_root: Path, batch_ids: Iterable[str] | None = None) -> list[Path]:
    if not cli_root.is_dir():
        return []
    requested = {batch_id.strip() for batch_id in batch_ids or [] if batch_id.strip()}
    if requested:
        candidates = [cli_root / batch_id for batch_id in sorted(requested)]
    else:
        candidates = sorted(cli_root.iterdir())
    return [path for path in candidates if _is_cli_batch_dir(path)]


def _iter_run_dirs(batch_dirs: Iterable[Path]) -> list[Path]:
    run_dirs: list[Path] = []
    for batch_dir in batch_dirs:
        try:
            children = sorted(batch_dir.iterdir())
        except OSError:
            continue
        run_dirs.extend(path for path in children if _is_cli_run_dir(path))
    return run_dirs


def _cleanup_targets(run_dir: Path) -> list[Path]:
    return [run_dir / name for name in CLEAN_TARGETS if (run_dir / name).exists() or (run_dir / name).is_symlink()]


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def clean_cli_inputs(
    *,
    workspaces_dir: Path = WORKSPACES_DIR,
    batch_ids: Iterable[str] | None = None,
    dry_run: bool = True,
) -> CleanupSummary:
    cli_root = workspaces_dir / CLI_WORKSPACE_GROUP
    batch_dirs = _iter_batch_dirs(cli_root, batch_ids)
    run_dirs = _iter_run_dirs(batch_dirs)

    before = PathStats()
    targets: list[Path] = []
    runs_with_targets = 0
    for run_dir in run_dirs:
        run_targets = _cleanup_targets(run_dir)
        if run_targets:
            runs_with_targets += 1
        for target in run_targets:
            before.add(_path_stats(target))
            targets.append(target)

    if not dry_run:
        for target in targets:
            _remove_path(target)
        after = PathStats()
        for target in targets:
            after.add(_path_stats(target))
        after_bytes = after.bytes
        reclaimed_bytes = max(before.bytes - after_bytes, 0)
    else:
        reclaimed_bytes = before.bytes
        after_bytes = 0

    return CleanupSummary(
        cli_root=cli_root,
        before_bytes=before.bytes,
        after_bytes=after_bytes,
        reclaimed_bytes=reclaimed_bytes,
        batches=len(batch_dirs),
        runs=len(run_dirs),
        runs_with_targets=runs_with_targets,
        targets=len(targets),
        files=before.files,
        dirs=before.dirs,
        dry_run=dry_run,
    )


def _print_summary(summary: CleanupSummary) -> None:
    mode = "DRY RUN" if summary.dry_run else "CLEANED"
    print(f"RCB CLI input cleanup: {mode}")
    print(f"CLI root: {summary.cli_root}")
    print(f"Batches scanned: {summary.batches}")
    print(f"Runs scanned: {summary.runs}")
    print(f"Runs with duplicated inputs: {summary.runs_with_targets}")
    print(f"Cleanup targets: {summary.targets}")
    print(f"Files counted: {summary.files}")
    print(f"Directories counted: {summary.dirs}")
    print(f"Duplicated inputs before cleanup: {_format_bytes(summary.before_bytes)}")
    print(f"Duplicated inputs after cleanup: {_format_bytes(summary.after_bytes)}")
    print(f"Reclaimable: {_format_bytes(summary.reclaimed_bytes)}" if summary.dry_run else f"Reclaimed: {_format_bytes(summary.reclaimed_bytes)}")
    if summary.dry_run:
        print("No files were deleted. Re-run with --yes to delete duplicated CLI task inputs.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Delete duplicated task inputs from ResearchClawBench CLI evaluation batches."
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete duplicated inputs. Without this flag, rcb-clear only prints a dry-run summary.",
    )
    parser.add_argument(
        "--batch",
        action="append",
        default=[],
        help="Limit cleanup to a specific CLI batch id. Can be passed multiple times.",
    )
    parser.add_argument(
        "--workspaces-dir",
        type=Path,
        default=WORKSPACES_DIR,
        help="Workspace root containing cli_runs/. Defaults to the repository workspaces directory.",
    )
    args = parser.parse_args(argv)
    summary = clean_cli_inputs(
        workspaces_dir=args.workspaces_dir,
        batch_ids=args.batch,
        dry_run=not args.yes,
    )
    _print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
