"""Generation-bound Docker host-path authority for managed reflectors.

The Core supervisor issues this non-secret mapping authority to exactly one
release-owned evolution-worker process.  The authority is authenticated with
the worker's existing internal-service credential and transported through an
inherited descriptor; paths and signatures never need to be placed in the
worker environment or persisted in the service ledger.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openevo.internal_auth import InternalServiceIdentity
from openevo.runtime.docker_host import DockerHostPathSpec

MANAGED_REFLECTOR_MOUNT_AUTHORITY_FD_ENV: Final[str] = (
    "OPENEVO_MANAGED_REFLECTOR_MOUNT_AUTHORITY_FD"
)
_MAX_AUTHORITY_BYTES: Final[int] = 64 * 1024
_MFD_CLOEXEC: Final[int] = 0x0001
_MFD_ALLOW_SEALING: Final[int] = 0x0002
_F_ADD_SEALS: Final[int] = 1033
_F_GET_SEALS: Final[int] = 1034
_F_SEAL_SEAL: Final[int] = 0x0001
_F_SEAL_SHRINK: Final[int] = 0x0002
_F_SEAL_GROW: Final[int] = 0x0004
_F_SEAL_WRITE: Final[int] = 0x0008
_AUTHORITY_SEALS: Final[int] = (
    _F_SEAL_SEAL | _F_SEAL_SHRINK | _F_SEAL_GROW | _F_SEAL_WRITE
)
_DIGEST_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_LAUNCH_ID_RE: Final[re.Pattern[str]] = re.compile(r"^mrl-[0-9a-f]{32}$")


class ManagedReflectorMountAuthorityError(RuntimeError):
    """The inherited managed-reflector mount authority is invalid."""


class ManagedReflectorMountGrant(BaseModel):
    """Closed, non-secret identity authenticated by the Core generation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    service_id: Literal["evolution-worker"] = "evolution-worker"
    service_identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    lifecycle: Literal["service_generation"] = "service_generation"
    generation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    daemon_release_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_install_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_registry_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    framework_lock_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_profile: Literal["managed_science"] = "managed_science"
    runtime_image: str = Field(min_length=1, max_length=512)
    runtime_identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    worker_launch_id: str = Field(pattern=r"^mrl-[0-9a-f]{32}$")
    docker_host_path: DockerHostPathSpec
    generation_lifecycle_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_id: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_derived_identities(self) -> ManagedReflectorMountGrant:
        if self.generation_lifecycle_digest != _digest_json(self._lifecycle_payload()):
            raise ValueError("managed reflector generation lifecycle digest is invalid")
        if self.authority_id != _digest_json(self._authority_payload()):
            raise ValueError("managed reflector mount authority ID is invalid")
        return self

    def _lifecycle_payload(self) -> dict[str, object]:
        return {
            "docker_host_path_identity": self.docker_host_path.identity_digest,
            "daemon_release_identity": self.daemon_release_identity,
            "framework_lock_digest": self.framework_lock_digest,
            "generation_digest": self.generation_digest,
            "lifecycle": self.lifecycle,
            "release_install_digest": self.release_install_digest,
            "release_registry_digest": self.release_registry_digest,
            "runtime_identity_digest": self.runtime_identity_digest,
            "runtime_image": self.runtime_image,
            "runtime_profile": self.runtime_profile,
            "schema_version": self.schema_version,
            "service_id": self.service_id,
            "service_identity_digest": self.service_identity_digest,
        }

    def _authority_payload(self) -> dict[str, object]:
        return {
            **self._lifecycle_payload(),
            "docker_host_path": self.docker_host_path.model_dump(mode="json"),
            "generation_lifecycle_digest": self.generation_lifecycle_digest,
            "worker_launch_id": self.worker_launch_id,
        }


