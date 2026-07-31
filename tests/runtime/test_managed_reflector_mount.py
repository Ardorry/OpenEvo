from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from openevo.internal_auth import InternalServiceIdentity
from openevo.runtime.docker_host import discover_docker_host_path
from openevo.runtime.managed_reflector_mount import (
    MANAGED_REFLECTOR_MOUNT_AUTHORITY_FD_ENV,
    ManagedReflectorContainerMountAdoptionReceipt,
    ManagedReflectorContainerMountAuthority,
    ManagedReflectorCredentialMountReadiness,
    ManagedReflectorMountAuthority,
    ManagedReflectorMountAuthorityError,
)


def _identity(
    *,
    generation: str = "1" * 64,
    registry: str = "2" * 64,
) -> InternalServiceIdentity:
    return InternalServiceIdentity(
        service_id="evolution-worker",
        generation_digest=generation,
        registry_digest=registry,
        framework_lock_digest="3" * 64,
        credential="mount-authority-test-credential-0123456789abcdef",
    )


def _docker_host_path(tmp_path: Path):
    hostname = "a" * 12
    container_id = hostname + ("b" * 52)
    data_root = tmp_path / "release-data"
    data_root.mkdir()
    evidence = json.dumps(
        {
            "id": container_id,
            "hostname": hostname,
            "running": True,
            "mounts": [
                {
                    "Type": "bind",
                    "Source": "/srv/openevo-data",
                    "Destination": os.fspath(data_root),
                    "RW": True,
                }
            ],
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return discover_docker_host_path(
        evidence,
        namespace="reflector-mount-test",
        hostname=hostname,
        minimum_available_bytes=0,
    )


def _authority(tmp_path: Path) -> ManagedReflectorMountAuthority:
    return ManagedReflectorMountAuthority.issue(
        identity=_identity(),
        daemon_release_identity="0" * 64,
        release_install_digest="4" * 64,
        runtime_identity_digest="5" * 64,
        runtime_image=f"sha256:{'6' * 64}",
        worker_launch_id=f"mrl-{'7' * 32}",
        docker_host_path=_docker_host_path(tmp_path),
    )


def _verify(authority: ManagedReflectorMountAuthority) -> None:
    authority.verify(
        _identity(),
        expected_worker_launch_id=f"mrl-{'7' * 32}",
        expected_docker_host_path_identity=authority.docker_host_path.identity_digest,
        expected_daemon_release_identity="0" * 64,
        expected_release_install_digest="4" * 64,
        expected_runtime_profile="managed_science",
        expected_runtime_image=f"sha256:{'6' * 64}",
        expected_runtime_identity_digest="5" * 64,
    )


def test_mount_authority_sealed_fd_round_trip_and_redacted_repr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority(tmp_path)
    descriptor = authority.open_inheritance_descriptor()
    monkeypatch.setenv(MANAGED_REFLECTOR_MOUNT_AUTHORITY_FD_ENV, str(descriptor))

    adopted = ManagedReflectorMountAuthority.from_inherited_environment(
        _identity(),
        expected_worker_launch_id=f"mrl-{'7' * 32}",
        expected_docker_host_path_identity=authority.docker_host_path.identity_digest,
        expected_daemon_release_identity="0" * 64,
        expected_release_install_digest="4" * 64,
        expected_runtime_profile="managed_science",
        expected_runtime_image=f"sha256:{'6' * 64}",
        expected_runtime_identity_digest="5" * 64,
        required=True,
    )

    assert adopted == authority
    assert MANAGED_REFLECTOR_MOUNT_AUTHORITY_FD_ENV not in os.environ
    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert authority.signature not in repr(authority)


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("expected_worker_launch_id", f"mrl-{'8' * 32}"),
        ("expected_docker_host_path_identity", "8" * 64),
        ("expected_daemon_release_identity", "8" * 64),
        ("expected_release_install_digest", "8" * 64),
        ("expected_runtime_profile", "another-profile"),
        ("expected_runtime_image", f"sha256:{'8' * 64}"),
        ("expected_runtime_identity_digest", "8" * 64),
    ],
)
def test_mount_authority_rejects_expected_identity_drift(
    tmp_path: Path,
    field: str,
    wrong: str,
) -> None:
    authority = _authority(tmp_path)
    arguments = {
        "expected_worker_launch_id": f"mrl-{'7' * 32}",
        "expected_docker_host_path_identity": authority.docker_host_path.identity_digest,
        "expected_daemon_release_identity": "0" * 64,
        "expected_release_install_digest": "4" * 64,
        "expected_runtime_profile": "managed_science",
        "expected_runtime_image": f"sha256:{'6' * 64}",
        "expected_runtime_identity_digest": "5" * 64,
    }
    arguments[field] = wrong

    with pytest.raises(ManagedReflectorMountAuthorityError):
        authority.verify(_identity(), **arguments)


