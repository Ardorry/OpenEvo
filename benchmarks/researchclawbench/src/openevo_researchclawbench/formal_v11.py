"""Versioned protocol preparation and identity checks for formal v11 runs.

The helper creates a *new* protocol and identity receipt.  It never rewrites a
historical protocol or readiness receipt, and it performs no Core, model, or
evaluator operation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .source_identity import active_source_identities
from .training_state_store import canonical_bytes, canonical_sha256

if TYPE_CHECKING:
    from .config import ExperimentConfig


FORMAL_V11_CONTRACT = "openevo.researchclawbench.formal_runs.v11"
LEGACY_CLASSIFICATION = "LEGACY_AUTHORITY_RECOVERY_NONBLOCKING_FOR_FRESH_FORMAL_RUN"
OFFICIAL_FROZEN_POLICY = {
    "evolution_enabled": False,
    "reflector_enabled": False,
    "training_feedback_attachment_enabled": False,
    "teacher_enabled": False,
    "task_local_overlay_enabled": False,
    "cross_task_state_updates": False,
    "feedback_released": False,
}

_RUN_ID = re.compile(r"rcb_oe_v0_[A-Za-z0-9][A-Za-z0-9._-]{0,95}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

OFFICIAL_V11_BUDGET = {
    "attempts_per_task": 1,
    "max_candidate_model_calls": 40,
    "max_judge_task_operations": 40,
    "max_unified_scorer_operations": 1,
    "max_infrastructure_retries_per_operation": 1,
    "candidate_runtime_seconds_per_task": 3600,
    "judge_runtime_seconds_per_task": 1800,
    "cumulative_runtime_seconds": 216000,
    "user_configurable": True,
}


class FormalV11ProtocolError(ValueError):
    """The formal v11 extension is absent, incomplete, or identity-drifted."""


@dataclass(frozen=True)
class FormalV11RuntimeAssetOverrides:
    """Fresh release assets which may not be inherited from a base protocol."""

    framework_lock: Path
    daemon_bundle: Path
    daemon_manifest: Path
    reflector_readiness_receipt: Path
    reflector_credential_mount_readiness_receipt: Path
    evaluator_dependency_lock: Path


def _closed_json_file(path: Path, *, label: str) -> dict[str, Any]:
    try:
        metadata = os.stat(path, follow_symlinks=False)
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FormalV11ProtocolError(f"{label} is unavailable") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
        or not isinstance(value, dict)
    ):
        raise FormalV11ProtocolError(f"{label} is not an immutable owned JSON file")
    return value


def _closed_asset_path(
    value: str | Path,
    *,
    experiment_root: Path,
    label: str,
) -> Path:
    source = Path(value)
    try:
        metadata = os.stat(source, follow_symlinks=False)
        path = source.resolve(strict=True)
    except OSError as exc:
        raise FormalV11ProtocolError(f"{label} is unavailable") from exc
    if (
        source.is_symlink()
        or not path.is_relative_to(experiment_root)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
    ):
        raise FormalV11ProtocolError(f"{label} is outside the immutable asset boundary")
    return path


def _require_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise FormalV11ProtocolError(f"{label} is not a SHA-256 digest")
    return value


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _runtime_asset_block(
    *,
    base_config: ExperimentConfig,
    overrides: FormalV11RuntimeAssetOverrides,
    reject_inherited_from_base: bool = True,
) -> dict[str, Any]:
    """Validate and bind a fresh, mutually consistent managed release set."""

    root = base_config.experiment_root
    framework_lock = _closed_asset_path(
        overrides.framework_lock,
        experiment_root=root,
        label="formal framework lock",
    )
    daemon_bundle = _closed_asset_path(
        overrides.daemon_bundle,
        experiment_root=root,
        label="formal daemon bundle",
    )
    daemon_manifest = _closed_asset_path(
        overrides.daemon_manifest,
        experiment_root=root,
        label="formal daemon manifest",
    )
    reflector_readiness = _closed_asset_path(
        overrides.reflector_readiness_receipt,
        experiment_root=root,
        label="formal reflector readiness receipt",
    )
    reflector_mount_readiness = _closed_asset_path(
        overrides.reflector_credential_mount_readiness_receipt,
        experiment_root=root,
        label="formal reflector credential-mount readiness receipt",
    )
    evaluator_dependency_lock = _closed_asset_path(
        overrides.evaluator_dependency_lock,
        experiment_root=root,
        label="formal evaluator dependency lock",
    )
    prior_values = [
        base_config.raw.get("native_openevo", {}).get("framework_lock"),
        base_config.raw.get("native_openevo", {})
        .get("remote_core", {})
        .get("daemon_bundle"),
        base_config.raw.get("native_openevo", {})
        .get("remote_core", {})
        .get("daemon_manifest"),
        base_config.raw.get("reflector", {}).get("readiness_receipt"),
        base_config.raw.get("reflector", {}).get(
            "credential_mount_readiness_receipt"
        ),
        base_config.raw.get("judge", {})
        .get("evaluator_dependency_lock", {})
        .get("path"),
    ]
    prior = {
        Path(value).resolve(strict=False)
        for value in prior_values
        if isinstance(value, str) and value
    }
    selected = {
        framework_lock,
        daemon_bundle,
        daemon_manifest,
        reflector_readiness,
        reflector_mount_readiness,
        evaluator_dependency_lock,
    }
    if reject_inherited_from_base and prior.intersection(selected):
        raise FormalV11ProtocolError(
            "formal v11 runtime assets must not inherit base-generation files"
        )

    lock = _closed_json_file(framework_lock, label="formal framework lock")
    manifest = _closed_json_file(daemon_manifest, label="formal daemon manifest")
    readiness = _closed_json_file(
        reflector_readiness, label="formal reflector readiness receipt"
    )
    mount_readiness = _closed_json_file(
        reflector_mount_readiness,
        label="formal reflector credential-mount readiness receipt",
    )
    try:
        from .evaluator_dependency_lock import validate_evaluator_dependency_lock

        evaluator_lock = validate_evaluator_dependency_lock(
            evaluator_dependency_lock
        )
    except (ImportError, RuntimeError) as exc:
        raise FormalV11ProtocolError(
            "formal evaluator dependency lock is invalid"
        ) from exc
    artifact = manifest.get("artifact")
    core = manifest.get("core")
    release = manifest.get("release")
    if not all(isinstance(item, dict) for item in (artifact, core, release)):
        raise FormalV11ProtocolError("formal daemon manifest identity is incomplete")
    core_lock = core.get("framework_lock")
    wheel = core.get("wheel")
    if not isinstance(core_lock, dict) or not isinstance(wheel, dict):
        raise FormalV11ProtocolError("formal daemon Core identity is incomplete")
    bundle_sha256 = _file_sha256(daemon_bundle)
    lock_sha256 = _file_sha256(framework_lock)
    if (
        artifact.get("filename") != daemon_bundle.name
        or artifact.get("sha256") != bundle_sha256
        or artifact.get("size") != daemon_bundle.stat().st_size
        or core_lock.get("filename") != framework_lock.name
        or core_lock.get("sha256") != lock_sha256
        or wheel.get("filename") != lock.get("wheel_filename")
        or wheel.get("sha256") != lock.get("distribution_digest")
        or release.get("source_commit")
        != base_config.require("source_identity.openevo_commit")
    ):
        raise FormalV11ProtocolError("formal daemon release assets disagree")
    release_identity = _require_sha256(
        release.get("identity"), label="formal daemon release identity"
    )
    registry_digest = _require_sha256(
        core.get("registry_digest"), label="formal registry digest"
    )
    if (
        readiness.get("schema_version")
        != "openevo.managed_reflector_readiness.v1"
        or readiness.get("runtime_profile") != "managed_science"
        or readiness.get("runtime_digest")
        != base_config.require("reflector.runtime_image_digest")
        or readiness.get("codex_binary") != base_config.require("reflector.codex_binary")
        or readiness.get("actual_cli_version")
        != base_config.require("reflector.codex_cli_version")
        or readiness.get("auth_mode") != "subscription"
        or readiness.get("capture_mode") != "transcript"
        or readiness.get("path_fallback_allowed") is not False
        or readiness.get("exit_status") != 0
    ):
        raise FormalV11ProtocolError("formal reflector readiness identity disagrees")
    try:
        from openevo.runtime.managed_reflector_mount import (
            ManagedReflectorCredentialMountReadiness,
            ManagedReflectorRuntimeReadiness,
        )

        mount = ManagedReflectorCredentialMountReadiness.model_validate(mount_readiness)
        runtime = ManagedReflectorRuntimeReadiness.model_validate(readiness)
    except (ImportError, ValueError) as exc:
        raise FormalV11ProtocolError(
            "formal reflector credential-mount receipt is invalid"
        ) from exc
    if (
        mount.daemon_release_identity != release_identity
        or mount.release_registry_digest != registry_digest
        or mount.runtime_digest != base_config.require("reflector.runtime_image_digest")
        or mount.codex_cli_started is not False
        or mount.model_started is not False
        or runtime.authority_id != mount.authority_id
        or runtime.worker_launch_id != mount.worker_launch_id
        or runtime.credential_mount_content_sha256 != mount.content_sha256
        or runtime.generation_digest != mount.generation_digest
        or runtime.daemon_release_identity != mount.daemon_release_identity
        or runtime.release_install_digest != mount.release_install_digest
        or runtime.release_registry_digest != mount.release_registry_digest
        or runtime.runtime_profile != mount.runtime_profile
        or runtime.runtime_digest != mount.runtime_digest
        or runtime.docker_host_path_identity != mount.docker_host_path_identity
        or runtime.codex_cli_started is not True
        or runtime.model_started is not False
    ):
        raise FormalV11ProtocolError(
            "formal reflector runtime and credential-mount identities disagree"
        )
    return {
        "framework_lock": {
            "path": str(framework_lock),
            "sha256": lock_sha256,
            "distribution_digest": lock["distribution_digest"],
        },
        "daemon_bundle": {
            "path": str(daemon_bundle),
            "sha256": bundle_sha256,
            "size": daemon_bundle.stat().st_size,
        },
        "daemon_manifest": {
            "path": str(daemon_manifest),
            "sha256": _file_sha256(daemon_manifest),
            "release_identity": release_identity,
            "registry_digest": registry_digest,
            "source_commit": release["source_commit"],
        },
        "reflector_readiness_receipt": {
            "path": str(reflector_readiness),
            "sha256": _file_sha256(reflector_readiness),
            "runtime_digest": readiness["runtime_digest"],
            "codex_cli_version": readiness["actual_cli_version"],
        },
        "reflector_credential_mount_readiness_receipt": {
            "path": str(reflector_mount_readiness),
            "sha256": _file_sha256(reflector_mount_readiness),
            "content_sha256": mount.content_sha256,
            "generation_digest": mount.generation_digest,
            "release_identity": mount.daemon_release_identity,
            "registry_digest": mount.release_registry_digest,
        },
        "evaluator_dependency_lock": {
            "path": str(evaluator_dependency_lock),
            "sha256": _file_sha256(evaluator_dependency_lock),
            "content_sha256": evaluator_lock["content_sha256"],
            "python_version": evaluator_lock["python"]["version"],
            "distribution_count": len(evaluator_lock["distributions"]),
        },
    }


def _atomic_write(path: Path, payload: bytes) -> None:
    """Publish one new immutable evidence file without replacing a peer."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(raw_tmp)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # A hard link gives this append-only control plane an atomic
        # create-if-absent operation.  ``os.replace`` would permit a racing
        # process to overwrite a protocol, identity, or Core-binding receipt
        # after the caller's initial existence check.
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _require_run_id(value: object, field: str) -> str:
    if not isinstance(value, str) or _RUN_ID.fullmatch(value) is None:
        raise FormalV11ProtocolError(f"{field} is not a formal run ID")
    return value


