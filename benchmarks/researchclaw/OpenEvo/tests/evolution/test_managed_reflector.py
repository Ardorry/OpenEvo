from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import ClassVar

import pytest

from openevo.evolution import managed_reflector as managed_reflector_module
from openevo.evolution import methods
from openevo.evolution.framework.execution import (
    HarnessInferenceRequest,
    HarnessInferenceResponse,
    ManagedReflectorRuntimeConfig,
    MethodExecutionContext,
    MethodExecutionServices,
    ReflectorInferenceRequest,
    ReflectorInferenceResponse,
    ReflectorRuntimeReceipt,
    build_execution_envelope,
    invoke_legacy_method,
    require_active_reflector_service,
)
from openevo.evolution.managed_reflector import (
    ManagedCodexReflectorService,
    _managed_reflector_exec_command,
    default_managed_reflector_runtime,
)
from openevo.evolution.models import ArtifactRegisterRequest, WorkerClaimedJob
from openevo.gateway.session_files import HeldCodexCredentialAuthority
from openevo.internal_auth import InternalServiceIdentity
from openevo.runtime.docker_host import DockerHostPathSpec, discover_docker_host_path
from openevo.runtime.managed import MANAGED_RUNTIME_RELEASES
from openevo.runtime.managed_reflector_mount import (
    ManagedReflectorContainerMountAdoptionReceipt,
    ManagedReflectorCredentialMountReadiness,
    ManagedReflectorMountAuthority,
    ManagedReflectorMountRegistrationExpectation,
    ManagedReflectorRuntimeReadiness,
)


class _Harness:
    def infer(self, request: HarnessInferenceRequest) -> HarnessInferenceResponse:
        return HarnessInferenceResponse(
            request_id=request.request_id,
            text="unused",
            capture_mode="transcript",
        )


class _Reflector:
    def __init__(self) -> None:
        self.requests: list[ReflectorInferenceRequest] = []

    def infer(self, request: ReflectorInferenceRequest) -> ReflectorInferenceResponse:
        self.requests.append(request)
        return ReflectorInferenceResponse(
            request_id=request.request_id,
            text="# Managed result",
            receipt=ReflectorRuntimeReceipt(
                request_id=request.request_id,
                session_id="reflector-session-1",
                runtime_profile="managed_science",
                runtime_digest=request.runtime.image_digest.removeprefix("sha256:"),
                codex_binary="/opt/codex/bin/codex",
                actual_cli_version="0.144.1",
                model_name=request.model_name,
                reasoning_effort=request.reasoning_effort,
                auth_mode="subscription",
                capture_mode="transcript",
                path_fallback_allowed=False,
                exit_status=0,
                transcript_sha256="b" * 64,
            ),
        )


def _credential_snapshot(tmp_path: Path):
    auth = tmp_path / "source-home" / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True, mode=0o700)
    auth.write_text('{"subscription":true}\n', encoding="utf-8")
    auth.chmod(0o600)
    authority = HeldCodexCredentialAuthority.open(auth)
    try:
        return authority.prepare_snapshot()
    finally:
        authority.close()