def test_mount_authority_rejects_generation_and_signature_drift(tmp_path: Path) -> None:
    authority = _authority(tmp_path)
    with pytest.raises(ManagedReflectorMountAuthorityError, match="generation"):
        authority.verify(
            _identity(generation="9" * 64),
            expected_worker_launch_id=f"mrl-{'7' * 32}",
            expected_docker_host_path_identity=authority.docker_host_path.identity_digest,
            expected_daemon_release_identity="0" * 64,
            expected_release_install_digest="4" * 64,
            expected_runtime_profile="managed_science",
            expected_runtime_image=f"sha256:{'6' * 64}",
            expected_runtime_identity_digest="5" * 64,
        )

    tampered = authority.model_copy(update={"signature": "0" * 64})
    with pytest.raises(ManagedReflectorMountAuthorityError, match="signature"):
        _verify(tampered)


def test_mount_authority_rejects_release_registry_drift(tmp_path: Path) -> None:
    authority = _authority(tmp_path)

    with pytest.raises(ManagedReflectorMountAuthorityError, match="registry"):
        authority.verify(
            _identity(registry="9" * 64),
            expected_worker_launch_id=f"mrl-{'7' * 32}",
            expected_docker_host_path_identity=authority.docker_host_path.identity_digest,
            expected_daemon_release_identity="0" * 64,
            expected_release_install_digest="4" * 64,
            expected_runtime_profile="managed_science",
            expected_runtime_image=f"sha256:{'6' * 64}",
            expected_runtime_identity_digest="5" * 64,
        )


def test_mount_authority_requires_a_sealed_inherited_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority(tmp_path)
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, authority.inherited_payload())
    finally:
        os.close(write_fd)
    monkeypatch.setenv(MANAGED_REFLECTOR_MOUNT_AUTHORITY_FD_ENV, str(read_fd))

    with pytest.raises(ManagedReflectorMountAuthorityError, match="inherited"):
        ManagedReflectorMountAuthority.from_inherited_environment(
            _identity(),
            expected_worker_launch_id=f"mrl-{'7' * 32}",
            expected_docker_host_path_identity=authority.docker_host_path.identity_digest,
            expected_daemon_release_identity="0" * 64,
            expected_release_install_digest="4" * 64,
            expected_runtime_profile="managed_science",
            expected_runtime_image=f"sha256:{'6' * 64}",
            expected_runtime_identity_digest="5" * 64,
            required=True,
        )


def test_mount_authority_missing_descriptor_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _authority(tmp_path)
    monkeypatch.delenv(MANAGED_REFLECTOR_MOUNT_AUTHORITY_FD_ENV, raising=False)

    with pytest.raises(ManagedReflectorMountAuthorityError, match="missing"):
        ManagedReflectorMountAuthority.from_inherited_environment(
            _identity(),
            expected_worker_launch_id=f"mrl-{'7' * 32}",
            expected_docker_host_path_identity=authority.docker_host_path.identity_digest,
            expected_daemon_release_identity="0" * 64,
            expected_release_install_digest="4" * 64,
            expected_runtime_profile="managed_science",
            expected_runtime_image=f"sha256:{'6' * 64}",
            expected_runtime_identity_digest="5" * 64,
            required=True,
        )


def test_container_mount_authority_binds_launch_nonce_signature_and_credentials(
    tmp_path: Path,
) -> None:
    parent = _authority(tmp_path)
    root_identity = (11, 12, 1000)
    auth_identity = (21, 22, 0o100600, 1000, 1, 256, 30, 40)
    child = parent.issue_container_authority(
        container_launch_id="openevo_reflector_child_a",
        credential_root_identity=root_identity,
        credential_auth_identity=auth_identity,
    )
    child.verify(
        parent,
        expected_container_launch_id="openevo_reflector_child_a",
        expected_docker_host_path_identity=parent.docker_host_path.identity_digest,
        expected_credential_root_identity=root_identity,
        expected_credential_auth_identity=auth_identity,
    )
    assert child.grant.daemon_release_identity == parent.grant.daemon_release_identity

    with pytest.raises(ManagedReflectorMountAuthorityError, match="identity mismatch"):
        child.verify(
            parent,
            expected_container_launch_id="openevo_reflector_child_b",
            expected_docker_host_path_identity=parent.docker_host_path.identity_digest,
            expected_credential_root_identity=root_identity,
            expected_credential_auth_identity=auth_identity,
        )

    tampered_daemon_release = child.model_copy(
        update={
            "grant": child.grant.model_copy(
                update={"daemon_release_identity": "9" * 64}
            )
        }
    )
    with pytest.raises(ManagedReflectorMountAuthorityError, match="identity mismatch"):
        tampered_daemon_release.verify(
            parent,
            expected_container_launch_id="openevo_reflector_child_a",
            expected_docker_host_path_identity=parent.docker_host_path.identity_digest,
            expected_credential_root_identity=root_identity,
            expected_credential_auth_identity=auth_identity,
        )

    tampered_nonce = child.model_copy(
        update={
            "grant": child.grant.model_copy(
                update={"adoption_nonce": "8" * 64}
            )
        }
    )
    with pytest.raises(ManagedReflectorMountAuthorityError, match="signature"):
        tampered_nonce.verify(
            parent,
            expected_container_launch_id="openevo_reflector_child_a",
            expected_docker_host_path_identity=parent.docker_host_path.identity_digest,
            expected_credential_root_identity=root_identity,
            expected_credential_auth_identity=auth_identity,
        )

    tampered_signature = ManagedReflectorContainerMountAuthority(
        grant=child.grant,
        signature="9" * 64,
    )
    with pytest.raises(ManagedReflectorMountAuthorityError, match="signature"):
        tampered_signature.verify(
            parent,
            expected_container_launch_id="openevo_reflector_child_a",
            expected_docker_host_path_identity=parent.docker_host_path.identity_digest,
            expected_credential_root_identity=root_identity,
            expected_credential_auth_identity=auth_identity,
        )

    assert child.grant.adoption_nonce not in repr(child)
    assert child.signature not in repr(child)