def _validate_runtime_asset_claims(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "framework_lock",
        "daemon_bundle",
        "daemon_manifest",
        "reflector_readiness_receipt",
        "reflector_credential_mount_readiness_receipt",
        "evaluator_dependency_lock",
    }:
        raise FormalV11ProtocolError("formal v11 runtime asset set is incomplete")
    expected_keys = {
        "framework_lock": {"path", "sha256", "distribution_digest"},
        "daemon_bundle": {"path", "sha256", "size"},
        "daemon_manifest": {
            "path",
            "sha256",
            "release_identity",
            "registry_digest",
            "source_commit",
        },
        "reflector_readiness_receipt": {
            "path",
            "sha256",
            "runtime_digest",
            "codex_cli_version",
        },
        "reflector_credential_mount_readiness_receipt": {
            "path",
            "sha256",
            "content_sha256",
            "generation_digest",
            "release_identity",
            "registry_digest",
        },
        "evaluator_dependency_lock": {
            "path",
            "sha256",
            "content_sha256",
            "python_version",
            "distribution_count",
        },
    }
    for label, keys in expected_keys.items():
        item = value.get(label)
        if not isinstance(item, dict) or set(item) != keys:
            raise FormalV11ProtocolError(f"formal v11 runtime asset drifted: {label}")
        if not isinstance(item.get("path"), str) or not item["path"]:
            raise FormalV11ProtocolError(f"formal v11 runtime path is invalid: {label}")
        for key in (
            keys
            - {
                "path",
                "size",
                "source_commit",
                "runtime_digest",
                "codex_cli_version",
                "python_version",
                "distribution_count",
            }
        ):
            _require_sha256(item.get(key), label=f"{label}.{key}")
    if (
        type(value["daemon_bundle"]["size"]) is not int
        or value["daemon_bundle"]["size"] <= 0
        or not re.fullmatch(
            r"[0-9a-f]{40}", value["daemon_manifest"]["source_commit"]
        )
        or not re.fullmatch(
            r"sha256:[0-9a-f]{64}",
            value["reflector_readiness_receipt"]["runtime_digest"],
        )
        or value["reflector_readiness_receipt"]["codex_cli_version"] != "0.144.1"
        or type(value["evaluator_dependency_lock"]["distribution_count"]) is not int
        or value["evaluator_dependency_lock"]["distribution_count"] <= 0
        or not re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+",
            value["evaluator_dependency_lock"]["python_version"],
        )
    ):
        raise FormalV11ProtocolError("formal v11 runtime identity is invalid")
    return json.loads(canonical_bytes(value))