def _docker_host_path(tmp_path: Path) -> DockerHostPathSpec:
    hostname = "a" * 12
    destination = tmp_path / "docker-shared"
    destination.mkdir()
    payload = json.dumps(
        {
            "id": hostname + ("b" * 52),
            "hostname": hostname,
            "running": True,
            "mounts": [
                {
                    "Type": "bind",
                    "Source": "/srv/openevo-shared",
                    "Destination": os.fspath(destination),
                    "RW": True,
                }
            ],
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return discover_docker_host_path(
        payload,
        namespace="reflector-tests",
        hostname=hostname,
        minimum_available_bytes=0,
    )


def _reflector_request() -> ReflectorInferenceRequest:
    return ReflectorInferenceRequest(
        request_id="reflector-runtime-creation",
        prompt="synthetic prompt",
        model_name="gpt-5.5",
        reasoning_effort="high",
        timeout_seconds=30,
        runtime=default_managed_reflector_runtime(),
    )


def _mount_authority(docker_host_path: DockerHostPathSpec) -> ManagedReflectorMountAuthority:
    return ManagedReflectorMountAuthority.issue(
        identity=InternalServiceIdentity(
            service_id="evolution-worker",
            generation_digest="a" * 64,
            registry_digest="b" * 64,
            framework_lock_digest="c" * 64,
            credential="managed-reflector-test-credential-0123456789abcdef",
        ),
        daemon_release_identity="f" * 64,
        release_install_digest="d" * 64,
        runtime_identity_digest="e" * 64,
        runtime_image=default_managed_reflector_runtime().image_digest,
        worker_launch_id="mrl-" + "1" * 32,
        docker_host_path=docker_host_path,
    )


def _container_adoption_receipt(
    authority: ManagedReflectorMountAuthority,
    container_launch_id: str,
) -> ManagedReflectorContainerMountAdoptionReceipt:
    grant = authority.grant
    payload: dict[str, object] = {
        "schema_version": "openevo.managed_reflector_container_mount_adoption.v1",
        "parent_authority_id": authority.authority_id,
        "container_authority_id": "2" * 64,
        "worker_launch_id": grant.worker_launch_id,
        "container_launch_id": container_launch_id,
        "adoption_nonce_sha256": "3" * 64,
        "service_identity_digest": grant.service_identity_digest,
        "generation_digest": grant.generation_digest,
        "daemon_release_identity": grant.daemon_release_identity,
        "release_install_digest": grant.release_install_digest,
        "release_registry_digest": grant.release_registry_digest,
        "runtime_profile": grant.runtime_profile,
        "runtime_digest": grant.runtime_image,
        "docker_host_path_identity": grant.docker_host_path.identity_digest,
        "credential_root_identity_sha256": "4" * 64,
        "credential_auth_identity_sha256": "5" * 64,
        "container_authority_verified": True,
        "adoption_receipt_valid": True,
    }
    payload["content_sha256"] = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    return ManagedReflectorContainerMountAdoptionReceipt.model_validate(payload)


def _credential_mount_readiness_receipt(
    authority: ManagedReflectorMountAuthority,
) -> ManagedReflectorCredentialMountReadiness:
    grant = authority.grant
    payload: dict[str, object] = {
        "schema_version": "openevo.managed_reflector_credential_mount_readiness.v1",
        "authority_id": authority.authority_id,
        "container_authority_id": "6" * 64,
        "worker_launch_id": grant.worker_launch_id,
        "container_launch_id": "openevo_reflector_mount_readiness",
        "adoption_nonce_sha256": "7" * 64,
        "service_identity_digest": grant.service_identity_digest,
        "runtime_profile": grant.runtime_profile,
        "runtime_digest": grant.runtime_image,
        "docker_host_path_identity": grant.docker_host_path.identity_digest,
        "generation_digest": grant.generation_digest,
        "daemon_release_identity": grant.daemon_release_identity,
        "release_install_digest": grant.release_install_digest,
        "release_registry_digest": grant.release_registry_digest,
        "credential_target": "/openevo/credentials/codex",
        "container_uid": grant.docker_host_path.runtime_uid,
        "container_gid": os.getgid(),
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
    payload["content_sha256"] = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    return ManagedReflectorCredentialMountReadiness.model_validate(payload)


def test_default_managed_reflector_is_exact_and_has_no_path_fallback() -> None:
    runtime = default_managed_reflector_runtime()
    assert runtime.mode == "managed"
    assert runtime.profile == "managed_science"
    assert runtime.codex_binary == "/opt/codex/bin/codex"
    assert runtime.expected_cli_version == "0.144.1"
    assert runtime.path_fallback_allowed is False
    assert runtime.image_digest == (
        "sha256:af67c6b8c9cb0debd3a29addc23f518a680369ad53ec5347a829ef7318529c5c"
    )


def test_release_owned_reflector_requires_verified_docker_host_path(
    tmp_path: Path,
) -> None:
    snapshot = _credential_snapshot(tmp_path)
    try:
        with pytest.raises(ValueError, match="Docker host path"):
            ManagedCodexReflectorService(
                root=tmp_path / "reflector-audit",
                credential_snapshot=snapshot,
                require_docker_host_path=True,
            )
    finally:
        snapshot.close()


def test_reflector_runtime_uses_verified_docker_host_path_and_mapped_private_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _credential_snapshot(tmp_path)
    docker_host_path = _docker_host_path(tmp_path)
    mount_authority = _mount_authority(docker_host_path)
    observed: dict[str, object] = {}
    service: ManagedCodexReflectorService | None = None

    def create_runtime(spec, session_id, session_dir, **kwargs):
        observed.update(
            {
                "spec": spec,
                "session_id": session_id,
                "session_dir": session_dir,
                **kwargs,
            }
        )
        return SimpleNamespace()

    monkeypatch.setattr(managed_reflector_module, "create_runtime", create_runtime)
    try:
        service = ManagedCodexReflectorService(
            root=tmp_path / "reflector-audit",
            credential_snapshot=snapshot,
            docker_host_path=docker_host_path,
            mount_authority=mount_authority,
            require_docker_host_path=True,
        )
        request = _reflector_request()
        _, ownership = service._create_runtime(request)
        _, session_dir, _, credential_dir, _, _ = ownership
        mapped_sessions = Path(docker_host_path.runtime_container_root) / "sessions"

        assert observed["docker_host_path"] == docker_host_path
        assert request.runtime.image_digest == (
            MANAGED_RUNTIME_RELEASES["managed_science"].trusted_digest
        )
        assert observed["spec"].image == (
            MANAGED_RUNTIME_RELEASES["managed_science"].loaded_image_id
        )
        assert request.runtime.image_digest != observed["spec"].image
        assert observed["managed_reflector_mount_authority"] == mount_authority
        assert observed["session_dir"] == session_dir
        assert session_dir.parent == mapped_sessions
        assert credential_dir.parent == mapped_sessions
        assert session_dir != credential_dir
        assert not session_dir.is_relative_to(service.root)
        assert not credential_dir.is_relative_to(service.root)
    finally:
        if service is not None:
            service.close()
        snapshot.close()


@pytest.mark.asyncio
async def test_reflector_concurrent_private_roots_cleanup_is_job_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _credential_snapshot(tmp_path)
    docker_host_path = _docker_host_path(tmp_path)
    mount_authority = _mount_authority(docker_host_path)
    service: ManagedCodexReflectorService | None = None

    class _Runtime:
        def __init__(self) -> None:
            self.stops = 0
            self.absence_proven = False

        async def stop(self) -> None:
            self.stops += 1
            self.absence_proven = True

    runtimes: list[_Runtime] = []
    container_authorities = []

    def create_runtime(*args, **kwargs):
        del args
        assert kwargs["docker_host_path"] == docker_host_path
        assert kwargs["managed_reflector_mount_authority"] == mount_authority
        container_authorities.append(
            kwargs["managed_reflector_container_mount_authority"]
        )
        runtime = _Runtime()
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr(managed_reflector_module, "create_runtime", create_runtime)
    try:
        service = ManagedCodexReflectorService(
            root=tmp_path / "reflector-audit",
            credential_snapshot=snapshot,
            docker_host_path=docker_host_path,
            mount_authority=mount_authority,
            require_docker_host_path=True,
        )
        first_runtime, first = service._create_runtime(_reflector_request())
        second_runtime, second = service._create_runtime(_reflector_request())
        first_session, first_credentials = first[1], first[3]
        second_session, second_credentials = second[1], second[3]

        assert first_session != second_session
        assert first_credentials != second_credentials
        assert first_session.is_dir() and first_credentials.is_dir()
        assert second_session.is_dir() and second_credentials.is_dir()
        assert container_authorities[0].authority_id != container_authorities[1].authority_id
        assert (
            container_authorities[0].grant.adoption_nonce
            != container_authorities[1].grant.adoption_nonce
        )
        assert (
            container_authorities[0].grant.credential_root_identity
            != container_authorities[1].grant.credential_root_identity
        )

        await service._stop_and_cleanup(
            first_runtime,
            session_dir=first[1],
            session_identity=first[2],
            credential_dir=first[3],
            credential_identity=first[4],
            auth_identity=first[5],
        )

        assert runtimes[0].stops == 1
        assert not first_session.exists()
        assert not first_credentials.exists()
        assert second_session.is_dir()
        assert second_credentials.is_dir()

        await service._stop_and_cleanup(
            second_runtime,
            session_dir=second[1],
            session_identity=second[2],
            credential_dir=second[3],
            credential_identity=second[4],
            auth_identity=second[5],
        )

        assert runtimes[1].stops == 1
        assert not second_session.exists()
        assert not second_credentials.exists()
    finally:
        if service is not None:
            service.close()
        snapshot.close()


@pytest.mark.asyncio
async def test_reflector_cleanup_retains_private_roots_when_stop_fails(
    tmp_path: Path,
) -> None:
    service = object.__new__(ManagedCodexReflectorService)
    session_dir = tmp_path / "session"
    credential_dir = tmp_path / "credentials"
    session_dir.mkdir()
    credential_dir.mkdir()

    class _Runtime:
        absence_proven = False

        async def stop(self) -> None:
            raise RuntimeError("synthetic stop failure")

    with pytest.raises(RuntimeError, match="synthetic stop failure"):
        await service._stop_and_cleanup(
            _Runtime(),
            session_dir=session_dir,
            session_identity=None,
            credential_dir=credential_dir,
            credential_identity=None,
            auth_identity=None,
        )

    assert session_dir.is_dir()
    assert credential_dir.is_dir()


@pytest.mark.asyncio
async def test_reflector_cleanup_retains_private_roots_without_absence_proof(
    tmp_path: Path,
) -> None:
    service = object.__new__(ManagedCodexReflectorService)
    session_dir = tmp_path / "session"
    credential_dir = tmp_path / "credentials"
    session_dir.mkdir()
    credential_dir.mkdir()

    class _Runtime:
        absence_proven = False

        async def stop(self) -> None:
            return None

    with pytest.raises(RuntimeError, match="absence was not proven"):
        await service._stop_and_cleanup(
            _Runtime(),
            session_dir=session_dir,
            session_identity=None,
            credential_dir=credential_dir,
            credential_identity=None,
            auth_identity=None,
        )

    assert session_dir.is_dir()
    assert credential_dir.is_dir()


def test_mount_readiness_adopts_credentials_without_starting_codex(
    tmp_path: Path,
) -> None:
    docker_host_path = _docker_host_path(tmp_path)
    authority = _mount_authority(docker_host_path)
    service = object.__new__(ManagedCodexReflectorService)
    service.mount_authority = authority
    service.docker_host_path = docker_host_path
    service.receipts_root = tmp_path / "receipts"
    service.receipts_root.mkdir()
    session_dir = tmp_path / "session"
    credential_dir = tmp_path / "credentials"
    session_dir.mkdir()
    credential_dir.mkdir()

    class _Runtime:
        session_id = "reflector-mount-canary"
        starts = 0
        execs: ClassVar[list[str]] = []
        stops = 0

        async def start(self) -> None:
            self.starts += 1

        async def exec(self, command, **kwargs):
            del kwargs
            self.execs.append(command)
            assert "/opt/codex/bin/codex" not in command
            assert " codex exec " not in command.lower()
            return SimpleNamespace(
                return_code=0,
                stdout=f"{os.getuid()}\n{os.getgid()}\n",
                stderr="",
            )

        async def stop(self) -> None:
            self.stops += 1

        def managed_reflector_container_mount_receipt(self):
            return _container_adoption_receipt(authority, self.session_id)

    runtime = _Runtime()

    def _create_runtime(self, request):
        del self, request
        return runtime, (
            "reflector-mount-canary",
            session_dir,
            None,
            credential_dir,
            None,
            None,
        )

    async def _cleanup(self, runtime_arg, **kwargs):
        del self, kwargs
        await runtime_arg.stop()
        credential_dir.rmdir()
        session_dir.rmdir()

    service._create_runtime = MethodType(_create_runtime, service)
    service._stop_and_cleanup = MethodType(_cleanup, service)

    receipt = service.credential_mount_readiness(default_managed_reflector_runtime())

    assert runtime.starts == 1
    assert runtime.stops == 1
    assert len(runtime.execs) == 1
    assert receipt["authority_issued"] is True
    assert receipt["docker_mount_created"] is True
    assert receipt["container_path_visible"] is True
    assert receipt["container_user_can_read"] is True
    assert receipt["generation_matches"] is True
    assert receipt["release_identity_matches"] is True
    assert receipt["daemon_release_identity"] == authority.grant.daemon_release_identity
    assert receipt["release_install_digest"] == authority.grant.release_install_digest
    assert receipt["adoption_receipt_valid"] is True
    assert receipt["container_authority_verified"] is True
    assert receipt["container_authority_id"] == "2" * 64
    assert receipt["adoption_nonce_sha256"] == "3" * 64
    assert receipt["cleanup_verified"] is True
    assert receipt["codex_cli_started"] is False
    assert receipt["model_started"] is False
    assert isinstance(receipt["content_sha256"], str)
    readiness = ManagedReflectorCredentialMountReadiness.model_validate(receipt)
    expectation = ManagedReflectorMountRegistrationExpectation.from_authority(
        authority
    )
    expectation.verify_readiness(readiness)

    drifted_payload = readiness.model_dump(mode="json", exclude={"content_sha256"})
    drifted_payload["runtime_digest"] = MANAGED_RUNTIME_RELEASES[
        "managed_science"
    ].loaded_image_id
    drifted_payload["content_sha256"] = hashlib.sha256(
        json.dumps(
            drifted_payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    with pytest.raises(
        RuntimeError,
        match="does not match Core expectation",
    ):
        expectation.verify_readiness(
            ManagedReflectorCredentialMountReadiness.model_validate(
                drifted_payload
            )
        )


def test_mount_readiness_adoption_failure_never_starts_codex(
    tmp_path: Path,
) -> None:
    docker_host_path = _docker_host_path(tmp_path)
    service = object.__new__(ManagedCodexReflectorService)
    service.mount_authority = _mount_authority(docker_host_path)
    service.docker_host_path = docker_host_path
    service.receipts_root = tmp_path / "receipts"
    service.receipts_root.mkdir()
    session_dir = tmp_path / "session"
    credential_dir = tmp_path / "credentials"
    session_dir.mkdir()
    credential_dir.mkdir()

    class _Runtime:
        execs = 0
        stops = 0

        async def start(self) -> None:
            raise RuntimeError("synthetic adoption failure")

        async def exec(self, *args, **kwargs):
            del args, kwargs
            self.execs += 1
            raise AssertionError("Codex/read probe must not start after adoption failure")

        async def stop(self) -> None:
            self.stops += 1

    runtime = _Runtime()

    def _create_runtime(self, request):
        del self, request
        return runtime, (
            "reflector-mount-failed-canary",
            session_dir,
            None,
            credential_dir,
            None,
            None,
        )

    async def _cleanup(self, runtime_arg, **kwargs):
        del self, kwargs
        await runtime_arg.stop()
        credential_dir.rmdir()
        session_dir.rmdir()

    service._create_runtime = MethodType(_create_runtime, service)
    service._stop_and_cleanup = MethodType(_cleanup, service)

    with pytest.raises(RuntimeError, match="synthetic adoption failure"):
        service.credential_mount_readiness(default_managed_reflector_runtime())
    assert runtime.execs == 0
    assert runtime.stops == 1
    assert list(service.receipts_root.iterdir()) == []


@pytest.mark.parametrize("stop_fails", [False, True])
def test_mount_readiness_is_not_published_without_container_absence_proof(
    tmp_path: Path,
    stop_fails: bool,
) -> None:
    docker_host_path = _docker_host_path(tmp_path)
    authority = _mount_authority(docker_host_path)
    service = object.__new__(ManagedCodexReflectorService)
    service.mount_authority = authority
    service.docker_host_path = docker_host_path
    service.receipts_root = tmp_path / "receipts"
    service.receipts_root.mkdir()
    session_dir = tmp_path / "session"
    credential_dir = tmp_path / "credentials"
    session_dir.mkdir()
    credential_dir.mkdir()

    class _Runtime:
        session_id = "reflector-cleanup-failed-canary"
        absence_proven = False
        execs: ClassVar[list[str]] = []

        async def start(self) -> None:
            return None

        async def exec(self, command, **kwargs):
            del kwargs
            self.execs.append(command)
            assert "/opt/codex/bin/codex" not in command
            return SimpleNamespace(
                return_code=0,
                stdout=f"{os.getuid()}\n{os.getgid()}\n",
                stderr="",
            )

        async def stop(self) -> None:
            if stop_fails:
                raise RuntimeError("synthetic stop failure")

        def managed_reflector_container_mount_receipt(self):
            return _container_adoption_receipt(authority, self.session_id)

    runtime = _Runtime()

    def _create_runtime(self, request):
        del self, request
        return runtime, (
            runtime.session_id,
            session_dir,
            None,
            credential_dir,
            None,
            None,
        )

    service._create_runtime = MethodType(_create_runtime, service)

    message = "synthetic stop failure" if stop_fails else "absence was not proven"
    with pytest.raises(RuntimeError, match=message):
        service.credential_mount_readiness(default_managed_reflector_runtime())

    assert session_dir.is_dir()
    assert credential_dir.is_dir()
    assert len(runtime.execs) == 1
    assert list(service.receipts_root.glob("*.credential-mount-readiness.json")) == []


def test_tampered_adoption_receipt_prevents_codex_readiness_exec(
    tmp_path: Path,
) -> None:
    docker_host_path = _docker_host_path(tmp_path)
    authority = _mount_authority(docker_host_path)
    service = object.__new__(ManagedCodexReflectorService)
    service.mount_authority = authority
    service.receipts_root = tmp_path / "receipts"
    service.receipts_root.mkdir()
    session_dir = tmp_path / "session"
    credential_dir = tmp_path / "credentials"
    session_dir.mkdir()
    credential_dir.mkdir()

    class _Runtime:
        session_id = "reflector-tampered-adoption"
        execs = 0

        async def start(self) -> None:
            return None

        async def exec(self, *args, **kwargs):
            del args, kwargs
            self.execs += 1
            raise AssertionError("Codex must not start after receipt tampering")

        def managed_reflector_container_mount_receipt(self):
            valid = _container_adoption_receipt(authority, self.session_id)
            return valid.model_copy(update={"adoption_nonce_sha256": "9" * 64})

    runtime = _Runtime()

    def _create_runtime(self, request):
        del self, request
        return runtime, (
            runtime.session_id,
            session_dir,
            None,
            credential_dir,
            None,
            None,
        )

    async def _cleanup(self, runtime_arg, **kwargs):
        del self, runtime_arg, kwargs

    service._create_runtime = MethodType(_create_runtime, service)
    service._stop_and_cleanup = MethodType(_cleanup, service)

    with pytest.raises(ValueError, match="adoption receipt digest"):
        service.readiness(
            default_managed_reflector_runtime(),
            credential_mount_readiness=(
                _credential_mount_readiness_receipt(authority)
            ),
        )

    assert runtime.execs == 0
    assert list(service.receipts_root.iterdir()) == []


def test_runtime_readiness_is_bound_to_mount_and_executes_only_codex_version(
    tmp_path: Path,
) -> None:
    docker_host_path = _docker_host_path(tmp_path)
    authority = _mount_authority(docker_host_path)
    mount = _credential_mount_readiness_receipt(authority)
    service = object.__new__(ManagedCodexReflectorService)
    service.mount_authority = authority
    service.docker_host_path = docker_host_path
    service.receipts_root = tmp_path / "receipts"
    service.receipts_root.mkdir()
    session_dir = tmp_path / "session"
    credential_dir = tmp_path / "credentials"
    session_dir.mkdir()
    credential_dir.mkdir()

    class _Runtime:
        session_id = "reflector-runtime-canary"
        commands: ClassVar[list[str]] = []

        async def start(self) -> None:
            return None

        async def exec(self, command, **kwargs):
            del kwargs
            self.commands.append(command)
            assert command == "/opt/codex/bin/codex --version"
            assert " exec " not in command
            return SimpleNamespace(
                return_code=0,
                stdout="codex-cli 0.144.1\n",
                stderr="",
            )

        def managed_reflector_container_mount_receipt(self):
            return _container_adoption_receipt(authority, self.session_id)

    runtime = _Runtime()

    def _create_runtime(self, request):
        del self, request
        return runtime, (
            runtime.session_id,
            session_dir,
            None,
            credential_dir,
            None,
            None,
        )

    async def _cleanup(self, runtime_arg, **kwargs):
        del self, runtime_arg, kwargs
        credential_dir.rmdir()
        session_dir.rmdir()

    service._create_runtime = MethodType(_create_runtime, service)
    service._stop_and_cleanup = MethodType(_cleanup, service)

    raw = service.readiness(
        default_managed_reflector_runtime(),
        credential_mount_readiness=mount,
    )
    receipt = ManagedReflectorRuntimeReadiness.model_validate(raw)
    ManagedReflectorMountRegistrationExpectation.from_authority(
        authority
    ).verify_runtime_readiness(receipt, credential_mount=mount)

    assert runtime.commands == ["/opt/codex/bin/codex --version"]
    assert receipt.actual_cli_version == "0.144.1"
    assert receipt.codex_cli_started is True
    assert receipt.model_started is False
    assert receipt.credential_mount_content_sha256 == mount.content_sha256
    assert not session_dir.exists()
    assert not credential_dir.exists()


def test_adoption_receipt_rejects_daemon_release_identity_drift(
    tmp_path: Path,
) -> None:
    docker_host_path = _docker_host_path(tmp_path)
    authority = _mount_authority(docker_host_path)
    service = object.__new__(ManagedCodexReflectorService)
    service.mount_authority = authority

    valid = _container_adoption_receipt(authority, "reflector-release-drift")
    payload = valid.model_dump(mode="json")
    payload["daemon_release_identity"] = "0" * 64
    payload["content_sha256"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in payload.items() if key != "content_sha256"},
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()
    drifted = ManagedReflectorContainerMountAdoptionReceipt.model_validate(payload)
    runtime = SimpleNamespace(
        session_id="reflector-release-drift",
        managed_reflector_container_mount_receipt=lambda: drifted,
    )

    with pytest.raises(RuntimeError, match="identity mismatch"):
        service._require_container_adoption_receipt(runtime)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("image_digest", "sha256:" + "0" * 64),
        ("codex_binary", "/usr/bin/codex"),
        ("expected_cli_version", "0.145.0"),
        ("path_fallback_allowed", True),
        ("auth_mode", "api_key"),
        ("capture_mode", "none"),
    ],
)
def test_managed_reflector_identity_drift_is_rejected(field: str, value: object) -> None:
    payload = default_managed_reflector_runtime().model_dump(mode="python")
    payload[field] = value
    with pytest.raises(ValueError, match="Core release"):
        ManagedReflectorRuntimeConfig.model_validate(payload)


def test_codex_cli_requires_explicit_runtime_and_absolute_legacy_binary() -> None:
    base = WorkerClaimedJob(
        job_id="job-1",
        lease_id="lease-1",
        job_type="reflect",
        method="text_memory_reflector",
        config={"reflector_llm": {"provider": "codex_cli", "model": "gpt-5.5"}},
    )
    with pytest.raises(ValueError, match="explicit runtime"):
        methods._reflector_llm_config(base)
    payload = base.model_dump(mode="python")
    payload["config"]["reflector_llm"].update(
        {
            "runtime": {"mode": "legacy_path", "path_fallback_allowed": True},
            "codex_bin": "codex",
        }
    )
    with pytest.raises(ValueError, match="absolute codex_bin"):
        methods._reflector_llm_config(WorkerClaimedJob.model_validate(payload))


def test_legacy_method_receives_core_reflector_service_only_during_invocation(
    tmp_path: Path,
) -> None:
    runtime = default_managed_reflector_runtime()
    envelope = build_execution_envelope(
        plan_id="plan-1",
        plan_digest="a" * 64,
        registry_snapshot_digest="b" * 64,
        target_id="agent_system",
        method_id="agent_system_reflector",
        method_identity_digest="c" * 64,
        user_config={},
        core_config={},
        input_bindings=(),
        output_artifact_types=("agent_system",),
    )
    job = WorkerClaimedJob(
        job_id="job-1",
        lease_id="lease-1",
        job_type="reflect",
        method="agent_system_reflector",
        config={},
    )
    reflector = _Reflector()
    context = MethodExecutionContext(
        job=job,
        artifact_root=tmp_path,
        envelope=envelope,
        services=MethodExecutionServices(harness=_Harness(), reflector=reflector),
    )

    def legacy(
        projected: WorkerClaimedJob, artifact_root: Path
    ) -> list[ArtifactRegisterRequest]:
        del projected, artifact_root
        response = require_active_reflector_service().infer(
            ReflectorInferenceRequest(
                request_id="request-1",
                prompt="reflect synthetic trajectory",
                model_name="gpt-5.5",
                reasoning_effort="high",
                timeout_seconds=900,
                runtime=runtime,
            )
        )
        assert response.text == "# Managed result"
        return []

    assert invoke_legacy_method(legacy, context) == []
    assert len(reflector.requests) == 1
    with pytest.raises(ValueError, match="unavailable"):
        require_active_reflector_service()


def test_managed_reflector_preserves_hashed_failure_receipt(tmp_path: Path) -> None:
    service = object.__new__(ManagedCodexReflectorService)
    service.root = tmp_path
    service.receipts_root = tmp_path / "receipts"
    service.transcripts_root = tmp_path / "transcripts"
    service.receipts_root.mkdir()
    service.transcripts_root.mkdir()
    session_id = "rcb_oe_v0_reflector_failed"
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    class _Runtime:
        calls = 0

        async def start(self) -> None:
            return None

        async def exec(self, *args, **kwargs):
            del args, kwargs
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(
                    return_code=0, stdout="codex-cli 0.144.1\n", stderr=""
                )
            return SimpleNamespace(
                return_code=1,
                stdout='{"type":"error","message":"synthetic failure"}\n',
                stderr="synthetic stderr\n",
            )

    runtime = _Runtime()

    def _create_runtime(self, request):
        del self, request
        return runtime, (session_id, session_dir, None, tmp_path / "credential", None, None)

    async def _cleanup(self, runtime_arg, **kwargs):
        del self, runtime_arg, kwargs

    service._create_runtime = MethodType(_create_runtime, service)
    service._stop_and_cleanup = MethodType(_cleanup, service)
    request = ReflectorInferenceRequest(
        request_id="failed-request",
        prompt="synthetic prompt",
        model_name="gpt-5.5",
        reasoning_effort="high",
        timeout_seconds=30,
        runtime=default_managed_reflector_runtime(),
    )

    with pytest.raises(RuntimeError, match="execution failed"):
        service.infer(request)

    receipt = (
        service.receipts_root / f"{session_id}.failure.json"
    ).read_text(encoding="utf-8")
    assert '"exit_status": 1' in receipt
    assert '"path_fallback_allowed": false' in receipt
    assert (service.transcripts_root / f"{session_id}.failed.jsonl").is_file()
    assert (service.transcripts_root / f"{session_id}.failed.stderr").is_file()


def test_managed_reflector_renders_policy_overrides_as_two_cli_arguments() -> None:
    request = ReflectorInferenceRequest(
        request_id="render-request",
        prompt="synthetic prompt",
        model_name="gpt-5.5",
        reasoning_effort="high",
        timeout_seconds=30,
        runtime=default_managed_reflector_runtime(),
    )

    command = _managed_reflector_exec_command(request)

    assert "-c 'default_permissions=" in command
    assert "'-c '" not in command
    assert command.endswith("--output-last-message /openevo/session/last-message.md -")
