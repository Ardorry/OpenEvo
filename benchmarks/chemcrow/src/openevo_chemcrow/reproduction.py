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

import yaml
from openevo.runtime.managed import (
    MANAGED_RUNTIME_ARCHIVE_RELEASE,
    MANAGED_RUNTIME_LABEL,
    MANAGED_RUNTIME_LABEL_VALUE,
    verify_managed_runtime_archive,
)

from .paper_evaluator import FROZEN_PAPER_TASK_IDS
from .submission import REPRODUCTION_CONTRACT, TASK_MANIFEST_SHA256

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
        if candidate.is_symlink() or (kind == "file" and not candidate.is_file()) or (
            kind == "tree" and not candidate.is_dir()
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
                hint = " (install Git LFS and run git lfs pull)" if locked.get(
                    "requires_git_lfs"
                ) else ""
                raise ValueError(
                    f"fixture file hash differs: {locked['name']}:{relative}{hint}"
                )
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
            subprocess.run(["git", "-C", os.fspath(temporary), "lfs", "install", "--local"],
                           check=True, timeout=30)
        subprocess.run(
            ["git", "-C", os.fspath(temporary), "checkout", "--detach", str(locked["commit"])],
            check=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            timeout=900,
        )
        if locked.get("requires_git_lfs"):
            subprocess.run(["git", "-C", os.fspath(temporary), "lfs", "pull"],
                           check=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, timeout=900)
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


def verify_publication(package_root: Path) -> dict[str, Any]:
    package_root = package_root.resolve()
    artifact_root = package_root / "artifacts"
    manifest_path = artifact_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "chemcrow_public_evolution_artifacts_v1":
        raise ValueError("public artifact manifest schema differs")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != manifest.get("artifact_count"):
        raise ValueError("public artifact manifest count differs")
    seen_paths: set[str] = set()
    counts: dict[str, int] = {}
    total_bytes = 0
    for item in artifacts:
        if not isinstance(item, dict):
            raise TypeError("public artifact entry must be an object")
        relative = _safe_relative_path(item.get("path"))
        if relative.as_posix() in seen_paths:
            raise ValueError("public artifact path is duplicated")
        seen_paths.add(relative.as_posix())
        path = artifact_root.joinpath(*relative.parts)
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"public artifact is not a regular file: {relative}")
        size = path.stat().st_size
        digest = _sha256(path)
        if size != item.get("size_bytes") or digest != item.get("sha256"):
            raise ValueError(f"public artifact bytes differ: {relative}")
        if digest != item.get("content_sha256") or size != item.get("content_bytes"):
            raise ValueError(f"public artifact source binding differs: {relative}")
        artifact_type = str(item.get("artifact_type"))
        counts[artifact_type] = counts.get(artifact_type, 0) + 1
        total_bytes += size
    if counts != manifest.get("artifact_count_by_type"):
        raise ValueError("public artifact type counts differ")

    sums: dict[str, str] = {}
    for line in (artifact_root / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, separator, relative = line.partition("  ")
        if separator != "  " or _HEX64.fullmatch(digest) is None:
            raise ValueError("public SHA256SUMS entry is malformed")
        _safe_relative_path(relative)
        if relative in sums:
            raise ValueError("public SHA256SUMS path is duplicated")
        sums[relative] = digest
    expected_sums = {str(item["path"]): str(item["sha256"]) for item in artifacts}
    if sums != expected_sums:
        raise ValueError("public SHA256SUMS differs from manifest")

    provenance_path = package_root / "reports/full_v8_public_20260831/provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    if provenance.get("schema_version") != "chemcrow_full_v8_public_provenance_v1":
        raise ValueError("public provenance schema differs")
    report_root = provenance_path.parent
    for item in provenance.get("public_files", []):
        relative = _safe_relative_path(item.get("relative_path"))
        path = report_root.joinpath(*relative.parts)
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != item.get("size_bytes")
            or _sha256(path) != item.get("sha256")
        ):
            raise ValueError(f"public result file differs: {relative}")
    return {
        "schema_version": "chemcrow_publication_verification_v1",
        "status": "PASS",
        "artifact_count": len(artifacts),
        "artifact_count_by_type": counts,
        "artifact_bytes": total_bytes,
        "artifact_manifest_sha256": _sha256(manifest_path),
        "result_provenance_sha256": _sha256(provenance_path),
        "private_runtime_state_bundled": bool(manifest.get("private_runtime_state_bundled")),
        "independent_full_audit_possible": bool(
            provenance.get("projection", {}).get(
                "independent_full_audit_possible_from_public_package_alone"
            )
        ),
    }


