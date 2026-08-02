#!/usr/bin/env python3
"""Build the private Gateway topology for Temperature full-evolve v1.

The host Rollout and the managed Gateway container deliberately receive two
different path spellings for the same owner-private persistence directory.
The host topology contains the absolute host path.  This bootstrap verifies
that value and rewrites it to the fixed container mount before starting the
official Gateway server.  It performs no benchmark or model invocation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import yaml
from openevo.runtime.docker_host import (
    DockerEngineAuthority,
    discover_docker_host_path,
    docker_self_inspect_argv,
)

_SCHEMA = "TemperatureFullEvolveGatewayBootstrapReceiptV1"
_ROLLOUT_CONTROL_URL = "http://host.docker.internal:8080"
_GATEWAY_BIND_HOST = "0.0.0.0"
_GATEWAY_PUBLIC_URL = "http://127.0.0.1:8100"
_CONTAINER_COMPLETION_ROOT = "/openevo-temperature-completions"
_ROOT_MARKER_NAME = ".openevo-completion-root.json"
_SHA256_RE_LENGTH = 64


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-config", type=Path, required=True)
    parser.add_argument("--effective-config", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--runtime-python", type=Path, required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--host-completion-root", type=Path, required=True)
    parser.add_argument("--container-completion-root", type=Path, required=True)
    parser.add_argument("--completion-root-marker-sha256", required=True)
    arguments = parser.parse_args()

    base = arguments.base_config.resolve(strict=True)
    effective = _new_private_path(arguments.effective_config)
    receipt = _new_private_path(arguments.receipt)
    runtime_python = _validated_runtime_python(arguments.runtime_python)
    host_completion_root = _validated_host_path(arguments.host_completion_root)
    container_completion_root = _validated_completion_root(
        arguments.container_completion_root,
        expected_marker_sha256=arguments.completion_root_marker_sha256,
    )

    authority = DockerEngineAuthority.open()
    inspected = subprocess.run(
        docker_self_inspect_argv(),
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=10,
        env=authority.environment(),
        cwd="/",
    )
    authority.verify()
    mapping = discover_docker_host_path(
        inspected.stdout,
        namespace=arguments.namespace,
    )
    topology = _effective_topology(
        base,
        mapping.model_dump(mode="json"),
        host_completion_root=host_completion_root,
        container_completion_root=container_completion_root,
    )
    encoded = yaml.safe_dump(
        topology,
        allow_unicode=False,
        default_flow_style=False,
        sort_keys=False,
    ).encode("utf-8")
    _exclusive_private_write(effective, encoded)

    completion_identity = _directory_identity(container_completion_root)
    receipt_payload = {
        "schema_version": _SCHEMA,
        "host_topology_sha256": _sha256_file(base),
        "effective_topology_sha256": hashlib.sha256(encoded).hexdigest(),
        "docker_engine_identity_sha256": authority.identity_digest,
        "docker_host_path_identity_sha256": mapping.identity_digest,
        "container_id": mapping.container_id,
        "completion_root_marker_sha256": arguments.completion_root_marker_sha256,
        "container_completion_root": _CONTAINER_COMPLETION_ROOT,
        "container_completion_root_identity_sha256": completion_identity,
    }
    _exclusive_private_write(
        receipt,
        (json.dumps(receipt_payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "ascii"
        ),
    )
    os.execve(
        runtime_python,
        (
            os.fspath(runtime_python),
            "-I",
            "-m",
            "openevo.gateway.server",
            "--config",
            os.fspath(effective),
            "--log-level",
            "warning",
            "--node-id",
            "core-gateway",
        ),
        {
            "HOME": "/home/openevo",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONNOUSERSITE": "1",
        },
    )


def _effective_topology(
    base: Path,
    docker_host_path: dict[str, object],
    *,
    host_completion_root: Path,
    container_completion_root: Path,
) -> dict[str, object]:
    loaded = yaml.safe_load(base.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise TypeError("GATEWAY_BOOTSTRAP_TOPOLOGY_INVALID")
    rollout = loaded.get("rollout")
    gateway = loaded.get("gateway")
    nodes = gateway.get("nodes") if isinstance(gateway, dict) else None
    persistence = gateway.get("completion_persistence") if isinstance(gateway, dict) else None
    if (
        not isinstance(rollout, dict)
        or rollout.get("save_dir") != os.fspath(host_completion_root)
        or not isinstance(nodes, list)
        or len(nodes) != 1
        or not isinstance(nodes[0], dict)
        or not isinstance(persistence, dict)
        or persistence.get("enabled") is not True
    ):
        raise RuntimeError("GATEWAY_BOOTSTRAP_TOPOLOGY_INVALID")
    node = nodes[0]
    if node.get("id") != "core-gateway" or node.get("public_url") != _GATEWAY_PUBLIC_URL:
        raise RuntimeError("GATEWAY_BOOTSTRAP_TOPOLOGY_INVALID")
    rollout["save_dir"] = os.fspath(container_completion_root)
    gateway["rollout_server_url"] = _ROLLOUT_CONTROL_URL
    node["host"] = _GATEWAY_BIND_HOST
    node["docker_host_path"] = docker_host_path
    return loaded


def _validated_host_path(path: Path) -> Path:
    if not path.is_absolute() or "\n" in os.fspath(path) or "\x00" in os.fspath(path):
        raise RuntimeError("GATEWAY_BOOTSTRAP_COMPLETION_ROOT_INVALID")
    return path


def _validated_completion_root(path: Path, *, expected_marker_sha256: str) -> Path:
    if (
        os.fspath(path) != _CONTAINER_COMPLETION_ROOT
        or len(expected_marker_sha256) != _SHA256_RE_LENGTH
        or any(character not in "0123456789abcdef" for character in expected_marker_sha256)
    ):
        raise RuntimeError("GATEWAY_BOOTSTRAP_COMPLETION_ROOT_INVALID")
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat(follow_symlinks=False)
        marker = resolved / _ROOT_MARKER_NAME
        marker_metadata = marker.stat(follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError("GATEWAY_BOOTSTRAP_COMPLETION_ROOT_INVALID") from exc
    if (
        resolved != path
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or not stat.S_ISREG(marker_metadata.st_mode)
        or marker_metadata.st_uid != os.geteuid()
        or marker_metadata.st_nlink != 1
        or stat.S_IMODE(marker_metadata.st_mode) != 0o600
        or _sha256_file(marker) != expected_marker_sha256
    ):
        raise RuntimeError("GATEWAY_BOOTSTRAP_COMPLETION_ROOT_INVALID")
    return resolved


def _validated_runtime_python(path: Path) -> Path:
    if not path.is_absolute():
        raise RuntimeError("GATEWAY_BOOTSTRAP_RUNTIME_INVALID")
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat(follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError("GATEWAY_BOOTSTRAP_RUNTIME_INVALID") from exc
    if not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK):
        raise RuntimeError("GATEWAY_BOOTSTRAP_RUNTIME_INVALID")
    return path


def _new_private_path(path: Path) -> Path:
    if not path.is_absolute():
        raise RuntimeError("GATEWAY_BOOTSTRAP_PATH_INVALID")
    parent = path.parent.resolve(strict=True)
    metadata = parent.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or path.exists()
    ):
        raise RuntimeError("GATEWAY_BOOTSTRAP_PATH_INVALID")
    return parent / path.name


def _directory_identity(path: Path) -> str:
    metadata = path.stat(follow_symlinks=False)
    payload = {
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mode": stat.S_IMODE(metadata.st_mode),
        "owner_uid": metadata.st_uid,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _exclusive_private_write(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
