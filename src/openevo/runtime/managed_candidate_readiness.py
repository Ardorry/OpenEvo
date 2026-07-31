"""No-model Docker readiness for the managed Codex candidate runtime.

The candidate pre-intent gate must prove more than image availability.  This
module exercises the same :class:`DockerRuntime` credential-mount contract
used by Gateway sessions, verifies the in-container credential view and the
release-pinned Codex binary, then removes the canary container and private
snapshot before returning a non-secret receipt.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openevo.gateway.session_files import (
    HeldCodexCredentialAuthority,
    PreparedCodexCredentialSnapshot,
    remove_credential_tree,
    remove_session_tree,
    stage_codex_subscription_auth,
)
from openevo.runtime.codex_isolation import (
    CODEX_SUBSCRIPTION_POLICY_ID,
    CODEX_SUBSCRIPTION_POLICY_SHA256,
    CodexSubscriptionIsolationError,
    codex_subscription_isolation_probe_command,
    codex_subscription_sandbox_smoke_command,
    parse_codex_sandbox_helper_diagnostic,
    validate_codex_subscription_isolation_result,
    validate_codex_subscription_sandbox_smoke_result,
)
from openevo.runtime.docker_host import DockerHostPathSpec, HeldDockerSessionRoot
from openevo.runtime.factory import create_runtime
from openevo.runtime.managed import (
    MANAGED_CODEX_BINARY,
    MANAGED_CODEX_HOME,
    MANAGED_CODEX_READINESS_WORKSPACE,
    MANAGED_CODEX_VERSION,
    MANAGED_HOME,
    MANAGED_PATH,
    MANAGED_RUNTIME_RELEASES,
    MANAGED_SUBSCRIPTION_ENV,
    MANAGED_WORKSPACE,
    ManagedCredentialMount,
)
from openevo.runtime.models import RuntimeSpec

_SHA256 = r"^[0-9a-f]{64}$"
_VERSION = re.compile(r"^codex-cli\s+([0-9]+\.[0-9]+\.[0-9]+)$")


def managed_candidate_runtime_probe_failure_frame(
    exc: BaseException,
    *,
    generation_digest: str,
    release_registry_digest: str,
) -> dict[str, object]:
    """Return one closed, non-secret child terminal frame for Core supervision."""

    if isinstance(exc, CodexSubscriptionIsolationError):
        failure_code = exc.code
        phase = "subscription_isolation"
        codex_cli_started = True
    else:
        failure_code = "managed_candidate_probe_failed"
        phase = "managed_runtime_probe"
        codex_cli_started = False
    frame: dict[str, object] = {
        "component": "managed_candidate_subscription_isolation",
        "event": "managed_candidate_runtime_probe_terminal",
        "status": "failed",
        "failure_code": failure_code,
        "phase": phase,
        "generation_digest": generation_digest,
        "release_registry_digest": release_registry_digest,
        "process_uid": os.getuid(),
        "codex_cli_started": codex_cli_started,
        "model_started": False,
        "retryable": False,
        "secret_recorded": False,
    }
    if isinstance(exc, CodexSubscriptionIsolationError):
        if exc.nested_return_code is not None:
            frame["nested_return_code"] = exc.nested_return_code
        if exc.probe_progress is not None:
            frame["probe_progress"] = exc.probe_progress
        if exc.stderr_class is not None:
            frame["stderr_class"] = exc.stderr_class
        if exc.helper_started is not None:
            frame["helper_started"] = exc.helper_started
        if exc.helper_return_code is not None:
            frame["helper_return_code"] = exc.helper_return_code
        if exc.missing_executable_basename is not None:
            frame["missing_executable_basename"] = exc.missing_executable_basename
    return frame


def managed_candidate_readiness_failure_code(service_error_code: object) -> str:
    """Project one trusted Gateway error into the closed readiness code set."""

    if (
        isinstance(service_error_code, str)
        and service_error_code in CodexSubscriptionIsolationError._CODES
    ):
        return f"CANDIDATE_SUBSCRIPTION_ISOLATION_NOT_READY:{service_error_code}"
    return "CANDIDATE_RUNTIME_NOT_READY"


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()


class ManagedCandidateRuntimeReadiness(BaseModel):
    """Generation-bound, non-secret receipt for one candidate canary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["openevo.managed_candidate_runtime_readiness.v1"] = (
        "openevo.managed_candidate_runtime_readiness.v1"
    )
    generation_digest: str = Field(pattern=_SHA256)
    release_registry_digest: str = Field(pattern=_SHA256)
    framework_lock_digest: str = Field(pattern=_SHA256)
    runtime_profile: Literal["managed_science"]
    runtime_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    docker_host_path_identity: str = Field(pattern=_SHA256)
    codex_binary: Literal["/opt/codex/bin/codex"]
    expected_cli_version: Literal["0.144.1"]
    actual_cli_version: Literal["0.144.1"]
    auth_mode: Literal["subscription"]
    capture_mode: Literal["transcript"]
    path_fallback_allowed: Literal[False]
    authority_issued: Literal[True]
    docker_mount_created: Literal[True]
    container_path_visible: Literal[True]
    container_user_can_read: Literal[True]
    auth_file_read_only: Literal[True]
    codex_cli_auth_visible: Literal[True]
    host_source_hidden: Literal[True]
    tool_sandbox_credential_hidden: Literal[True]
    tool_environment_clean: Literal[True]
    parent_process_secret_unreadable: Literal[True]
    generation_matches: Literal[True]
    image_digest_matches: Literal[True]
    adoption_verified: Literal[True]
    cleanup_verified: Literal[True]
    codex_cli_started: Literal[True]
    isolation_policy_id: str
    isolation_policy_sha256: str = Field(pattern=_SHA256)
    isolation_probe_no_model: Literal[True]
    model_started: Literal[False]
    exit_status: Literal[0]
    created_at: str
    content_sha256: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def _content_identity(self) -> ManagedCandidateRuntimeReadiness:
        payload = self.model_dump(mode="json", exclude={"content_sha256"})
        if self.content_sha256 != _canonical_sha256(payload):
            raise ValueError("managed candidate readiness digest is invalid")
        return self


