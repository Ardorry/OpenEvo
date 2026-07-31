"""Append-only recovery of a paid candidate already sealed by OpenEvo Core.

This narrow migration is intentionally separate from normal training launch.
It never creates a Task or invokes an agent.  It accepts only the documented
``BLOCKED``-before-local-seal failure, verifies the original immutable Core
authority, and creates one uniquely fenced reconciliation successor namespace.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import FROZEN_TASKS, ExperimentConfig
from .managed_core_control import ManagedCoreControlAuthority
from .production_operation_ports import build_production_ports
from .production_training_operations import ProductionTrainingOperations
from .run_manifest import atomic_write_json
from .training_state_store import (
    TrainingStateStore,
    canonical_bytes,
    canonical_sha256,
)
from .training_supervisor import CommunityTrainingSupervisor, SupervisorIdentity
from .transition_engine import TrainingStage

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^rcb_oe_v0_[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_OPERATION_ID = re.compile(r"^reconcile-[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_VALIDATOR_OPERATION_ID = re.compile(
    r"^validator-reconcile-[A-Za-z0-9][A-Za-z0-9._-]{0,95}$"
)
_SOURCE_FAILURE = "CANDIDATE_OPERATION_FAILED_BEFORE_SEAL"
_MIGRATION_REASON = "adapter_dataset_publication_race"
_POSTSEAL_MIGRATION_REASON = "adapter_runtime_injection_metadata_projection"


def _is_reconciliation_namespace(value: str) -> bool:
    """Accept an explicit reconciliation suffix before the formal run version."""

    return bool(
        value.endswith("_reconcile")
        or re.search(r"_reconcile_v[0-9]+\Z", value)
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_file(path: Path, *, root: Path) -> Path:
    value = Path(os.path.abspath(path))
    if not value.is_relative_to(root) or value.is_symlink() or not value.is_file():
        raise ValueError("reconciliation evidence path is unsafe")
    metadata = os.stat(value, follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError("reconciliation evidence is not an unlinked regular file")
    return value


def _tree_sha256(root: Path) -> str:
    """Hash source content while excluding SQLite's non-authoritative sidecars."""

    base = Path(os.path.abspath(root))
    if base.is_symlink() or not base.is_dir():
        raise ValueError("reconciliation tree root is unsafe")
    digest = hashlib.sha256(b"openevo-reconciliation-source-tree-v1\0")
    count = 0
    for item in sorted(base.rglob("*"), key=lambda value: value.as_posix()):
        relative = item.relative_to(base)
        # SQLite may checkpoint committed WAL pages while a read-only logical
        # audit opens and closes connections.  Supervisor state and ledgers
        # are fenced separately by their canonical hashes below; this tree
        # digest covers the remaining namespace files without depending on
        # SQLite's physical page layout.
        if item.name == "training-supervisor.sqlite3" or item.name.endswith(
            (".sqlite3-wal", ".sqlite3-shm")
        ):
            continue
        if item.is_symlink():
            raise ValueError("reconciliation source tree contains a symlink")
        if not item.is_file():
            continue
        payload = item.read_bytes()
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
        count += 1
    if count == 0:
        raise ValueError("reconciliation source tree is empty")
    return digest.hexdigest()


