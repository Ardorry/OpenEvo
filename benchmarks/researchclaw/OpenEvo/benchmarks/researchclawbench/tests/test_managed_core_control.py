from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from pydantic import SecretStr

from openevo.backend.service import CoreServiceAttachment
from openevo_researchclawbench.managed_core_control import (
    ManagedCoreControlAuthority,
    _acquire_isolated_core_service_attachment,
    _remote_fixture_host_profile,
    acquire_managed_core_control,
)


class _Config:
    raw = {"native_openevo": {"core_control_mode": "managed_host_service"}}

    def __init__(self, lock: Path, probe_bin: Path) -> None:
        self.lock = lock
        self.probe_bin = probe_bin

    def require(self, key: str):
        return {
            "native_openevo.framework_lock": str(self.lock),
            "native_openevo.core_host_codex_probe_bin": str(self.probe_bin),
            "source_identity.openevo_commit": "1" * 40,
        }[key]


def _probe_bin(tmp_path: Path) -> Path:
    probe_bin = tmp_path / "probe-bin"
    probe_bin.mkdir(mode=0o700)
    executable = probe_bin / "codex"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o500)
    return probe_bin


def _attachment() -> CoreServiceAttachment:
    return CoreServiceAttachment(
        port=17810,
        release_identity="2" * 64,
        registry_digest="3" * 64,
        source_commit="1" * 40,
        generation="4" * 32,
        status_proof="5" * 64,
        attached=True,
        _bearer=SecretStr("x" * 64),
    )


def test_formal_launcher_returns_nonserializable_secret_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}\n", encoding="utf-8")
    calls = {}

    def acquire(**kwargs):
        calls.update(kwargs)
        return _attachment()

    monkeypatch.setattr(
        "openevo_researchclawbench.managed_core_control._acquire_isolated_core_service_attachment",
        acquire,
    )
    monkeypatch.setattr(
        "openevo_researchclawbench.managed_core_control.authenticate_core_service_endpoint",
        lambda **_kwargs: "5" * 64,
    )
    monkeypatch.setattr(
        "openevo_researchclawbench.managed_core_control.managed_core_host_profile_readiness",
        lambda: {"ready": True},
    )
    probe_bin = _probe_bin(tmp_path)
    authority = acquire_managed_core_control(_Config(lock, probe_bin))
    public = authority.public_readiness()
    rendered = json.dumps(public, sort_keys=True)
    assert calls["framework_lock"] == lock
    assert calls["host_probe_bin"] == probe_bin
    assert calls["source_commit"] == "1" * 40
    assert public["bearer_present"] is True
    assert public["bearer_valid"] is True
    assert public["generation_matches"] is True
    assert public["candidate_port_authorized"] is True
    assert public["secret_recorded"] is False
    assert public["environment_fallback_used"] is False
    assert public["managed_host_profile_ready"] is True
    assert "x" * 64 not in rendered
    assert "OPENEVO_CORE_CONTROL_BEARER" not in rendered


def test_authority_repr_and_public_projection_never_expose_bearer() -> None:
    authority = ManagedCoreControlAuthority.from_verified_attachment(_attachment())
    assert "x" * 64 not in repr(authority)
    assert "x" * 64 not in json.dumps(authority.public_readiness())


def test_managed_remote_mode_dispatches_to_formal_daemon_transport(monkeypatch) -> None:
    expected = ManagedCoreControlAuthority.from_verified_attachment(_attachment())

    class RemoteConfig:
        raw = {"native_openevo": {"core_control_mode": "managed_remote_daemon"}}

    monkeypatch.setattr(
        "openevo_researchclawbench.managed_core_control._acquire_remote_daemon_core_control",
        lambda config, deadline_seconds: expected,
    )
    assert acquire_managed_core_control(RemoteConfig()) is expected


def test_remote_fixture_host_profile_accepts_only_exact_closed_docker_set(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = tmp_path / "project"
    experiment = project / "experiments" / "run"
    script = project / "OpenEvo" / "scripts" / "e2e" / "docker_release_host_fixture.py"
    script.parent.mkdir(parents=True)
    script.write_text("# fixture\n", encoding="utf-8")
    experiment.mkdir(parents=True)
    payload = {
        "schema_version": 2,
        "action": "check",
        "outcome": "ready",
        "release_host_profile": "docker_user_container_v1",
        "docker_server": {
            "observed": {
                "version": "29.5.2",
                "api_version": "1.54",
                "os": "linux",
                "architecture": "amd64",
            },
            "supported": {
                "versions": ["29.3.0", "29.5.2"],
                "api_versions": ["1.54"],
                "os": ["linux"],
                "architecture": ["amd64"],
            },
        },
        "fixture": {
            "container_name": "owned-v5",
            "container_hostname": "1" * 12,
            "docker_socket_accessible": True,
            "ssh": {
                "host": "127.0.0.1",
                "port": 61430,
                "reachable": True,
                "user": "openevo",
            },
        },
    }

    class RemoteFixtureConfig:
        project_root = project
        experiment_root = experiment

        def require(self, key: str):
            values = {
                "native_openevo.remote_core.fixture_check_script": str(script),
                "native_openevo.remote_core.supported_docker_server_versions": [
                    "29.3.0",
                    "29.5.2",
                ],
                "native_openevo.remote_core.docker_server_version": "29.5.2",
                "native_openevo.remote_core.docker_server_api": "1.54",
                "native_openevo.remote_core.container_name": "owned-v5",
                "native_openevo.remote_core.container_hostname": "1" * 12,
                "native_openevo.remote_core.host": "127.0.0.1",
                "native_openevo.remote_core.port": 61430,
                "native_openevo.remote_core.user": "openevo",
            }
            return values[key]

    monkeypatch.setattr(
        "openevo_researchclawbench.managed_core_control.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload).encode("utf-8"),
        ),
    )
    receipt = _remote_fixture_host_profile(RemoteFixtureConfig())
    assert receipt["ready"] is True
    assert receipt["docker_server_version"] == "29.5.2"
    assert receipt["supported_docker_server_versions"] == ["29.3.0", "29.5.2"]

    payload["docker_server"]["supported"]["versions"] = ["29.5.2"]
    try:
        _remote_fixture_host_profile(RemoteFixtureConfig())
    except Exception as exc:
        assert "identity drifted" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("an open-ended Docker support projection was accepted")