def validate_formal_v11_block(value: object) -> dict[str, Any]:
    """Validate the closed Community17 -> frozen official40 control block."""

    if not isinstance(value, dict):
        raise FormalV11ProtocolError("formal_runs must be an object")
    community = value.get("community")
    official = value.get("official")
    legacy = value.get("legacy_authority_recovery")
    if not all(isinstance(item, dict) for item in (community, official, legacy)):
        raise FormalV11ProtocolError("formal v11 run sections are incomplete")
    community_run_id = _require_run_id(community.get("run_id"), "community run_id")
    official_run_id = _require_run_id(official.get("run_id"), "official run_id")
    common_community = {
        "run_id": community_run_id,
        "scope": "community-17",
        "tasks": 17,
        "attempts_per_task": 3,
        "candidate_runs": 51,
        "reflector_cycles": 34,
        "artifact_evolution_jobs": 102,
        "terminal_stage": "FINAL_FROZEN",
    }
    continuation = community.get("successor_recovery_continuation")
    completed_prefix = community.get("completed_prefix_continuation")
    if continuation is not None and completed_prefix is not None:
        raise FormalV11ProtocolError("formal continuation modes conflict")
    if continuation is None and completed_prefix is None:
        expected_community = {
            **common_community,
            "namespace_type": "fresh_community17_formal",
            "fresh_start_required": True,
            "legacy_source_import_allowed": False,
        }
        expected_legacy = {
            "required": False,
            "classification": LEGACY_CLASSIFICATION,
            "fresh_path_uses_recovery_endpoint": False,
        }
    elif continuation is not None:
        if not isinstance(continuation, dict):
            raise FormalV11ProtocolError("formal continuation source must be an object")
        required_source = {
            "source_namespace",
            "source_state_sha256",
            "source_transition_id",
            "source_transition_attempt_id",
            "source_candidate_session_id",
            "source_completed_dataset_id",
            "source_dataset_revision",
            "source_attachment_id",
            "source_resolved_view_sha256",
            "source_attempt_index",
            "candidate_reexecuted",
            "additional_candidate_model_calls",
        }
        if (
            set(continuation) != required_source
            or any(
                not isinstance(continuation[name], str)
                or not continuation[name]
                for name in required_source
                - {
                    "source_attempt_index",
                    "candidate_reexecuted",
                    "additional_candidate_model_calls",
                }
            )
            or continuation["source_attempt_index"] != 0
            or continuation["candidate_reexecuted"] is not False
            or continuation["additional_candidate_model_calls"] != 0
            or not _SHA256.fullmatch(continuation["source_state_sha256"])
            or not _SHA256.fullmatch(continuation["source_resolved_view_sha256"])
        ):
            raise FormalV11ProtocolError("formal continuation source authority is invalid")
        expected_community = {
            **common_community,
            "namespace_type": "append_only_successor_recovery_continuation",
            "fresh_start_required": False,
            "legacy_source_import_allowed": True,
            "source_attempts_consumed": 1,
            "candidate_operations_remaining": 50,
            "successor_recovery_continuation": continuation,
        }
        expected_legacy = {
            "required": True,
            "classification": "APPEND_ONLY_PRE_JOB_SUCCESSOR_RECOVERY_CONTINUATION",
            "fresh_path_uses_recovery_endpoint": True,
        }
    else:
        if not isinstance(completed_prefix, dict):
            raise FormalV11ProtocolError(
                "formal completed-prefix source must be an object"
            )
        required_prefix = {
            "source_namespace",
            "source_state_sha256",
            "source_database_sha256",
            "source_protocol_sha256",
            "source_core_identity_sha256",
            "source_adapter_identity_sha256",
            "source_reconciliation_receipt_sha256",
            "source_successor_transition_id",
            "source_successor_commit_sha256",
            "source_stage",
            "current_task_index",
            "current_attempt",
            "source_attempts_consumed",
            "source_reflector_cycles_consumed",
            "source_evolution_jobs_consumed",
            "source_candidate_model_calls",
            "source_judge_operations",
            "source_reflector_model_calls",
            "source_active_resources",
            "source_pending_side_effects",
            "source_failed_side_effects",
            "candidate_reexecuted",
            "model_execution_allowed",
        }
        digest_fields = {
            "source_state_sha256",
            "source_database_sha256",
            "source_protocol_sha256",
            "source_core_identity_sha256",
            "source_adapter_identity_sha256",
            "source_reconciliation_receipt_sha256",
            "source_successor_commit_sha256",
        }
        if (
            set(completed_prefix) != required_prefix
            or not isinstance(completed_prefix.get("source_namespace"), str)
            or _RUN_ID.fullmatch(completed_prefix["source_namespace"]) is None
            or not isinstance(
                completed_prefix.get("source_successor_transition_id"), str
            )
            or not completed_prefix["source_successor_transition_id"]
            or any(
                not isinstance(completed_prefix.get(name), str)
                or _SHA256.fullmatch(completed_prefix[name]) is None
                for name in digest_fields
            )
            or completed_prefix.get("source_stage") != "NEXT_ATTEMPT_READY"
            or type(completed_prefix.get("current_task_index")) is not int
            or not 0 <= completed_prefix["current_task_index"] < 17
            or completed_prefix.get("current_attempt") not in {0, 1}
            or completed_prefix.get("source_active_resources") != 0
            or completed_prefix.get("source_pending_side_effects") != 0
            or completed_prefix.get("source_failed_side_effects") != 0
            or completed_prefix.get("candidate_reexecuted") is not False
            or completed_prefix.get("model_execution_allowed") is not False
        ):
            raise FormalV11ProtocolError(
                "formal completed-prefix source authority is invalid"
            )
        attempts_consumed = (
            completed_prefix["current_task_index"] * 3
            + completed_prefix["current_attempt"]
            + 1
        )
        cycles_consumed = (
            completed_prefix["current_task_index"] * 2
            + completed_prefix["current_attempt"]
            + 1
        )
        if (
            completed_prefix.get("source_attempts_consumed") != attempts_consumed
            or completed_prefix.get("source_candidate_model_calls")
            != attempts_consumed
            or completed_prefix.get("source_judge_operations")
            != attempts_consumed
            or completed_prefix.get("source_reflector_cycles_consumed")
            != cycles_consumed
            or completed_prefix.get("source_evolution_jobs_consumed")
            != cycles_consumed * 3
            or completed_prefix.get("source_reflector_model_calls")
            != cycles_consumed * 5
        ):
            raise FormalV11ProtocolError(
                "formal completed-prefix operation inventory is inconsistent"
            )
        expected_community = {
            **common_community,
            "namespace_type": "append_only_completed_prefix_continuation",
            "fresh_start_required": False,
            "legacy_source_import_allowed": True,
            "source_attempts_consumed": attempts_consumed,
            "candidate_operations_remaining": 51 - attempts_consumed,
            "source_reflector_cycles_consumed": cycles_consumed,
            "reflector_cycles_remaining": 34 - cycles_consumed,
            "source_evolution_jobs_consumed": cycles_consumed * 3,
            "evolution_jobs_remaining": 102 - cycles_consumed * 3,
            "completed_prefix_continuation": completed_prefix,
        }
        expected_legacy = {
            "required": True,
            "classification": "APPEND_ONLY_COMPLETED_PREFIX_CONTINUATION",
            "fresh_path_uses_recovery_endpoint": True,
        }
    expected_official = {
        "run_id": official_run_id,
        "source_community_run_id": community_run_id,
        "namespace_type": "official_frozen_test",
        "scope": "official-40",
        "independent_state": True,
        "requires_source_stage": "FINAL_FROZEN",
        "candidate_runs": 40,
        "attempts_per_task": 1,
        "runs_per_attempt": 1,
        "pass_at_k": 1,
        "scoring_gate": "ALL_40_CANDIDATE_RUNS_SEALED",
        "budget": OFFICIAL_V11_BUDGET,
        "policy": OFFICIAL_FROZEN_POLICY,
    }
    if community != expected_community:
        raise FormalV11ProtocolError("formal Community17 contract drifted")
    if official != expected_official:
        raise FormalV11ProtocolError("formal official40 contract drifted")
    if legacy != expected_legacy:
        raise FormalV11ProtocolError("legacy recovery classification drifted")
    receipt = value.get("identity_receipt")
    if not isinstance(receipt, str) or not receipt:
        raise FormalV11ProtocolError("formal v11 identity receipt path is absent")
    if value.get("contract_version") != FORMAL_V11_CONTRACT:
        raise FormalV11ProtocolError("formal v11 contract version drifted")
    _validate_runtime_asset_claims(value.get("runtime_assets"))
    return json.loads(canonical_bytes(value))