def test_each_container_gets_independent_authority_and_nonce(tmp_path: Path) -> None:
    parent = _authority(tmp_path)
    first = parent.issue_container_authority(
        container_launch_id="openevo_reflector_child_a",
        credential_root_identity=(11, 12, 1000),
        credential_auth_identity=(21, 22, 0o100600, 1000, 1, 256, 30, 40),
    )
    second = parent.issue_container_authority(
        container_launch_id="openevo_reflector_child_b",
        credential_root_identity=(31, 32, 1000),
        credential_auth_identity=(41, 42, 0o100600, 1000, 1, 256, 50, 60),
    )

    assert first.authority_id != second.authority_id
    assert first.grant.adoption_nonce != second.grant.adoption_nonce
    assert first.grant.credential_root_identity != second.grant.credential_root_identity


def _content_digest(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    ).hexdigest()


def test_container_adoption_receipt_rejects_unrecomputed_tamper() -> None:
    payload: dict[str, object] = {
        "schema_version": "openevo.managed_reflector_container_mount_adoption.v1",
        "parent_authority_id": "1" * 64,
        "container_authority_id": "2" * 64,
        "worker_launch_id": f"mrl-{'3' * 32}",
        "container_launch_id": "openevo_reflector_receipt_test",
        "adoption_nonce_sha256": "4" * 64,
        "service_identity_digest": "5" * 64,
        "generation_digest": "6" * 64,
        "daemon_release_identity": "e" * 64,
        "release_install_digest": "7" * 64,
        "release_registry_digest": "8" * 64,
        "runtime_profile": "managed_science",
        "runtime_digest": f"sha256:{'9' * 64}",
        "docker_host_path_identity": "a" * 64,
        "credential_root_identity_sha256": "b" * 64,
        "credential_auth_identity_sha256": "c" * 64,
        "container_authority_verified": True,
        "adoption_receipt_valid": True,
    }
    payload["content_sha256"] = _content_digest(payload)
    valid = ManagedReflectorContainerMountAdoptionReceipt.model_validate(payload)
    tampered = valid.model_dump(mode="json")
    tampered["adoption_nonce_sha256"] = "d" * 64

    with pytest.raises(ValueError, match="adoption receipt digest"):
        ManagedReflectorContainerMountAdoptionReceipt.model_validate(tampered)


def test_mount_readiness_receipt_rejects_unrecomputed_tamper() -> None:
    payload: dict[str, object] = {
        "schema_version": "openevo.managed_reflector_credential_mount_readiness.v1",
        "authority_id": "1" * 64,
        "container_authority_id": "2" * 64,
        "worker_launch_id": f"mrl-{'3' * 32}",
        "container_launch_id": "openevo_reflector_readiness_test",
        "adoption_nonce_sha256": "4" * 64,
        "service_identity_digest": "5" * 64,
        "runtime_profile": "managed_science",
        "runtime_digest": f"sha256:{'6' * 64}",
        "docker_host_path_identity": "7" * 64,
        "generation_digest": "8" * 64,
        "daemon_release_identity": "e" * 64,
        "release_install_digest": "9" * 64,
        "release_registry_digest": "a" * 64,
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
    payload["content_sha256"] = _content_digest(payload)
    valid = ManagedReflectorCredentialMountReadiness.model_validate(payload)
    tampered = valid.model_dump(mode="json")
    tampered["cleanup_verified"] = False

    with pytest.raises(ValueError):
        ManagedReflectorCredentialMountReadiness.model_validate(tampered)