class ManagedReflectorMountAuthority(BaseModel):
    """HMAC-authenticated grant inherited by one evolution worker."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    grant: ManagedReflectorMountGrant
    signature: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)

    @property
    def authority_id(self) -> str:
        return self.grant.authority_id

    @property
    def docker_host_path(self) -> DockerHostPathSpec:
        return self.grant.docker_host_path

    @classmethod
    def issue(
        cls,
        *,
        identity: InternalServiceIdentity,
        daemon_release_identity: str,
        release_install_digest: str,
        runtime_identity_digest: str,
        runtime_image: str,
        worker_launch_id: str,
        docker_host_path: DockerHostPathSpec,
    ) -> ManagedReflectorMountAuthority:
        """Issue one launch-bound authority using the generation credential."""

        if identity.service_id != "evolution-worker":
            raise ValueError("mount authority must target the evolution worker")
        _require_digest(daemon_release_identity, "daemon release identity")
        _require_digest(release_install_digest, "release install digest")
        _require_digest(runtime_identity_digest, "runtime identity digest")
        if _LAUNCH_ID_RE.fullmatch(worker_launch_id) is None:
            raise ValueError("managed reflector worker launch ID is invalid")
        if not runtime_image or len(runtime_image.encode("utf-8")) > 512:
            raise ValueError("managed reflector runtime image identity is invalid")
        lifecycle_payload = {
            "docker_host_path_identity": docker_host_path.identity_digest,
            "daemon_release_identity": daemon_release_identity,
            "framework_lock_digest": identity.framework_lock_digest,
            "generation_digest": identity.generation_digest,
            "lifecycle": "service_generation",
            "release_install_digest": release_install_digest,
            "release_registry_digest": identity.registry_digest,
            "runtime_identity_digest": runtime_identity_digest,
            "runtime_image": runtime_image,
            "runtime_profile": "managed_science",
            "schema_version": 1,
            "service_id": "evolution-worker",
            "service_identity_digest": _digest_json(identity.health_identity()),
        }
        generation_lifecycle_digest = _digest_json(lifecycle_payload)
        authority_payload = {
            **lifecycle_payload,
            "docker_host_path": docker_host_path.model_dump(mode="json"),
            "generation_lifecycle_digest": generation_lifecycle_digest,
            "worker_launch_id": worker_launch_id,
        }
        authority_id = _digest_json(authority_payload)
        grant = ManagedReflectorMountGrant(
            generation_digest=identity.generation_digest,
            daemon_release_identity=daemon_release_identity,
            release_install_digest=release_install_digest,
            release_registry_digest=identity.registry_digest,
            framework_lock_digest=identity.framework_lock_digest,
            service_identity_digest=_digest_json(identity.health_identity()),
            runtime_image=runtime_image,
            runtime_identity_digest=runtime_identity_digest,
            worker_launch_id=worker_launch_id,
            docker_host_path=docker_host_path,
            generation_lifecycle_digest=generation_lifecycle_digest,
            authority_id=authority_id,
        )
        signature = hmac.new(
            identity.credential.encode("utf-8"),
            _canonical_bytes(grant.model_dump(mode="json")),
            hashlib.sha256,
        ).hexdigest()
        return cls(grant=grant, signature=signature)

    def verify(
        self,
        identity: InternalServiceIdentity,
        *,
        expected_worker_launch_id: str,
        expected_docker_host_path_identity: str,
        expected_daemon_release_identity: str,
        expected_release_install_digest: str,
        expected_runtime_profile: str,
        expected_runtime_image: str,
        expected_runtime_identity_digest: str,
    ) -> None:
        """Verify signer, generation, target launch, and exact Docker mapping."""

        grant = self.grant
        if identity.service_id != "evolution-worker" or grant.service_id != identity.service_id:
            raise ManagedReflectorMountAuthorityError(
                "managed reflector mount authority targets another service"
            )
        if not hmac.compare_digest(grant.generation_digest, identity.generation_digest):
            raise ManagedReflectorMountAuthorityError(
                "managed reflector mount authority targets another generation"
            )
        if not hmac.compare_digest(
            grant.release_registry_digest,
            identity.registry_digest,
        ):
            raise ManagedReflectorMountAuthorityError(
                "managed reflector mount authority targets another release registry"
            )
        if not hmac.compare_digest(
            grant.framework_lock_digest,
            identity.framework_lock_digest,
        ):
            raise ManagedReflectorMountAuthorityError(
                "managed reflector mount authority targets another framework lock"
            )
        if not hmac.compare_digest(
            grant.service_identity_digest,
            _digest_json(identity.health_identity()),
        ):
            raise ManagedReflectorMountAuthorityError(
                "managed reflector mount authority targets another service identity"
            )
        if not hmac.compare_digest(grant.worker_launch_id, expected_worker_launch_id):
            raise ManagedReflectorMountAuthorityError(
                "managed reflector mount authority targets another worker launch"
            )
        if not hmac.compare_digest(
            grant.docker_host_path.identity_digest,
            expected_docker_host_path_identity,
        ):
            raise ManagedReflectorMountAuthorityError(
                "managed reflector mount authority Docker mapping changed"
            )
        if not hmac.compare_digest(
            grant.daemon_release_identity,
            expected_daemon_release_identity,
        ):
            raise ManagedReflectorMountAuthorityError(
                "managed reflector mount authority targets another daemon release"
            )
        if not hmac.compare_digest(
            grant.release_install_digest,
            expected_release_install_digest,
        ):
            raise ManagedReflectorMountAuthorityError(
                "managed reflector mount authority targets another release install"
            )
        if not hmac.compare_digest(grant.runtime_profile, expected_runtime_profile):
            raise ManagedReflectorMountAuthorityError(
                "managed reflector mount authority targets another runtime profile"
            )
        if not hmac.compare_digest(grant.runtime_image, expected_runtime_image):
            raise ManagedReflectorMountAuthorityError(
                "managed reflector mount authority targets another runtime image"
            )
        if not hmac.compare_digest(
            grant.runtime_identity_digest,
            expected_runtime_identity_digest,
        ):
            raise ManagedReflectorMountAuthorityError(
                "managed reflector mount authority targets another runtime identity"
            )
        expected_signature = hmac.new(
            identity.credential.encode("utf-8"),
            _canonical_bytes(grant.model_dump(mode="json")),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(self.signature, expected_signature):
            raise ManagedReflectorMountAuthorityError(
                "managed reflector mount authority signature is invalid"
            )

    def inherited_payload(self) -> bytes:
        return _canonical_bytes(self.model_dump(mode="json"))

    def open_inheritance_descriptor(self) -> int:
        """Return one sealed anonymous FD containing the canonical authority."""

        payload = self.inherited_payload()
        if not payload or len(payload) > _MAX_AUTHORITY_BYTES:
            raise ManagedReflectorMountAuthorityError(
                "managed reflector mount authority payload is outside the byte limit"
            )
        descriptor = -1
        try:
            descriptor = os.memfd_create(
                "openevo-reflector-mount-authority",
                _MFD_CLOEXEC | _MFD_ALLOW_SEALING,
            )
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("mount authority descriptor write made no progress")
                offset += written
            os.fsync(descriptor)
            fcntl.fcntl(descriptor, _F_ADD_SEALS, _AUTHORITY_SEALS)
            if fcntl.fcntl(descriptor, _F_GET_SEALS) != _AUTHORITY_SEALS:
                raise OSError("mount authority descriptor did not retain all seals")
            os.lseek(descriptor, 0, os.SEEK_SET)
            result = descriptor
            descriptor = -1
            return result
        except (AttributeError, OSError) as exc:
            raise ManagedReflectorMountAuthorityError(
                "managed reflector mount authority descriptor is unavailable"
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def issue_container_authority(
        self,
        *,
        container_launch_id: str,
        credential_root_identity: tuple[int, int, int],
        credential_auth_identity: tuple[int, int, int, int, int, int, int, int],
    ) -> ManagedReflectorContainerMountAuthority:
        """Derive one HMAC authority for exactly one reflector container."""

        if re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,127}$", container_launch_id) is None:
            raise ValueError("managed reflector container launch ID is invalid")
        adoption_nonce = secrets.token_hex(32)
        grant = self.grant
        payload: dict[str, object] = {
            "schema_version": 1,
            "service_id": grant.service_id,
            "service_identity_digest": grant.service_identity_digest,
            "parent_authority_id": grant.authority_id,
            "worker_launch_id": grant.worker_launch_id,
            "container_launch_id": container_launch_id,
            "adoption_nonce": adoption_nonce,
            "generation_digest": grant.generation_digest,
            "daemon_release_identity": grant.daemon_release_identity,
            "release_install_digest": grant.release_install_digest,
            "release_registry_digest": grant.release_registry_digest,
            "framework_lock_digest": grant.framework_lock_digest,
            "runtime_profile": grant.runtime_profile,
            "runtime_image": grant.runtime_image,
            "runtime_identity_digest": grant.runtime_identity_digest,
            "docker_host_path_identity": grant.docker_host_path.identity_digest,
            "credential_root_identity": credential_root_identity,
            "credential_auth_identity": credential_auth_identity,
        }
        payload["container_authority_id"] = _digest_json(payload)
        container_grant = ManagedReflectorContainerMountGrant.model_validate(payload)
        signature = hmac.new(
            self.signature.encode("ascii"),
            _canonical_bytes(container_grant.model_dump(mode="json")),
            hashlib.sha256,
        ).hexdigest()
        return ManagedReflectorContainerMountAuthority(
            grant=container_grant,
            signature=signature,
        )

    @classmethod
    def from_inherited_environment(
        cls,
        identity: InternalServiceIdentity,
        *,
        expected_worker_launch_id: str,
        expected_docker_host_path_identity: str,
        expected_daemon_release_identity: str,
        expected_release_install_digest: str,
        expected_runtime_profile: str,
        expected_runtime_image: str,
        expected_runtime_identity_digest: str,
        required: bool,
    ) -> ManagedReflectorMountAuthority | None:
        """Consume, authenticate, and close one inherited authority descriptor."""

        raw_descriptor = os.environ.pop(MANAGED_REFLECTOR_MOUNT_AUTHORITY_FD_ENV, None)
        if raw_descriptor is None:
            if required:
                raise ManagedReflectorMountAuthorityError(
                    "release evolution worker is missing managed reflector mount authority"
                )
            return None
        descriptor = -1
        try:
            descriptor = int(raw_descriptor, 10)
            if descriptor < 3 or str(descriptor) != raw_descriptor:
                raise ValueError("authority FD is invalid")
            os.set_inheritable(descriptor, False)
            if fcntl.fcntl(descriptor, _F_GET_SEALS) != _AUTHORITY_SEALS:
                raise ValueError("authority FD is not sealed")
            payload = _read_bounded(descriptor)
            authority = cls.model_validate_json(payload)
            if authority.inherited_payload() != payload:
                raise ValueError("authority payload is not canonical")
            authority.verify(
                identity,
                expected_worker_launch_id=expected_worker_launch_id,
                expected_docker_host_path_identity=expected_docker_host_path_identity,
                expected_daemon_release_identity=expected_daemon_release_identity,
                expected_release_install_digest=expected_release_install_digest,
                expected_runtime_profile=expected_runtime_profile,
                expected_runtime_image=expected_runtime_image,
                expected_runtime_identity_digest=expected_runtime_identity_digest,
            )
            return authority
        except ManagedReflectorMountAuthorityError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise ManagedReflectorMountAuthorityError(
                "inherited managed reflector mount authority is invalid"
            ) from exc
        finally:
            if descriptor >= 3:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


class ManagedReflectorContainerMountGrant(BaseModel):
    """One container-scoped credential-mount adoption grant."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    service_id: Literal["evolution-worker"] = "evolution-worker"
    service_identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_authority_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    worker_launch_id: str = Field(pattern=r"^mrl-[0-9a-f]{32}$")
    container_launch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,127}$")
    adoption_nonce: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)
    generation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    daemon_release_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_install_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_registry_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    framework_lock_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_profile: Literal["managed_science"] = "managed_science"
    runtime_image: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    runtime_identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    docker_host_path_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    credential_root_identity: tuple[int, int, int]
    credential_auth_identity: tuple[int, int, int, int, int, int, int, int]
    container_authority_id: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _verify_authority_id(self) -> ManagedReflectorContainerMountGrant:
        expected = _digest_json(
            self.model_dump(mode="json", exclude={"container_authority_id"})
        )
        if not hmac.compare_digest(self.container_authority_id, expected):
            raise ValueError("managed reflector container authority ID is invalid")
        return self


