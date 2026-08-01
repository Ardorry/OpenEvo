"""Generation-bound attachment to the host-global managed OpenEvo Core.

The bearer is issued and rotated by :mod:`openevo.backend.service`.  This
module deliberately keeps it inside the trusted training process as a
``SecretStr`` carried by the formal ``CoreServiceAttachment``.  It is never
copied into the process environment, protocol, supervisor state, or receipts.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
from typing import Any, Iterator

from pydantic import SecretStr

from openevo.backend.runtime_identity import default_core_service_root
from openevo.backend.service import (
    CoreServiceAttachment,
    authenticate_core_service_endpoint,
    consume_core_service_attachment,
)
from openevo.deployment.host_keys import ProviderKnownHostStore
from openevo.deployment.profile import RemoteProfileConfig, SSHAuthConfig
from openevo.deployment.ssh import SshRemoteExecutorTransport
from openevo.runtime.docker_host import (
    DockerEngineAuthority,
    discover_docker_host_path,
    docker_self_inspect_argv,
)

from .config import ExperimentConfig


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_GENERATION = re.compile(r"[0-9a-f]{32}\Z")


class ManagedCoreControlUnavailable(RuntimeError):
    """The exact managed Core authority could not be acquired or verified."""


@contextmanager
def _managed_core_attach_lock(
    experiment_root: Path,
    *,
    deadline_seconds: float,
) -> Iterator[None]:
    """Serialize Daemon observe/ensure across formal runner and monitor processes."""

    runtime_root = experiment_root / "runtime"
    if (
        deadline_seconds <= 0
        or runtime_root.is_symlink()
        or not runtime_root.is_dir()
        or runtime_root.stat().st_uid != os.getuid()
    ):
        raise ManagedCoreControlUnavailable("managed Core attach lock root is unsafe")
    path = runtime_root / ".managed-core-attach.lock"
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except OSError as exc:
        raise ManagedCoreControlUnavailable(
            "managed Core attach lock is unavailable"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise ManagedCoreControlUnavailable("managed Core attach lock is unsafe")
        deadline = time.monotonic() + deadline_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ManagedCoreControlUnavailable(
                        "managed Core attach lock timed out"
                    ) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class ManagedCoreControlAuthority:
    """Opaque trusted-process projection of one Core service attachment."""

    base_url: str
    service_identity_id: str
    generation: str
    release_identity: str
    registry_digest: str
    source_commit: str
    status_proof: str
    attached: bool
    managed_host_profile_ready: bool
    _bearer: SecretStr = field(repr=False, compare=False)
    _lifetime: tuple[Any, ...] = field(default=(), repr=False, compare=False)
    _host_profile: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.base_url.startswith("http://127.0.0.1:"):
            raise ValueError("managed Core URL is not loopback")
        if _GENERATION.fullmatch(self.generation) is None:
            raise ValueError("managed Core generation is invalid")
        if _SHA256.fullmatch(self.release_identity) is None:
            raise ValueError("managed Core release identity is invalid")
        if _SHA256.fullmatch(self.registry_digest) is None:
            raise ValueError("managed Core registry identity is invalid")
        if _COMMIT.fullmatch(self.source_commit) is None:
            raise ValueError("managed Core source commit is invalid")
        if not self._bearer.get_secret_value():
            raise ValueError("managed Core bearer is absent")

    @classmethod
    def from_verified_attachment(
        cls,
        attachment: CoreServiceAttachment,
        *,
        managed_host_profile_ready: bool = True,
    ) -> "ManagedCoreControlAuthority":
        material = json.dumps(
            {
                "generation": attachment.generation,
                "release_identity": attachment.release_identity,
                "registry_digest": attachment.registry_digest,
                "source_commit": attachment.source_commit,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return cls(
            base_url=f"http://127.0.0.1:{attachment.port}",
            service_identity_id=f"core-service-{hashlib.sha256(material).hexdigest()[:24]}",
            generation=attachment.generation,
            release_identity=attachment.release_identity,
            registry_digest=attachment.registry_digest,
            source_commit=attachment.source_commit,
            status_proof=attachment.status_proof,
            attached=attachment.attached,
            managed_host_profile_ready=managed_host_profile_ready,
            _bearer=SecretStr(attachment.bearer_token),
        )

    def bearer_value_for_core_client(self) -> str:
        """Return the secret only to the trusted Core client constructor."""

        return self._bearer.get_secret_value()

    def public_readiness(self) -> dict[str, Any]:
        """Return the complete non-secret launch/attach receipt projection."""

        value = {
            "service_reachable": True,
            "bearer_present": True,
            "bearer_valid": True,
            "generation_matches": True,
            "candidate_port_authorized": True,
            "secret_recorded": False,
            "service_identity_id": self.service_identity_id,
            "generation": self.generation,
            "release_identity": self.release_identity,
            "registry_digest": self.registry_digest,
            "source_commit": self.source_commit,
            "base_url": self.base_url,
            "attached": self.attached,
            "expires_at": None,
            "bearer_lifecycle": "core_generation",
            "bearer_transport": "in_process_core_service_attachment",
            "environment_fallback_used": False,
            "managed_host_profile_ready": self.managed_host_profile_ready,
        }
        if self._host_profile:
            value["managed_host_profile"] = json.loads(
                json.dumps(self._host_profile, separators=(",", ":"), sort_keys=True)
            )
        return value

    def close(self) -> None:
        """Close only transport resources owned by this in-process authority."""

        for item in self._lifetime:
            close = getattr(item, "close", None)
            if callable(close):
                close()


def acquire_managed_core_control(
    config: ExperimentConfig,
    *,
    deadline_seconds: float = 45.0,
) -> ManagedCoreControlAuthority:
    """Start or attach the exact verified Core release and authenticate it.

    A mismatched live service is never replaced.  ``ensure_core_service`` owns
    bearer issuance/rotation and returns the only authority consumed here.
    """

    mode = str(
        config.raw.get("native_openevo", {}).get(
            "core_control_mode", "managed_host_service"
        )
    )
    if mode == "managed_remote_daemon":
        return _acquire_remote_daemon_core_control(
            config,
            deadline_seconds=deadline_seconds,
        )
    if mode != "managed_host_service":
        raise ManagedCoreControlUnavailable(
            "formal training requires managed_host_service Core control"
        )
    framework_lock = Path(config.require("native_openevo.framework_lock"))
    host_probe_bin = Path(
        config.require("native_openevo.core_host_codex_probe_bin")
    )
    source_commit = str(config.require("source_identity.openevo_commit"))
    if not framework_lock.is_absolute() or not framework_lock.is_file():
        raise ManagedCoreControlUnavailable("verified framework lock is unavailable")
    if _COMMIT.fullmatch(source_commit) is None:
        raise ManagedCoreControlUnavailable("OpenEvo source commit identity is invalid")
    _validate_host_codex_probe_bin(host_probe_bin)
    try:
        attachment = _acquire_isolated_core_service_attachment(
            framework_lock=framework_lock,
            source_commit=source_commit,
            host_probe_bin=host_probe_bin,
            deadline_seconds=deadline_seconds,
        )
        proof = authenticate_core_service_endpoint(
            host="127.0.0.1",
            port=attachment.port,
            bearer=attachment.bearer_token,
            release_identity=attachment.release_identity,
            registry_digest=attachment.registry_digest,
            source_commit=attachment.source_commit,
            generation=attachment.generation,
            deadline=time.monotonic() + min(deadline_seconds, 10.0),
            require_production_v2=True,
        )
        if proof != attachment.status_proof:
            raise ManagedCoreControlUnavailable(
                "managed Core authenticated status proof changed"
            )
        host_profile = managed_core_host_profile_readiness()
        return ManagedCoreControlAuthority.from_verified_attachment(
            attachment,
            managed_host_profile_ready=bool(host_profile["ready"]),
        )
    except ManagedCoreControlUnavailable:
        raise
    except Exception as exc:
        raise ManagedCoreControlUnavailable(
            "managed Core launch/attach failed closed"
        ) from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_private_key(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ManagedCoreControlUnavailable("remote Core SSH key is unavailable") from exc
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
    ):
        raise ManagedCoreControlUnavailable("remote Core SSH key authority is unsafe")


def _remote_fixture_host_profile(config: ExperimentConfig) -> dict[str, Any]:
    """Run the owned release-host fixture's complete read-only check."""

    script = Path(config.require("native_openevo.remote_core.fixture_check_script"))
    if (
        not script.is_absolute()
        or script.is_symlink()
        or not script.is_file()
        or script.name != "docker_release_host_fixture.py"
        or not script.is_relative_to(config.project_root / "OpenEvo" / "scripts" / "e2e")
    ):
        raise ManagedCoreControlUnavailable("remote Core fixture checker is invalid")
    allowed = {
        key: value
        for key in ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "TZ", "DOCKER_HOST")
        if (value := os.environ.get(key)) is not None
    }
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "check",
            "--name",
            str(config.require("native_openevo.remote_core.container_name")),
        ],
        env=allowed,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        close_fds=True,
        timeout=60,
    )
    if completed.returncode != 0 or len(completed.stdout) > 1024 * 1024:
        raise ManagedCoreControlUnavailable("remote Core fixture check failed closed")
    try:
        value = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManagedCoreControlUnavailable("remote Core fixture evidence is invalid") from exc
    observed = value.get("docker_server", {}).get("observed", {})
    supported = value.get("docker_server", {}).get("supported", {})
    fixture = value.get("fixture", {})
    ssh = fixture.get("ssh", {})
    expected_versions = list(
        config.require("native_openevo.remote_core.supported_docker_server_versions")
    )
    if (
        value.get("schema_version") != 2
        or value.get("action") != "check"
        or value.get("outcome") != "ready"
        or value.get("release_host_profile") != "docker_user_container_v1"
        or observed
        != {
            "version": config.require("native_openevo.remote_core.docker_server_version"),
            "api_version": config.require("native_openevo.remote_core.docker_server_api"),
            "os": "linux",
            "architecture": "amd64",
        }
        or supported.get("versions") != expected_versions
        or supported.get("api_versions")
        != [config.require("native_openevo.remote_core.docker_server_api")]
        or supported.get("os") != ["linux"]
        or supported.get("architecture") != ["amd64"]
        or fixture.get("container_name")
        != config.require("native_openevo.remote_core.container_name")
        or fixture.get("container_hostname")
        != config.require("native_openevo.remote_core.container_hostname")
        or fixture.get("docker_socket_accessible") is not True
        or ssh
        != {
            "host": config.require("native_openevo.remote_core.host"),
            "port": config.require("native_openevo.remote_core.port"),
            "reachable": True,
            "user": config.require("native_openevo.remote_core.user"),
        }
    ):
        raise ManagedCoreControlUnavailable("remote Core fixture identity drifted")
    return {
        "ready": True,
        "profile": "docker_user_container_v1",
        "mapping_identity_present": True,
        "reason_code": None,
        "secret_recorded": False,
        "container_hostname": fixture["container_hostname"],
        "docker_server_version": observed["version"],
        "docker_server_api": observed["api_version"],
        "supported_docker_server_versions": expected_versions,
    }


