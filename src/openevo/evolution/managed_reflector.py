"""Core-managed Codex subscription runtime for evolution reflectors."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shlex
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from openevo.evolution.framework.execution import (
    ManagedReflectorRuntimeConfig,
    ReflectorInferenceRequest,
    ReflectorInferenceResponse,
    ReflectorRuntimeReceipt,
)
from openevo.gateway.session_files import (
    PreparedCodexCredentialSnapshot,
    capture_session_root_identity,
    remove_credential_tree,
    remove_session_tree,
    stage_codex_subscription_auth,
)
from openevo.harness.presets._subscription import command_with_unset_proxy_env
from openevo.runtime.codex_isolation import codex_subscription_cli_flags
from openevo.runtime.docker_host import DockerHostPathSpec, HeldDockerSessionRoot
from openevo.runtime.factory import create_runtime
from openevo.runtime.managed import (
    MANAGED_CODEX_BINARY,
    MANAGED_CODEX_HOME,
    MANAGED_CODEX_VERSION,
    MANAGED_HOME,
    MANAGED_PATH,
    MANAGED_RUNTIME_RELEASES,
    MANAGED_SUBSCRIPTION_ENV,
    MANAGED_WORKSPACE,
    ManagedCredentialMount,
)
from openevo.runtime.managed_reflector_mount import (
    ManagedReflectorContainerMountAdoptionReceipt,
    ManagedReflectorCredentialMountReadiness,
    ManagedReflectorMountAuthority,
    ManagedReflectorRuntimeReadiness,
)
from openevo.runtime.models import RuntimeSpec

_VERSION_RE: Final[re.Pattern[str]] = re.compile(r"^codex-cli\s+([0-9]+\.[0-9]+\.[0-9]+)$")
_MAX_CAPTURE_BYTES: Final[int] = 4 * 1024 * 1024


class ManagedCodexReflectorService:
    """Run each reflection in a fresh, credential-isolated managed container."""

    def __init__(
        self,
        *,
        root: Path,
        credential_snapshot: PreparedCodexCredentialSnapshot,
        docker_host_path: DockerHostPathSpec | None = None,
        mount_authority: ManagedReflectorMountAuthority | None = None,
        require_docker_host_path: bool = False,
        session_prefix: str = "openevo_reflector_",
    ) -> None:
        self.root = Path(os.path.abspath(root))
        self.credential_snapshot = credential_snapshot
        if require_docker_host_path and docker_host_path is None:
            raise ValueError("managed reflector Docker host path authority is required")
        if mount_authority is not None and mount_authority.docker_host_path != docker_host_path:
            raise ValueError("managed reflector mount authority mapping is inconsistent")
        self.docker_host_path = docker_host_path
        self.mount_authority = mount_authority
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,63}", session_prefix) is None:
            raise ValueError("managed reflector session prefix is invalid")
        self.session_prefix = session_prefix
        self.sessions_root = self.root / "sessions"
        self.credentials_root = self.root / "credentials"
        self.receipts_root = self.root / "receipts"
        self.transcripts_root = self.root / "transcripts"
        for path in (
            self.root,
            self.sessions_root,
            self.credentials_root,
            self.receipts_root,
            self.transcripts_root,
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
            self._require_private_directory(path)
        self.credential_snapshot.verify()
        # Open the held mapping only after all non-mapping constructor checks
        # have succeeded.  Otherwise an invalid prefix/root/snapshot could
        # strand the stable root descriptor before the service object exists
        # and can be closed.
        self._docker_session_root = (
            None
            if docker_host_path is None
            else HeldDockerSessionRoot.open(docker_host_path)
        )

    def readiness(
        self,
        config: ManagedReflectorRuntimeConfig,
        *,
        credential_mount_readiness: (
            ManagedReflectorCredentialMountReadiness | None
        ) = None,
    ) -> dict[str, object]:
        """Verify image, binary and version without making a model request."""

        request = ReflectorInferenceRequest(
            request_id=f"reflector-readiness-{secrets.token_hex(8)}",
            prompt="readiness-only",
            model_name="readiness-only",
            reasoning_effort="high",
            timeout_seconds=60.0,
            runtime=config,
        )
        return asyncio.run(
            self._readiness(
                request,
                credential_mount_readiness=credential_mount_readiness,
            )
        )

    def infer(self, request: ReflectorInferenceRequest) -> ReflectorInferenceResponse:
        request = ReflectorInferenceRequest.model_validate(request)
        return asyncio.run(self._infer(request))

    def credential_mount_readiness(
        self,
        config: ManagedReflectorRuntimeConfig,
    ) -> dict[str, object]:
        """Prove Docker credential adoption without starting Codex or a model."""

        if self.mount_authority is None or self.docker_host_path is None:
            raise RuntimeError("managed reflector mount authority is unavailable")
        request = ReflectorInferenceRequest(
            request_id=f"reflector-mount-readiness-{secrets.token_hex(8)}",
            prompt="mount-readiness-only",
            model_name="mount-readiness-only",
            reasoning_effort="high",
            timeout_seconds=60.0,
            runtime=config,
        )
        return asyncio.run(self._credential_mount_readiness(request))

    async def _credential_mount_readiness(
        self,
        request: ReflectorInferenceRequest,
    ) -> dict[str, object]:
        authority = getattr(self, "mount_authority", None)
        if authority is None:
            raise RuntimeError("managed reflector mount authority is unavailable")
        runtime, ownership = self._create_runtime(request)
        (
            session_id,
            session_dir,
            session_identity,
            credential_dir,
            credential_identity,
            auth_identity,
        ) = ownership
        started = False
        readable = False
        read_only = False
        observed_uid: int | None = None
        observed_gid: int | None = None
        container_receipt: ManagedReflectorContainerMountAdoptionReceipt | None = None
        try:
            # DockerRuntime.start() already verifies the exact translated bind
            # sources, auth read-only flag, pinned inodes, process identity and
            # session adoption marker before returning.
            await runtime.start()
            started = True
            container_receipt = self._require_container_adoption_receipt(runtime)
            self._publish_json(
                self.receipts_root
                / f"{session_id}.credential-mount-adoption.json",
                container_receipt.model_dump(mode="json"),
            )
            probe = await runtime.exec(
                (
                    "/bin/sh -ceu '"
                    f"test -f {shlex.quote(MANAGED_CODEX_HOME + '/auth.json')}; "
                    f"test -r {shlex.quote(MANAGED_CODEX_HOME + '/auth.json')}; "
                    f"test ! -w {shlex.quote(MANAGED_CODEX_HOME + '/auth.json')}; "
                    "/usr/bin/id -u; /usr/bin/id -g'"
                ),
                cwd=MANAGED_WORKSPACE,
                env=dict(MANAGED_SUBSCRIPTION_ENV),
                timeout_sec=min(request.timeout_seconds, 60.0),
            )
            lines = (probe.stdout or "").strip().splitlines()
            if probe.return_code != 0 or len(lines) != 2:
                raise RuntimeError("managed reflector credential read probe failed")
            try:
                observed_uid, observed_gid = (int(lines[0]), int(lines[1]))
            except ValueError as exc:
                raise RuntimeError(
                    "managed reflector credential read identity is invalid"
                ) from exc
            if (observed_uid, observed_gid) != (os.getuid(), os.getgid()):
                raise RuntimeError("managed reflector credential reader identity changed")
            readable = True
            read_only = True
        finally:
            await self._stop_and_cleanup(
                runtime,
                session_dir=session_dir,
                session_identity=session_identity,
                credential_dir=credential_dir,
                credential_identity=credential_identity,
                auth_identity=auth_identity,
            )
        cleanup_verified = not session_dir.exists() and not credential_dir.exists()
        if not cleanup_verified:
            raise RuntimeError("managed reflector credential readiness cleanup failed")
        if not (started and readable and read_only):
            raise RuntimeError("managed reflector credential mount readiness failed")
        if container_receipt is None:
            raise RuntimeError("managed reflector container adoption receipt is missing")
        grant = authority.grant
        receipt: dict[str, object] = {
            "schema_version": "openevo.managed_reflector_credential_mount_readiness.v1",
            "authority_id": authority.authority_id,
            "container_authority_id": container_receipt.container_authority_id,
            "worker_launch_id": grant.worker_launch_id,
            "container_launch_id": session_id,
            "adoption_nonce_sha256": container_receipt.adoption_nonce_sha256,
            "service_identity_digest": grant.service_identity_digest,
            "runtime_profile": request.runtime.profile,
            "runtime_digest": request.runtime.image_digest,
            "docker_host_path_identity": self.docker_host_path.identity_digest,
            "generation_digest": grant.generation_digest,
            "daemon_release_identity": grant.daemon_release_identity,
            "release_install_digest": grant.release_install_digest,
            "release_registry_digest": grant.release_registry_digest,
            "credential_target": MANAGED_CODEX_HOME,
            "container_uid": observed_uid,
            "container_gid": observed_gid,
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
            "created_at": datetime.now(UTC).isoformat(),
        }
        receipt["content_sha256"] = hashlib.sha256(
            json.dumps(
                receipt,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        validated = ManagedReflectorCredentialMountReadiness.model_validate(receipt)
        self._publish_json(
            self.receipts_root / f"{session_id}.credential-mount-readiness.json",
            validated.model_dump(mode="json"),
        )
        return validated.model_dump(mode="json")

    async def _readiness(
        self,
        request: ReflectorInferenceRequest,
        *,
        credential_mount_readiness: (
            ManagedReflectorCredentialMountReadiness | None
        ),
    ) -> dict[str, object]:
        authority = getattr(self, "mount_authority", None)
        if authority is not None and credential_mount_readiness is None:
            raise RuntimeError(
                "managed reflector runtime readiness requires mount readiness"
            )
        runtime, ownership = self._create_runtime(request)
        (
            session_id,
            session_dir,
            session_identity,
            credential_dir,
            credential_identity,
            auth_identity,
        ) = ownership
        container_receipt: ManagedReflectorContainerMountAdoptionReceipt | None = None
        actual: str | None = None
        exit_status: int | None = None
        try:
            await runtime.start()
            if authority is not None:
                container_receipt = self._require_container_adoption_receipt(runtime)
                self._publish_json(
                    self.receipts_root
                    / f"{session_id}.runtime-readiness-adoption.json",
                    container_receipt.model_dump(mode="json"),
                )
            version = await runtime.exec(
                f"{shlex.quote(request.runtime.codex_binary)} --version",
                cwd=MANAGED_WORKSPACE,
                env=dict(MANAGED_SUBSCRIPTION_ENV),
                timeout_sec=min(request.timeout_seconds, 60.0),
            )
            exit_status = version.return_code
            if version.return_code != 0:
                raise RuntimeError("managed reflector Codex version canary failed")
            actual = self._parse_version(version.stdout or "")
            if actual != request.runtime.expected_cli_version:
                raise RuntimeError("managed reflector Codex CLI version drifted")
        finally:
            await self._stop_and_cleanup(
                runtime,
                session_dir=session_dir,
                session_identity=session_identity,
                credential_dir=credential_dir,
                credential_identity=credential_identity,
                auth_identity=auth_identity,
            )
        cleanup_verified = not session_dir.exists() and not credential_dir.exists()
        if not cleanup_verified:
            raise RuntimeError("managed reflector runtime readiness cleanup failed")
        if actual is None or exit_status is None:
            raise RuntimeError("managed reflector runtime readiness is incomplete")
        if authority is None:
            # Development mode remains explicit and does not claim a release
            # worker authority.  Formal training never accepts this receipt.
            receipt: dict[str, object] = {
                "schema_version": "openevo.managed_reflector_readiness.v1",
                "session_id": session_id,
                "runtime_profile": request.runtime.profile,
                "runtime_digest": request.runtime.image_digest,
                "codex_binary": request.runtime.codex_binary,
                "actual_cli_version": actual,
                "model": request.model_name,
                "auth_mode": request.runtime.auth_mode,
                "capture_mode": request.runtime.capture_mode,
                "path_fallback_allowed": request.runtime.path_fallback_allowed,
                "exit_status": exit_status,
                "created_at": datetime.now(UTC).isoformat(),
            }
            self._publish_json(
                self.receipts_root / f"{session_id}.readiness.json", receipt
            )
            return receipt
        if container_receipt is None or credential_mount_readiness is None:
            raise RuntimeError("managed reflector runtime adoption proof is missing")
        grant = authority.grant
        receipt = {
            "schema_version": "openevo.managed_reflector_readiness.v1",
            "authority_id": authority.authority_id,
            "container_authority_id": container_receipt.container_authority_id,
            "worker_launch_id": grant.worker_launch_id,
            "container_launch_id": session_id,
            "adoption_nonce_sha256": container_receipt.adoption_nonce_sha256,
            "service_identity_digest": grant.service_identity_digest,
            "generation_digest": grant.generation_digest,
            "daemon_release_identity": grant.daemon_release_identity,
            "release_install_digest": grant.release_install_digest,
            "release_registry_digest": grant.release_registry_digest,
            "runtime_profile": request.runtime.profile,
            "runtime_digest": request.runtime.image_digest,
            "docker_host_path_identity": self.docker_host_path.identity_digest,
            "credential_mount_content_sha256": (
                credential_mount_readiness.content_sha256
            ),
            "codex_binary": request.runtime.codex_binary,
            "expected_cli_version": request.runtime.expected_cli_version,
            "actual_cli_version": actual,
            "model": request.model_name,
            "auth_mode": request.runtime.auth_mode,
            "capture_mode": request.runtime.capture_mode,
            "path_fallback_allowed": request.runtime.path_fallback_allowed,
            "exit_status": exit_status,
            "container_authority_verified": True,
            "adoption_receipt_valid": True,
            "cleanup_verified": True,
            "codex_cli_started": True,
            "model_started": False,
            "created_at": datetime.now(UTC).isoformat(),
        }
        receipt["content_sha256"] = hashlib.sha256(
            json.dumps(
                receipt,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        validated = ManagedReflectorRuntimeReadiness.model_validate(receipt)
        self._publish_json(
            self.receipts_root / f"{session_id}.readiness.json",
            validated.model_dump(mode="json"),
        )
        return validated.model_dump(mode="json")

    async def _infer(self, request: ReflectorInferenceRequest) -> ReflectorInferenceResponse:
        runtime, ownership = self._create_runtime(request)
        (
            session_id,
            session_dir,
            session_identity,
            credential_dir,
            credential_identity,
            auth_identity,
        ) = ownership
        transcript = ""
        try:
            await runtime.start()
            if getattr(self, "mount_authority", None) is not None:
                self._publish_container_adoption_receipt(runtime)
            version_result = await runtime.exec(
                f"{shlex.quote(request.runtime.codex_binary)} --version",
                cwd=MANAGED_WORKSPACE,
                env=dict(MANAGED_SUBSCRIPTION_ENV),
                timeout_sec=min(request.timeout_seconds, 60.0),
            )
            actual_version = self._parse_version(version_result.stdout or "")
            if (
                version_result.return_code != 0
                or actual_version != request.runtime.expected_cli_version
            ):
                raise RuntimeError("managed reflector Codex CLI version canary failed")
            prompt_path = session_dir / "reflector-prompt.txt"
            prompt_path.write_text(request.prompt, encoding="utf-8")
            os.chmod(prompt_path, 0o600)
            output_path = session_dir / "last-message.md"
            command = _managed_reflector_exec_command(request)
            command = command_with_unset_proxy_env(command)
            command += " < /openevo/session/reflector-prompt.txt"
            result = await runtime.exec(
                command,
                cwd=MANAGED_WORKSPACE,
                env=dict(MANAGED_SUBSCRIPTION_ENV),
                timeout_sec=request.timeout_seconds,
            )
            transcript = result.stdout or ""
            if len(transcript.encode("utf-8")) > _MAX_CAPTURE_BYTES:
                raise RuntimeError("managed reflector transcript exceeds capture limit")
            if result.return_code != 0:
                # A failed model operation is still immutable audit evidence.  Keep
                # the raw CLI streams in this private reflector namespace and
                # publish only hashes/identity in the machine receipt.  This also
                # prevents a retry from erasing whether a billed request started.
                transcript_bytes = transcript.encode("utf-8")
                stderr_bytes = (result.stderr or "").encode("utf-8")
                transcript_path = self.transcripts_root / f"{session_id}.failed.jsonl"
                stderr_path = self.transcripts_root / f"{session_id}.failed.stderr"
                self._publish_bytes(transcript_path, transcript_bytes)
                self._publish_bytes(stderr_path, stderr_bytes)
                self._publish_json(
                    self.receipts_root / f"{session_id}.failure.json",
                    {
                        "schema_version": "openevo.managed_reflector_failure.v1",
                        "request_id": request.request_id,
                        "session_id": session_id,
                        "runtime_profile": request.runtime.profile,
                        "runtime_digest": request.runtime.image_digest,
                        "codex_binary": request.runtime.codex_binary,
                        "actual_cli_version": actual_version,
                        "model_name": request.model_name,
                        "reasoning_effort": request.reasoning_effort,
                        "auth_mode": request.runtime.auth_mode,
                        "capture_mode": request.runtime.capture_mode,
                        "path_fallback_allowed": False,
                        "exit_status": result.return_code,
                        "transcript_sha256": hashlib.sha256(transcript_bytes).hexdigest(),
                        "stderr_sha256": hashlib.sha256(stderr_bytes).hexdigest(),
                        "created_at": datetime.now(UTC).isoformat(),
                    },
                )
                raise RuntimeError("managed reflector Codex execution failed")
            if not output_path.exists():
                raise RuntimeError("managed reflector produced no final message")
            text = output_path.read_text(encoding="utf-8").strip()
            if not text:
                raise RuntimeError("managed reflector returned empty content")
            transcript_sha256 = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
            transcript_path = self.transcripts_root / f"{session_id}.jsonl"
            self._publish_bytes(transcript_path, transcript.encode("utf-8"))
            receipt = ReflectorRuntimeReceipt(
                request_id=request.request_id,
                session_id=session_id,
                runtime_profile=request.runtime.profile,
                runtime_digest=request.runtime.image_digest.removeprefix("sha256:"),
                codex_binary=request.runtime.codex_binary,
                actual_cli_version=actual_version,
                model_name=request.model_name,
                reasoning_effort=request.reasoning_effort,
                auth_mode=request.runtime.auth_mode,
                capture_mode=request.runtime.capture_mode,
                path_fallback_allowed=False,
                exit_status=result.return_code,
                transcript_sha256=transcript_sha256,
            )
            self._publish_json(
                self.receipts_root / f"{session_id}.execution.json",
                receipt.model_dump(mode="json"),
            )
            return ReflectorInferenceResponse(
                request_id=request.request_id,
                text=text,
                receipt=receipt,
            )
        finally:
            await self._stop_and_cleanup(
                runtime,
                session_dir=session_dir,
                session_identity=session_identity,
                credential_dir=credential_dir,
                credential_identity=credential_identity,
                auth_identity=auth_identity,
            )

    def _create_runtime(self, request: ReflectorInferenceRequest):
        release = MANAGED_RUNTIME_RELEASES[request.runtime.profile]  # type: ignore[index]
        if release.trusted_digest != request.runtime.image_digest:
            raise ValueError("managed reflector runtime digest is not release-owned")
        session_id = f"{self.session_prefix}{secrets.token_hex(12)}"
        session_dir: Path | None = None
        credential_dir: Path | None = None
        session_identity = None
        credential_identity = None
        try:
            if self._docker_session_root is None:
                session_dir = self.sessions_root / session_id
                credential_dir = self.credentials_root / session_id
                session_dir.mkdir(mode=0o700)
                session_identity = capture_session_root_identity(session_dir)
                credential_dir.mkdir(mode=0o700)
                credential_identity = capture_session_root_identity(credential_dir)
            else:
                self._docker_session_root.verify()
                session_dir, session_identity = (
                    self._docker_session_root.create_private_directory("session")
                )
                credential_dir, credential_identity = (
                    self._docker_session_root.create_private_directory("credentials")
                )
        except BaseException:
            if credential_dir is not None and credential_identity is not None:
                remove_session_tree(credential_dir, credential_identity)
            if session_dir is not None and session_identity is not None:
                remove_session_tree(session_dir, session_identity)
            raise
        assert session_dir is not None
        assert credential_dir is not None
        assert session_identity is not None
        assert credential_identity is not None
        for relative in ("workspace", "home", "logs", "logs/agent"):
            path = session_dir / relative
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
        snapshot = self.credential_snapshot.prepare_snapshot()
        try:
            staged = stage_codex_subscription_auth(
                source=Path("/nonexistent-openevo-reflector-auth"),
                prepared_snapshot=snapshot,
                session_dir=credential_dir,
                session_identity=credential_identity,
                target_home_parts=(),
            )
        finally:
            snapshot.close()
        credential_mount = ManagedCredentialMount(
            root=credential_dir,
            root_identity=credential_identity,
            auth_identity=staged.auth_identity,
        )
        container_mount_authority = (
            None
            if self.mount_authority is None
            else self.mount_authority.issue_container_authority(
                container_launch_id=session_id,
                credential_root_identity=credential_identity,
                credential_auth_identity=staged.auth_identity,
            )
        )
        spec = RuntimeSpec(
            backend="docker",
            profile=request.runtime.profile,  # type: ignore[arg-type]
            container_user="host",
            # The release archive is loaded by its verified offline image ID;
            # ``request.runtime.image_digest`` remains the signed release
            # authority recorded in every readiness/execution receipt.
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
        )
        try:
            runtime = create_runtime(
                spec,
                session_id,
                session_dir,
                credential_mount=credential_mount,
                docker_ownership_root=self.root / "docker-ownership",
                docker_host_path=self.docker_host_path,
                managed_reflector_mount_authority=self.mount_authority,
                managed_reflector_container_mount_authority=(
                    container_mount_authority
                ),
            )
        except BaseException:
            remove_credential_tree(
                credential_dir,
                credential_identity,
                staged.auth_identity,
            )
            remove_session_tree(session_dir, session_identity)
            raise
        return runtime, (
            session_id,
            session_dir,
            session_identity,
            credential_dir,
            credential_identity,
            staged.auth_identity,
        )

    def _require_container_adoption_receipt(
        self,
        runtime,
    ) -> ManagedReflectorContainerMountAdoptionReceipt:
        authority = self.mount_authority
        if authority is None:
            raise RuntimeError("managed reflector mount authority is unavailable")
        receipt_factory = getattr(
            runtime,
            "managed_reflector_container_mount_receipt",
            None,
        )
        if not callable(receipt_factory):
            raise TypeError(
                "managed reflector runtime lacks container adoption receipts"
            )
        receipt = ManagedReflectorContainerMountAdoptionReceipt.model_validate(
            receipt_factory()
        )
        grant = authority.grant
        if (
            receipt.parent_authority_id != authority.authority_id
            or receipt.worker_launch_id != grant.worker_launch_id
            or receipt.container_launch_id != runtime.session_id
            or receipt.service_identity_digest != grant.service_identity_digest
            or receipt.generation_digest != grant.generation_digest
            or receipt.daemon_release_identity != grant.daemon_release_identity
            or receipt.release_install_digest != grant.release_install_digest
            or receipt.release_registry_digest != grant.release_registry_digest
            or receipt.runtime_profile != grant.runtime_profile
            or receipt.runtime_digest != grant.runtime_image
            or receipt.docker_host_path_identity
            != grant.docker_host_path.identity_digest
        ):
            raise RuntimeError(
                "managed reflector container adoption receipt identity mismatch"
            )
        return receipt

    def _publish_container_adoption_receipt(self, runtime) -> None:
        receipt = self._require_container_adoption_receipt(runtime)
        self._publish_json(
            self.receipts_root
            / f"{receipt.container_launch_id}.credential-mount-adoption.json",
            receipt.model_dump(mode="json"),
        )

    def close(self) -> None:
        """Release the held Docker mapping authority after the worker exits."""

        if self._docker_session_root is not None:
            self._docker_session_root.close()
            self._docker_session_root = None

    async def _stop_and_cleanup(
        self,
        runtime,
        *,
        session_dir: Path,
        session_identity,
        credential_dir: Path,
        credential_identity,
        auth_identity,
    ) -> None:
        await runtime.stop()
        if getattr(runtime, "absence_proven", False) is not True:
            raise RuntimeError(
                "managed reflector container absence was not proven; "
                "private cleanup roots were retained"
            )
        remove_credential_tree(credential_dir, credential_identity, auth_identity)
        remove_session_tree(session_dir, session_identity)

    @staticmethod
    def _parse_version(value: str) -> str:
        match = _VERSION_RE.fullmatch(value.strip())
        if match is None:
            raise RuntimeError("managed reflector Codex version output is invalid")
        return match.group(1)

    @staticmethod
    def _require_private_directory(path: Path) -> None:
        opened = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) != 0o700
        ):
            raise ValueError("managed reflector root is not private")

    @staticmethod
    def _publish_json(path: Path, value: object) -> None:
        payload = json.dumps(value, indent=2, sort_keys=True, allow_nan=False).encode("utf-8")
        ManagedCodexReflectorService._publish_bytes(path, payload)

    @staticmethod
    def _publish_bytes(path: Path, payload: bytes) -> None:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("managed reflector receipt write failed")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def default_managed_reflector_runtime() -> ManagedReflectorRuntimeConfig:
    release = MANAGED_RUNTIME_RELEASES["managed_science"]
    return ManagedReflectorRuntimeConfig(
        mode="managed",
        profile="managed_science",
        image_digest=release.trusted_digest,
        codex_binary=MANAGED_CODEX_BINARY,
        expected_cli_version=MANAGED_CODEX_VERSION,
        auth_mode="subscription",
        capture_mode="transcript",
        path_fallback_allowed=False,
    )


def _managed_reflector_exec_command(request: ReflectorInferenceRequest) -> str:
    """Render Codex 0.144.1 arguments without re-quoting shell-ready policy flags."""

    # ``codex_subscription_cli_flags`` deliberately returns shell-ready
    # fragments such as ``-c 'key=value'``.  Treating each fragment as a single
    # argv value changes the key to ``'key`` and causes strict-config rejection.
    # Caller-owned model fields remain individually shell quoted, while the
    # Core-owned isolation profile stays final among config overrides.
    policy_flags = " ".join(codex_subscription_cli_flags(allow_internet=False))
    return " ".join(
        (
            shlex.quote(request.runtime.codex_binary),
            "exec",
            "--json",
            "--ephemeral",
            "--skip-git-repo-check",
            "--model",
            shlex.quote(request.model_name),
            "-c",
            shlex.quote(f"model_reasoning_effort={request.reasoning_effort}"),
            policy_flags,
            "--output-last-message",
            "/openevo/session/last-message.md",
            "-",
        )
    )


__all__ = ["ManagedCodexReflectorService", "default_managed_reflector_runtime"]