def validate_formal_v11_identity(config: ExperimentConfig) -> dict[str, Any]:
    """Verify the immutable v11 protocol identity receipt without side effects."""

    block = validate_formal_v11_block(config.raw.get("formal_runs"))
    try:
        receipt_path = Path(block["identity_receipt"]).resolve(strict=True)
    except OSError as exc:
        raise FormalV11ProtocolError("formal v11 identity receipt is unavailable") from exc
    if not receipt_path.is_relative_to(config.experiment_root / "protocol"):
        raise FormalV11ProtocolError("formal v11 identity receipt escaped protocol root")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FormalV11ProtocolError("formal v11 identity receipt is invalid") from exc
    if not isinstance(receipt, dict):
        raise FormalV11ProtocolError("formal v11 identity receipt is not an object")
    assets = block["runtime_assets"]
    observed_assets = _runtime_asset_block(
        base_config=config,
        overrides=FormalV11RuntimeAssetOverrides(
            framework_lock=Path(assets["framework_lock"]["path"]),
            daemon_bundle=Path(assets["daemon_bundle"]["path"]),
            daemon_manifest=Path(assets["daemon_manifest"]["path"]),
            reflector_readiness_receipt=Path(
                assets["reflector_readiness_receipt"]["path"]
            ),
            reflector_credential_mount_readiness_receipt=Path(
                assets["reflector_credential_mount_readiness_receipt"]["path"]
            ),
            evaluator_dependency_lock=Path(
                assets["evaluator_dependency_lock"]["path"]
            ),
        ),
        reject_inherited_from_base=False,
    )
    if observed_assets != assets:
        raise FormalV11ProtocolError("formal v11 runtime assets drifted")
    content_sha256 = receipt.get("content_sha256")
    body = {key: item for key, item in receipt.items() if key != "content_sha256"}
    active = active_source_identities(config.project_root)
    expected = {
        "schema_version": "openevo.researchclawbench.formal_protocol_identity.v11",
        "protocol_path": str(config.path),
        "protocol_sha256": _file_sha256(config.path),
        "formal_runs_sha256": canonical_sha256(block),
        "runtime_assets_sha256": canonical_sha256(assets),
        "source_identities": active,
        "community_run_id": block["community"]["run_id"],
        "official_run_id": block["official"]["run_id"],
        "model_calls": 0,
        "judge_calls": 0,
        "secret_recorded": False,
    }
    for key, expected_value in expected.items():
        if body.get(key) != expected_value:
            raise FormalV11ProtocolError(f"formal identity field drifted: {key}")
    if (
        set(body) != {*expected, "created_at"}
        or not isinstance(body.get("created_at"), str)
        or not isinstance(content_sha256, str)
        or _SHA256.fullmatch(content_sha256) is None
        or canonical_sha256(body) != content_sha256
    ):
        raise FormalV11ProtocolError("formal v11 identity content hash is invalid")
    return json.loads(canonical_bytes(receipt))


