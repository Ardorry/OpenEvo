from __future__ import annotations

import hashlib
import json
import signal
from pathlib import Path

import pytest

from openevo_chembench.supervised_transfer_v2 import runtime_services as services
from openevo_chembench.supervised_transfer_v2.runtime_services import (
    GatewayContainerIdentityV2,
    RuntimeProcessIdentityV2,
    RuntimeServicesV2Error,
    _runtime_metadata,
    _runtime_paths,
    _validate_runtime_inputs,
)

REPOSITORY = Path(__file__).resolve().parents[4]


def _write_private_json(path: Path, payload: dict[str, object]) -> bytes:
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(encoded)
    path.chmod(0o600)
    return encoded


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


def test_stop_cleans_exact_stale_source_receipt_without_reusing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    (repository / ".git").mkdir(parents=True)
    service_run_id = "stv2-services-20990101T000000Z-deadbeef"
    run_root = (
        repository
        / services.SERVICE_ROOT_RELATIVE
        / "runs"
        / service_run_id
    )
    process = {
        "pid": 999_999,
        "process_group_id": 999_999,
        "session_id": 999_999,
        "start_time_ticks": 1,
        "executable_sha256": "1" * 64,
        "cmdline_sha256": "2" * 64,
    }
    receipt = {
        "schema_version": services.SERVICE_RECEIPT_SCHEMA,
        "service_run_id": service_run_id,
        "source_commit": "a" * 40,
        "framework_lock_sha256": "3" * 64,
        "framework_wheel_sha256": "4" * 64,
        "topology_sha256": "5" * 64,
        "gateway_bootstrap_sha256": "6" * 64,
        "runtime_python_sha256": "7" * 64,
        "openevo_version": "0.1.8",
        "rollout_url": services.ROLLOUT_URL,
        "gateway_url": services.GATEWAY_URL,
        "rollout_process": {"service": "rollout", **process},
        "gateway_process": {"service": "gateway", **process},
        "gateway_container": {
            "container_name": "openevo-stv2-gateway-" + "b" * 24,
            "container_id": "8" * 64,
            "image_id": "sha256:" + "9" * 64,
            "effective_topology_sha256": "a" * 64,
            "docker_host_path_identity_sha256": "b" * 64,
            "docker_engine_identity_sha256": "c" * 64,
            "docker_launcher_sha256": "d" * 64,
        },
        "rollout_health_sha256": "e" * 64,
        "gateway_health_sha256": "f" * 64,
    }
    receipt_path = run_root / "runtime_services_receipt_v2.json"
    encoded = _write_private_json(receipt_path, receipt)
    pointer_path = repository / services.SERVICE_ROOT_RELATIVE / "current.json"
    _write_private_json(
        pointer_path,
        {
            "schema_version": services.SERVICE_POINTER_SCHEMA,
            "service_run_id": service_run_id,
            "receipt_sha256": hashlib.sha256(encoded).hexdigest(),
        },
    )
    removed: list[dict[str, object]] = []
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(services, "_process_exists", lambda _pid: False)
    monkeypatch.setattr(
        services.os,
        "killpg",
        lambda process_group_id, sent_signal: signals.append(
            (process_group_id, sent_signal)
        ),
    )
    monkeypatch.setattr(
        services,
        "_remove_gateway_container_if_owned",
        lambda **values: removed.append(values),
    )

    result = services.stop_runtime_services_v2(repository_root=repository.resolve())

    assert result["cleanup_complete"] is True
    assert result["service_run_id"] == service_run_id
    assert not pointer_path.exists()
    assert (run_root / "runtime_services_stop_receipt_v2.json").is_file()
    assert signals == []
    assert removed == [
        {
            "container_name": receipt["gateway_container"]["container_name"],
            "expected_container_id": receipt["gateway_container"]["container_id"],
            "source_commit": receipt["source_commit"],
            "service_run_id": service_run_id,
        }
    ]
