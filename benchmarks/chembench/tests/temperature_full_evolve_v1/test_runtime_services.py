from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Self

import pytest
import yaml
from openevo.rollout.models import SessionResult
from openevo.rollout.server import _build_state

from openevo_chembench.temperature_full_evolve_v1 import runtime_services as services
from openevo_chembench.temperature_full_evolve_v1.runtime_services import (
    CompletionRootIdentityV1,
    TemperatureRuntimeServicesError,
    audit_empty_temperature_runtime_services_v1,
    audit_persisted_rollout_result_v1,
)

REPOSITORY = Path(__file__).resolve().parents[4]
SERVICE_RUN_ID = "stv3-temperature-services-20990101T000000Z-deadbeef"
TASK_ID = "temperature-pre-b01-i001"
SESSION_ID = "sk-openevo-11111111-2222-3333-4444-555555555555"
RUNTIME_SERVICES_SCRIPT = (
    REPOSITORY
    / "benchmarks/chembench/scripts/temperature_full_evolve_v1/runtime_services.py"
)


def _runtime_services_cli_module():
    spec = importlib.util.spec_from_file_location(
        "temperature_full_evolve_runtime_services_cli",
        RUNTIME_SERVICES_SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_services_cli_redacts_typed_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _runtime_services_cli_module()

    def fail(**_values: object) -> object:
        raise TemperatureRuntimeServicesError("TEMPERATURE_RUNTIME_STATE_MISSING")

    monkeypatch.setattr(module, "temperature_runtime_services_status_v1", fail)
    monkeypatch.setattr(sys, "argv", [os.fspath(RUNTIME_SERVICES_SCRIPT), "status"])

    assert module._entrypoint() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "status": "FAIL_CLOSED",
        "finding_code": "TEMPERATURE_RUNTIME_STATE_MISSING",
    }


def _completion_identity(repository: Path, root: Path) -> CompletionRootIdentityV1:
    metadata = root.stat(follow_symlinks=False)
    return CompletionRootIdentityV1(
        root_relative=root.relative_to(repository).as_posix(),
        host_topology_relative="private/host_topology.yaml",
        host_topology_sha256="a" * 64,
        marker_sha256="b" * 64,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        owner_uid=os.getuid(),
        mode=0o700,
        container_path=services.GATEWAY_COMPLETION_MOUNT,
        max_field_bytes=services.COMPLETION_MAX_FIELD_BYTES,
        queue_size=services.COMPLETION_QUEUE_SIZE,
    )


def _evidence_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    root = repository / "completion_persistence"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    identity = SimpleNamespace(
        repository_root=repository,
        service_run_id=SERVICE_RUN_ID,
        completion_root=_completion_identity(repository, root),
    )
    monkeypatch.setattr(
        services,
        "load_temperature_runtime_evidence_v1",
        lambda **_values: identity,
    )
    return repository, root


def _storage_payload(
    *,
    task_id: str = TASK_ID,
    session_id: str = SESSION_ID,
    status: str = "COMPLETED",
    assistant_content: str | None = "A",
) -> bytes:
    response_messages = (
        [{"role": "assistant", "content": assistant_content}]
        if assistant_content is not None
        else []
    )
    result = SessionResult.model_validate(
        {
            "session_id": session_id,
            "task_id": task_id,
            "status": status,
            "trajectory": {
                "status": status,
                "metadata": {"capture_mode": "transcript"},
                "traces": [
                    {
                        "response_messages": response_messages,
                        "metadata": {
                            "capture_mode": "transcript",
                            "transcript": json.dumps(
                                {
                                    "type": "item.completed",
                                    "item": {"type": "agent_message"},
                                }
                            ),
                        },
                    }
                ],
                "error": None if status == "COMPLETED" else "synthetic failure",
            },
            "error": None if status == "COMPLETED" else "synthetic failure",
        }
    )
    payload = result.model_dump(mode="json")
    payload["trajectory"].pop("status")
    payload["trajectory"].pop("error")
    return json.dumps(payload, separators=(",", ":")).encode()