def bind_formal_core_identity(
    *,
    config: ExperimentConfig,
    run_id: str,
    authority_readiness: dict[str, Any],
) -> dict[str, Any]:
    """Create or verify the run-wide Core generation/release fence.

    The receipt contains no bearer material.  A later operation-scoped attach
    must resolve to the exact same generation and release before a supervisor
    transition is allowed.
    """

    run_id = _require_run_id(run_id, "community run_id")
    identity = validate_formal_v11_identity(config)
    block = validate_formal_v11_block(config.raw.get("formal_runs"))
    fields = (
        "generation",
        "release_identity",
        "registry_digest",
        "source_commit",
        "service_identity_id",
    )
    if (
        any(
            not isinstance(authority_readiness.get(field), str) or not authority_readiness[field]
            for field in fields
        )
        or authority_readiness.get("bearer_present") is not True
        or authority_readiness.get("bearer_valid") is not True
        or authority_readiness.get("generation_matches") is not True
        or authority_readiness.get("candidate_port_authorized") is not True
        or authority_readiness.get("secret_recorded") is not False
        or authority_readiness.get("environment_fallback_used") is not False
    ):
        raise FormalV11ProtocolError("managed Core attachment is not ready")
    assets = block["runtime_assets"]
    manifest_identity = assets["daemon_manifest"]
    mount_identity = assets["reflector_credential_mount_readiness_receipt"]
    if (
        authority_readiness["release_identity"]
        != manifest_identity["release_identity"]
        or authority_readiness["registry_digest"]
        != manifest_identity["registry_digest"]
        or authority_readiness["source_commit"] != manifest_identity["source_commit"]
        or mount_identity["release_identity"]
        != authority_readiness["release_identity"]
        or mount_identity["registry_digest"]
        != authority_readiness["registry_digest"]
    ):
        raise FormalV11ProtocolError("FORMAL_CORE_RELEASE_IDENTITY_DRIFT")
    body = {
        "schema_version": "openevo.researchclawbench.formal_core_binding.v11",
        "run_id": run_id,
        "protocol_sha256": identity["protocol_sha256"],
        "formal_identity_sha256": identity["content_sha256"],
        **{field: authority_readiness[field] for field in fields},
        "bearer_present": True,
        "bearer_valid": True,
        "generation_matches": True,
        "candidate_port_authorized": True,
        "secret_recorded": False,
        "environment_fallback_used": False,
    }
    receipt = {**body, "content_sha256": canonical_sha256(body)}
    path = config.experiment_root / "formal_core_bindings" / f"{run_id}.json"
    if path.is_file():
        try:
            prior = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FormalV11ProtocolError("formal Core binding is invalid") from exc
        if prior != receipt:
            raise FormalV11ProtocolError("FORMAL_CORE_GENERATION_OR_RELEASE_DRIFT")
        return prior
    _atomic_write(path, canonical_bytes(receipt) + b"\n")
    return receipt