def test_isolated_launcher_strips_parent_secrets_and_consumes_private_attachment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}\n", encoding="utf-8")
    observed = {}
    monkeypatch.setenv("OPENEVO_CORE_CONTROL_BEARER", "must-not-inherit")
    monkeypatch.setenv("JUDGE_API_KEY", "must-not-inherit")
    monkeypatch.setenv("PYTHONPATH", "/must/not/inherit")

    def run(command, **kwargs):
        observed["command"] = command
        observed["environment"] = kwargs["env"]
        return SimpleNamespace(returncode=0)

    def consume(**kwargs):
        observed["attachment_name"] = kwargs["attachment_name"]
        return json.dumps(
            {
                "port": 17810,
                "release_identity": "2" * 64,
                "registry_digest": "3" * 64,
                "source_commit": "1" * 40,
                "generation": "4" * 32,
                "status_proof": "5" * 64,
                "attached": True,
                "bearer_token": "x" * 64,
                "execution_mode": "subscription",
                "capture_mode": "transcript",
            }
        ).encode()

    monkeypatch.setattr(
        "openevo_researchclawbench.managed_core_control.subprocess.run", run
    )
    monkeypatch.setattr(
        "openevo_researchclawbench.managed_core_control.consume_core_service_attachment",
        consume,
    )
    attachment = _acquire_isolated_core_service_attachment(
        framework_lock=lock,
        source_commit="1" * 40,
        host_probe_bin=_probe_bin(tmp_path),
        deadline_seconds=10,
    )
    assert observed["command"][1:3] == ["-I", "-m"]
    assert observed["attachment_name"].startswith("bootstrap-")
    assert "OPENEVO_CORE_CONTROL_BEARER" not in observed["environment"]
    assert "JUDGE_API_KEY" not in observed["environment"]
    assert "PYTHONPATH" not in observed["environment"]
    assert observed["environment"]["PATH"].split(":", 1)[0].endswith("probe-bin")
    assert attachment.bearer_token == "x" * 64


def test_formal_launcher_rejects_symlink_host_probe(
    tmp_path: Path,
) -> None:
    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}\n", encoding="utf-8")
    probe_bin = tmp_path / "probe-bin"
    probe_bin.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o500)
    (probe_bin / "codex").symlink_to(target)
    try:
        acquire_managed_core_control(_Config(lock, probe_bin))
    except Exception as exc:
        assert "not a regular file" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("symlinked host probe was accepted")


def test_managed_host_profile_failure_is_closed_and_nonsecret(monkeypatch) -> None:
    from openevo_researchclawbench.managed_core_control import (
        managed_core_host_profile_readiness,
    )

    monkeypatch.setattr(
        "openevo_researchclawbench.managed_core_control.DockerEngineAuthority.open",
        lambda: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    receipt = managed_core_host_profile_readiness()
    assert receipt == {
        "ready": False,
        "profile": "docker_user_container_v1",
        "mapping_identity_present": False,
        "reason_code": "DOCKER_USER_CONTAINER_MAPPING_UNAVAILABLE",
        "secret_recorded": False,
    }


def test_training_start_rejects_missing_host_profile_before_control_init(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from openevo_researchclawbench import cli

    authority = ManagedCoreControlAuthority.from_verified_attachment(
        _attachment(),
        managed_host_profile_ready=False,
    )
    monkeypatch.setattr(cli.ExperimentConfig, "load", lambda _path: object())
    monkeypatch.setattr(cli, "acquire_managed_core_control", lambda _config: authority)
    monkeypatch.setattr(
        cli,
        "DurableTrainingControl",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("control initialized before host-profile gate")
        ),
    )
    result = cli.main(
        [
            "training-start",
            "--protocol",
            str(tmp_path / "protocol.yaml"),
            "--run-id",
            "closed-preflight",
            "--scope",
            "life-005",
            "--until",
            "CANDIDATE_SEALED",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert result == 2
    assert output["status"] == "FAILED_CLOSED"
    assert output["model_started"] is None
    assert output["model_started_status"] == "UNKNOWN_REQUIRES_DURABLE_RECEIPT"
    assert "docker_user_container_v1" in output["message"]