class ManagedReflectorContainerMountAuthority(BaseModel):
    """Parent-authenticated authority for one Docker container launch."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    grant: ManagedReflectorContainerMountGrant
    signature: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)

    @property
    def authority_id(self) -> str:
        return self.grant.container_authority_id

    def verify(
        self,
        parent: ManagedReflectorMountAuthority,
        *,
        expected_container_launch_id: str,
        expected_docker_host_path_identity: str,
        expected_credential_root_identity: tuple[int, int, int],
        expected_credential_auth_identity: tuple[int, int, int, int, int, int, int, int],
    ) -> None:
        grant = self.grant
        parent_grant = parent.grant
        expected = {
            "service_id": parent_grant.service_id,
            "service_identity_digest": parent_grant.service_identity_digest,
            "parent_authority_id": parent.authority_id,
            "worker_launch_id": parent_grant.worker_launch_id,
            "generation_digest": parent_grant.generation_digest,
            "daemon_release_identity": parent_grant.daemon_release_identity,
            "release_install_digest": parent_grant.release_install_digest,
            "release_registry_digest": parent_grant.release_registry_digest,
            "framework_lock_digest": parent_grant.framework_lock_digest,
            "runtime_profile": parent_grant.runtime_profile,
            "runtime_image": parent_grant.runtime_image,
            "runtime_identity_digest": parent_grant.runtime_identity_digest,
            "docker_host_path_identity": expected_docker_host_path_identity,
            "container_launch_id": expected_container_launch_id,
            "credential_root_identity": expected_credential_root_identity,
            "credential_auth_identity": expected_credential_auth_identity,
        }
        observed = {
            key: getattr(grant, key)
            for key in expected
        }
        if observed != expected:
            raise ManagedReflectorMountAuthorityError(
                "managed reflector container mount authority identity mismatch"
            )
        expected_signature = hmac.new(
            parent.signature.encode("ascii"),
            _canonical_bytes(grant.model_dump(mode="json")),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(self.signature, expected_signature):
            raise ManagedReflectorMountAuthorityError(
                "managed reflector container mount authority signature is invalid"
            )


class ManagedReflectorContainerMountAdoptionReceipt(BaseModel):
    """Non-secret proof that one container adopted its exact child authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[
        "openevo.managed_reflector_container_mount_adoption.v1"
    ]
    parent_authority_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    container_authority_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    worker_launch_id: str = Field(pattern=r"^mrl-[0-9a-f]{32}$")
    container_launch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,127}$")
    adoption_nonce_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    service_identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    daemon_release_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_install_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_registry_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_profile: Literal["managed_science"]
    runtime_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    docker_host_path_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    credential_root_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    credential_auth_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    container_authority_verified: Literal[True]
    adoption_receipt_valid: Literal[True]
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _verify_content_digest(
        self,
    ) -> ManagedReflectorContainerMountAdoptionReceipt:
        expected = _digest_json(
            self.model_dump(mode="json", exclude={"content_sha256"})
        )
        if not hmac.compare_digest(self.content_sha256, expected):
            raise ValueError(
                "managed reflector container adoption receipt digest is invalid"
            )
        return self


