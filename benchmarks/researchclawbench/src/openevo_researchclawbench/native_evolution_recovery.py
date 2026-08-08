"""Thin ResearchClaw adapter over Core's native successor recovery contract.

This module does not implement an evolution runner.  It freezes the failed
per-item Supervisor evidence, asks Core to reproduce the durable source
authorities, authorizes one existing native recovery target at a time, and
finally seeds an otherwise unused Core project with the three admitted
artifacts.  Candidate and Judge execution are never available on this path.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openevo.backend.science_successor_recovery_control_v1 import (
    ManagedReflectorReadinessResponseV1,
)
from openevo.backend.science_successor_recovery_v1 import (
    HistoricalEvolutionEvidenceDigestV1,
    HistoricalEvolutionSourceAttestationV1,
    ScienceSuccessorRecoveryCreateRequestV1,
    ScienceSuccessorRecoveryProjectSeedAuthorityV1,
    ScienceSuccessorRecoveryProjectSeedRequestV1,
    ScienceSuccessorRecoverySnapshotV1,
    ScienceSuccessorRecoverySourceResolveRequestV1,
    ScienceSuccessorRecoverySourceResponseV1,
    ScienceSuccessorRecoveryTargetOperationV1,
)

from .candidate_reconciliation import _tree_sha256
from .config import MANAGED_CODEX_MODEL, ExperimentConfig
from .legacy_supervisor_attestation import (
    _closed_supervisor_file_inventory,
    _file_sha256,
    _read_immutable_supervisor_state,
    build_legacy_supervisor_attested_inventory,
)
from .managed_core_control import ManagedCoreControlAuthority
from .production_operation_ports import (
    CoreControlError,
    CoreControlV2Client,
    build_production_ports,
)
from .run_manifest import atomic_write_json
from .training_state_store import canonical_bytes, canonical_sha256

_RECOVERY_BASE = "/v2/internal/science-successor-recoveries"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_RECEIPT_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "bearer",
        "bearer_token",
        "credential_value",
        "hmac",
        "private_key",
        "secret",
        "signature",
        "token",
    }
)


class NativeEvolutionRecoveryError(RuntimeError):
    """The closed engineering recovery contract is incomplete or changed."""


@dataclass(frozen=True, slots=True)
class NativeEvolutionRecoverySettings:
    task_id: str
    source_run_id: str
    recovery_run_id: str
    source_resolution_idempotency_key: str
    source_resolution_prepared_path: Path | None
    source_runtime_generation_digest: str
    source_daemon_release_identity: str
    source_framework_lock: Path
    source_release_bundle_manifest: Path
    source_failure_code: str
    source_transition_attempt_capacity: int
    target_authorization_order: tuple[str, ...]

    @classmethod
    def load(
        cls,
        config: ExperimentConfig,
        *,
        source_run_id: str,
        recovery_run_id: str,
    ) -> NativeEvolutionRecoverySettings:
        raw = config.raw.get("native_engineering_recovery")
        if not isinstance(raw, dict):
            raise NativeEvolutionRecoveryError(
                "native engineering recovery config is absent"
            )
        expected_keys = {
            "task",
            "source_run_id",
            "recovery_run_id",
            "source_runtime_generation_digest",
            "source_resolution_idempotency_key",
            "source_resolution_prepared_path",
            "source_daemon_release_identity",
            "source_framework_lock",
            "source_release_bundle_manifest",
            "source_failure_code",
            "source_transition_attempt_capacity",
            "target_authorization_order",
        }
        if set(raw) != expected_keys:
            raise NativeEvolutionRecoveryError(
                "native engineering recovery config is not the closed schema"
            )
        order = raw.get("target_authorization_order")
        settings = cls(
            task_id=str(raw.get("task")),
            source_run_id=str(raw.get("source_run_id")),
            recovery_run_id=str(raw.get("recovery_run_id")),
            source_resolution_idempotency_key=str(
                raw.get("source_resolution_idempotency_key")
            ),
            source_resolution_prepared_path=(
                None
                if raw.get("source_resolution_prepared_path") is None
                else Path(str(raw.get("source_resolution_prepared_path")))
            ),
            source_runtime_generation_digest=str(
                raw.get("source_runtime_generation_digest")
            ),
            source_daemon_release_identity=str(
                raw.get("source_daemon_release_identity")
            ),
            source_framework_lock=Path(str(raw.get("source_framework_lock"))),
            source_release_bundle_manifest=Path(
                str(raw.get("source_release_bundle_manifest"))
            ),
            source_failure_code=str(raw.get("source_failure_code")),
            source_transition_attempt_capacity=int(
                raw.get("source_transition_attempt_capacity")
            ),
            target_authorization_order=(
                tuple(str(item) for item in order)
                if isinstance(order, list)
                else ()
            ),
        )
        if (
            source_run_id != settings.source_run_id
            or recovery_run_id != settings.recovery_run_id
            or settings.task_id != "Life_005"
            or any(
                _SAFE_ID.fullmatch(value) is None
                for value in (
                    settings.source_run_id,
                    settings.recovery_run_id,
                    settings.source_failure_code,
                    settings.source_resolution_idempotency_key,
                )
            )
            or _SHA256.fullmatch(settings.source_runtime_generation_digest) is None
            or _SHA256.fullmatch(settings.source_daemon_release_identity) is None
            or settings.source_transition_attempt_capacity != 2
            or settings.target_authorization_order
            != ("agent_system", "skill_bundle", "text_memory")
            or not settings.source_framework_lock.is_absolute()
            or not settings.source_framework_lock.is_file()
            or not settings.source_release_bundle_manifest.is_absolute()
            or not settings.source_release_bundle_manifest.is_file()
            or (
                settings.source_resolution_prepared_path is not None
                and (
                    not settings.source_resolution_prepared_path.is_absolute()
                    or not settings.source_resolution_prepared_path.is_file()
                )
            )
        ):
            raise NativeEvolutionRecoveryError(
                "native engineering recovery identity is invalid or drifted"
            )
        return settings


def _contains_forbidden_key(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).lower() in _FORBIDDEN_RECEIPT_KEYS
            or _contains_forbidden_key(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def _load_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise NativeEvolutionRecoveryError("recovery evidence path is unsafe")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise NativeEvolutionRecoveryError("recovery evidence is not an object")
    return value


def _write_idempotent(path: Path, payload: dict[str, Any]) -> None:
    if _contains_forbidden_key(payload):
        raise NativeEvolutionRecoveryError(
            "refusing to persist a secret-shaped recovery receipt"
        )
    if path.exists():
        if _load_json(path) != payload:
            raise NativeEvolutionRecoveryError(
                "append-only recovery evidence conflicts"
            )
        return
    atomic_write_json(path, payload)
    os.chmod(path, 0o600)


def _strict_wire(model_type, payload: dict[str, Any]):
    return model_type.model_validate_json(
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _successor_destination_run_id(*, task_id: str, recovery_run_id: str) -> str:
    digest = canonical_sha256(
        {
            "recovery_run_id": recovery_run_id,
            "task_id": task_id,
        }
    )
    run_id = f"{task_id}_a1_recovery_{digest[:20]}"
    if not run_id.startswith(f"{task_id}_a") or any(
        char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
        for char in run_id
    ):
        raise NativeEvolutionRecoveryError(
            "successor destination run identity is unsafe"
        )
    return run_id


def _operation_receipts(source_root: Path) -> dict[str, dict[str, Any]]:
    operation_root = source_root / "operations"
    if operation_root.is_symlink() or not operation_root.is_dir():
        raise NativeEvolutionRecoveryError("source operation authority is unavailable")
    by_kind: dict[str, dict[str, Any]] = {}
    for path in sorted(operation_root.glob("*.json")):
        receipt = _load_json(path)
        content_sha256 = receipt.get("content_sha256")
        body = {key: value for key, value in receipt.items() if key != "content_sha256"}
        kind = receipt.get("operation_kind")
        if (
            not isinstance(kind, str)
            or kind in by_kind
            or not isinstance(content_sha256, str)
            or canonical_sha256(body) != content_sha256
        ):
            raise NativeEvolutionRecoveryError(
                "source operation receipt identity is invalid"
            )
        by_kind[kind] = receipt
    if set(by_kind) != {"candidate", "validation", "feedback_attachment", "evolution"}:
        raise NativeEvolutionRecoveryError(
            "source operation inventory is not the closed engineering set"
        )
    return by_kind


def _validate_source_receipts(
    receipts: dict[str, dict[str, Any]],
    settings: NativeEvolutionRecoverySettings,
) -> dict[str, Any]:
    candidate = receipts["candidate"]
    validation = receipts["validation"]
    attachment = receipts["feedback_attachment"]
    evolution = receipts["evolution"]
    checkpoint = evolution.get("successor_recovery_checkpoint")
    if not isinstance(checkpoint, dict):
        raise NativeEvolutionRecoveryError(
            "source terminal operation has no recovery checkpoint"
        )
    checkpoint_content = checkpoint.get("content_sha256")
    checkpoint_body = {
        key: value for key, value in checkpoint.items() if key != "content_sha256"
    }
    if (
        candidate.get("status") != "SUCCEEDED"
        or candidate.get("completed") is not True
        or validation.get("status") != "SUCCEEDED"
        or validation.get("artifact_valid") is not True
        or attachment.get("status") != "SUCCEEDED"
        or attachment.get("judge_calls") != 0
        or attachment.get("judge_feedback_included") is not False
        or attachment.get("task_local_overlay_scope_id")
        != candidate.get("core_task_id")
        or evolution.get("status") != "FAILED_TERMINAL"
        or evolution.get("failure_class") != "AUTHORITY"
        or checkpoint_content != canonical_sha256(checkpoint_body)
        or checkpoint.get("schema_version")
        != "openevo.researchclawbench.successor_recovery_checkpoint.v1"
        or checkpoint.get("successor_transition_id")
        != candidate.get("successor_transition_id")
        or checkpoint.get("successor_transition_id")
        != attachment.get("successor_transition_id")
        or checkpoint.get("attachment_id") != attachment.get("attachment_id")
        or checkpoint.get("resolved_view_sha256")
        != attachment.get("resolved_view_sha256")
        or checkpoint.get("terminal_error_code") != settings.source_failure_code
        or checkpoint.get("transition_attempt_count")
        != settings.source_transition_attempt_capacity
        or checkpoint.get("commit_absent") is not True
        or checkpoint.get("successor_artifact_count") != 0
        or checkpoint.get("source_mutation_allowed") is not False
        or checkpoint.get("candidate_reexecution_allowed") is not False
        or checkpoint.get("judge_reexecution_allowed") is not False
        or checkpoint.get("reflector_reexecution_allowed") is not False
    ):
        raise NativeEvolutionRecoveryError(
            "source baseline, GT attachment, or terminal authority changed"
        )
    required_text = {
        "successor_transition_id": checkpoint.get("successor_transition_id"),
        "failed_pre_job_operation_id": checkpoint.get(
            "latest_transition_attempt_id"
        ),
        "dataset_id": candidate.get("dataset_id"),
        "dataset_revision": candidate.get("dataset_revision"),
        "attachment_id": attachment.get("attachment_id"),
        "attachment_sha256": attachment.get("attachment_sha256"),
        "resolved_dataset_artifact_id": attachment.get(
            "resolved_dataset_artifact_id"
        ),
        "resolved_view_sha256": attachment.get("resolved_view_sha256"),
    }
    if any(
        not isinstance(value, str) or not value
        for value in required_text.values()
    ):
        raise NativeEvolutionRecoveryError(
            "source Core cross-check identity is incomplete"
        )
    return {
        "checkpoint": checkpoint,
        "core_cross_check": required_text,
        "source_core_task_id": candidate["core_task_id"],
        "candidate_receipt_sha256": candidate["content_sha256"],
        "validation_receipt_sha256": validation["content_sha256"],
        "attachment_receipt_sha256": attachment["content_sha256"],
        "terminal_operation_receipt_sha256": evolution["content_sha256"],
        "terminal_operation": evolution,
    }


class NativeEvolutionRecoveryAdapter:
    """Drive one Life_005 recovery through existing production Core APIs."""

    def __init__(
        self,
        *,
        config: ExperimentConfig,
        source_run_id: str,
        recovery_run_id: str,
        core_authority: ManagedCoreControlAuthority,
    ) -> None:
        if not isinstance(core_authority, ManagedCoreControlAuthority):
            raise NativeEvolutionRecoveryError("managed Core authority is required")
        self.config = config
        self.settings = NativeEvolutionRecoverySettings.load(
            config,
            source_run_id=source_run_id,
            recovery_run_id=recovery_run_id,
        )
        self.core_authority = core_authority
        self.source_root = (
            config.experiment_root / "supervisor" / source_run_id
        ).resolve(strict=False)
        self.recovery_root = (
            config.experiment_root / "successor_recovery" / recovery_run_id
        ).resolve(strict=False)
        if (
            not self.source_root.is_relative_to(
                (config.experiment_root / "supervisor").resolve(strict=False)
            )
            or not self.source_root.is_dir()
            or not self.recovery_root.is_relative_to(
                (config.experiment_root / "successor_recovery").resolve(strict=False)
            )
        ):
            raise NativeEvolutionRecoveryError("recovery namespace path is unsafe")

    @property
    def prepared_path(self) -> Path:
        return self.recovery_root / "source" / "prepared_source.json"

    def _readiness(self, client: CoreControlV2Client) -> ManagedReflectorReadinessResponseV1:
        readiness = _strict_wire(
            ManagedReflectorReadinessResponseV1,
            client.json("GET", "/v2/internal/managed-reflector/readiness"),
        )
        identity = readiness.execution_identity
        if (
            identity.core_generation == self.settings.source_runtime_generation_digest
            or identity.daemon_release_identity
            == self.settings.source_daemon_release_identity
            or readiness.codex_cli_started is not True
            or readiness.model_started is not False
            or readiness.managed_reflector_runtime.codex_cli_started is not True
            or readiness.managed_reflector_runtime.model_started is not False
        ):
            raise NativeEvolutionRecoveryError(
                "current managed reflector is not a fresh no-model recovery identity"
            )
        return readiness

    def _prepare_from_sealed_resolution(
        self,
        *,
        source: dict[str, Any],
        state: dict[str, Any],
        tree_sha256: str,
    ) -> dict[str, Any]:
        path = self.settings.source_resolution_prepared_path
        if path is None:
            raise NativeEvolutionRecoveryError(
                "sealed source resolution path is absent"
            )
        sealed_resolution = _load_json(path)
        sealed_content = sealed_resolution.get("content_sha256")
        sealed_body = {
            key: value
            for key, value in sealed_resolution.items()
            if key != "content_sha256"
        }
        if (
            sealed_resolution.get("schema_version")
            != "openevo.researchclawbench.native_evolution_recovery_prepared.v1"
            or sealed_content != canonical_sha256(sealed_body)
            or sealed_resolution.get("source_run_id")
            != self.settings.source_run_id
            or sealed_resolution.get("task_id") != self.settings.task_id
            or sealed_resolution.get("supervisor_state_sha256")
            != state["state_sha256"]
            or sealed_resolution.get("supervisor_database_sha256")
            != state["database_sha256_before"]
            or sealed_resolution.get("supervisor_tree_sha256") != tree_sha256
            or sealed_resolution.get("source_core_cross_check")
            != source["core_cross_check"]
            or sealed_resolution.get("provider_calls") != 0
            or sealed_resolution.get("model_started") is not False
            or not isinstance(
                sealed_resolution.get("historical_attestation"), dict
            )
        ):
            raise NativeEvolutionRecoveryError(
                "sealed source resolution evidence changed"
            )
        client = CoreControlV2Client(self.core_authority)
        try:
            readiness = self._readiness(client)
        finally:
            client.close()
        prepared = {
            "schema_version": (
                "openevo.researchclawbench.native_evolution_recovery_prepared.v1"
            ),
            "source_run_id": self.settings.source_run_id,
            "recovery_run_id": self.settings.recovery_run_id,
            "task_id": self.settings.task_id,
            "supervisor_state_sha256": state["state_sha256"],
            "supervisor_database_sha256": state["database_sha256_before"],
            "supervisor_tree_sha256": tree_sha256,
            "source_core_cross_check": source["core_cross_check"],
            "source_core_task_id": source["source_core_task_id"],
            "source_attachment_judge_feedback_included": False,
            "source_candidate_reexecuted": False,
            "source_reflector_model_calls": 0,
            "historical_attestation": sealed_resolution[
                "historical_attestation"
            ],
            "sealed_source_resolution_prepared_sha256": sealed_content,
            "execution_identity": readiness.execution_identity.model_dump(
                mode="json"
            ),
            "managed_reflector_credential_mount": (
                readiness.managed_reflector_credential_mount.model_dump(mode="json")
            ),
            "managed_reflector_runtime": (
                readiness.managed_reflector_runtime.model_dump(mode="json")
            ),
            "codex_cli_started": True,
            "model_started": False,
            "provider_calls": 0,
        }
        prepared["content_sha256"] = canonical_sha256(prepared)
        _write_idempotent(self.prepared_path, prepared)
        return prepared

    def prepare(self) -> dict[str, Any]:
        """Freeze caller evidence and current no-model readiness; create no recovery."""

        if self.prepared_path.exists():
            prepared = _load_json(self.prepared_path)
            self._revalidate_prepared_source(prepared)
            return prepared
        receipts = _operation_receipts(self.source_root)
        source = _validate_source_receipts(receipts, self.settings)
        state = _read_immutable_supervisor_state(
            database=self.source_root / "training-supervisor.sqlite3",
            namespace=self.settings.source_run_id,
            allow_stable_empty_wal_sidecars=True,
        )
        files = _closed_supervisor_file_inventory(self.source_root)
        file_hashes = {
            relative: receipt["sha256"] for relative, receipt in files.items()
        }
        tree_sha256 = _tree_sha256(self.source_root)
        if self.settings.source_resolution_prepared_path is not None:
            return self._prepare_from_sealed_resolution(
                source=source,
                state=state,
                tree_sha256=tree_sha256,
            )
        source_dir = self.recovery_root / "source"
        terminal_path = source_dir / "terminal_classification.json"
        failure_path = source_dir / "core_failure_authority.json"
        terminal = {
            "schema_version": (
                "openevo.researchclawbench.native_pre_job_terminal.v1"
            ),
            "source_run_id": self.settings.source_run_id,
            "task_id": self.settings.task_id,
            "successor_transition_id": source["core_cross_check"][
                "successor_transition_id"
            ],
            "failed_pre_job_operation_id": source["core_cross_check"][
                "failed_pre_job_operation_id"
            ],
            "failure_code": self.settings.source_failure_code,
            "transition_attempt_capacity": (
                self.settings.source_transition_attempt_capacity
            ),
            "commit_absent": True,
            "successor_artifact_count": 0,
            "judge_feedback_included": False,
            "candidate_reexecuted": False,
            "reflector_model_calls": 0,
            "terminal_operation_receipt_sha256": source[
                "terminal_operation_receipt_sha256"
            ],
        }
        terminal["content_sha256"] = canonical_sha256(terminal)
        _write_idempotent(terminal_path, terminal)
        _write_idempotent(failure_path, source["terminal_operation"])
        evidence_files = {
            "core_failure_authority": failure_path,
            "terminal_classification": terminal_path,
            "release_bundle_manifest": (
                self.settings.source_release_bundle_manifest
            ),
            "framework_lock": self.settings.source_framework_lock,
        }
        evidence_sha256 = {
            label: _file_sha256(path) for label, path in evidence_files.items()
        }
        inventory = build_legacy_supervisor_attested_inventory(
            experiment_root=self.config.experiment_root,
            supervisor_namespace=self.settings.source_run_id,
            expected_supervisor_stage=state["stage"],
            expected_supervisor_state_sha256=state["state_sha256"],
            expected_supervisor_database_sha256=state[
                "database_sha256_before"
            ],
            expected_supervisor_tree_sha256=tree_sha256,
            expected_supervisor_files_sha256=file_hashes,
            historical_baseline_tree_sha256=tree_sha256,
            evidence_files=evidence_files,
            expected_evidence_sha256=evidence_sha256,
            expected_core_cross_check=source["core_cross_check"],
            allow_stable_empty_wal_sidecars=True,
        )
        inventory_path = source_dir / "legacy_supervisor_inventory.json"
        _write_idempotent(inventory_path, inventory)
        evidence = tuple(
            HistoricalEvolutionEvidenceDigestV1(label=label, sha256=digest)
            for label, digest in sorted(
                {
                    **evidence_sha256,
                    "legacy_supervisor_inventory": inventory["content_sha256"],
                }.items()
            )
        )
        attestation_body: dict[str, Any] = {
            "historical_evolution_source_attestation_contract_version": "1",
            "attestation_id": (
                f"rcb-life005-prejob-{inventory['content_sha256'][:24]}"
            ),
            "provenance_tier": "legacy_supervisor_attested",
            "core_native": False,
            "authority_minted": False,
            "supervisor_namespace": self.settings.source_run_id,
            "supervisor_observed_state": state["stage"],
            "supervisor_state_sha256": state["state_sha256"],
            "supervisor_database_sha256": state["database_sha256_before"],
            "supervisor_tree_sha256": tree_sha256,
            "legacy_supervisor_inventory_sha256": inventory["content_sha256"],
            "terminal_classification_sha256": evidence_sha256[
                "terminal_classification"
            ],
            "source_transition_id": source["core_cross_check"][
                "successor_transition_id"
            ],
            "failed_pre_job_operation_id": source["core_cross_check"][
                "failed_pre_job_operation_id"
            ],
            "claimed_source_core_generation": (
                self.settings.source_runtime_generation_digest
            ),
            "claimed_source_release_identity": (
                self.settings.source_daemon_release_identity
            ),
            "claimed_failure_code": self.settings.source_failure_code,
            "evidence_inventory": tuple(
                item.model_dump(mode="json") for item in evidence
            ),
        }
        attestation_body["content_sha256"] = canonical_sha256(attestation_body)
        attestation = _strict_wire(
            HistoricalEvolutionSourceAttestationV1,
            json.loads(canonical_bytes(attestation_body)),
        )
        attestation_path = source_dir / "historical_attestation.json"
        _write_idempotent(attestation_path, attestation.model_dump(mode="json"))
        client = CoreControlV2Client(self.core_authority)
        try:
            readiness = self._readiness(client)
        finally:
            client.close()
        prepared = {
            "schema_version": (
                "openevo.researchclawbench.native_evolution_recovery_prepared.v1"
            ),
            "source_run_id": self.settings.source_run_id,
            "recovery_run_id": self.settings.recovery_run_id,
            "task_id": self.settings.task_id,
            "supervisor_state_sha256": state["state_sha256"],
            "supervisor_database_sha256": state["database_sha256_before"],
            "supervisor_tree_sha256": tree_sha256,
            "source_core_cross_check": source["core_cross_check"],
            "source_core_task_id": source["source_core_task_id"],
            "source_attachment_judge_feedback_included": False,
            "source_candidate_reexecuted": False,
            "source_reflector_model_calls": 0,
            "historical_attestation": attestation.model_dump(mode="json"),
            "execution_identity": readiness.execution_identity.model_dump(
                mode="json"
            ),
            "managed_reflector_credential_mount": (
                readiness.managed_reflector_credential_mount.model_dump(mode="json")
            ),
            "managed_reflector_runtime": (
                readiness.managed_reflector_runtime.model_dump(mode="json")
            ),
            "codex_cli_started": True,
            "model_started": False,
            "provider_calls": 0,
        }
        prepared["content_sha256"] = canonical_sha256(prepared)
        _write_idempotent(self.prepared_path, prepared)
        return prepared

    def _revalidate_prepared_source(self, prepared: dict[str, Any]) -> None:
        content = prepared.get("content_sha256")
        body = {key: value for key, value in prepared.items() if key != "content_sha256"}
        if (
            prepared.get("schema_version")
            != "openevo.researchclawbench.native_evolution_recovery_prepared.v1"
            or content != canonical_sha256(body)
            or prepared.get("source_run_id") != self.settings.source_run_id
            or prepared.get("recovery_run_id") != self.settings.recovery_run_id
            or prepared.get("task_id") != self.settings.task_id
            or prepared.get("model_started") is not False
            or prepared.get("provider_calls") != 0
        ):
            raise NativeEvolutionRecoveryError("prepared recovery source changed")
        receipts = _operation_receipts(self.source_root)
        _validate_source_receipts(receipts, self.settings)
        state = _read_immutable_supervisor_state(
            database=self.source_root / "training-supervisor.sqlite3",
            namespace=self.settings.source_run_id,
            allow_stable_empty_wal_sidecars=True,
        )
        if (
            state["state_sha256"] != prepared.get("supervisor_state_sha256")
            or state["database_sha256_before"]
            != prepared.get("supervisor_database_sha256")
            or _tree_sha256(self.source_root)
            != prepared.get("supervisor_tree_sha256")
        ):
            raise NativeEvolutionRecoveryError("prepared Supervisor source changed")

    def _resolve_source(
        self,
        client: CoreControlV2Client,
        prepared: dict[str, Any],
    ) -> ScienceSuccessorRecoverySourceResponseV1:
        cross = prepared["source_core_cross_check"]
        attestation = _strict_wire(
            HistoricalEvolutionSourceAttestationV1,
            prepared["historical_attestation"],
        )
        request = ScienceSuccessorRecoverySourceResolveRequestV1(
            resolution_idempotency_key=(
                self.settings.source_resolution_idempotency_key
            ),
            successor_transition_id=cross["successor_transition_id"],
            failed_pre_job_operation_id=cross[
                "failed_pre_job_operation_id"
            ],
            historical_attestation=attestation,
            attachment_ids=(cross["attachment_id"],),
            resolved_dataset_artifact_id=cross[
                "resolved_dataset_artifact_id"
            ],
            transition_attempt_capacity=(
                self.settings.source_transition_attempt_capacity
            ),
        )
        response = _strict_wire(
            ScienceSuccessorRecoverySourceResponseV1,
            client.json(
                "POST",
                f"{_RECOVERY_BASE}/sources/resolve",
                payload=request.model_dump(mode="json"),
            ),
        )
        source = response.source
        feedback = source.dataset.training_feedback
        failed = source.failed_pre_job_operation
        if (
            source.task_id != prepared["source_core_task_id"]
            or source.successor_transition_id != cross["successor_transition_id"]
            or source.source_core_generation
            != self.settings.source_runtime_generation_digest
            or source.source_release_identity
            != self.settings.source_daemon_release_identity
            or source.dataset.dataset_id != cross["dataset_id"]
            or feedback is None
            or feedback.attachment_ids != (cross["attachment_id"],)
            or feedback.attachment_sha256 != (cross["attachment_sha256"],)
            or feedback.resolved_dataset_artifact_id
            != cross["resolved_dataset_artifact_id"]
            or feedback.resolved_view_sha256 != cross["resolved_view_sha256"]
            or failed is None
            or failed.operation_id != cross["failed_pre_job_operation_id"]
            or failed.failure_code != self.settings.source_failure_code
            or tuple(item.target_id for item in response.plan.enabled_methods)
            != self.settings.target_authorization_order
        ):
            raise NativeEvolutionRecoveryError(
                "Core resolved source differs from the frozen adapter evidence"
            )
        _write_idempotent(
            self.recovery_root / "source" / "core_source_response.json",
            response.model_dump(mode="json"),
        )
        return response

    def _create_recovery(
        self,
        client: CoreControlV2Client,
        prepared: dict[str, Any],
        source: ScienceSuccessorRecoverySourceResponseV1,
    ) -> ScienceSuccessorRecoverySnapshotV1:
        readiness = _strict_wire(
            ManagedReflectorReadinessResponseV1,
            {
                "schema_version": "openevo.managed_reflector_readiness.v1",
                "execution_identity": prepared["execution_identity"],
                "managed_reflector_credential_mount": prepared[
                    "managed_reflector_credential_mount"
                ],
                "managed_reflector_runtime": prepared[
                    "managed_reflector_runtime"
                ],
                "codex_cli_started": True,
                "model_started": False,
            },
        )
        live = self._readiness(client)
        if live.execution_identity != readiness.execution_identity:
            raise NativeEvolutionRecoveryError(
                "managed recovery execution identity changed after prepare"
            )
        request = ScienceSuccessorRecoveryCreateRequestV1(
            idempotency_key=f"{self.settings.recovery_run_id}.create",
            source=source.source,
            plan=source.plan,
            execution_identity=live.execution_identity,
            target_authorization_order=self.settings.target_authorization_order,
            source_mutation_allowed=False,
            candidate_execution_allowed=False,
            automatic_target_progression=False,
            automatic_successor_commit=False,
        )
        receipt_path = self.recovery_root / "recovery_create.json"
        if receipt_path.exists():
            initial = _strict_wire(
                ScienceSuccessorRecoverySnapshotV1,
                _load_json(receipt_path),
            )
            snapshot = _strict_wire(
                ScienceSuccessorRecoverySnapshotV1,
                client.json(
                    "GET",
                    f"{_RECOVERY_BASE}/{initial.record.recovery_id}",
                ),
            )
            if (
                snapshot.record.source_transition_id
                != source.source.successor_transition_id
                or snapshot.record.execution_identity != live.execution_identity
                or snapshot.record.target_authorization_order
                != self.settings.target_authorization_order
            ):
                raise NativeEvolutionRecoveryError(
                    "persisted Core recovery identity changed"
                )
            return snapshot
        snapshot = _strict_wire(
            ScienceSuccessorRecoverySnapshotV1,
            client.json(
                "POST",
                _RECOVERY_BASE,
                payload=request.model_dump(mode="json"),
            ),
        )
        if (
            snapshot.record.state != "awaiting_target_authorization"
            or snapshot.record.target_authorization_order
            != self.settings.target_authorization_order
            or snapshot.record.execution_identity != live.execution_identity
        ):
            raise NativeEvolutionRecoveryError(
                "Core recovery did not stop before target authorization"
            )
        _write_idempotent(
            receipt_path,
            snapshot.model_dump(mode="json"),
        )
        return snapshot

    def create(self) -> dict[str, Any]:
        """Resolve and create the recovery, but authorize no model target."""

        prepared = self.prepare()
        client = CoreControlV2Client(self.core_authority)
        try:
            source = self._resolve_source(client, prepared)
            snapshot = self._create_recovery(client, prepared, source)
        finally:
            client.close()
        if (
            snapshot.record.state != "awaiting_target_authorization"
            or snapshot.record.completed_target_ids
            or snapshot.record.active_target_id is not None
        ):
            raise NativeEvolutionRecoveryError(
                "zero-call recovery create did not stop before target authorization"
            )
        receipt = {
            "status": "NATIVE_EVOLUTION_RECOVERY_CREATED_NO_MODEL_CALLS",
            "task_id": self.settings.task_id,
            "source_run_id": self.settings.source_run_id,
            "recovery_run_id": self.settings.recovery_run_id,
            "recovery_id": snapshot.record.recovery_id,
            "recovery_record_sha256": snapshot.record_sha256,
            "state": snapshot.record.state,
            "target_authorization_order": list(
                snapshot.record.target_authorization_order
            ),
            "completed_target_ids": [],
            "candidate_reexecuted": False,
            "judge_executed": False,
            "provider_calls": 0,
            "model_started": False,
        }
        receipt["content_sha256"] = canonical_sha256(receipt)
        _write_idempotent(self.recovery_root / "create_only_receipt.json", receipt)
        return receipt

    def _read_target_until_terminal(
        self,
        client: CoreControlV2Client,
        recovery_id: str,
        target_id: str,
        *,
        timeout_seconds: float,
    ) -> ScienceSuccessorRecoveryTargetOperationV1:
        deadline = time.monotonic() + timeout_seconds
        while True:
            operation = _strict_wire(
                ScienceSuccessorRecoveryTargetOperationV1,
                client.json(
                    "GET",
                    f"{_RECOVERY_BASE}/{recovery_id}/targets/{target_id}",
                ),
            )
            if operation.state in {"succeeded", "failed"}:
                return operation
            if time.monotonic() >= deadline:
                raise NativeEvolutionRecoveryError(
                    "authorized recovery target outcome is indeterminate"
                )
            time.sleep(10.0)

    def _run_target(
        self,
        client: CoreControlV2Client,
        snapshot: ScienceSuccessorRecoverySnapshotV1,
        target_id: str,
    ) -> tuple[ScienceSuccessorRecoverySnapshotV1, ScienceSuccessorRecoveryTargetOperationV1]:
        recovery_id = snapshot.record.recovery_id
        intent = {
            "schema_version": (
                "openevo.researchclawbench.native_recovery_target_intent.v1"
            ),
            "recovery_id": recovery_id,
            "target_id": target_id,
            "idempotency_key": f"{self.settings.recovery_run_id}.{target_id}",
            "candidate_execution_allowed": False,
            "judge_execution_allowed": False,
            "max_reflector_model_calls": 1,
        }
        intent["content_sha256"] = canonical_sha256(intent)
        _write_idempotent(
            self.recovery_root / "targets" / f"{target_id}-intent.json",
            intent,
        )
        operation_path = f"{_RECOVERY_BASE}/{recovery_id}/targets/{target_id}"
        try:
            _strict_wire(
                ScienceSuccessorRecoveryTargetOperationV1,
                client.json("GET", operation_path),
            )
            operation_exists = True
        except CoreControlError as exc:
            if exc.status_code != 404:
                raise
            operation_exists = False
        post_error: CoreControlError | None = None
        if not operation_exists:
            try:
                snapshot = _strict_wire(
                    ScienceSuccessorRecoverySnapshotV1,
                    client.json(
                        "POST",
                        operation_path,
                        payload={
                            "science_successor_recovery_target_run_contract_version": (
                                "1"
                            ),
                            "idempotency_key": intent["idempotency_key"],
                        },
                        timeout_seconds=float(
                            self.config.require("reflector.max_runtime_seconds")
                        )
                        + 180.0,
                    ),
                )
            except CoreControlError as exc:
                # A submitted POST is never repeated.  Resolve its durable
                # intent by GET only so a lost response cannot duplicate paid
                # inference.  If Core has no intent, preserve the POST error.
                post_error = exc
        try:
            operation = self._read_target_until_terminal(
                client,
                recovery_id,
                target_id,
                timeout_seconds=float(
                    self.config.require("reflector.max_runtime_seconds")
                )
                + 300.0,
            )
        except CoreControlError as exc:
            if post_error is not None and exc.status_code == 404:
                raise post_error from exc
            raise
        _write_idempotent(
            self.recovery_root / "targets" / f"{target_id}-operation.json",
            operation.model_dump(mode="json"),
        )
        snapshot = _strict_wire(
            ScienceSuccessorRecoverySnapshotV1,
            client.json("GET", f"{_RECOVERY_BASE}/{recovery_id}"),
        )
        if operation.state != "succeeded" or operation.result is None:
            raise NativeEvolutionRecoveryError(
                f"native recovery target failed closed: {target_id}"
            )
        result = operation.result
        if (
            result.target_id != target_id
            or result.output.artifact_type
            not in {"agent_system", "skill_bundle", "text_memory"}
            or result.reflector_runtime_receipt.model_name != MANAGED_CODEX_MODEL
            or result.reflector_runtime_receipt.runtime_profile != "managed_science"
            or result.reflector_runtime_receipt.auth_mode != "subscription"
            or result.reflector_runtime_receipt.capture_mode != "transcript"
            or result.inference_budget_receipt.actual_reflector_model_calls != 1
        ):
            raise NativeEvolutionRecoveryError(
                "native target result violates the managed GPT-5.5 contract"
            )
        return snapshot, operation

    def _seed_project(
        self,
        client: CoreControlV2Client,
        snapshot: ScienceSuccessorRecoverySnapshotV1,
    ) -> ScienceSuccessorRecoveryProjectSeedAuthorityV1:
        destination_root = self.recovery_root / "destination"
        ports = build_production_ports(
            self.config,
            destination_root,
            core_authority=self.core_authority,
        )
        destination_run_id = _successor_destination_run_id(
            task_id=self.settings.task_id,
            recovery_run_id=self.settings.recovery_run_id,
        )
        destination = ports.candidate.prepare_successor_recovery_destination(
            {
                "task_id": self.settings.task_id,
                "run_id": destination_run_id,
                "experiment_id": self.settings.recovery_run_id,
                "attempt_index": 1,
                "fresh_workspace": True,
                "resume_in_place": False,
                "core_project_id": None,
            },
            f"{self.settings.recovery_run_id}.destination",
        )
        if (
            destination.get("candidate_started") is not False
            or destination.get("task_created") is not False
            or destination.get("attempt_budget_consumed") is not False
        ):
            raise NativeEvolutionRecoveryError(
                "recovery destination unexpectedly executed a Candidate"
            )
        head = destination["project_head"]
        request = ScienceSuccessorRecoveryProjectSeedRequestV1(
            idempotency_key=f"{self.settings.recovery_run_id}.seed-project",
            recovery_id=snapshot.record.recovery_id,
            recovery_record_sha256=snapshot.record_sha256,
            destination_project_id=destination["project_id"],
            expected_destination_project_head_id=head["project_head_id"],
            expected_destination_project_head_sha256=head["manifest_sha256"],
        )
        authority = _strict_wire(
            ScienceSuccessorRecoveryProjectSeedAuthorityV1,
            client.json(
                "POST",
                f"{_RECOVERY_BASE}/{snapshot.record.recovery_id}/seed-project",
                payload=request.model_dump(mode="json"),
                timeout_seconds=300.0,
            ),
        )
        _write_idempotent(
            self.recovery_root / "project_seed.json",
            authority.model_dump(mode="json"),
        )
        return authority

    def run(self) -> dict[str, Any]:
        """Complete exactly three native targets and stop after project seed."""

        prepared = self.prepare()
        client = CoreControlV2Client(self.core_authority)
        try:
            source = self._resolve_source(client, prepared)
            snapshot = self._create_recovery(client, prepared, source)
            operations: list[ScienceSuccessorRecoveryTargetOperationV1] = []
            for target_id in self.settings.target_authorization_order:
                snapshot, operation = self._run_target(
                    client,
                    snapshot,
                    target_id,
                )
                operations.append(operation)
            if snapshot.record.state != "all_targets_completed_awaiting_commit":
                raise NativeEvolutionRecoveryError(
                    "native recovery did not complete the exact target inventory"
                )
            seed = self._seed_project(client, snapshot)
        finally:
            client.close()
        results = [operation.result for operation in operations]
        if any(result is None for result in results):
            raise NativeEvolutionRecoveryError("native target result is absent")
        typed_results = [result for result in results if result is not None]
        artifacts = {
            result.output.artifact_type: {
                "artifact_id": result.registry_artifact_id,
                "artifact_manifest_sha256": (
                    result.registry_artifact_manifest_sha256
                ),
                "job_id": result.job_id,
                "target_id": result.target_id,
            }
            for result in typed_results
        }
        if set(artifacts) != {"agent_system", "skill_bundle", "text_memory"}:
            raise NativeEvolutionRecoveryError(
                "native recovery artifact inventory is incomplete"
            )
        receipt = {
            "schema_version": (
                "openevo.researchclawbench.native_evolution_closed.v1"
            ),
            "status": "ONE_TASK_BASELINE_PLUS_EVOLVE_CLOSED",
            "task_id": self.settings.task_id,
            "source_run_id": self.settings.source_run_id,
            "recovery_run_id": self.settings.recovery_run_id,
            "recovery_id": snapshot.record.recovery_id,
            "recovery_record_sha256": snapshot.record_sha256,
            "destination_project_id": seed.destination_project_id,
            "successor_project_head_id": (
                seed.successor_destination_project_head.project_head_id
            ),
            "successor_project_head_sha256": (
                seed.successor_destination_project_head.manifest_sha256
            ),
            "project_seed_id": seed.seed_request_id,
            "project_seed_sha256": seed.content_sha256,
            "artifacts": artifacts,
            "candidate_core_used": True,
            "candidate_codex_harness_used": True,
            "candidate_managed_runtime_used": True,
            "candidate_direct_subprocess": False,
            "baseline_sealed": True,
            "native_successor_path": True,
            "managed_reflector": True,
            "native_artifact_registry": True,
            "judge_feedback_included": False,
            "current_task_gt_included": True,
            "candidate_reexecuted": False,
            "judge_executed": False,
            "evolved_candidate_executed": False,
            "gpt55_reflector_calls": sum(
                result.inference_budget_receipt.actual_reflector_model_calls
                for result in typed_results
            ),
            "EVOLUTION_SEALED": True,
        }
        receipt["content_sha256"] = canonical_sha256(receipt)
        _write_idempotent(self.recovery_root / "closed_receipt.json", receipt)
        return receipt


def native_evolution_recovery_dry_run(
    config: ExperimentConfig,
    *,
    source_run_id: str,
    recovery_run_id: str,
) -> dict[str, Any]:
    settings = NativeEvolutionRecoverySettings.load(
        config,
        source_run_id=source_run_id,
        recovery_run_id=recovery_run_id,
    )
    return {
        "status": "NATIVE_EVOLUTION_RECOVERY_DRY_RUN_NO_MODEL_CALLS",
        "task_id": settings.task_id,
        "source_run_id": settings.source_run_id,
        "recovery_run_id": settings.recovery_run_id,
        "route": (
            "Core source resolve -> native successor recovery -> managed reflector "
            "-> artifact registry -> successor project seed"
        ),
        "target_authorization_order": list(settings.target_authorization_order),
        "candidate_executed": False,
        "judge_executed": False,
        "evolved_candidate_executed": False,
        "provider_calls": 0,
        "model_started": False,
        "output_root": str(
            config.experiment_root / "successor_recovery" / recovery_run_id
        ),
    }


__all__ = [
    "NativeEvolutionRecoveryAdapter",
    "NativeEvolutionRecoveryError",
    "NativeEvolutionRecoverySettings",
    "native_evolution_recovery_dry_run",
]