class ManagedCandidateRuntimeProbe:
    """Launch one disposable managed container without making a model call."""

    def __init__(
        self,
        *,
        root: Path,
        credential_authority: (
            HeldCodexCredentialAuthority | PreparedCodexCredentialSnapshot
        ),
        docker_host_path: DockerHostPathSpec,
        generation_digest: str,
        release_registry_digest: str,
        framework_lock_digest: str,
    ) -> None:
        self.root = Path(os.path.abspath(root))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.credential_authority = credential_authority
        self.docker_host_path = docker_host_path
        self.generation_digest = generation_digest
        self.release_registry_digest = release_registry_digest
        self.framework_lock_digest = framework_lock_digest

    async def verify(self) -> ManagedCandidateRuntimeReadiness:
        self.credential_authority.verify()
        held_root = HeldDockerSessionRoot.open(self.docker_host_path)
        session_dir: Path | None = None
        credential_dir: Path | None = None
        session_identity = None
        credential_identity = None
        auth_identity = None
        runtime = None
        cleanup_authorized = False
        try:
            session_id = f"openevo_candidate_readiness_{secrets.token_hex(12)}"
            session_dir, session_identity = held_root.create_private_directory("session")
            credential_dir, credential_identity = held_root.create_private_directory(
                "credentials"
            )
            for relative in (
                "workspace",
                "home",
                "home/.openevo-codex-readiness",
                "logs",
                "logs/agent",
            ):
                path = session_dir / relative
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(path, 0o700)
            snapshot = self.credential_authority.prepare_snapshot()
            try:
                staged = stage_codex_subscription_auth(
                    source=Path("/nonexistent-openevo-candidate-readiness-auth"),
                    prepared_snapshot=snapshot,
                    session_dir=credential_dir,
                    session_identity=credential_identity,
                    target_home_parts=(),
                )
            finally:
                snapshot.close()
            auth_identity = staged.auth_identity
            release = MANAGED_RUNTIME_RELEASES["managed_science"]
            runtime = create_runtime(
                RuntimeSpec(
                    backend="docker",
                    profile="managed_science",
                    container_user="host",
                    image=release.loaded_image_id,
                    env={
                        "HOME": MANAGED_HOME,
                        "PATH": MANAGED_PATH,
                        "CODEX_HOME": MANAGED_CODEX_HOME,
                    },
                    network="host",
                    workdir=MANAGED_WORKSPACE,
                    cpus=2,
                    memory_mb=4096,
                    gpus=0,
                    allow_internet=False,
                    allow_model_control_plane_network=True,
                ),
                session_id,
                session_dir,
                credential_mount=ManagedCredentialMount(
                    root=credential_dir,
                    root_identity=credential_identity,
                    auth_identity=auth_identity,
                ),
                docker_ownership_root=self.root / "docker-ownership",
                docker_host_path=self.docker_host_path,
            )
            await runtime.start()
            dependency_probe = await runtime.exec(
                "/usr/bin/test -x /usr/bin/bwrap && "
                "/usr/bin/test -x /opt/codex/bin/codex-linux-sandbox",
                cwd=MANAGED_WORKSPACE,
                env=dict(MANAGED_SUBSCRIPTION_ENV),
                timeout_sec=30.0,
            )
            if dependency_probe.return_code != 0:
                raise CodexSubscriptionIsolationError(
                    "sandbox_runtime_dependency_unavailable",
                    nested_return_code=dependency_probe.return_code,
                    probe_progress="not_started",
                    stderr_class="unclassified",
                )
            read_probe = await runtime.exec(
                (
                    "/bin/sh -ceu '"
                    f"test -f {shlex.quote(MANAGED_CODEX_HOME + '/auth.json')}; "
                    f"test -r {shlex.quote(MANAGED_CODEX_HOME + '/auth.json')}; "
                    f"test ! -w {shlex.quote(MANAGED_CODEX_HOME + '/auth.json')}; "
                    "/usr/bin/id -u; /usr/bin/id -g'"
                ),
                cwd=MANAGED_WORKSPACE,
                env=dict(MANAGED_SUBSCRIPTION_ENV),
                timeout_sec=60.0,
            )
            lines = (read_probe.stdout or "").strip().splitlines()
            if (
                read_probe.return_code != 0
                or lines != [str(os.getuid()), str(os.getgid())]
            ):
                raise RuntimeError("managed candidate credential read probe failed")
            version = await runtime.exec(
                f"{shlex.quote(MANAGED_CODEX_BINARY)} --version",
                cwd=MANAGED_WORKSPACE,
                env=dict(MANAGED_SUBSCRIPTION_ENV),
                timeout_sec=60.0,
            )
            match = _VERSION.fullmatch((version.stdout or "").strip())
            actual = None if match is None else match.group(1)
            if version.return_code != 0 or actual != MANAGED_CODEX_VERSION:
                raise RuntimeError("managed candidate Codex CLI version drifted")
            sandbox_smoke = await runtime.exec(
                codex_subscription_sandbox_smoke_command(allow_internet=False),
                cwd=MANAGED_CODEX_READINESS_WORKSPACE,
                env=dict(MANAGED_SUBSCRIPTION_ENV),
                timeout_sec=60.0,
            )
            helper_started = None
            helper_return_code = None
            missing_executable_basename = None
            if sandbox_smoke.return_code != 0:
                helper_diagnostic = await runtime.exec(
                    (
                        "/bin/sh -c 'if test -r "
                        "/tmp/.openevo-sandbox-helper-state; then "
                        "/bin/cat /tmp/.openevo-sandbox-helper-state; fi'"
                    ),
                    cwd=MANAGED_CODEX_READINESS_WORKSPACE,
                    env=dict(MANAGED_SUBSCRIPTION_ENV),
                    timeout_sec=30.0,
                )
                if helper_diagnostic.return_code == 0:
                    (
                        helper_started,
                        helper_return_code,
                        missing_executable_basename,
                    ) = parse_codex_sandbox_helper_diagnostic(
                        helper_diagnostic.stdout,
                    )
            validate_codex_subscription_sandbox_smoke_result(
                return_code=sandbox_smoke.return_code,
                stderr=sandbox_smoke.stderr,
                probe_progress="credential_smoke",
                helper_started=helper_started,
                helper_return_code=helper_return_code,
                missing_executable_basename=missing_executable_basename,
            )
            isolation = await runtime.exec(
                codex_subscription_isolation_probe_command(
                    allow_internet=False,
                ),
                cwd=MANAGED_CODEX_READINESS_WORKSPACE,
                env=dict(MANAGED_SUBSCRIPTION_ENV),
                timeout_sec=60.0,
            )
            validate_codex_subscription_isolation_result(
                return_code=isolation.return_code,
                stdout=isolation.stdout,
                stderr=isolation.stderr,
            )
        finally:
            try:
                if runtime is not None:
                    await runtime.stop()
                    if getattr(runtime, "absence_proven", False) is not True:
                        raise RuntimeError(
                            "managed candidate canary container absence was not proven"
                        )
                cleanup_authorized = True
            finally:
                # Never delete a bind-mounted credential/session tree until the
                # runtime has proved that its owned container is absent.  A
                # failed stop intentionally leaves the private trees in place
                # for typed-owner recovery instead of risking use-after-delete.
                if cleanup_authorized:
                    if (
                        credential_dir is not None
                        and credential_identity is not None
                        and auth_identity is not None
                    ):
                        remove_credential_tree(
                            credential_dir,
                            credential_identity,
                            auth_identity,
                        )
                    if session_dir is not None and session_identity is not None:
                        remove_session_tree(session_dir, session_identity)
                held_root.close()
        assert session_dir is not None and credential_dir is not None
        if session_dir.exists() or credential_dir.exists():
            raise RuntimeError("managed candidate readiness cleanup failed")
        payload: dict[str, object] = {
            "schema_version": "openevo.managed_candidate_runtime_readiness.v1",
            "generation_digest": self.generation_digest,
            "release_registry_digest": self.release_registry_digest,
            "framework_lock_digest": self.framework_lock_digest,
            "runtime_profile": "managed_science",
            "runtime_digest": MANAGED_RUNTIME_RELEASES["managed_science"].trusted_digest,
            "docker_host_path_identity": self.docker_host_path.identity_digest,
            "codex_binary": MANAGED_CODEX_BINARY,
            "expected_cli_version": MANAGED_CODEX_VERSION,
            "actual_cli_version": MANAGED_CODEX_VERSION,
            "auth_mode": "subscription",
            "capture_mode": "transcript",
            "path_fallback_allowed": False,
            "authority_issued": True,
            "docker_mount_created": True,
            "container_path_visible": True,
            "container_user_can_read": True,
            "auth_file_read_only": True,
            "codex_cli_auth_visible": True,
            "host_source_hidden": True,
            "tool_sandbox_credential_hidden": True,
            "tool_environment_clean": True,
            "parent_process_secret_unreadable": True,
            "generation_matches": True,
            "image_digest_matches": True,
            "adoption_verified": True,
            "cleanup_verified": True,
            "codex_cli_started": True,
            "isolation_policy_id": CODEX_SUBSCRIPTION_POLICY_ID,
            "isolation_policy_sha256": CODEX_SUBSCRIPTION_POLICY_SHA256,
            "isolation_probe_no_model": True,
            "model_started": False,
            "exit_status": 0,
            "created_at": datetime.now(UTC).isoformat(),
        }
        payload["content_sha256"] = _canonical_sha256(payload)
        return ManagedCandidateRuntimeReadiness.model_validate(payload)


__all__ = [
    "ManagedCandidateRuntimeProbe",
    "ManagedCandidateRuntimeReadiness",
    "managed_candidate_readiness_failure_code",
    "managed_candidate_runtime_probe_failure_frame",
]