class ManagedReflectorCredentialMountReadiness(BaseModel):
    """Non-secret adoption receipt registered before a worker may claim jobs."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[
        "openevo.managed_reflector_credential_mount_readiness.v1"
    ]
    authority_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    container_authority_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    worker_launch_id: str = Field(pattern=r"^mrl-[0-9a-f]{32}$")
    container_launch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,127}$")
    adoption_nonce_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    service_identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_profile: Literal["managed_science"]
    runtime_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    docker_host_path_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    daemon_release_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_install_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_registry_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    credential_target: Literal["/openevo/credentials/codex"]
    container_uid: int = Field(ge=0)
    container_gid: int = Field(ge=0)
    authority_issued: Literal[True]
    docker_mount_created: Literal[True]
    container_path_visible: Literal[True]
    container_user_can_read: Literal[True]
    auth_file_read_only: Literal[True]
    generation_matches: Literal[True]
    release_identity_matches: Literal[True]
    adoption_receipt_valid: Literal[True]
    container_authority_verified: Literal[True]
    cleanup_verified: Literal[True]
    codex_cli_started: Literal[False]
    model_started: Literal[False]
    created_at: str = Field(min_length=20, max_length=40)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _verify_content_digest(self) -> ManagedReflectorCredentialMountReadiness:
        expected = _digest_json(
            self.model_dump(mode="json", exclude={"content_sha256"})
        )
        if not hmac.compare_digest(self.content_sha256, expected):
            raise ValueError("managed reflector mount readiness digest is invalid")
        return self


class ManagedReflectorRuntimeReadiness(BaseModel):
    """No-model proof that the registered worker can execute pinned Codex.

    This receipt is deliberately separate from credential-mount readiness.  A
    release worker first proves the mount contract without starting Codex, then
    launches a second fresh container and executes only ``codex --version``.
    The two receipts are content-addressed and tied to the same worker authority
    so a stale CLI canary cannot be combined with a current mount adoption.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["openevo.managed_reflector_readiness.v1"] = (
        "openevo.managed_reflector_readiness.v1"
    )
    authority_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    container_authority_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    worker_launch_id: str = Field(pattern=r"^mrl-[0-9a-f]{32}$")
    container_launch_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,127}$")
    adoption_nonce_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    service_identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    daemon_release_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_install_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_registry_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime_profile: Literal["managed_science"]
    runtime_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    docker_host_path_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    credential_mount_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    codex_binary: Literal["/opt/codex/bin/codex"]
    expected_cli_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    actual_cli_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    model: Literal["readiness-only"]
    auth_mode: Literal["subscription"]
    capture_mode: Literal["transcript"]
    path_fallback_allowed: Literal[False]
    exit_status: Literal[0]
    container_authority_verified: Literal[True]
    adoption_receipt_valid: Literal[True]
    cleanup_verified: Literal[True]
    codex_cli_started: Literal[True]
    model_started: Literal[False]
    created_at: str = Field(min_length=20, max_length=40)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _verify_content_digest(self) -> ManagedReflectorRuntimeReadiness:
        expected = _digest_json(
            self.model_dump(mode="json", exclude={"content_sha256"})
        )
        if not hmac.compare_digest(self.content_sha256, expected):
            raise ValueError("managed reflector runtime readiness digest is invalid")
        if self.actual_cli_version != self.expected_cli_version:
            raise ValueError("managed reflector runtime readiness version drifted")
        return self