def _write_result(
    root: Path,
    encoded: bytes,
    *,
    task_id: str = TASK_ID,
    session_id: str = SESSION_ID,
    mode: int = 0o644,
) -> Path:
    task_root = root / f"task_{task_id}"
    task_root.mkdir(mode=0o755, exist_ok=True)
    task_root.chmod(0o755)
    path = task_root / f"ses_{session_id}.json"
    path.write_bytes(encoded)
    path.chmod(mode)
    return path


def test_host_topology_binds_owner_private_rollout_root() -> None:
    template = REPOSITORY / services.TOPOLOGY_RELATIVE
    completion_root = (REPOSITORY / "state/test-only-completion-root").resolve()

    encoded = services._render_host_topology(template, completion_root=completion_root)
    topology = yaml.safe_load(encoded)

    assert topology["rollout"]["host"] == "127.0.0.1"
    assert topology["rollout"]["public_url"] == (
        "http://host.docker.internal:8080"
    )
    assert topology["rollout"]["save_dir"] == os.fspath(completion_root)
    parsed = services.TopologyConfig.model_validate(topology)
    assert parsed.gateway.rollout_server_url == services.ROLLOUT_CONTAINER_URL
    assert _build_state(parsed).pipeline.callback_url == services.ROLLOUT_CALLBACK_URL
    assert services.ROLLOUT_URL == "http://127.0.0.1:8080"
    assert services.ROLLOUT_CALLBACK_URL == (
        "http://host.docker.internal:8080/callbacks/session_result"
    )
    assert topology["gateway"]["completion_persistence"] == {
        "enabled": True,
        "max_field_bytes": 16 * 1024 * 1024,
        "queue_size": 128,
    }
    assert services.SERVICE_ROOT_RELATIVE == (
        "state/chembench_temperature_full_evolve_v1/runtime_services"
    )


def test_gateway_container_keeps_exact_host_gateway_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_metadata = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660, st_gid=998)
    original_stat = Path.stat

    def controlled_stat(path: Path, *, follow_symlinks: bool = True):
        if path == Path("/var/run/docker.sock"):
            return socket_metadata
        return original_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", controlled_stat)
    command = services._gateway_container_command(
        repository=REPOSITORY,
        paths={
            "runtime_python": Path("/runtime/bin/python"),
            "gateway_bootstrap": Path("/runtime/gateway_service_bootstrap.py"),
        },
        source_commit="a" * 40,
        service_run_id=SERVICE_RUN_ID,
        container_name="openevo-temperature-gateway-test",
        gateway_runtime_root=tmp_path / "gateway-runtime",
        completion_root=tmp_path / "completions",
        host_topology=tmp_path / "host-topology.yaml",
        completion_root_marker_sha256="b" * 64,
        image_id="sha256:" + "c" * 64,
        auth_source=tmp_path / "auth.json",
        docker_launcher=Path("/usr/bin/docker"),
    )

    add_host = command.index("--add-host")
    publish = command.index("--publish")
    assert command[add_host + 1] == "host.docker.internal:host-gateway"
    assert command[publish + 1] == "127.0.0.1:8100:8100"