def check_reproduction_environment(
    *,
    package_root: Path,
    config_path: Path,
    fixture_lock_path: Path,
    fixture_root: Path,
    toolchain_lock_path: Path,
    runtime_archive: Path | None = None,
) -> dict[str, Any]:
    package_root = package_root.resolve()
    config_path = config_path.resolve()
    toolchain = json.loads(toolchain_lock_path.read_text(encoding="utf-8"))
    if toolchain.get("schema_version") != "chemcrow_reproduction_toolchain_v1":
        raise ValueError("reproduction toolchain lock schema differs")
    uv_probe = subprocess.run(
        ["uv", "--version"], check=False, capture_output=True, text=True, timeout=20
    )
    uv_output = uv_probe.stdout.strip()
    uv_parts = uv_output.split()
    uv_ok = (
        uv_probe.returncode == 0
        and len(uv_parts) >= 2
        and uv_parts[0] == "uv"
        and uv_parts[1] == toolchain["uv"]
    )
    python_ok = sys.version.split()[0] == toolchain["python"]
    dependency_lock_path = (
        toolchain_lock_path.parent / str(toolchain["package_lock"])
    ).resolve()
    dependency_lock_ok = (
        dependency_lock_path.is_file()
        and dependency_lock_path.stat().st_size == toolchain["package_lock_size_bytes"]
        and _sha256(dependency_lock_path) == toolchain["package_lock_sha256"]
    )

    raw_config_text = config_path.read_text(encoding="utf-8")
    raw_config = yaml.safe_load(raw_config_text)
    if not isinstance(raw_config, dict):
        raise TypeError("portable reproduction config must be an object")
    host_path_leaks = sorted(
        set(
            re.findall(
                r"(?<![A-Za-z0-9._-])/home/[A-Za-z0-9._-]+",
                raw_config_text,
            )
        )
    )
    required_env = sorted(
        set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", raw_config_text))
    )
    configured_manifest = raw_config.get("task_manifest")
    manifest_path = (
        (config_path.parent / configured_manifest).resolve()
        if isinstance(configured_manifest, str)
        else None
    )
    manifest_task_ids: list[str] = []
    task_manifest_ok = False
    if manifest_path is not None and manifest_path.is_file():
        manifest_task_ids = [
            str(json.loads(line).get("task_id"))
            for line in manifest_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        task_manifest_ok = (
            _sha256(manifest_path) == TASK_MANIFEST_SHA256
            and tuple(manifest_task_ids) == FROZEN_PAPER_TASK_IDS
        )
    portable_config_ok = (
        not host_path_leaks
        and raw_config.get("schema_version") == "chemcrow_experiment_v8"
        and raw_config.get("execution_scope") == "full_g1_g2_pair"
        and raw_config.get("artifact_protocol") == "three_isolated_core_native_v2"
        and raw_config.get("task_ids") == "all"
        and raw_config.get("random_seed") == REPRODUCTION_CONTRACT["random_seed"]
        and raw_config.get("reproduction_contract") == REPRODUCTION_CONTRACT
        and task_manifest_ok
    )
    fixture_receipt = prepare_or_validate_fixtures(
        lock_path=fixture_lock_path.resolve(),
        destination_root=fixture_root.resolve(),
        check_only=True,
        required_for=frozenset({"tests", "controlled_chemical_data"}),
    )
    publication_receipt = verify_publication(package_root)

    docker_binary = shutil.which("docker")
    docker_cli_version: str | None = None
    docker_cli_ok = False
    docker_server_version: str | None = None
    docker_server_ready = False
    runtime_image_ready = False
    runtime_image_id: str | None = None
    if docker_binary:
        client = subprocess.run(
            [docker_binary, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        client_match = re.fullmatch(
            r"Docker version ([0-9]+(?:\.[0-9]+){2}), build .+",
            client.stdout.strip(),
        )
        if client.returncode == 0 and client_match is not None:
            docker_cli_version = client_match.group(1)
            docker_cli_ok = docker_cli_version == toolchain["docker"]["cli"]
        server = subprocess.run(
            [docker_binary, "info", "--format", "{{.ServerVersion}}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        docker_server_version = server.stdout.strip() or None
        docker_server_ready = (
            server.returncode == 0
            and docker_server_version == toolchain["docker"]["engine"]
        )
        if docker_server_ready:
            image = str(toolchain["managed_runtime"]["oci_index_id"])
            inspect = subprocess.run(
                [
                    docker_binary,
                    "image",
                    "inspect",
                    image,
                    "--format",
                    f'{{{{.Id}}}}|{{{{index .Config.Labels "{MANAGED_RUNTIME_LABEL}"}}}}',
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
            parts = inspect.stdout.strip().split("|", 1)
            runtime_image_id = parts[0] if parts and parts[0] else None
            runtime_image_ready = (
                inspect.returncode == 0
                and len(parts) == 2
                and parts[0] == image
                and parts[1] == MANAGED_RUNTIME_LABEL_VALUE
            )
    archive_receipt: dict[str, Any] | None = None
    if runtime_archive is not None:
        authority = verify_managed_runtime_archive(
            runtime_archive.resolve(), require_private=False
        )
        archive_sha256 = _sha256(runtime_archive.resolve())
        archive_size = runtime_archive.resolve().stat().st_size
        expected_runtime = toolchain["managed_runtime"]
        archive_verified = (
            archive_sha256 == expected_runtime["archive_sha256"]
            and archive_size == expected_runtime["archive_size_bytes"]
            and authority.platform == expected_runtime["platform"]
            and authority.config_id == expected_runtime["config_id"]
            and authority.oci_index_id == expected_runtime["oci_index_id"]
        )
        if not archive_verified:
            raise ValueError("managed runtime archive differs from reproduction lock")
        archive_receipt = {
            "path_supplied": True,
            "sha256": archive_sha256,
            "size_bytes": archive_size,
            "platform": authority.platform,
            "config_id": authority.config_id,
            "oci_index_id": authority.oci_index_id,
            "verified": True,
        }
    else:
        archive_receipt = {
            "path_supplied": False,
            "expected_filename": MANAGED_RUNTIME_ARCHIVE_RELEASE.filename,
            "expected_sha256": MANAGED_RUNTIME_ARCHIVE_RELEASE.sha256,
            "expected_size_bytes": MANAGED_RUNTIME_ARCHIVE_RELEASE.byte_size,
            "verified": False,
        }
    offline_ready = bool(
        python_ok
        and uv_ok
        and dependency_lock_ok
        and portable_config_ok
        and fixture_receipt["status"] == "PASS"
        and publication_receipt["status"] == "PASS"
    )
    runtime_ready = docker_cli_ok and docker_server_ready and runtime_image_ready
    return {
        "schema_version": "chemcrow_reproduction_environment_check_v1",
        "status": "READY" if offline_ready and runtime_ready else "BLOCKED",
        "model_calls": 0,
        "paid_operations": 0,
        "offline_ready": offline_ready,
        "runtime_ready": runtime_ready,
        "checks": {
            "python": {
                "expected": toolchain["python"],
                "observed": sys.version.split()[0],
                "pass": python_ok,
            },
            "uv": {
                "expected": toolchain["uv"],
                "observed": uv_output or None,
                "pass": uv_ok,
            },
            "dependency_lock": {
                "path": str(toolchain["package_lock"]),
                "sha256": (
                    _sha256(dependency_lock_path) if dependency_lock_path.is_file() else None
                ),
                "size_bytes": (
                    dependency_lock_path.stat().st_size
                    if dependency_lock_path.is_file()
                    else None
                ),
                "pass": dependency_lock_ok,
            },
            "portable_config": {
                "pass": portable_config_ok,
                "host_path_leaks": host_path_leaks,
                "required_environment": required_env,
                "task_manifest_pass": task_manifest_ok,
                "task_ids": manifest_task_ids,
            },
            "fixtures": fixture_receipt,
            "publication": publication_receipt,
            "docker": {
                "binary_present": docker_binary is not None,
                "cli_expected": toolchain["docker"]["cli"],
                "cli_observed": docker_cli_version,
                "cli_pass": docker_cli_ok,
                "engine_expected": toolchain["docker"]["engine"],
                "engine_observed": docker_server_version,
                "server_ready": docker_server_ready,
                "managed_runtime_image_ready": runtime_image_ready,
                "managed_runtime_image_id": runtime_image_id,
            },
            "managed_runtime_archive": archive_receipt,
        },
    }
