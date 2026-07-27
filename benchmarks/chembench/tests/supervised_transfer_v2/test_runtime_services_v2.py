from __future__ import annotations

from pathlib import Path

import pytest

from openevo_chembench.supervised_transfer_v2.runtime_services import (
    GatewayContainerIdentityV2,
    RuntimeProcessIdentityV2,
    RuntimeServicesV2Error,
    _runtime_metadata,
    _runtime_paths,
    _validate_runtime_inputs,
)

REPOSITORY = Path(__file__).resolve().parents[4]


def test_runtime_services_bind_the_isolated_noneditable_core_wheel() -> None:
    paths = _runtime_paths(REPOSITORY.resolve())
    _validate_runtime_inputs(paths)
    metadata = _runtime_metadata(paths)

    assert paths["runtime_python"] != REPOSITORY / ".venv/bin/python"
    assert metadata["openevo_version"] == "0.1.8"
    assert all(len(metadata[key]) == 64 for key in metadata if key.endswith("sha256"))


def test_runtime_process_receipt_is_closed_and_content_free() -> None:
    payload = {
        "service": "rollout",
        "pid": 100,
        "process_group_id": 100,
        "session_id": 100,
        "start_time_ticks": 200,
        "executable_sha256": "a" * 64,
        "cmdline_sha256": "b" * 64,
    }

    identity = RuntimeProcessIdentityV2.from_payload(payload)

    assert identity.payload == payload
    assert "cmdline" not in identity.payload
    assert "environment" not in identity.payload


def test_runtime_process_receipt_rejects_unknown_fields() -> None:
    payload = {
        "service": "gateway",
        "pid": 100,
        "process_group_id": 100,
        "session_id": 100,
        "start_time_ticks": 200,
        "executable_sha256": "a" * 64,
        "cmdline_sha256": "b" * 64,
        "credential": "forbidden",
    }

    with pytest.raises(RuntimeServicesV2Error, match="RUNTIME_SERVICE_RECEIPT_INVALID"):
        RuntimeProcessIdentityV2.from_payload(payload)


def test_gateway_container_receipt_is_closed_and_digest_bound() -> None:
    payload = {
        "container_name": "openevo-stv2-gateway-" + "a" * 24,
        "container_id": "b" * 64,
        "image_id": "sha256:" + "c" * 64,
        "effective_topology_sha256": "d" * 64,
        "docker_host_path_identity_sha256": "e" * 64,
        "docker_engine_identity_sha256": "f" * 64,
        "docker_launcher_sha256": "0" * 64,
    }

    identity = GatewayContainerIdentityV2.from_payload(payload)

    assert identity.payload == payload
    assert not any(value.startswith("/") for value in identity.payload.values())
    with pytest.raises(RuntimeServicesV2Error, match="RUNTIME_SERVICE_RECEIPT_INVALID"):
        GatewayContainerIdentityV2.from_payload({**payload, "host_path": "/forbidden"})