def test_callback_route_receipt_requires_exact_registered_container_origin(
    tmp_path: Path,
) -> None:
    assert services.SERVICE_RECEIPT_SCHEMA == (
        "TemperatureFullEvolveRuntimeServicesReceiptV2"
    )
    template = REPOSITORY / services.TOPOLOGY_RELATIVE
    completion_root = (tmp_path / "host-completions").resolve()
    host_topology = tmp_path / "host.yaml"
    host_topology.write_bytes(
        services._render_host_topology(template, completion_root=completion_root)
    )
    effective_payload = yaml.safe_load(host_topology.read_text(encoding="utf-8"))
    effective_payload["rollout"]["save_dir"] = services.GATEWAY_COMPLETION_MOUNT
    effective_payload["gateway"]["rollout_server_url"] = (
        services.ROLLOUT_CONTAINER_URL
    )
    effective_payload["gateway"]["nodes"][0]["host"] = "0.0.0.0"
    effective_topology = tmp_path / "effective.yaml"
    effective_topology.write_text(
        yaml.safe_dump(effective_payload, sort_keys=False),
        encoding="utf-8",
    )
    rollout_health = {
        "status": "ok",
        "gateway_registration": {
            "gateway_url": services.GATEWAY_URL,
            "node_id": "core-gateway",
            "registered": True,
            "schedulable": True,
        },
    }

    receipt = services._require_callback_route_ready(
        host_topology=host_topology,
        effective_topology=effective_topology,
        rollout_health=rollout_health,
    )

    services._require_callback_route_receipt(receipt)
    assert receipt["host_client_url"] == "http://127.0.0.1:8080"
    assert receipt["rollout_bind_host"] == "127.0.0.1"
    assert receipt["rollout_callback_url"] == services.ROLLOUT_CALLBACK_URL
    assert receipt["gateway_registered"] is True
    assert receipt["model_calls_observed"] == 0

    rollout_health["gateway_registration"]["schedulable"] = False
    with pytest.raises(
        TemperatureRuntimeServicesError,
        match="TEMPERATURE_RUNTIME_CALLBACK_ROUTE_INVALID",
    ):
        services._require_callback_route_ready(
            host_topology=host_topology,
            effective_topology=effective_topology,
            rollout_health=rollout_health,
        )