@dataclass(frozen=True)
class CandidateReconciliationSpec:
    operation_id: str
    source_namespace: str
    target_namespace: str
    source_task_id: str
    source_attempt_index: int
    source_run_id: str
    source_core_project_id: str
    source_core_task_id: str
    source_core_attempt_id: str
    source_session_id: str
    source_dataset_id: str
    source_dataset_revision: str
    source_session_result_sha256: str
    source_state_sha256: str
    source_protocol_identity: str
    source_active_identity: str
    source_readiness_identity: str
    source_adapter_identity: str
    source_core_identity: str
    source_failure_classification_path: str
    source_failure_classification_sha256: str
    source_active_identity_path: str
    source_readiness_path: str
    migration_active_identity_path: str
    migration_active_identity_sha256: str
    migration_readiness_path: str
    migration_readiness_sha256: str
    expected_model: str
    expected_harness: str
    expected_capture_mode: str
    expected_codex_version: str
    migration_reason: str

    @classmethod
    def load(cls, path: str | Path) -> CandidateReconciliationSpec:
        value = Path(path).resolve(strict=True)
        payload = json.loads(value.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("candidate reconciliation spec is invalid")
        fields = cls.__dataclass_fields__
        if set(payload) != {"schema_version", *fields}:
            raise ValueError("candidate reconciliation spec has unknown or missing fields")
        spec = cls(**{name: payload[name] for name in fields})
        spec.validate()
        return spec

    def validate(self) -> None:
        if _OPERATION_ID.fullmatch(self.operation_id) is None:
            raise ValueError("reconciliation operation ID is invalid")
        if (
            _RUN_ID.fullmatch(self.source_namespace) is None
            or _RUN_ID.fullmatch(self.target_namespace) is None
            or not _is_reconciliation_namespace(self.target_namespace)
            or self.source_namespace == self.target_namespace
        ):
            raise ValueError("reconciliation namespace identity is invalid")
        if self.source_task_id not in FROZEN_TASKS or not 0 <= self.source_attempt_index <= 2:
            raise ValueError("reconciliation task/attempt is outside the frozen schedule")
        expected_reason = (
            _MIGRATION_REASON
            if self.source_attempt_index == 0
            else _POSTSEAL_MIGRATION_REASON
        )
        if self.migration_reason != expected_reason:
            raise ValueError("reconciliation reason is not allowlisted")
        for name in (
            "source_session_result_sha256",
            "source_state_sha256",
            "source_protocol_identity",
            "source_active_identity",
            "source_readiness_identity",
            "source_adapter_identity",
            "source_core_identity",
            "source_failure_classification_sha256",
            "migration_active_identity_sha256",
            "migration_readiness_sha256",
        ):
            if _SHA256.fullmatch(str(getattr(self, name))) is None:
                raise ValueError(f"reconciliation {name} is not SHA-256")
        for name in (
            "source_run_id",
            "source_core_project_id",
            "source_core_task_id",
            "source_core_attempt_id",
            "source_session_id",
            "source_dataset_id",
            "source_dataset_revision",
            "expected_model",
            "expected_harness",
            "expected_capture_mode",
            "expected_codex_version",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"reconciliation {name} is empty")

    @property
    def source_key(self) -> str:
        return canonical_sha256(
            {
                "namespace": self.source_namespace,
                "task_id": self.source_task_id,
                "attempt_index": self.source_attempt_index,
                "session_id": self.source_session_id,
                "dataset_id": self.source_dataset_id,
                "dataset_revision": self.source_dataset_revision,
                "session_result_sha256": self.source_session_result_sha256,
            }
        )

    def canonical_request(self) -> dict[str, Any]:
        return {
            name: getattr(self, name) for name in self.__dataclass_fields__
        } | {"source_key": self.source_key}


@dataclass(frozen=True)
class ValidatorTerminalReconciliationSpec:
    operation_id: str
    namespace: str
    task_id: str
    attempt_index: int
    source_state_sha256: str
    source_protocol_identity: str
    source_adapter_identity: str
    source_core_identity: str
    source_protocol_path: str
    source_active_identity_path: str
    source_active_identity_sha256: str
    source_readiness_path: str
    source_readiness_sha256: str
    expected_validator_receipt_sha256: str
    executor_active_identity_path: str
    executor_active_identity_sha256: str
    executor_readiness_path: str
    executor_readiness_sha256: str
    migration_reason: str

    @classmethod
    def load(cls, path: str | Path) -> ValidatorTerminalReconciliationSpec:
        value = Path(path).resolve(strict=True)
        payload = json.loads(value.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("validator-terminal reconciliation spec is invalid")
        fields = cls.__dataclass_fields__
        if set(payload) != {"schema_version", *fields}:
            raise ValueError(
                "validator-terminal reconciliation spec has unknown or missing fields"
            )
        spec = cls(**{name: payload[name] for name in fields})
        spec.validate()
        return spec

    def validate(self) -> None:
        if _VALIDATOR_OPERATION_ID.fullmatch(self.operation_id) is None:
            raise ValueError("validator-terminal operation ID is invalid")
        if (
            _RUN_ID.fullmatch(self.namespace) is None
            or not self.namespace.endswith("_reconcile")
            or self.task_id not in FROZEN_TASKS
            or self.attempt_index != 0
            or self.migration_reason
            != "completed_invalid_validator_transition_graph_gap"
        ):
            raise ValueError("validator-terminal reconciliation scope is invalid")
        for name in (
            "source_state_sha256",
            "source_protocol_identity",
            "source_adapter_identity",
            "source_core_identity",
            "source_active_identity_sha256",
            "source_readiness_sha256",
            "expected_validator_receipt_sha256",
            "executor_active_identity_sha256",
            "executor_readiness_sha256",
        ):
            if _SHA256.fullmatch(str(getattr(self, name))) is None:
                raise ValueError(f"validator-terminal {name} is not SHA-256")


class CandidateReconciliationRegistry:
    """Global uniqueness fence for reconciliation sources and targets."""

    def __init__(self, root: Path) -> None:
        self.root = Path(os.path.abspath(root))
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        self.path = self.root / "candidate-reconciliation.sqlite3"
        descriptor = os.open(
            self.path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        os.close(descriptor)
        os.chmod(self.path, 0o600)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS candidate_reconciliations (
                    source_key TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL UNIQUE,
                    target_namespace TEXT NOT NULL UNIQUE,
                    request_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('planned', 'completed')),
                    receipt_json BLOB,
                    receipt_sha256 TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TEXT
                )
                """
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def reserve(self, spec: CandidateReconciliationSpec) -> dict[str, Any]:
        request_sha = canonical_sha256(spec.canonical_request())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM candidate_reconciliations WHERE source_key = ? "
                "OR operation_id = ? OR target_namespace = ?",
                (spec.source_key, spec.operation_id, spec.target_namespace),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO candidate_reconciliations(source_key, operation_id, "
                    "target_namespace, request_sha256, status) VALUES (?, ?, ?, ?, 'planned')",
                    (
                        spec.source_key,
                        spec.operation_id,
                        spec.target_namespace,
                        request_sha,
                    ),
                )
                connection.commit()
                return {"status": "planned", "request_sha256": request_sha}
            if (
                row["source_key"] != spec.source_key
                or row["operation_id"] != spec.operation_id
                or row["target_namespace"] != spec.target_namespace
                or row["request_sha256"] != request_sha
            ):
                raise ValueError("candidate authority was already reconciled differently")
            result = {"status": row["status"], "request_sha256": request_sha}
            if row["receipt_json"] is not None:
                payload = bytes(row["receipt_json"])
                if hashlib.sha256(payload).hexdigest() != row["receipt_sha256"]:
                    raise ValueError("reconciliation registry receipt hash is invalid")
                result["receipt"] = json.loads(payload)
            connection.commit()
            return result

    def complete(
        self, spec: CandidateReconciliationSpec, receipt: dict[str, Any]
    ) -> dict[str, Any]:
        payload = canonical_bytes(receipt)
        digest = hashlib.sha256(payload).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM candidate_reconciliations WHERE source_key = ?",
                (spec.source_key,),
            ).fetchone()
            if row is None:
                raise ValueError("candidate reconciliation was not reserved")
            if row["status"] == "completed":
                if row["receipt_sha256"] != digest or bytes(row["receipt_json"]) != payload:
                    raise ValueError("completed reconciliation receipt conflicts")
                connection.commit()
                return json.loads(payload)
            updated = connection.execute(
                "UPDATE candidate_reconciliations SET status = 'completed', "
                "receipt_json = ?, receipt_sha256 = ?, completed_at = CURRENT_TIMESTAMP "
                "WHERE source_key = ? AND status = 'planned'",
                (payload, digest, spec.source_key),
            )
            if updated.rowcount != 1:
                raise ValueError("candidate reconciliation completion lost its fence")
            connection.commit()
        return json.loads(payload)


def _validate_evidence_file(
    path: str,
    expected_sha256: str,
    *,
    project_root: Path,
) -> dict[str, Any]:
    value = _safe_file(Path(path), root=project_root)
    if _file_sha256(value) != expected_sha256:
        raise ValueError("reconciliation evidence hash changed")
    payload = json.loads(value.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("reconciliation evidence is not an object")
    return payload


def _source_authority(
    config: ExperimentConfig,
    spec: CandidateReconciliationSpec,
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_root = config.experiment_root / "supervisor" / spec.source_namespace
    if not (source_root / "training-supervisor.sqlite3").is_file():
        raise ValueError("source Supervisor namespace is unavailable")
    source_store = TrainingStateStore(source_root)
    state = source_store.load(spec.source_namespace)
    if (
        state.get("stage") != "BLOCKED"
        or state.get("failure_reason") != _SOURCE_FAILURE
        or state.get("_state_sha256") != spec.source_state_sha256
        or state.get("_protocol_sha256") != spec.source_protocol_identity
        or state.get("_adapter_identity_sha256") != spec.source_adapter_identity
        or state.get("_core_identity_sha256") != spec.source_core_identity
        or state.get("current_task_index") != 0
        or state.get("current_attempt") != spec.source_attempt_index
        or state.get("session_ids") != []
        or state.get("dataset_ids") != []
        or "active_candidate_receipt" in state
    ):
        raise ValueError("source Supervisor does not satisfy reconciliation closure")
    tasks = state.get("task_ids")
    if not isinstance(tasks, list) or tasks[0] != spec.source_task_id:
        raise ValueError("source Supervisor task identity changed")
    intent = state.get("active_candidate_intent")
    if (
        not isinstance(intent, dict)
        or intent.get("task_id") != spec.source_task_id
        or intent.get("attempt_index") != spec.source_attempt_index
        or intent.get("run_id") != spec.source_run_id
        or intent.get("adapter_identity_sha256") != spec.source_adapter_identity
        or intent.get("protocol_sha256") != spec.source_protocol_identity
    ):
        raise ValueError("source candidate intent identity changed")
    if source_store.attempts_for_task(spec.source_namespace, spec.source_task_id):
        raise ValueError("source already contains a sealed candidate attempt receipt")
    if source_store.active_resources(spec.source_namespace):
        raise ValueError("source still owns active resources")
    effects = source_store.side_effects_for_experiment(spec.source_namespace)
    if (
        len(effects) != 1
        or effects[0].get("kind") != "candidate"
        or effects[0].get("status") != "planned"
    ):
        raise ValueError("source side-effect ledger is not the closed race signature")
    transitions = source_store.transition_receipts_for_experiment(spec.source_namespace)
    if len(transitions) != 3 or transitions[-1].get("target_stage") != "BLOCKED":
        raise ValueError("source transition ledger is incomplete")
    classification = _validate_evidence_file(
        spec.source_failure_classification_path,
        spec.source_failure_classification_sha256,
        project_root=config.project_root,
    )
    if (
        classification.get("run_id") != spec.source_namespace
        or classification.get("adapter_failure")
        != "DATASET_SEALED_TIMELINE_PUBLICATION_RACE"
        or classification.get("protocol_retry_budget_exhausted") is not True
        or classification.get("sealed_attempt_must_not_be_overwritten") is not True
        or classification.get("new_model_call_required_for_recovery") is not False
        or classification.get("session_id") != spec.source_session_id
        or classification.get("completed_dataset_id") != spec.source_dataset_id
        or classification.get("completed_dataset_revision")
        != spec.source_dataset_revision
    ):
        raise ValueError("source failure classification is not eligible")
    return state, classification


def reconcile_candidate_sealed_authority(
    *,
    config: ExperimentConfig,
    spec: CandidateReconciliationSpec,
    core_authority: ManagedCoreControlAuthority,
) -> dict[str, Any]:
    """Execute or recover one uniquely fenced reconciliation operation."""

    spec.validate()
    project_root = config.project_root
    source_root = config.experiment_root / "supervisor" / spec.source_namespace
    target_root = config.experiment_root / "supervisor" / spec.target_namespace
    source_state, _ = _source_authority(config, spec)
    # Opening SQLite may checkpoint its own already-committed WAL.  Establish
    # the immutable content fence after authoritative read/verification and
    # before any migration operation is reserved or executed.
    source_before = _tree_sha256(source_root)
    source_receipts_before = _tree_sha256(source_root / "receipts")
    _validate_evidence_file(
        spec.source_active_identity_path,
        spec.source_active_identity,
        project_root=project_root,
    )
    _validate_evidence_file(
        spec.source_readiness_path,
        spec.source_readiness_identity,
        project_root=project_root,
    )
    _validate_evidence_file(
        spec.migration_active_identity_path,
        spec.migration_active_identity_sha256,
        project_root=project_root,
    )
    migration_readiness = _validate_evidence_file(
        spec.migration_readiness_path,
        spec.migration_readiness_sha256,
        project_root=project_root,
    )
    if migration_readiness.get("ready") is not True or migration_readiness.get(
        "blockers"
    ) != []:
        raise ValueError("migration readiness is not green")
    protocol_identity = _file_sha256(config.path)
    adapter_identity = str(config.require("source_identity.adapter_tree_sha256"))
    core_identity = str(
        config.require("source_identity.openevo_core_source_tree_sha256")
    )
    if (
        protocol_identity
        != migration_readiness.get("protocol_sha256", protocol_identity)
        or core_authority.generation
        != migration_readiness.get("core_control", {}).get("generation")
        or core_authority.release_identity
        != migration_readiness.get("core_control", {}).get("release_identity")
    ):
        raise ValueError("migration protocol or Core readiness identity drifted")
    registry = CandidateReconciliationRegistry(
        config.experiment_root / "supervisor" / "reconciliation-authority"
    )
    registry.reserve(spec)
    ports = build_production_ports(
        config,
        target_root,
        core_authority=core_authority,
    )
    operations = ProductionTrainingOperations(
        config=config,
        experiment_run_id=spec.target_namespace,
        receipt_root=target_root / "operations",
        ports=ports,
    )
    identity = SupervisorIdentity(
        protocol_sha256=protocol_identity,
        core_identity_sha256=core_identity,
        adapter_identity_sha256=adapter_identity,
    )
    supervisor = CommunityTrainingSupervisor(
        store=TrainingStateStore(target_root),
        experiment_id=spec.target_namespace,
        identity=identity,
        operations=operations,
        task_ids=tuple(source_state["task_ids"]),
    )
    source_reference = {
        "namespace": spec.source_namespace,
        "source_key": spec.source_key,
        "task_id": spec.source_task_id,
        "attempt_index": spec.source_attempt_index,
        "session_id": spec.source_session_id,
        "dataset_id": spec.source_dataset_id,
        "dataset_revision": spec.source_dataset_revision,
        "session_result_sha256": spec.source_session_result_sha256,
        "state_sha256": spec.source_state_sha256,
        "adapter_identity": spec.source_adapter_identity,
        "protocol_identity": spec.source_protocol_identity,
        "active_identity": spec.source_active_identity,
        "readiness_identity": spec.source_readiness_identity,
        "core_identity": spec.source_core_identity,
        "terminal_state": "BLOCKED",
    }
    request = {
        "task_id": spec.source_task_id,
        "source_namespace": spec.source_namespace,
        "source_state_sha256": spec.source_state_sha256,
        "source_task_id": spec.source_task_id,
        "source_attempt_index": spec.source_attempt_index,
        "source_run_id": spec.source_run_id,
        "source_candidate_output_root": os.fspath(
            source_root / "runs" / spec.source_run_id
        ),
        "source_core_project_id": spec.source_core_project_id,
        "source_core_task_id": spec.source_core_task_id,
        "source_core_attempt_id": spec.source_core_attempt_id,
        "source_session_id": spec.source_session_id,
        "source_dataset_id": spec.source_dataset_id,
        "source_dataset_revision": spec.source_dataset_revision,
        "source_session_result_sha256": spec.source_session_result_sha256,
        "source_adapter_identity": spec.source_adapter_identity,
        "source_protocol_identity": spec.source_protocol_identity,
        "source_core_identity": spec.source_core_identity,
        "expected_model": spec.expected_model,
        "expected_harness": spec.expected_harness,
        "expected_capture_mode": spec.expected_capture_mode,
        "expected_codex_version": spec.expected_codex_version,
        "migration_executor_adapter_identity": adapter_identity,
        "migration_executor_core_identity": core_identity,
        "migration_protocol_identity": protocol_identity,
        "migration_core_generation": core_authority.generation,
        "migration_release_identity": core_authority.release_identity,
        "fresh_workspace": True,
        "candidate_reexecuted": False,
        "additional_candidate_model_calls": 0,
    }
    state = supervisor.reconcile_candidate_sealed(
        source=source_reference,
        request=request,
    )
    verify = supervisor.verify()
    if state.get("stage") != "CANDIDATE_SEALED" or verify.get("status") != "PASS":
        raise ValueError("reconciliation successor did not reach a sealed state")
    source_state_after, _ = _source_authority(config, spec)
    source_after = _tree_sha256(source_root)
    source_receipts_after = _tree_sha256(source_root / "receipts")
    if source_before != source_after or source_receipts_before != source_receipts_after:
        raise ValueError(
            "source namespace changed during reconciliation: "
            f"tree={source_before}:{source_after},"
            f"receipts={source_receipts_before}:{source_receipts_after}"
        )
    candidate = state["active_candidate_receipt"]
    migration_timestamp = candidate["completed_at"]
    receipt = {
        "schema_version": 1,
        "operation": "candidate_sealed_authority_reconciliation",
        "operation_id": spec.operation_id,
        "source_key": spec.source_key,
        "source": source_reference,
        "migration": {
            "namespace": spec.target_namespace,
            "namespace_type": "reconciliation_successor",
            "executor_adapter_identity": adapter_identity,
            "executor_core_identity": core_identity,
            "core_generation": core_authority.generation,
            "release_identity": core_authority.release_identity,
            "protocol_identity": protocol_identity,
            "active_identity": spec.migration_active_identity_sha256,
            "readiness_identity": spec.migration_readiness_sha256,
            "state_sha256": state["_state_sha256"],
            "state_revision": state["_revision"],
            "candidate_reexecuted": False,
            "additional_model_calls": 0,
            "source_mutated": False,
            "seal_origin": "reconciliation",
            "created_at": migration_timestamp,
        },
        "verification": {
            "source_tree_before_sha256": source_before,
            "source_tree_after_sha256": source_after,
            "source_receipts_before_sha256": source_receipts_before,
            "source_receipts_after_sha256": source_receipts_after,
            "source_state_before_sha256": source_state["_state_sha256"],
            "source_state_after_sha256": source_state_after["_state_sha256"],
            "source_supervisor_verify": "PASS",
            "target_supervisor_verify": verify["status"],
            # The first durable state for every accepted operation is planned;
            # exact replays may observe completed, but must reproduce the same
            # immutable receipt bytes.
            "registry_reservation_status": "planned",
            "candidate_model_process_started": False,
        },
    }
    receipt["content_sha256"] = canonical_sha256(receipt)
    closed = registry.complete(spec, receipt)
    receipt_path = target_root / "candidate-sealed-reconciliation.json"
    if receipt_path.exists():
        if json.loads(receipt_path.read_text(encoding="utf-8")) != closed:
            raise ValueError("reconciliation receipt file conflicts")
    else:
        atomic_write_json(receipt_path, closed)
        os.chmod(receipt_path, 0o600)
    return closed


def reconcile_invalid_validator_terminal(
    *,
    config: ExperimentConfig,
    spec: ValidatorTerminalReconciliationSpec,
    core_authority: ManagedCoreControlAuthority,
) -> dict[str, Any]:
    """Seal a completed invalid validator result after a graph-only failure."""

    spec.validate()
    project_root = config.project_root
    source_protocol = _safe_file(Path(spec.source_protocol_path), root=project_root)
    if _file_sha256(source_protocol) != spec.source_protocol_identity:
        raise ValueError("validator-terminal source protocol identity changed")
    for path, digest in (
        (spec.source_active_identity_path, spec.source_active_identity_sha256),
        (spec.source_readiness_path, spec.source_readiness_sha256),
        (spec.executor_active_identity_path, spec.executor_active_identity_sha256),
        (spec.executor_readiness_path, spec.executor_readiness_sha256),
    ):
        _validate_evidence_file(path, digest, project_root=project_root)
    executor_readiness = _validate_evidence_file(
        spec.executor_readiness_path,
        spec.executor_readiness_sha256,
        project_root=project_root,
    )
    protocol_identity = _file_sha256(config.path)
    executor_identity = SupervisorIdentity(
        protocol_sha256=protocol_identity,
        core_identity_sha256=str(
            config.require("source_identity.openevo_core_source_tree_sha256")
        ),
        adapter_identity_sha256=str(
            config.require("source_identity.adapter_tree_sha256")
        ),
    )
    core_readiness = executor_readiness.get("core_control", {})
    if (
        executor_readiness.get("ready") is not True
        or executor_readiness.get("blockers") != []
        or protocol_identity
        != executor_readiness.get("protocol_sha256", protocol_identity)
        or core_authority.generation != core_readiness.get("generation")
        or core_authority.release_identity != core_readiness.get("release_identity")
    ):
        raise ValueError("validator-terminal executor readiness drifted")
    source_identity = SupervisorIdentity(
        protocol_sha256=spec.source_protocol_identity,
        core_identity_sha256=spec.source_core_identity,
        adapter_identity_sha256=spec.source_adapter_identity,
    )
    root = config.experiment_root / "supervisor" / spec.namespace
    store = TrainingStateStore(root)
    supervisor = CommunityTrainingSupervisor(
        store=store,
        experiment_id=spec.namespace,
        identity=executor_identity,
        operations=None,  # type: ignore[arg-type]
        task_ids=FROZEN_TASKS,
    )
    state = supervisor.reconcile_invalid_validator_terminal(
        operation_id=spec.operation_id,
        source_identity=source_identity,
        source_state_sha256=spec.source_state_sha256,
        expected_validator_receipt_sha256=spec.expected_validator_receipt_sha256,
        executor_identity=executor_identity,
        executor_active_identity_sha256=spec.executor_active_identity_sha256,
        executor_readiness_sha256=spec.executor_readiness_sha256,
    )
    TrainingStateStore.verify_identity(
        state,
        protocol_sha256=source_identity.protocol_sha256,
        core_identity_sha256=source_identity.core_identity_sha256,
        adapter_identity_sha256=source_identity.adapter_identity_sha256,
    )
    if (
        state.get("stage") != TrainingStage.FAILED.value
        or state.get("failure_reason") != "ARTIFACT_VALIDATOR_FAILED"
        or store.active_resources(spec.namespace)
    ):
        raise ValueError("validator-terminal reconciliation did not seal failure")
    return {
        "schema_version": 1,
        "operation": "completed_validator_terminal_reconciliation",
        "operation_id": spec.operation_id,
        "namespace": spec.namespace,
        "task_id": spec.task_id,
        "attempt_index": spec.attempt_index,
        "state": state["stage"],
        "failure_reason": state["failure_reason"],
        "state_sha256": state["_state_sha256"],
        "source_identity": {
            "protocol_sha256": spec.source_protocol_identity,
            "adapter_identity_sha256": spec.source_adapter_identity,
            "core_identity_sha256": spec.source_core_identity,
            "state_sha256": spec.source_state_sha256,
        },
        "executor_identity": {
            "protocol_sha256": executor_identity.protocol_sha256,
            "adapter_identity_sha256": executor_identity.adapter_identity_sha256,
            "core_identity_sha256": executor_identity.core_identity_sha256,
            "active_identity_sha256": spec.executor_active_identity_sha256,
            "readiness_sha256": spec.executor_readiness_sha256,
            "core_generation": core_authority.generation,
            "release_identity": core_authority.release_identity,
        },
        "candidate_reexecuted": False,
        "additional_candidate_model_calls": 0,
        "judge_calls": 0,
        "source_candidate_artifacts_modified": False,
    }


__all__ = [
    "CandidateReconciliationRegistry",
    "CandidateReconciliationSpec",
    "ValidatorTerminalReconciliationSpec",
    "reconcile_candidate_sealed_authority",
    "reconcile_invalid_validator_terminal",
]
