from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

_HEX40 = re.compile(r"[0-9a-f]{40}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_FIXTURE_SCHEMA = "chemcrow_fixture_lock_v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _run_git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", os.fspath(repository), *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git command failed"
        raise ValueError(detail)
    return result.stdout.strip()


def _safe_relative_path(value: object) -> PurePosixPath:
    if not isinstance(value, str):
        raise TypeError("fixture path must be a string")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ValueError(f"fixture path is not a normalized relative path: {value!r}")
    return path


def load_fixture_lock(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != _FIXTURE_SCHEMA:
        raise ValueError("fixture lock schema differs")
    repositories = value.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise ValueError("fixture lock must contain repositories")
    names: set[str] = set()
    for item in repositories:
        if not isinstance(item, dict):
            raise TypeError("fixture repository entry must be an object")
        name = item.get("name")
        commit = item.get("commit")
        root_tree = item.get("root_tree")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name)
            or name in names
            or not isinstance(item.get("url"), str)
            or not str(item["url"]).startswith("https://github.com/")
            or not isinstance(commit, str)
            or _HEX40.fullmatch(commit) is None
            or not isinstance(root_tree, str)
            or _HEX40.fullmatch(root_tree) is None
            or not isinstance(item.get("required_for"), list)
            or not isinstance(item.get("redistribution"), dict)
            or not isinstance(item.get("required_paths"), list)
        ):
            raise ValueError(f"fixture repository lock is malformed: {name!r}")
        names.add(name)
        for required in item["required_paths"]:
            if not isinstance(required, dict):
                raise TypeError("fixture required path must be an object")
            _safe_relative_path(required.get("path"))
            if required.get("kind") not in {"file", "tree"}:
                raise ValueError("fixture required path kind differs")
            object_id = required.get("git_object")
            if object_id is not None and (
                not isinstance(object_id, str) or _HEX40.fullmatch(object_id) is None
            ):
                raise ValueError("fixture Git object ID differs")
            sha256 = required.get("sha256")
            if sha256 is not None and (
                not isinstance(sha256, str) or _HEX64.fullmatch(sha256) is None
            ):
                raise ValueError("fixture SHA-256 differs")
    return value


def _validate_fixture(repository: Path, locked: dict[str, Any]) -> dict[str, Any]:
    if not repository.is_dir() or not (repository / ".git").exists():
        raise FileNotFoundError(f"fixture repository is absent: {locked['name']}")
    head = _run_git(repository, "rev-parse", "HEAD")
    if head != locked["commit"]:
        raise ValueError(f"fixture commit differs: {locked['name']}")
    root_tree = _run_git(repository, "rev-parse", "HEAD^{tree}")
    if root_tree != locked["root_tree"]:
        raise ValueError(f"fixture root tree differs: {locked['name']}")
    origin = _run_git(repository, "config", "--get", "remote.origin.url")
    if origin.rstrip("/") != str(locked["url"]).rstrip("/"):
        raise ValueError(f"fixture origin differs: {locked['name']}")
    if _run_git(repository, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError(f"fixture worktree is not clean: {locked['name']}")

    checked: list[dict[str, Any]] = []
    for required in locked["required_paths"]:
        relative = _safe_relative_path(required["path"])
        candidate = repository.joinpath(*relative.parts)
        kind = required["kind"]
        if (
            candidate.is_symlink()
            or (kind == "file" and not candidate.is_file())
            or (kind == "tree" and not candidate.is_dir())
        ):
            raise ValueError(f"fixture required {kind} is absent: {locked['name']}:{relative}")
        object_id = required.get("git_object")
        if object_id is not None:
            observed = _run_git(repository, "rev-parse", f"HEAD:{relative.as_posix()}")
            if observed != object_id:
                raise ValueError(f"fixture Git object differs: {locked['name']}:{relative}")
        sha256 = required.get("sha256")
        if sha256 is not None:
            observed_sha256 = _sha256(candidate)
            if observed_sha256 != sha256:
                hint = (
                    " (install Git LFS and run git lfs pull)"
                    if locked.get("requires_git_lfs")
                    else ""
                )
                raise ValueError(f"fixture file hash differs: {locked['name']}:{relative}{hint}")
        size_bytes = required.get("size_bytes")
        if size_bytes is not None and candidate.stat().st_size != size_bytes:
            raise ValueError(f"fixture file size differs: {locked['name']}:{relative}")
        checked.append(
            {
                "path": relative.as_posix(),
                "kind": kind,
                "git_object": object_id,
                "sha256": sha256,
                "size_bytes": size_bytes,
            }
        )
    return {
        "name": locked["name"],
        "url": locked["url"],
        "commit": head,
        "root_tree": root_tree,
        "required_paths": checked,
        "redistribution": locked["redistribution"],
        "status": "VERIFIED",
    }


def _clone_fixture(destination: Path, locked: dict[str, Any]) -> None:
    if destination.exists():
        raise FileExistsError(f"fixture destination already exists: {destination}")
    if locked.get("requires_git_lfs") and shutil.which("git-lfs") is None:
        raise ValueError("Git LFS is required; install git-lfs before fetching RXN fixtures")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{locked['name']}-", dir=os.fspath(destination.parent))
    )
    try:
        subprocess.run(
            [
                "git",
                "-c",
                "credential.helper=",
                "clone",
                "--no-checkout",
                "--filter=blob:none",
                str(locked["url"]),
                os.fspath(temporary),
            ],
            check=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            timeout=600,
        )
        if locked.get("requires_git_lfs"):
            subprocess.run(
                ["git", "-C", os.fspath(temporary), "lfs", "install", "--local"],
                check=True,
                timeout=30,
            )
        subprocess.run(
            ["git", "-C", os.fspath(temporary), "checkout", "--detach", str(locked["commit"])],
            check=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            timeout=900,
        )
        if locked.get("requires_git_lfs"):
            subprocess.run(
                ["git", "-C", os.fspath(temporary), "lfs", "pull"],
                check=True,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                timeout=900,
            )
        _validate_fixture(temporary, locked)
        os.rename(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def prepare_or_validate_fixtures(
    *,
    lock_path: Path,
    destination_root: Path,
    check_only: bool,
    required_for: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    lock = load_fixture_lock(lock_path)
    destination_root = destination_root.resolve()
    selected = [
        item
        for item in lock["repositories"]
        if not required_for or required_for.intersection(item["required_for"])
    ]
    if not selected:
        raise ValueError("fixture selection is empty")
    results: list[dict[str, Any]] = []
    for item in selected:
        destination = destination_root / item["name"]
        if not destination.exists() and not check_only:
            _clone_fixture(destination, item)
        results.append(_validate_fixture(destination, item))
    return {
        "schema_version": "chemcrow_fixture_verification_v1",
        "status": "PASS",
        "check_only": check_only,
        "python": sys.version.split()[0],
        "lock_sha256": _sha256(lock_path),
        "required_for": sorted(required_for),
        "repositories": results,
    }
