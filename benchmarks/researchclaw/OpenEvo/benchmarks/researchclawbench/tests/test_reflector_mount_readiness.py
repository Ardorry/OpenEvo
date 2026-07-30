from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from openevo_researchclawbench.config import ExperimentConfig

_TRUE_FIELDS = (
    "authority_issued",
    "docker_mount_created",
    "container_path_visible",
    "container_user_can_read",
    "generation_matches",
    "release_identity_matches",
    "adoption_receipt_valid",
    "cleanup_verified",
)


def _digest(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _receipt() -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": (
            "openevo.managed_reflector_credential_mount_readiness.v1"
        ),
        "authority_id": "a" * 64,
        "container_authority_id": "2" * 64,
        "worker_launch_id": f"mrl-{'b' * 32}",
        "container_launch_id": "openevo_reflector_canary",
        "adoption_nonce_sha256": "3" * 64,
        "service_identity_digest": "4" * 64,
        "runtime_profile": "managed_science",
        "runtime_digest": f"sha256:{'c' * 64}",
        "docker_host_path_identity": "d" * 64,
        "generation_digest": "e" * 64,
        "daemon_release_identity": "5" * 64,
        "release_install_digest": "f" * 64,
        "release_registry_digest": "1" * 64,
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
        "created_at": "2026-07-29T00:00:00Z",
    }
    value["content_sha256"] = _digest(value)
    return value


def _config(tmp_path: Path, receipt: Path, payload: dict[str, object]) -> ExperimentConfig:
    experiment = tmp_path / "experiment"
    experiment.mkdir(exist_ok=True)
    expected = {
        key: payload[key]
        for key in (
            "authority_id",
            "worker_launch_id",
            "generation_digest",
            "daemon_release_identity",
            "release_install_digest",
            "release_registry_digest",
            "docker_host_path_identity",
            "runtime_digest",
        )
    }
    return ExperimentConfig(
        path=tmp_path / "protocol.yaml",
        raw={
            "paths": {
                "project_root": str(tmp_path),
                "experiment_root": str(experiment),
            },
            "reflector": {
                "require_credential_mount_readiness": True,
                "credential_mount_readiness_receipt": str(receipt),
                "credential_mount_expected": expected,
            },
        },
    )


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_managed_reflector_mount_readiness_accepts_exact_pinned_receipt(
    tmp_path: Path,
) -> None:
    payload = _receipt()
    receipt = tmp_path / "experiment" / "mount-readiness.json"
    _write(receipt, payload)

    observed = _config(tmp_path, receipt, payload).reflector_credential_mount_readiness()

    assert observed["ready"] is True
    assert observed["required"] is True
    assert observed["identity_matches"] is True
    assert all(observed[key] is True for key in _TRUE_FIELDS)
    assert observed["codex_cli_started"] is False
    assert observed["model_started"] is False
    assert observed["secret_recorded"] is False


@pytest.mark.parametrize("field", _TRUE_FIELDS)
def test_managed_reflector_mount_readiness_rejects_any_false_adoption_field(
    tmp_path: Path,
    field: str,
) -> None:
    payload = _receipt()
    payload[field] = False
    payload["content_sha256"] = _digest(
        {key: value for key, value in payload.items() if key != "content_sha256"}
    )
    receipt = tmp_path / "experiment" / "mount-readiness.json"
    _write(receipt, payload)

    observed = _config(tmp_path, receipt, _receipt()).reflector_credential_mount_readiness()

    assert observed["ready"] is False
    assert observed["identity_matches"] is False


def test_managed_reflector_mount_readiness_rejects_identity_drift(
    tmp_path: Path,
) -> None:
    payload = _receipt()
    receipt = tmp_path / "experiment" / "mount-readiness.json"
    _write(receipt, payload)
    config = _config(tmp_path, receipt, payload)
    config.raw["reflector"]["credential_mount_expected"]["generation_digest"] = "2" * 64

    observed = config.reflector_credential_mount_readiness()

    assert observed["ready"] is False
    assert observed["identity_matches"] is False


def test_managed_reflector_mount_readiness_rejects_missing_receipt(
    tmp_path: Path,
) -> None:
    payload = _receipt()
    receipt = tmp_path / "experiment" / "missing.json"

    observed = _config(tmp_path, receipt, payload).reflector_credential_mount_readiness()

    assert observed["ready"] is False
    assert observed["identity_matches"] is False


def test_managed_reflector_mount_readiness_rejects_receipt_outside_experiment(
    tmp_path: Path,
) -> None:
    payload = _receipt()
    receipt = tmp_path / "outside.json"
    _write(receipt, payload)

    observed = _config(tmp_path, receipt, payload).reflector_credential_mount_readiness()

    assert observed["ready"] is False
    assert observed["identity_matches"] is False
