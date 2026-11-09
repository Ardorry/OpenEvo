"""Read-only Harbor Hub metadata snapshot and immutable TB2.1 manifest creation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tomllib
from typing import Any
from urllib.request import urlopen

from harbor.models.dataset.manifest import DatasetManifest
from harbor.models.task.config import TaskConfig
from harbor.publisher.packager import Packager
from harbor.registry.client.package import PackageDatasetClient

from openevo_terminal_bench.campaign import build_task_manifest


OFFICIAL_DATASET = "terminal-bench/terminal-bench-2-1"
OFFICIAL_DATASET_REVISION = 6
OFFICIAL_DATASET_VERSION = "2.0.2"
OFFICIAL_DATASET_REF = (
    "sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a"
)
OFFICIAL_SOURCE_COMMIT = "dde3cd95b80ff25af5abd99a80b6513a018ad3b4"
OFFICIAL_DATASET_TOML_SHA256 = (
    "d90b4389992d07ed6f4ab8de963a70241eaa4b60072eeaec4c3b261b6c4a6dd8"
)
OFFICIAL_DATASET_TOML_URL = (
    "https://raw.githubusercontent.com/harbor-framework/terminal-bench-2-1/"
    f"{OFFICIAL_SOURCE_COMMIT}/tasks/dataset.toml"
)


async def fetch_tb21_metadata() -> dict[str, Any]:
    metadata = await PackageDatasetClient().get_dataset_metadata(
        f"{OFFICIAL_DATASET}@{OFFICIAL_DATASET_REF}"
    )
    return {
        "requested_ref": f"{OFFICIAL_DATASET}@{OFFICIAL_DATASET_REF}",
        "dataset_version": metadata.version,
        "dataset_version_id": metadata.dataset_version_id,
        "tasks": [
            {"name": task.get_name(), "ref": task.ref}
            for task in metadata.task_ids
        ],
    }


def parse_official_source_manifest(payload: bytes) -> dict[str, Any]:
    """Parse the pinned source manifest used only as a Registry cross-check."""
    digest = hashlib.sha256(payload).hexdigest()
    if digest != OFFICIAL_DATASET_TOML_SHA256:
        raise ValueError("official source dataset.toml hash mismatch")
    parsed = tomllib.loads(payload.decode("utf-8"))
    tasks = parsed.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 89:
        raise ValueError("official source manifest must contain 89 tasks")
    normalized = []
    for item in tasks:
        name, ref = item.get("name"), item.get("digest")
        if not isinstance(name, str) or not isinstance(ref, str):
            raise ValueError("invalid source task entry")
        normalized.append({"name": name, "ref": ref})
    return {
        "dataset_version": OFFICIAL_DATASET_REF,
        "dataset_version_id": f"revision:{OFFICIAL_DATASET_REVISION}",
        "tasks": normalized,
        "source_commit": OFFICIAL_SOURCE_COMMIT,
        "source_manifest_sha256": digest,
    }


def fetch_official_source_manifest(*, timeout_seconds: float = 20.0) -> dict[str, Any]:
    with urlopen(OFFICIAL_DATASET_TOML_URL, timeout=timeout_seconds) as response:
        payload = response.read(1024 * 1024 + 1)
    if len(payload) > 1024 * 1024:
        raise ValueError("official source manifest exceeded 1 MiB")
    return parse_official_source_manifest(payload)


def verify_registry_source_agreement(
    registry: dict[str, Any], source: dict[str, Any]
) -> None:
    if registry.get("dataset_version") != OFFICIAL_DATASET_REF:
        raise ValueError("Registry did not resolve the canonical TB2.1 dataset digest")
    registry_tasks = sorted((item["name"], item["ref"]) for item in registry["tasks"])
    source_tasks = sorted((item["name"], item["ref"]) for item in source["tasks"])
    if registry_tasks != source_tasks:
        raise ValueError("Registry task digests differ from the pinned source manifest")


def verify_local_tb21_mirror(dataset_path: Path) -> dict[str, Any]:
    """Verify a fixed Git checkout and every local task against dataset.toml."""
    if not dataset_path.is_absolute() or dataset_path.is_symlink():
        raise ValueError("local dataset path must be absolute and not a symlink")
    dataset_path = dataset_path.resolve(strict=True)
    manifest_path = dataset_path / "dataset.toml"
    if not manifest_path.is_file():
        raise ValueError("local TB2.1 mirror is missing dataset.toml")
    source = parse_official_source_manifest(manifest_path.read_bytes())
    repository = dataset_path.parent
    head = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()
    if head != OFFICIAL_SOURCE_COMMIT:
        raise ValueError("local TB2.1 mirror is not at the pinned source commit")
    status = subprocess.run(
        ["git", "-C", str(repository), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    if status:
        raise ValueError("local TB2.1 mirror has modified or untracked files")

    manifest = DatasetManifest.from_toml_file(manifest_path)
    expected = {item["name"]: item["ref"] for item in source["tasks"]}
    local: dict[str, Path] = {}
    for child in sorted(dataset_path.iterdir()):
        config_path = child / "task.toml"
        if not child.is_dir() or not config_path.is_file():
            continue
        config = TaskConfig.model_validate_toml(config_path.read_text(encoding="utf-8"))
        if config.task is None or not config.task.name:
            raise ValueError(f"local task has no canonical name: {child}")
        if child.name != config.task.name.removeprefix("terminal-bench/"):
            raise ValueError(f"local task directory/name mismatch: {child}")
        if config.task.name in local:
            raise ValueError(f"duplicate local task name: {config.task.name}")
        local[config.task.name] = child
    if len(local) != 89 or set(local) != set(expected):
        raise ValueError("local TB2.1 mirror does not contain the exact 89 tasks")
    for name, path in local.items():
        content_hash, _ = Packager.compute_content_hash(path)
        if f"sha256:{content_hash}" != expected[name]:
            raise ValueError(f"local task digest mismatch: {name}")

    return {
        "dataset_version": OFFICIAL_DATASET_REF,
        "dataset_version_id": f"local-git:{OFFICIAL_SOURCE_COMMIT}",
        "registry_verified": False,
        "local_mirror_verified": True,
        "tasks": source["tasks"],
        "provenance": {
            "source_commit": OFFICIAL_SOURCE_COMMIT,
            "source_manifest_sha256": OFFICIAL_DATASET_TOML_SHA256,
            "hub_dataset_digest_identity": OFFICIAL_DATASET_REF,
            "harbor_local_manifest_digest": f"sha256:{manifest.compute_content_hash()}",
            "local_dataset_path": str(dataset_path),
            "all_task_digests_match": True,
            "git_clean": True,
        },
    }


def write_tb21_local_manifest(
    output: Path, historical_failed_path: Path, dataset_path: Path
) -> dict[str, Any]:
    """Write an exclusive manifest for an exact, fully hashed local mirror."""
    baseline = json.loads(historical_failed_path.read_text(encoding="utf-8"))
    metadata = verify_local_tb21_mirror(dataset_path)
    manifest = build_task_manifest(metadata, historical_failed=baseline["tasks"])
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    return manifest


def write_tb21_manifest(output: Path, historical_failed_path: Path) -> dict[str, Any]:
    baseline = json.loads(historical_failed_path.read_text(encoding="utf-8"))
    registry_metadata = asyncio.run(fetch_tb21_metadata())
    source_metadata = fetch_official_source_manifest()
    verify_registry_source_agreement(registry_metadata, source_metadata)
    registry_metadata["provenance"] = {
        "hub_dataset_revision": OFFICIAL_DATASET_REVISION,
        "hub_dataset_version": OFFICIAL_DATASET_VERSION,
        "source_commit": source_metadata["source_commit"],
        "source_manifest_sha256": source_metadata["source_manifest_sha256"],
        "registry_source_task_digests_match": True,
    }
    manifest = build_task_manifest(
        registry_metadata,
        historical_failed=baseline["tasks"],
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


__all__ = [
    "OFFICIAL_DATASET",
    "OFFICIAL_DATASET_REF",
    "OFFICIAL_DATASET_REVISION",
    "OFFICIAL_DATASET_VERSION",
    "OFFICIAL_SOURCE_COMMIT",
    "fetch_official_source_manifest",
    "fetch_tb21_metadata",
    "parse_official_source_manifest",
    "verify_local_tb21_mirror",
    "verify_registry_source_agreement",
    "write_tb21_local_manifest",
    "write_tb21_manifest",
]