class ManagedReflectorMountRegistrationExpectation(BaseModel):
    """Core-issued public identity used to fence worker registration.

    The evolution backend receives this non-secret expectation directly from
    the Core supervisor.  A worker may register (and therefore claim work)
    only when its no-model Docker adoption receipt matches every field.  This
    closes the interval between worker self-registration and the supervisor's
    asynchronous health probe without transferring the mount authority HMAC
    to the backend.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[
        "openevo.managed_reflector_mount_registration_expectation.v1"
    ] = "openevo.managed_reflector_mount_registration_expectation.v1"
    authority_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    service_identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    worker_launch_id: str = Field(pattern=r"^mrl-[0-9a-f]{32}$")
    runtime_profile: Literal["managed_science"] = "managed_science"
    runtime_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    docker_host_path_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    daemon_release_identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_install_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_registry_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    container_uid: int = Field(ge=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_authority(
        cls,
        authority: ManagedReflectorMountAuthority,
    ) -> ManagedReflectorMountRegistrationExpectation:
        grant = authority.grant
        payload: dict[str, object] = {
            "schema_version": (
                "openevo.managed_reflector_mount_registration_expectation.v1"
            ),
            "authority_id": authority.authority_id,
            "service_identity_digest": grant.service_identity_digest,
            "worker_launch_id": grant.worker_launch_id,
            "runtime_profile": grant.runtime_profile,
            "runtime_digest": grant.runtime_image,
            "docker_host_path_identity": grant.docker_host_path.identity_digest,
            "generation_digest": grant.generation_digest,
            "daemon_release_identity": grant.daemon_release_identity,
            "release_install_digest": grant.release_install_digest,
            "release_registry_digest": grant.release_registry_digest,
            "container_uid": grant.docker_host_path.runtime_uid,
        }
        payload["content_sha256"] = _digest_json(payload)
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def _verify_content_digest(
        self,
    ) -> ManagedReflectorMountRegistrationExpectation:
        expected = _digest_json(
            self.model_dump(mode="json", exclude={"content_sha256"})
        )
        if not hmac.compare_digest(self.content_sha256, expected):
            raise ValueError(
                "managed reflector mount registration expectation digest is invalid"
            )
        return self

    def verify_readiness(
        self,
        readiness: ManagedReflectorCredentialMountReadiness,
    ) -> None:
        observed = {
            "authority_id": readiness.authority_id,
            "service_identity_digest": readiness.service_identity_digest,
            "worker_launch_id": readiness.worker_launch_id,
            "runtime_profile": readiness.runtime_profile,
            "runtime_digest": readiness.runtime_digest,
            "docker_host_path_identity": readiness.docker_host_path_identity,
            "generation_digest": readiness.generation_digest,
            "daemon_release_identity": readiness.daemon_release_identity,
            "release_install_digest": readiness.release_install_digest,
            "release_registry_digest": readiness.release_registry_digest,
            "container_uid": readiness.container_uid,
        }
        expected = self.model_dump(
            mode="json",
            exclude={"schema_version", "content_sha256"},
        )
        if observed != expected:
            raise ManagedReflectorMountAuthorityError(
                "managed reflector mount readiness does not match Core expectation"
            )

    def verify_runtime_readiness(
        self,
        readiness: ManagedReflectorRuntimeReadiness,
        *,
        credential_mount: ManagedReflectorCredentialMountReadiness,
    ) -> None:
        """Verify the CLI canary and mount receipt share one authority."""

        if (
            readiness.authority_id == self.authority_id
            and readiness.worker_launch_id == self.worker_launch_id
            and readiness.service_identity_digest == self.service_identity_digest
            and readiness.runtime_profile == self.runtime_profile
            and readiness.runtime_digest == self.runtime_digest
            and readiness.docker_host_path_identity
            == self.docker_host_path_identity
            and readiness.generation_digest == self.generation_digest
            and readiness.daemon_release_identity == self.daemon_release_identity
            and readiness.release_install_digest == self.release_install_digest
            and readiness.release_registry_digest == self.release_registry_digest
            and readiness.credential_mount_content_sha256
            == credential_mount.content_sha256
            and readiness.codex_cli_started is True
            and readiness.model_started is False
        ):
            return
        raise ManagedReflectorMountAuthorityError(
            "managed reflector runtime readiness does not match Core expectation"
        )


def _read_bounded(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = os.read(descriptor, min(4096, _MAX_AUTHORITY_BYTES + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > _MAX_AUTHORITY_BYTES:
            raise ManagedReflectorMountAuthorityError(
                "managed reflector mount authority payload is too large"
            )
    if size == 0:
        raise ManagedReflectorMountAuthorityError(
            "managed reflector mount authority payload is empty"
        )
    return b"".join(chunks)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest_json(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _require_digest(value: str, label: str) -> None:
    if _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"managed reflector {label} is invalid")


__all__ = [
    "MANAGED_REFLECTOR_MOUNT_AUTHORITY_FD_ENV",
    "ManagedReflectorContainerMountAdoptionReceipt",
    "ManagedReflectorContainerMountAuthority",
    "ManagedReflectorContainerMountGrant",
    "ManagedReflectorCredentialMountReadiness",
    "ManagedReflectorMountAuthority",
    "ManagedReflectorMountAuthorityError",
    "ManagedReflectorMountGrant",
    "ManagedReflectorMountRegistrationExpectation",
    "ManagedReflectorRuntimeReadiness",
]