def test_mount_inventory_requires_readonly_repository_and_shared_rw_root(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    gateway_runtime = tmp_path / "gateway-runtime"
    completion_root = tmp_path / "completion-root"
    auth = tmp_path / "auth.json"
    docker = tmp_path / "docker"
    mounts = [
        {
            "Type": "bind",
            "Source": "/var/run/docker.sock",
            "Destination": "/var/run/docker.sock",
            "RW": True,
        },
        {
            "Type": "bind",
            "Source": os.fspath(docker),
            "Destination": services.GATEWAY_DOCKER_SOURCE_MOUNT,
            "RW": False,
        },
        {
            "Type": "bind",
            "Source": os.fspath(repository),
            "Destination": os.fspath(repository),
            "RW": False,
        },
        {
            "Type": "bind",
            "Source": os.fspath(gateway_runtime),
            "Destination": services.GATEWAY_RUNTIME_MOUNT,
            "RW": True,
        },
        {
            "Type": "bind",
            "Source": os.fspath(completion_root),
            "Destination": services.GATEWAY_COMPLETION_MOUNT,
            "RW": True,
        },
        {
            "Type": "bind",
            "Source": os.fspath(auth),
            "Destination": services.GATEWAY_AUTH_SOURCE_MOUNT,
            "RW": False,
        },
    ]

    digest = services._validate_mount_inventory(
        mounts,
        repository=repository,
        gateway_runtime_root=gateway_runtime,
        completion_root=completion_root,
        auth_source=auth,
        docker_launcher=docker,
    )

    assert len(digest) == 64
    repository_mount = next(
        row for row in mounts if row["Destination"] == os.fspath(repository)
    )
    repository_mount["RW"] = True
    with pytest.raises(
        TemperatureRuntimeServicesError,
        match="TEMPERATURE_RUNTIME_MOUNT_IDENTITY_INVALID",
    ):
        services._validate_mount_inventory(
            mounts,
            repository=repository,
            gateway_runtime_root=gateway_runtime,
            completion_root=completion_root,
            auth_source=auth,
            docker_launcher=docker,
        )


def test_runtime_identity_exposes_v2_executor_duck_contract() -> None:
    fields = services.TemperatureRuntimeServicesIdentityV1.__dataclass_fields__

    assert {"repository_root", "receipt_path"}.issubset(fields)
    assert isinstance(services.TemperatureRuntimeServicesIdentityV1.rollout_url, property)
    assert isinstance(services.TemperatureRuntimeServicesIdentityV1.digest, property)
    assert callable(services.TemperatureRuntimeServicesIdentityV1.require_current)
    assert callable(services.TemperatureRuntimeServicesIdentityV1.require_identity_current)


def test_runtime_paths_are_bound_to_noneditable_formal_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path.resolve()
    formal_root = repository / "state/formal"
    identity = SimpleNamespace(
        runtime_python=formal_root / "runtime_venv/bin/python",
        framework_lock=formal_root / "framework/framework-lock.json",
        core_wheel=formal_root / "framework/openevo-0.3.0-py3-none-any.whl",
    )
    monkeypatch.setattr(
        services,
        "load_temperature_formal_runtime_v1",
        lambda **_values: identity,
    )

    paths = services._runtime_paths(repository)

    assert paths["runtime_python"] == identity.runtime_python
    assert paths["framework_lock"] == identity.framework_lock
    assert paths["framework_wheel"] == identity.core_wheel
    assert "chembench_supervised_transfer_v2" not in os.fspath(
        paths["runtime_python"]
    )


def test_empty_runtime_inventory_binds_rollout_and_completion_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    root = repository / "completion_persistence"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    marker = root / services.COMPLETION_ROOT_MARKER
    marker.write_bytes(b"{}\n")
    marker.chmod(0o600)
    completion = _completion_identity(repository, root)
    completion = CompletionRootIdentityV1(
        **{
            **completion.payload,
            "marker_sha256": hashlib.sha256(marker.read_bytes()).hexdigest(),
        }
    )
    identity = SimpleNamespace(
        repository_root=repository,
        service_run_id=SERVICE_RUN_ID,
        completion_root=completion,
        rollout_url="http://127.0.0.1:8080",
        digest="c" * 64,
        require_current=dict,
    )
    monkeypatch.setattr(
        services,
        "load_temperature_runtime_services_v1",
        lambda **_values: identity,
    )

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"tasks": []}

    class Client:
        def __init__(self, **_values: object) -> None:
            return None

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_values: object) -> None:
            return None

        def get(self, *_args: object, **_kwargs: object) -> Response:
            return Response()

    monkeypatch.setattr(services.httpx, "Client", Client)

    receipt = audit_empty_temperature_runtime_services_v1(repository_root=repository)

    assert receipt["rollout_task_count"] == 0
    assert receipt["persisted_task_directory_count"] == 0
    assert len(str(receipt["inventory_sha256"])) == 64

    (root / "task_unexpected").mkdir()
    with pytest.raises(TemperatureRuntimeServicesError, match="NOT_EMPTY"):
        audit_empty_temperature_runtime_services_v1(repository_root=repository)


def test_rollout_result_proves_subscription_completion_without_gateway_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, root = _evidence_root(tmp_path, monkeypatch)
    encoded = _storage_payload()
    _write_result(root, encoded)

    audit = audit_persisted_rollout_result_v1(
        repository_root=repository,
        service_run_id=SERVICE_RUN_ID,
        task_id=TASK_ID,
    )

    assert audit.state == "PROVEN_COMPLETE"
    assert audit.terminal_status == "COMPLETED"
    assert audit.completion_exists is True
    assert audit.completion_sha256 == hashlib.sha256(b"A").hexdigest()
    assert audit.result is not None
    assert audit.result.session_id == SESSION_ID
    assert audit.public_identity["safe_to_repeat_model_call"] is False
    public = json.dumps(audit.public_identity, sort_keys=True)
    assert "agent_message" not in public
    assert '"A"' not in public
    assert not (root / f"task_{TASK_ID}" / "sessions").exists()


