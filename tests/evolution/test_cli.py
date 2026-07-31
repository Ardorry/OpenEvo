from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import uvicorn

from openevo.evolution import cli as evolution_cli
from openevo.evolution.cli import build_parser
from openevo.internal_auth import InternalServiceIdentity


def test_evolution_cli_defaults_use_openevo_state_root() -> None:
    parser = build_parser()

    cases = [
        ["serve", "--framework-lock", "/tmp/framework-lock.json"],
        ["worker"],
    ]

    for argv in cases:
        args = parser.parse_args(argv)
        if hasattr(args, "db"):
            assert args.db == ".openevo/evolution/evolution.db"
        if hasattr(args, "artifact_root"):
            assert args.artifact_root == ".openevo/evolution"


def test_serve_passes_full_executable_registry_to_backend(
    tmp_path,
    monkeypatch,
) -> None:
    registry = object()
    app = object()
    observed: dict[str, object] = {}

    def fake_load_registry(lock_path):
        observed["lock_path"] = lock_path
        return registry

    monkeypatch.setattr(
        evolution_cli,
        "load_verified_framework_registry",
        fake_load_registry,
    )

    def fake_create_app(**kwargs):
        observed["create_app"] = kwargs
        return app

    def fake_uvicorn_run(candidate, *, host, port):
        observed["uvicorn"] = (candidate, host, port)

    monkeypatch.setattr(evolution_cli, "create_app", fake_create_app)
    monkeypatch.setattr(uvicorn, "run", fake_uvicorn_run)
    lock_path = tmp_path / "framework-lock.json"
    db_path = tmp_path / "evolution.db"
    artifact_root = tmp_path / "artifacts"

    result = evolution_cli.main(
        [
            "serve",
            "--framework-lock",
            str(lock_path),
            "--db",
            str(db_path),
            "--artifact-root",
            str(artifact_root),
            "--host",
            "127.0.0.2",
            "--port",
            "8300",
        ]
    )

    assert result == 0
    assert observed["lock_path"] == lock_path
    assert observed["create_app"] == {
        "db_path": db_path,
        "artifact_root": artifact_root,
        "executable_registry": registry,
    }
    assert observed["uvicorn"] == (app, "127.0.0.2", 8300)


