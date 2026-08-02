from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest
import yaml

REPOSITORY = Path(__file__).resolve().parents[4]
SCRIPT = (
    REPOSITORY
    / "benchmarks/chembench/scripts/temperature_full_evolve_v1/gateway_service_bootstrap.py"
)


def _module():
    spec = importlib.util.spec_from_file_location(
        "temperature_full_evolve_gateway_bootstrap",
        SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _base_topology(path: Path, host_completion_root: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "rollout": {
                    "host": "127.0.0.1",
                    "port": 8080,
                    "public_url": "http://host.docker.internal:8080",
                    "save_dir": os.fspath(host_completion_root),
                },
                "gateway": {
                    "completion_persistence": {
                        "enabled": True,
                        "max_field_bytes": 16 * 1024 * 1024,
                        "queue_size": 128,
                    },
                    "nodes": [
                        {
                            "id": "core-gateway",
                            "host": "127.0.0.1",
                            "port": 8100,
                            "public_url": "http://127.0.0.1:8100",
                        }
                    ],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_effective_topology_rewrites_only_container_view(tmp_path: Path) -> None:
    module = _module()
    host_root = (tmp_path / "host-completions").resolve()
    container_root = Path("/openevo-temperature-completions")
    base = tmp_path / "base.yaml"
    _base_topology(base, host_root)
    mapping = {
        "schema_version": 1,
        "container_id": "a" * 64,
        "identity_digest": "b" * 64,
    }

    effective = module._effective_topology(
        base,
        mapping,
        host_completion_root=host_root,
        container_completion_root=container_root,
    )

    assert effective["rollout"]["save_dir"] == os.fspath(container_root)
    assert effective["rollout"]["host"] == "127.0.0.1"
    assert effective["rollout"]["public_url"] == (
        "http://host.docker.internal:8080"
    )
    assert effective["gateway"]["rollout_server_url"] == (
        "http://host.docker.internal:8080"
    )
    assert effective["gateway"]["nodes"][0]["docker_host_path"] == mapping
    persisted = yaml.safe_load(base.read_text(encoding="utf-8"))
    assert persisted["rollout"]["save_dir"] == os.fspath(host_root)
    assert module._ROLLOUT_CALLBACK_URL == (
        "http://host.docker.internal:8080/callbacks/session_result"
    )


def test_runtime_mount_evidence_excludes_separate_writable_completion_bind() -> None:
    module = _module()
    observed = {
        "id": "a" * 64,
        "hostname": "a" * 12,
        "running": True,
        "mounts": [
            {
                "Type": "bind",
                "Source": "/host/runtime",
                "Destination": "/openevo-temperature-runtime",
                "RW": True,
            },
            {
                "Type": "bind",
                "Source": "/host/completions",
                "Destination": "/openevo-temperature-completions",
                "RW": True,
            },
        ],
    }

    selected = json.loads(
        module._runtime_mount_inspect_evidence(
            json.dumps(observed, separators=(",", ":")).encode("ascii")
        )
    )

    assert selected == {**observed, "mounts": [observed["mounts"][0]]}


@pytest.mark.parametrize(
    "mounts",
    (
        [],
        [
            {
                "Type": "bind",
                "Source": "/host/other",
                "Destination": "/somewhere-else",
                "RW": True,
            }
        ],
        [
            {
                "Type": "bind",
                "Source": f"/host/runtime-{index}",
                "Destination": "/openevo-temperature-runtime",
                "RW": True,
            }
            for index in range(2)
        ],
    ),
)
def test_runtime_mount_evidence_fails_closed_without_one_exact_runtime_bind(
    mounts: list[dict[str, object]],
) -> None:
    module = _module()
    observed = {
        "id": "a" * 64,
        "hostname": "a" * 12,
        "running": True,
        "mounts": mounts,
    }

    with pytest.raises(RuntimeError, match="RUNTIME_MOUNT_INVALID"):
        module._runtime_mount_inspect_evidence(
            json.dumps(observed, separators=(",", ":")).encode("ascii")
        )


def test_effective_topology_rejects_unbound_or_disabled_persistence(
    tmp_path: Path,
) -> None:
    module = _module()
    host_root = (tmp_path / "host-completions").resolve()
    base = tmp_path / "base.yaml"
    _base_topology(base, host_root)
    loaded = yaml.safe_load(base.read_text(encoding="utf-8"))
    loaded["gateway"]["completion_persistence"]["enabled"] = False
    base.write_text(yaml.safe_dump(loaded), encoding="utf-8")

    with pytest.raises(RuntimeError, match="GATEWAY_BOOTSTRAP_TOPOLOGY_INVALID"):
        module._effective_topology(
            base,
            {},
            host_completion_root=host_root,
            container_completion_root=Path("/openevo-temperature-completions"),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("host", "0.0.0.0"),
        ("public_url", "http://127.0.0.1:8080"),
    ),
)
def test_effective_topology_rejects_callback_route_drift(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    module = _module()
    host_root = (tmp_path / "host-completions").resolve()
    base = tmp_path / "base.yaml"
    _base_topology(base, host_root)
    loaded = yaml.safe_load(base.read_text(encoding="utf-8"))
    loaded["rollout"][field] = value
    base.write_text(yaml.safe_dump(loaded), encoding="utf-8")

    with pytest.raises(RuntimeError, match="GATEWAY_BOOTSTRAP_TOPOLOGY_INVALID"):
        module._effective_topology(
            base,
            {},
            host_completion_root=host_root,
            container_completion_root=Path("/openevo-temperature-completions"),
        )


def test_gateway_bootstrap_has_no_benchmark_or_model_execution() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "codex exec" not in source
    assert "allow-paid" not in source
    assert "openevo.gateway.server" in source