def test_rollout_result_proves_terminal_no_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, root = _evidence_root(tmp_path, monkeypatch)
    _write_result(root, _storage_payload(status="ERROR", assistant_content=None))

    audit = audit_persisted_rollout_result_v1(
        repository_root=repository,
        service_run_id=SERVICE_RUN_ID,
        task_id=TASK_ID,
    )

    assert audit.state == "PROVEN_TERMINAL_NO_COMPLETION"
    assert audit.terminal_status == "ERROR"
    assert audit.completion_exists is False
    assert audit.completion_sha256 is None
    assert audit.public_identity["safe_to_repeat_model_call"] is False


def test_dual_no_completion_receipt_supplies_exact_ledger_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, root = _evidence_root(tmp_path, monkeypatch)
    _write_result(root, _storage_payload(status="ERROR", assistant_content=None))

    evidence = services.audit_durable_no_completion_v1(
        repository_root=repository,
        service_run_id=SERVICE_RUN_ID,
        task_id=TASK_ID,
    )

    assert evidence.state == "PROVEN_NO_COMPLETION"
    assert evidence.durable_rollout_no_completion is True
    assert evidence.durable_gateway_absent is True
    assert evidence.gateway_absence_basis == "GATEWAY_SESSIONS_DIRECTORY_ABSENT"
    assert evidence.ledger_fields == {
        "durable_rollout_no_completion": True,
        "durable_gateway_absent": True,
        "no_completion_evidence_sha256": evidence.digest,
    }
    assert evidence.receipt["no_completion_evidence_sha256"] == evidence.digest
    assert evidence.receipt["safe_to_repeat_model_call"] is False


def test_dual_no_completion_is_ambiguous_when_gateway_path_is_populated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, root = _evidence_root(tmp_path, monkeypatch)
    _write_result(root, _storage_payload(status="ERROR", assistant_content=None))
    completions = (
        root
        / f"task_{TASK_ID}"
        / "sessions"
        / SESSION_ID
        / "completions"
    )
    completions.mkdir(parents=True)
    (completions / "0001-conflict.json").write_text("{}", encoding="utf-8")

    evidence = services.audit_durable_no_completion_v1(
        repository_root=repository,
        service_run_id=SERVICE_RUN_ID,
        task_id=TASK_ID,
    )

    assert evidence.state == "AMBIGUOUS"
    assert evidence.durable_rollout_no_completion is True
    assert evidence.durable_gateway_absent is False
    with pytest.raises(
        TemperatureRuntimeServicesError,
        match="DURABLE_NO_COMPLETION_NOT_PROVEN",
    ):
        _ = evidence.ledger_fields


@pytest.mark.parametrize("failure", ["missing", "multiple", "partial", "mismatch", "unsafe"])
def test_rollout_result_ambiguity_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    repository, root = _evidence_root(tmp_path, monkeypatch)
    if failure == "missing":
        (root / f"task_{TASK_ID}").mkdir(mode=0o755)
    elif failure == "multiple":
        _write_result(root, _storage_payload())
        _write_result(
            root,
            _storage_payload(session_id="sk-openevo-second"),
            session_id="sk-openevo-second",
        )
    elif failure == "partial":
        _write_result(root, b'{"session_id":')
    elif failure == "mismatch":
        _write_result(root, _storage_payload(task_id="different-task"))
    else:
        _write_result(root, _storage_payload(), mode=0o666)

    audit = audit_persisted_rollout_result_v1(
        repository_root=repository,
        service_run_id=SERVICE_RUN_ID,
        task_id=TASK_ID,
    )

    assert audit.state == "AMBIGUOUS"
    assert audit.completion_exists is None
    assert audit.result is None
    assert audit.finding_code == "PERSISTED_ROLLOUT_RESULT_AMBIGUOUS"
    assert audit.public_identity["safe_to_repeat_model_call"] is False


def test_rollout_storage_decoder_rejects_unknown_fields() -> None:
    payload = json.loads(_storage_payload())
    payload["credential"] = "forbidden"

    with pytest.raises(
        TemperatureRuntimeServicesError,
        match="PERSISTED_ROLLOUT_RESULT_AMBIGUOUS",
    ):
        services._decode_rollout_storage_result(json.dumps(payload).encode())