def _acquire_remote_daemon_core_control(
    config: ExperimentConfig,
    *,
    deadline_seconds: float,
) -> ManagedCoreControlAuthority:
    """Attach to the release Daemon through the formal verified SSH transport."""

    remote = config.require("native_openevo.remote_core")
    if not isinstance(remote, dict):
        raise ManagedCoreControlUnavailable("remote Core configuration is invalid")
    key_path = Path(config.require("native_openevo.remote_core.private_key_path"))
    _require_private_key(key_path)
    profile = RemoteProfileConfig(
        id=str(config.require("native_openevo.remote_core.profile_id")),
        host=str(config.require("native_openevo.remote_core.host")),
        port=int(config.require("native_openevo.remote_core.port")),
        user=str(config.require("native_openevo.remote_core.user")),
        auth=SSHAuthConfig(method="private_key", private_key_path=str(key_path)),
    )
    trust_root = Path(config.require("native_openevo.remote_core.host_key_store_root"))
    if (
        not trust_root.is_absolute()
        or not trust_root.is_relative_to(config.experiment_root / "runtime")
        or trust_root.is_symlink()
        or not trust_root.is_dir()
        or trust_root.stat().st_uid != os.getuid()
        or trust_root.stat().st_mode & 0o077
    ):
        raise ManagedCoreControlUnavailable("remote Core host-key store is unsafe")
    fingerprint = str(config.require("native_openevo.remote_core.host_key_fingerprint"))
    binding = ProviderKnownHostStore(trust_root).load(
        profile,
        expected_fingerprint=fingerprint,
    )
    if binding is None or binding.algorithm != str(
        config.require("native_openevo.remote_core.host_key_algorithm")
    ):
        raise ManagedCoreControlUnavailable("remote Core host key is not trusted")
    host_profile = _remote_fixture_host_profile(config)
    bundle = Path(config.require("native_openevo.remote_core.daemon_bundle"))
    manifest = Path(config.require("native_openevo.remote_core.daemon_manifest"))
    framework_lock = Path(config.require("native_openevo.framework_lock"))
    for path in (bundle, manifest, framework_lock):
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise ManagedCoreControlUnavailable("remote Core release asset is unsafe")
    bundle_sha = _sha256_file(bundle)
    manifest_sha = _sha256_file(manifest)
    transport = SshRemoteExecutorTransport(profile, trusted_host=binding)
    tunnel = None
    try:
        with _managed_core_attach_lock(
            config.experiment_root,
            deadline_seconds=deadline_seconds,
        ):
            staged = transport.stage_daemon_bundle(
                bundle_path=str(bundle),
                bundle_sha256=bundle_sha,
                bundle_size=bundle.stat().st_size,
                manifest_path=str(manifest),
                manifest_sha256=manifest_sha,
                manifest_size=manifest.stat().st_size,
                timeout_seconds=min(300.0, max(60.0, deadline_seconds * 4)),
            )
            identity = transport.daemon_bundle_identity(staged, timeout_seconds=30)
            if (
                identity.source_commit
                != config.require("source_identity.openevo_commit")
                or identity.framework_lock_sha256 != _sha256_file(framework_lock)
            ):
                raise ManagedCoreControlUnavailable(
                    "remote Core release identity drifted"
                )
            predecessor = transport.observe_daemon_bundle_service(
                staged,
                canonical_manifest_sha256=manifest_sha,
                timeout_seconds=30,
            )
            attachment, service = transport.ensure_daemon_bundle(
                staged,
                expected_predecessor=predecessor,
                canonical_manifest_sha256=manifest_sha,
                timeout_seconds=min(180.0, max(60.0, deadline_seconds * 3)),
            )
            tunnel = transport.open_tunnel(
                remote_port=attachment.remote_port,
                timeout_seconds=30,
            )
            port = int(tunnel.base_url.rsplit(":", 1)[1])
            proof = authenticate_core_service_endpoint(
                host="127.0.0.1",
                port=port,
                bearer=attachment.bearer_token,
                release_identity=attachment.release_identity,
                registry_digest=attachment.registry_digest,
                source_commit=attachment.source_commit,
                generation=attachment.generation,
                deadline=time.monotonic() + 10,
                require_production_v2=True,
            )
            if (
                proof != attachment.status_proof
                or service.generation != attachment.generation
            ):
                raise ManagedCoreControlUnavailable(
                    "remote Core tunnel identity drifted"
                )
        material = json.dumps(
            {
                "generation": attachment.generation,
                "release_identity": attachment.release_identity,
                "registry_digest": attachment.registry_digest,
                "source_commit": attachment.source_commit,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return ManagedCoreControlAuthority(
            base_url=tunnel.base_url,
            service_identity_id=f"core-service-{hashlib.sha256(material).hexdigest()[:24]}",
            generation=attachment.generation,
            release_identity=attachment.release_identity,
            registry_digest=attachment.registry_digest,
            source_commit=attachment.source_commit,
            status_proof=proof,
            attached=attachment.attached,
            managed_host_profile_ready=True,
            _bearer=SecretStr(attachment.bearer_token),
            _lifetime=(tunnel, transport),
            _host_profile=host_profile,
        )
    except Exception as exc:
        if tunnel is not None:
            tunnel.close()
        transport.close()
        if isinstance(exc, ManagedCoreControlUnavailable):
            raise
        raise ManagedCoreControlUnavailable(
            "remote managed Core launch/attach failed closed"
        ) from exc


def _acquire_isolated_core_service_attachment(
    *,
    framework_lock: Path,
    source_commit: str,
    host_probe_bin: Path,
    deadline_seconds: float,
) -> CoreServiceAttachment:
    """Use the installed locked distribution's formal service launcher.

    The adapter itself is imported from a source tree during development.  An
    isolated child is therefore required so the framework verifier sees only
    the wheel installed from ``framework_lock``.  Core writes its existing
    owner-private one-shot attachment, which this process consumes and unlinks
    immediately.  No bearer is placed in argv, environment, stdout, or disk
    outside Core's canonical private service root.
    """

    attachment_name = f"bootstrap-{secrets.token_hex(16)}.json"
    service_root = default_core_service_root()
    allowed = (
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "TMPDIR",
        "TZ",
        "DOCKER_HOST",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    )
    environment = {
        key: value
        for key in allowed
        if (value := os.environ.get(key)) is not None
    }
    inherited_path = environment.get("PATH", os.defpath)
    environment["PATH"] = f"{host_probe_bin}{os.pathsep}{inherited_path}"
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONSAFEPATH"] = "1"
    environment["PYTHONUNBUFFERED"] = "1"
    command = [
        sys.executable,
        "-I",
        "-m",
        "openevo.backend.service",
        "ensure",
        "--service-root",
        str(service_root),
        "--framework-lock",
        str(framework_lock),
        "--source-commit",
        source_commit,
        "--port",
        "0",
        "--deadline-seconds",
        str(deadline_seconds),
        "--attachment-name",
        attachment_name,
    ]
    completed = subprocess.run(
        command,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
        close_fds=True,
        timeout=deadline_seconds + 5.0,
    )
    if completed.returncode != 0:
        raise ManagedCoreControlUnavailable(
            "isolated managed Core launcher failed closed"
        )
    payload = consume_core_service_attachment(
        service_root=service_root,
        attachment_name=attachment_name,
    )
    if len(payload) > 64 * 1024:
        raise ManagedCoreControlUnavailable("managed Core attachment is oversized")
    value = json.loads(payload)
    required = {
        "port",
        "release_identity",
        "registry_digest",
        "source_commit",
        "generation",
        "status_proof",
        "attached",
        "bearer_token",
        "execution_mode",
        "capture_mode",
    }
    if (
        not isinstance(value, dict)
        or not required.issubset(value)
        or value["execution_mode"] != "subscription"
        or value["capture_mode"] != "transcript"
    ):
        raise ManagedCoreControlUnavailable("managed Core attachment is invalid")
    return CoreServiceAttachment(
        port=int(value["port"]),
        release_identity=str(value["release_identity"]),
        registry_digest=str(value["registry_digest"]),
        source_commit=str(value["source_commit"]),
        generation=str(value["generation"]),
        status_proof=str(value["status_proof"]),
        attached=bool(value["attached"]),
        _bearer=SecretStr(str(value["bearer_token"])),
    )


def _validate_host_codex_probe_bin(path: Path) -> None:
    """Validate the private regular-file shim used by Core readiness only.

    The managed Science runtime probe intentionally binds a host executable
    before it launches the isolated candidate container.  Formal training must
    not let that binding drift to the user's global ``PATH``.  The configured
    directory and its ``codex`` entry are therefore absolute, owner-private,
    non-symlink, link-count-one objects.  The shim merely selects the exact CLI
    extracted from the already pinned managed image; it is not the candidate
    executable and carries no credentials.
    """

    if not path.is_absolute():
        raise ManagedCoreControlUnavailable(
            "managed Core host Codex probe directory must be absolute"
        )
    try:
        directory_stat = path.lstat()
        executable = path / "codex"
        executable_stat = executable.lstat()
    except OSError as exc:
        raise ManagedCoreControlUnavailable(
            "managed Core host Codex probe executable is unavailable"
        ) from exc
    if not path.is_dir() or path.is_symlink():
        raise ManagedCoreControlUnavailable(
            "managed Core host Codex probe directory is not a real directory"
        )
    if directory_stat.st_uid != os.getuid() or directory_stat.st_mode & 0o077:
        raise ManagedCoreControlUnavailable(
            "managed Core host Codex probe directory is not owner-private"
        )
    if not executable.is_file() or executable.is_symlink():
        raise ManagedCoreControlUnavailable(
            "managed Core host Codex probe executable is not a regular file"
        )
    if (
        executable_stat.st_uid != os.getuid()
        or executable_stat.st_nlink != 1
        or not executable_stat.st_mode & 0o100
        or executable_stat.st_mode & 0o077
    ):
        raise ManagedCoreControlUnavailable(
            "managed Core host Codex probe executable permissions are unsafe"
        )


def managed_core_host_profile_readiness() -> dict[str, Any]:
    """Verify the release Docker user-container boundary without mutation.

    The release-mode Core will perform the same self-container and writable
    bind-root proof when it prepares managed Science services.  Surfacing that
    proof here prevents a training namespace or candidate intent from being
    persisted on an unsupported bare host.  No container is created and no
    credential is consulted.
    """

    try:
        engine = DockerEngineAuthority.open()
        command = docker_self_inspect_argv()
        completed = subprocess.run(
            engine.argv(*command[1:]),
            env=engine.environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            close_fds=True,
            timeout=10.0,
        )
        if completed.returncode != 0:
            raise ManagedCoreControlUnavailable(
                "Docker could not inspect the Core user container"
            )
        namespace = "core-" + hashlib.sha256(
            os.fsencode(os.fspath(default_core_service_root().absolute()))
        ).hexdigest()[:24]
        mapping = discover_docker_host_path(
            completed.stdout,
            namespace=namespace,
        )
        return {
            "ready": True,
            "profile": "docker_user_container_v1",
            "mapping_identity_present": bool(mapping.identity_digest),
            "reason_code": None,
            "secret_recorded": False,
        }
    except Exception:
        return {
            "ready": False,
            "profile": "docker_user_container_v1",
            "mapping_identity_present": False,
            "reason_code": "DOCKER_USER_CONTAINER_MAPPING_UNAVAILABLE",
            "secret_recorded": False,
        }


__all__ = [
    "ManagedCoreControlAuthority",
    "ManagedCoreControlUnavailable",
    "acquire_managed_core_control",
    "managed_core_host_profile_readiness",
]