def test_release_worker_mount_readiness_precedes_registration_and_job_claim(
    tmp_path: Path,
    monkeypatch,
) -> None:
    order: list[str] = []
    generation = "1" * 64
    registry = "2" * 64
    framework = "3" * 64
    authority_id = "4" * 64
    launch_id = f"mrl-{'5' * 32}"
    mapping_identity = "6" * 64
    identity = InternalServiceIdentity(
        service_id="evolution-worker",
        generation_digest=generation,
        registry_digest=registry,
        framework_lock_digest=framework,
        credential="cli-order-test-credential-0123456789abcdef",
    )
    daemon_release = "d" * 64
    release_install = "7" * 64
    runtime_digest = f"sha256:{'8' * 64}"
    service_identity = "c" * 64
    authority = SimpleNamespace(
        authority_id=authority_id,
        docker_host_path=SimpleNamespace(
            identity_digest=mapping_identity,
            runtime_uid=1000,
        ),
        grant=SimpleNamespace(
            worker_launch_id=launch_id,
            service_identity_digest=service_identity,
            runtime_profile="managed_science",
            runtime_image=runtime_digest,
            generation_digest=generation,
            daemon_release_identity=daemon_release,
            release_install_digest=release_install,
            release_registry_digest=registry,
            docker_host_path=SimpleNamespace(
                identity_digest=mapping_identity,
                runtime_uid=1000,
            ),
        ),
    )
    receipt: dict[str, object] = {
        "schema_version": "openevo.managed_reflector_credential_mount_readiness.v1",
        "authority_id": authority_id,
        "container_authority_id": "a" * 64,
        "worker_launch_id": launch_id,
        "container_launch_id": "openevo_reflector_cli_probe",
        "adoption_nonce_sha256": "b" * 64,
        "service_identity_digest": service_identity,
        "runtime_profile": "managed_science",
        "runtime_digest": runtime_digest,
        "docker_host_path_identity": mapping_identity,
        "generation_digest": generation,
        "daemon_release_identity": daemon_release,
        "release_install_digest": release_install,
        "release_registry_digest": registry,
        "credential_target": "/openevo/credentials/codex",
        "container_uid": 1000,
        "container_gid": 1000,
        "authority_issued": True,
        "docker_mount_created": True,
        "container_path_visible": True,
        "container_user_can_read": True,
        "auth_file_read_only": True,
        "generation_matches": True,
        "release_identity_matches": True,
        "adoption_receipt_valid": True,
        "container_authority_verified": True,
        "cleanup_verified": True,
        "codex_cli_started": False,
        "model_started": False,
        "created_at": "2026-07-29T00:00:00+00:00",
    }
    receipt["content_sha256"] = hashlib.sha256(
        json.dumps(
            receipt,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    runtime_readiness: dict[str, object] = {
        "schema_version": "openevo.managed_reflector_readiness.v1",
        "authority_id": authority_id,
        "container_authority_id": "e" * 64,
        "worker_launch_id": launch_id,
        "container_launch_id": "openevo_reflector_cli_runtime_probe",
        "adoption_nonce_sha256": "f" * 64,
        "service_identity_digest": service_identity,
        "generation_digest": generation,
        "daemon_release_identity": daemon_release,
        "release_install_digest": release_install,
        "release_registry_digest": registry,
        "runtime_profile": "managed_science",
        "runtime_digest": runtime_digest,
        "docker_host_path_identity": mapping_identity,
        "credential_mount_content_sha256": receipt["content_sha256"],
        "codex_binary": "/opt/codex/bin/codex",
        "expected_cli_version": "0.144.1",
        "actual_cli_version": "0.144.1",
        "model": "readiness-only",
        "auth_mode": "subscription",
        "capture_mode": "transcript",
        "path_fallback_allowed": False,
        "exit_status": 0,
        "container_authority_verified": True,
        "adoption_receipt_valid": True,
        "cleanup_verified": True,
        "codex_cli_started": True,
        "model_started": False,
        "created_at": "2026-07-29T00:00:01+00:00",
    }
    runtime_readiness["content_sha256"] = hashlib.sha256(
        json.dumps(
            runtime_readiness,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    mount_verification: dict[str, object] = {}

    class _Snapshot:
        @classmethod
        def from_inherited_environment(cls, *, required):
            assert required is True
            return cls()

        def close(self):
            order.append("snapshot_close")

    class _MountAuthority:
        @classmethod
        def from_inherited_environment(cls, *_args, **_kwargs):
            mount_verification.update(_kwargs)
            order.append("authority_verified")
            return authority

    class _Reflector:
        def __init__(self, **kwargs):
            assert kwargs["mount_authority"] is authority
            assert kwargs["docker_host_path"] is authority.docker_host_path
            assert kwargs["require_docker_host_path"] is True
            order.append("reflector_created")

        def credential_mount_readiness(self, _runtime):
            order.append("mount_readiness")
            return receipt

        def readiness(self, _runtime, *, credential_mount_readiness):
            assert credential_mount_readiness.content_sha256 == receipt["content_sha256"]
            order.append("runtime_readiness")
            return runtime_readiness

        def close(self):
            order.append("reflector_close")

    class _Client:
        def __init__(self, *_args, **_kwargs):
            order.append("client_created")

        def __enter__(self):
            order.append("client_enter")
            return self

        def __exit__(self, *_args):
            order.append("client_exit")

        def register_internal_worker(self, **kwargs):
            assert kwargs["managed_reflector_credential_mount"] is receipt
            assert kwargs["managed_reflector_runtime_readiness"] is runtime_readiness
            order.append("registered")

    lock = tmp_path / "framework-lock.json"
    lock.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        evolution_cli,
        "load_verified_framework_registry",
        lambda _path: SimpleNamespace(snapshot=SimpleNamespace(registry_digest=registry)),
    )
    monkeypatch.setattr(
        evolution_cli,
        "read_internal_service_identity",
        lambda **_kwargs: identity,
    )
    monkeypatch.setattr(
        evolution_cli,
        "verified_private_file_sha256",
        lambda *_args, **_kwargs: framework,
    )
    monkeypatch.setattr(evolution_cli, "PreparedCodexCredentialSnapshot", _Snapshot)
    monkeypatch.setattr(evolution_cli, "ManagedReflectorMountAuthority", _MountAuthority)
    monkeypatch.setattr(evolution_cli, "ManagedCodexReflectorService", _Reflector)
    monkeypatch.setattr(
        evolution_cli,
        "default_managed_reflector_runtime",
        lambda: SimpleNamespace(profile="managed_science"),
    )
    monkeypatch.setattr(evolution_cli, "EvolutionWorkerClient", _Client)

    def _run_once(*_args, **_kwargs):
        order.append("job_claim")
        return False

    monkeypatch.setattr(evolution_cli, "run_once", _run_once)
    monkeypatch.setenv(
        evolution_cli.MANAGED_REFLECTOR_MOUNT_AUTHORITY_FD_ENV,
        "9",
    )

    assert (
        evolution_cli.main(
            [
                "worker",
                "--once",
                "--framework-lock",
                os.fspath(lock),
                "--managed-reflector-root",
                os.fspath(tmp_path / "reflector"),
                "--managed-reflector-mount-launch-id",
                launch_id,
                "--managed-reflector-docker-host-path-identity",
                mapping_identity,
                "--managed-reflector-daemon-release-identity",
                daemon_release,
                "--managed-reflector-release-install-identity",
                release_install,
                "--managed-reflector-runtime-profile",
                "managed_science",
                "--managed-reflector-runtime-image",
                runtime_digest,
                "--managed-reflector-runtime-identity",
                "9" * 64,
            ]
        )
        == 0
    )
    assert order.index("mount_readiness") < order.index("runtime_readiness")
    assert order.index("runtime_readiness") < order.index("registered")
    assert order.index("registered") < order.index("job_claim")
    assert mount_verification["expected_daemon_release_identity"] == daemon_release
    assert order[-2:] == ["reflector_close", "snapshot_close"]
