from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

REPOSITORY = Path(__file__).resolve().parents[4]
SCRIPT = (
    REPOSITORY
    / "benchmarks/chembench/scripts/supervised_transfer_v2/gateway_service_bootstrap_v2.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("gateway_service_bootstrap_v2", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_effective_gateway_topology_binds_verified_docker_host_path(tmp_path: Path) -> None:
    base = tmp_path / "base.yaml"
    base.write_text(
        yaml.safe_dump(
            {
                "rollout": {"host": "127.0.0.1", "port": 8080},
                "gateway": {
                    "nodes": [
                        {
                            "id": "core-gateway",
                            "host": "127.0.0.1",
                            "port": 8100,
                            "public_url": "http://127.0.0.1:8100",
                        }
                    ]
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    docker_host_path = {
        "schema_version": 1,
        "container_id": "a" * 64,
        "identity_digest": "b" * 64,
    }

    effective = _module()._effective_topology(base, docker_host_path)

    gateway = effective["gateway"]
    node = gateway["nodes"][0]
    assert gateway["rollout_server_url"] == "http://host.docker.internal:8080"
    assert node["host"] == "0.0.0.0"
    assert node["public_url"] == "http://127.0.0.1:8100"
    assert node["docker_host_path"] == docker_host_path


def test_gateway_bootstrap_source_has_no_model_execution() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert "codex exec" not in source
    assert "allow-paid" not in source
    assert "openevo.gateway.server" in source