def prepare_formal_v11_protocol(
    *,
    base_config: ExperimentConfig,
    output_path: str | Path,
    identity_path: str | Path,
    community_run_id: str,
    official_run_id: str,
    runtime_assets: FormalV11RuntimeAssetOverrides,
    successor_recovery_continuation: dict[str, Any] | None = None,
    completed_prefix_continuation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create one fresh, closed v11 protocol and companion identity receipt."""

    community_run_id = _require_run_id(community_run_id, "community run_id")
    official_run_id = _require_run_id(official_run_id, "official run_id")
    community_suffix = re.search(r"_(v[0-9]+)\Z", community_run_id)
    official_suffix = re.search(r"_(v[0-9]+)\Z", official_run_id)
    if (
        community_suffix is None
        or official_suffix is None
        or community_suffix.group(1) != official_suffix.group(1)
    ):
        raise FormalV11ProtocolError(
            "formal community and official run IDs require one matching version suffix"
        )
    run_id_suffix = community_suffix.group(1)
    output = Path(os.path.abspath(output_path))
    identity = Path(os.path.abspath(identity_path))
    protocol_root = (base_config.experiment_root / "protocol").resolve(strict=True)
    if (
        output.parent.resolve(strict=True) != protocol_root
        or identity.parent.resolve(strict=True) != protocol_root
        or output == base_config.path
        or output.exists()
        or identity.exists()
        or output.suffix not in {".yaml", ".yml"}
        or identity.suffix != ".json"
    ):
        raise FormalV11ProtocolError("formal v11 output must be new files under the protocol root")
    payload = copy.deepcopy(base_config.raw)
    deployment_bootstrap = (
        base_config.raw.get("protocol_name") == "formal_v11_deployment_bootstrap"
    )
    if deployment_bootstrap:
        expected_scope = {
            "core_control_attach_allowed": True,
            "managed_candidate_readiness_get_allowed": True,
            "managed_reflector_readiness_get_allowed": True,
            "candidate_execution_allowed": False,
            "judge_execution_allowed": False,
            "evolution_execution_allowed": False,
            "official_execution_allowed": False,
            "recovery_creation_allowed": False,
            "model_operations_allowed": 0,
        }
        framework = Path(runtime_assets.framework_lock).resolve(strict=True)
        bundle = Path(runtime_assets.daemon_bundle).resolve(strict=True)
        manifest = Path(runtime_assets.daemon_manifest).resolve(strict=True)
        bootstrap_claim = base_config.raw.get("formal_v11_bootstrap_assets")
        runtime_release_claim = {
            "managed_runtime_image_digest": base_config.require(
                "reflector.runtime_image_digest"
            ),
            "managed_runtime_loaded_image_id": base_config.require(
                "candidate.runtime_image_id"
            ),
        }
        if (
            base_config.raw.get("bootstrap_scope") != expected_scope
            or bootstrap_claim
            != {
                "framework_lock_sha256": _file_sha256(framework),
                "daemon_bundle_sha256": _file_sha256(bundle),
                "daemon_manifest_sha256": _file_sha256(manifest),
                **runtime_release_claim,
            }
            or Path(base_config.require("native_openevo.framework_lock")).resolve(
                strict=True
            )
            != framework
            or Path(
                base_config.require("native_openevo.remote_core.daemon_bundle")
            ).resolve(strict=True)
            != bundle
            or Path(
                base_config.require("native_openevo.remote_core.daemon_manifest")
            ).resolve(strict=True)
            != manifest
        ):
            raise FormalV11ProtocolError(
                "formal deployment bootstrap does not attest the selected release"
            )
    runtime_asset_block = _runtime_asset_block(
        base_config=base_config,
        overrides=runtime_assets,
        # The no-mutation deployment bootstrap is deliberately bound to the
        # same fresh Core release.  Its exact release hashes and zero-model
        # scope are verified above.  All other source protocols retain the
        # strict non-inheritance fence.
        reject_inherited_from_base=not deployment_bootstrap,
    )
    # The deployment bootstrap is intentionally non-mutating.  A formal
    # training protocol derived from it must not retain that scope or its
    # bootstrap-only asset summary: the closed ``formal_runs`` block below is
    # the sole execution boundary for the new protocol.
    payload.pop("bootstrap_scope", None)
    payload.pop("formal_v11_bootstrap_assets", None)
    payload["protocol_name"] = "community17_official40_formal_v11"
    payload["protocol_version"] = "2.0.0-community17-official40-v11"
    payload["status"] = "FORMAL_V11_CONFIGURED_NOT_STARTED"
    payload["created_at"] = datetime.now(UTC).isoformat()
    source = payload.get("source_identity")
    if not isinstance(source, dict):
        raise FormalV11ProtocolError("base protocol source identity is absent")
    source.update(active_source_identities(base_config.project_root))
    payload["native_openevo"]["framework_lock"] = runtime_asset_block[
        "framework_lock"
    ]["path"]
    payload["native_openevo"]["remote_core"]["daemon_bundle"] = (
        runtime_asset_block["daemon_bundle"]["path"]
    )
    payload["native_openevo"]["remote_core"]["daemon_manifest"] = (
        runtime_asset_block["daemon_manifest"]["path"]
    )
    payload["reflector"]["readiness_receipt"] = runtime_asset_block[
        "reflector_readiness_receipt"
    ]["path"]
    payload["reflector"]["credential_mount_readiness_receipt"] = (
        runtime_asset_block["reflector_credential_mount_readiness_receipt"]["path"]
    )
    payload["reflector"]["require_credential_mount_readiness"] = True
    mount_claim = runtime_asset_block[
        "reflector_credential_mount_readiness_receipt"
    ]
    mount_payload = _closed_json_file(
        Path(mount_claim["path"]),
        label="formal reflector credential-mount readiness receipt",
    )
    payload["reflector"]["credential_mount_expected"] = {
        key: mount_payload[key]
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
    payload["judge"]["evaluator_dependency_lock"] = runtime_asset_block[
        "evaluator_dependency_lock"
    ]
    # ResearchClawBench's production scorer exposes a total score and private
    # rubric/judge material, but it does not expose a separately attested,
    # machine-readable hard-GT authority.  Formal v11 therefore routes normal
    # scored attempts through the strict SOFT_JUDGE partition.  HARD_GT/MIXED
    # remain supported Core attachment classes, but may only be selected when
    # an evaluator supplies an explicit structured authority; they are never
    # inferred from checklist text or judge reasoning.
    payload["teacher"] = {
        "enabled_on_community": True,
        "modes": ["HARD_GT", "SOFT_JUDGE", "MIXED"],
        "production_judge_partition": "SOFT_JUDGE",
        "hard_gt_authority_required": "STRUCTURED_EVALUATOR_AUTHORITY",
        "hard_gt_unavailable_behavior": "SOFT_JUDGE",
        "validator_failure_partition": "MIXED_TASK_LOCAL_VALIDATOR_ONLY",
        "task_local_overlay": True,
        "global_artifact_answers_forbidden": True,
        "official_enabled": False,
    }
    payload["community_validator_failure_policy"] = {
        "enabled": True,
        "consumes_attempt": True,
        "candidate_retry_same_attempt": False,
        "judge_on_validator_failure": False,
        "reflector_on_validator_failure": True,
        "feedback_source": [
            "candidate_trace",
            "candidate_report",
            "validator_receipt",
        ],
        "official_test_enabled": False,
        "run_id_suffix": run_id_suffix,
    }
    payload["official_validator_failure_policy"] = {
        "enabled": False,
        "fail_closed": True,
        "reflector_allowed": False,
        "evolution_allowed": False,
    }
    if (
        successor_recovery_continuation is not None
        and completed_prefix_continuation is not None
    ):
        raise FormalV11ProtocolError("formal continuation modes conflict")
    if (
        successor_recovery_continuation is None
        and completed_prefix_continuation is None
    ):
        legacy_authority_recovery = {
            "required": False,
            "classification": LEGACY_CLASSIFICATION,
            "fresh_path_uses_recovery_endpoint": False,
        }
        community = {
            "run_id": community_run_id,
            "namespace_type": "fresh_community17_formal",
            "scope": "community-17",
            "fresh_start_required": True,
            "legacy_source_import_allowed": False,
            "tasks": 17,
            "attempts_per_task": 3,
            "candidate_runs": 51,
            "reflector_cycles": 34,
            "artifact_evolution_jobs": 102,
            "terminal_stage": "FINAL_FROZEN",
        }
    elif successor_recovery_continuation is not None:
        continuation = json.loads(canonical_bytes(successor_recovery_continuation))
        legacy_authority_recovery = {
            "required": True,
            "classification": "APPEND_ONLY_PRE_JOB_SUCCESSOR_RECOVERY_CONTINUATION",
            "fresh_path_uses_recovery_endpoint": True,
        }
        community = {
            "run_id": community_run_id,
            "namespace_type": "append_only_successor_recovery_continuation",
            "scope": "community-17",
            "fresh_start_required": False,
            "legacy_source_import_allowed": True,
            "tasks": 17,
            "attempts_per_task": 3,
            "candidate_runs": 51,
            "source_attempts_consumed": 1,
            "candidate_operations_remaining": 50,
            "reflector_cycles": 34,
            "artifact_evolution_jobs": 102,
            "terminal_stage": "FINAL_FROZEN",
            "successor_recovery_continuation": continuation,
        }
    else:
        completed_prefix = json.loads(
            canonical_bytes(completed_prefix_continuation)
        )
        attempts_consumed = completed_prefix.get("source_attempts_consumed")
        cycles_consumed = completed_prefix.get(
            "source_reflector_cycles_consumed"
        )
        jobs_consumed = completed_prefix.get("source_evolution_jobs_consumed")
        if not all(type(item) is int for item in (
            attempts_consumed,
            cycles_consumed,
            jobs_consumed,
        )):
            raise FormalV11ProtocolError(
                "formal completed-prefix counts are invalid"
            )
        legacy_authority_recovery = {
            "required": True,
            "classification": "APPEND_ONLY_COMPLETED_PREFIX_CONTINUATION",
            "fresh_path_uses_recovery_endpoint": True,
        }
        community = {
            "run_id": community_run_id,
            "namespace_type": "append_only_completed_prefix_continuation",
            "scope": "community-17",
            "fresh_start_required": False,
            "legacy_source_import_allowed": True,
            "tasks": 17,
            "attempts_per_task": 3,
            "candidate_runs": 51,
            "source_attempts_consumed": attempts_consumed,
            "candidate_operations_remaining": 51 - attempts_consumed,
            "reflector_cycles": 34,
            "source_reflector_cycles_consumed": cycles_consumed,
            "reflector_cycles_remaining": 34 - cycles_consumed,
            "artifact_evolution_jobs": 102,
            "source_evolution_jobs_consumed": jobs_consumed,
            "evolution_jobs_remaining": 102 - jobs_consumed,
            "terminal_stage": "FINAL_FROZEN",
            "completed_prefix_continuation": completed_prefix,
        }
    payload["formal_runs"] = {
        "contract_version": FORMAL_V11_CONTRACT,
        "identity_receipt": str(identity),
        "runtime_assets": runtime_asset_block,
        "legacy_authority_recovery": legacy_authority_recovery,
        "community": community,
        "official": {
            "run_id": official_run_id,
            "source_community_run_id": community_run_id,
            "namespace_type": "official_frozen_test",
            "scope": "official-40",
            "independent_state": True,
            "requires_source_stage": "FINAL_FROZEN",
            "candidate_runs": 40,
            "attempts_per_task": 1,
            "runs_per_attempt": 1,
            "pass_at_k": 1,
            "scoring_gate": "ALL_40_CANDIDATE_RUNS_SEALED",
            "budget": OFFICIAL_V11_BUDGET,
            "policy": OFFICIAL_FROZEN_POLICY,
        },
    }
    validate_formal_v11_block(payload["formal_runs"])
    protocol_bytes = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True).encode("utf-8")
    _atomic_write(output, protocol_bytes)
    receipt_body = {
        "schema_version": "openevo.researchclawbench.formal_protocol_identity.v11",
        "protocol_path": str(output),
        "protocol_sha256": _file_sha256(output),
        "formal_runs_sha256": canonical_sha256(payload["formal_runs"]),
        "runtime_assets_sha256": canonical_sha256(runtime_asset_block),
        "source_identities": active_source_identities(base_config.project_root),
        "community_run_id": community_run_id,
        "official_run_id": official_run_id,
        "model_calls": 0,
        "judge_calls": 0,
        "secret_recorded": False,
        "created_at": datetime.now(UTC).isoformat(),
    }
    receipt = {**receipt_body, "content_sha256": canonical_sha256(receipt_body)}
    try:
        _atomic_write(identity, canonical_bytes(receipt) + b"\n")
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return receipt


__all__ = [
    "FORMAL_V11_CONTRACT",
    "LEGACY_CLASSIFICATION",
    "OFFICIAL_FROZEN_POLICY",
    "OFFICIAL_V11_BUDGET",
    "FormalV11ProtocolError",
    "FormalV11RuntimeAssetOverrides",
    "bind_formal_core_identity",
    "prepare_formal_v11_protocol",
    "validate_formal_v11_block",
    "validate_formal_v11_identity",
]
