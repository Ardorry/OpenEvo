"""Append-only Community validator-failure learning initialization.

This module references a sealed candidate and a terminal validator receipt.  It
never creates a candidate, copies a Candidate authority, or rewrites either
source supervisor namespace.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .candidate_reconciliation import _tree_sha256
from .config import ExperimentConfig
from .managed_core_control import ManagedCoreControlAuthority
from .production_operation_ports import CoreControlV2Client, build_production_ports
from .production_training_operations import ProductionTrainingOperations
from .run_manifest import atomic_write_json
from .training_state_store import TrainingStateStore, canonical_sha256
from .training_supervisor import CommunityTrainingSupervisor, SupervisorIdentity

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID = re.compile(r"^rcb_oe_v0_[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_OPERATION_ID = re.compile(r"^validator-learning-[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _immutable_supervisor_snapshot(root: Path, namespace: str) -> dict[str, Any]:
    database = (root / namespace / "training-supervisor.sqlite3").resolve(strict=True)
    connection = sqlite3.connect(
        f"file:{database}?mode=ro&immutable=1", uri=True, timeout=5.0
    )
    try:
        row = connection.execute(
            "SELECT protocol_sha256, core_identity_sha256, adapter_identity_sha256, "
            "stage, state_json, state_sha256, revision FROM experiment_state "
            "WHERE experiment_id = ?",
            (namespace,),
        ).fetchone()
        if row is None:
            raise ValueError("validator-learning source namespace is absent")
        effects = connection.execute(
            "SELECT kind, status, receipt_json FROM side_effects "
            "WHERE experiment_id = ? ORDER BY idempotency_key",
            (namespace,),
        ).fetchall()
        attempts = connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE experiment_id = ?", (namespace,)
        ).fetchone()[0]
        active_resources = connection.execute(
            "SELECT COUNT(*) FROM owned_resources WHERE experiment_id = ? AND active = 1",
            (namespace,),
        ).fetchone()[0]
    finally:
        connection.close()
    state = json.loads(row[4])
    return {
        "namespace": namespace,
        "database_sha256": _file_sha256(database),
        "protocol_sha256": row[0],
        "core_identity_sha256": row[1],
        "adapter_identity_sha256": row[2],
        "stage": row[3],
        "state": state,
        "state_sha256": row[5],
        "revision": row[6],
        "attempt_rows": attempts,
        "active_resources": active_resources,
        "effects": [
            {
                "kind": kind,
                "status": status,
                "receipt": None if receipt is None else json.loads(receipt),
            }
            for kind, status, receipt in effects
        ],
    }


@dataclass(frozen=True)
class ValidatorFailureLearningSpec:
    operation_id: str
    source_candidate_namespace: str
    source_validator_namespace: str
    target_namespace: str
    task_id: str
    attempt_index: int
    source_candidate_state_sha256: str
    source_validator_state_sha256: str
    source_session_id: str
    source_dataset_id: str
    source_dataset_revision: str
    source_session_result_sha256: str
    source_validator_receipt_path: str
    source_validator_file_sha256: str
    source_validator_operation_sha256: str
    source_candidate_adapter_identity: str
    source_candidate_protocol_identity: str
    source_validator_adapter_identity: str
    source_validator_protocol_identity: str
    expected_model: str
    expected_harness: str
    expected_capture_mode: str
    expected_codex_version: str

    @classmethod
    def load(cls, path: str | Path) -> ValidatorFailureLearningSpec:
        payload = json.loads(Path(path).resolve(strict=True).read_text(encoding="utf-8"))
        fields = cls.__dataclass_fields__
        if not isinstance(payload, dict) or set(payload) != {"schema_version", *fields}:
            raise ValueError("validator-learning specification is not closed")
        if payload.get("schema_version") != 1:
            raise ValueError("validator-learning specification version is unsupported")
        value = cls(**{name: payload[name] for name in fields})
        value.validate()
        return value

    def validate(self) -> None:
        if _OPERATION_ID.fullmatch(self.operation_id) is None:
            raise ValueError("validator-learning operation ID is invalid")
        if (
            _RUN_ID.fullmatch(self.source_candidate_namespace) is None
            or _RUN_ID.fullmatch(self.source_validator_namespace) is None
            or _RUN_ID.fullmatch(self.target_namespace) is None
            or self.target_namespace in {
                self.source_candidate_namespace,
                self.source_validator_namespace,
            }
            or "validator_learning_v9" not in self.target_namespace
            or self.task_id != "Astronomy_004"
            or self.attempt_index != 0
        ):
            raise ValueError("validator-learning scope is invalid")
        for name in (
            "source_candidate_state_sha256",
            "source_validator_state_sha256",
            "source_session_result_sha256",
            "source_validator_file_sha256",
            "source_validator_operation_sha256",
            "source_candidate_adapter_identity",
            "source_candidate_protocol_identity",
            "source_validator_adapter_identity",
            "source_validator_protocol_identity",
        ):
            if _SHA256.fullmatch(str(getattr(self, name))) is None:
                raise ValueError(f"validator-learning {name} is not SHA-256")
        for name in (
            "source_session_id",
            "source_dataset_id",
            "source_dataset_revision",
            "expected_model",
            "expected_harness",
            "expected_capture_mode",
            "expected_codex_version",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"validator-learning {name} is empty")

    @property
    def source_key(self) -> str:
        return canonical_sha256(
            {
                "candidate_namespace": self.source_candidate_namespace,
                "validator_namespace": self.source_validator_namespace,
                "session_id": self.source_session_id,
                "dataset_id": self.source_dataset_id,
                "dataset_revision": self.source_dataset_revision,
                "session_result_sha256": self.source_session_result_sha256,
                "validator_operation_sha256": self.source_validator_operation_sha256,
            }
        )


class _ValidatorLearningRegistry:
    def __init__(self, root: Path) -> None:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = root / "validator-learning.sqlite3"
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS migrations ("
                "source_key TEXT PRIMARY KEY, operation_id TEXT UNIQUE NOT NULL, "
                "target_namespace TEXT UNIQUE NOT NULL, request_sha256 TEXT NOT NULL, "
                "receipt_json BLOB, receipt_sha256 TEXT)"
            )
            connection.commit()
        finally:
            connection.close()

    def reserve(self, spec: ValidatorFailureLearningSpec) -> None:
        request_sha256 = canonical_sha256(spec.__dict__)
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT operation_id, target_namespace, request_sha256 FROM migrations "
                "WHERE source_key = ?",
                (spec.source_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO migrations VALUES (?, ?, ?, ?, NULL, NULL)",
                    (
                        spec.source_key,
                        spec.operation_id,
                        spec.target_namespace,
                        request_sha256,
                    ),
                )
            elif row != (spec.operation_id, spec.target_namespace, request_sha256):
                raise ValueError("validator-learning source is already consumed")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete(self, spec: ValidatorFailureLearningSpec, receipt: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(payload).hexdigest()
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT receipt_json, receipt_sha256 FROM migrations WHERE source_key = ?",
                (spec.source_key,),
            ).fetchone()
            if row is None:
                raise ValueError("validator-learning migration was not reserved")
            if row[0] is None:
                connection.execute(
                    "UPDATE migrations SET receipt_json = ?, receipt_sha256 = ? "
                    "WHERE source_key = ?",
                    (payload, digest, spec.source_key),
                )
            elif row != (payload, digest):
                raise ValueError("validator-learning receipt conflicts")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return receipt


@dataclass(frozen=True)
class ValidatorLearningPreEffectReplacementSpec:
    operation_id: str
    source_namespace: str
    target_namespace: str
    source_state_sha256: str
    source_protocol_identity: str
    source_core_identity: str
    source_adapter_identity: str

    @classmethod
    def load(cls, path: str | Path) -> ValidatorLearningPreEffectReplacementSpec:
        payload = json.loads(Path(path).resolve(strict=True).read_text(encoding="utf-8"))
        fields = cls.__dataclass_fields__
        if not isinstance(payload, dict) or set(payload) != {"schema_version", *fields}:
            raise ValueError("validator-learning replacement specification is not closed")
        if payload.get("schema_version") != 1:
            raise ValueError("validator-learning replacement specification version is unsupported")
        value = cls(**{name: payload[name] for name in fields})
        value.validate()
        return value

    def validate(self) -> None:
        if _OPERATION_ID.fullmatch(self.operation_id) is None:
            raise ValueError("validator-learning replacement operation ID is invalid")
        if (
            _RUN_ID.fullmatch(self.source_namespace) is None
            or _RUN_ID.fullmatch(self.target_namespace) is None
            or self.source_namespace == self.target_namespace
            or "validator_learning_v9" not in self.source_namespace
            or "validator_learning_v9" not in self.target_namespace
        ):
            raise ValueError("validator-learning replacement scope is invalid")
        for name in (
            "source_state_sha256",
            "source_protocol_identity",
            "source_core_identity",
            "source_adapter_identity",
        ):
            if _SHA256.fullmatch(str(getattr(self, name))) is None:
                raise ValueError(f"validator-learning replacement {name} is not SHA-256")


class _ValidatorLearningReplacementRegistry:
    def __init__(self, root: Path) -> None:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = root / "validator-learning.sqlite3"
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS pre_effect_replacements ("
                "source_namespace TEXT PRIMARY KEY, operation_id TEXT UNIQUE NOT NULL, "
                "target_namespace TEXT UNIQUE NOT NULL, request_sha256 TEXT NOT NULL, "
                "receipt_json BLOB, receipt_sha256 TEXT)"
            )
            connection.commit()
        finally:
            connection.close()

    def reserve(self, spec: ValidatorLearningPreEffectReplacementSpec) -> None:
        request_sha256 = canonical_sha256(spec.__dict__)
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT operation_id, target_namespace, request_sha256 "
                "FROM pre_effect_replacements WHERE source_namespace = ?",
                (spec.source_namespace,),
            ).fetchone()
            expected = (spec.operation_id, spec.target_namespace, request_sha256)
            if row is None:
                connection.execute(
                    "INSERT INTO pre_effect_replacements VALUES (?, ?, ?, ?, NULL, NULL)",
                    (spec.source_namespace, *expected),
                )
            elif row != expected:
                raise ValueError("validator-learning pre-effect source is already replaced")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete(
        self,
        spec: ValidatorLearningPreEffectReplacementSpec,
        receipt: dict[str, Any],
    ) -> None:
        payload = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(payload).hexdigest()
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT receipt_json, receipt_sha256 FROM pre_effect_replacements "
                "WHERE source_namespace = ?",
                (spec.source_namespace,),
            ).fetchone()
            if row is None:
                raise ValueError("validator-learning replacement was not reserved")
            if row[0] is None:
                connection.execute(
                    "UPDATE pre_effect_replacements SET receipt_json = ?, receipt_sha256 = ? "
                    "WHERE source_namespace = ?",
                    (payload, digest, spec.source_namespace),
                )
            elif row != (payload, digest):
                raise ValueError("validator-learning replacement receipt conflicts")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def initialize_validator_failure_learning(
    *,
    config: ExperimentConfig,
    spec: ValidatorFailureLearningSpec,
    core_authority: ManagedCoreControlAuthority,
) -> dict[str, Any]:
    spec.validate()
    if config.community_validator_failure_policy is None:
        raise ValueError("Community validator-failure learning is not enabled")
    supervisor_root = config.experiment_root / "supervisor"
    target_root = supervisor_root / spec.target_namespace
    candidate_root = supervisor_root / spec.source_candidate_namespace
    validator_root = supervisor_root / spec.source_validator_namespace
    candidate_before = _tree_sha256(candidate_root)
    validator_before = _tree_sha256(validator_root)
    source_candidate = _immutable_supervisor_snapshot(
        supervisor_root, spec.source_candidate_namespace
    )
    source_validator = _immutable_supervisor_snapshot(
        supervisor_root, spec.source_validator_namespace
    )
    validator_effects = [
        item
        for item in source_validator["effects"]
        if item["kind"] == "validator" and item["status"] == "completed"
    ]
    forbidden_effects = [
        item
        for item in source_validator["effects"]
        if item["kind"] in {"evaluation", "feedback-attachment", "evolution"}
    ]
    if (
        source_candidate["stage"] != "BLOCKED"
        or source_candidate["state"].get("failure_reason")
        != "CANDIDATE_OPERATION_FAILED_BEFORE_SEAL"
        or source_candidate["state_sha256"] != spec.source_candidate_state_sha256
        or source_candidate["adapter_identity_sha256"]
        != spec.source_candidate_adapter_identity
        or source_candidate["protocol_sha256"]
        != spec.source_candidate_protocol_identity
        or source_candidate["active_resources"] != 0
        or source_validator["stage"] != "FAILED"
        or source_validator["state"].get("failure_reason")
        != "ARTIFACT_VALIDATOR_FAILED"
        or source_validator["state_sha256"] != spec.source_validator_state_sha256
        or source_validator["adapter_identity_sha256"]
        != spec.source_validator_adapter_identity
        or source_validator["protocol_sha256"]
        != spec.source_validator_protocol_identity
        or source_validator["active_resources"] != 0
        or len(validator_effects) != 1
        or forbidden_effects
    ):
        raise ValueError("validator-learning source supervisors are not closed")
    candidate = source_validator["state"].get("active_candidate_receipt")
    validation = validator_effects[0]["receipt"]
    if (
        not isinstance(candidate, dict)
        or not isinstance(validation, dict)
        or candidate.get("session_id") != spec.source_session_id
        or candidate.get("dataset_id") != spec.source_dataset_id
        or candidate.get("dataset_revision") != spec.source_dataset_revision
        or candidate.get("session_result_id") != spec.source_session_result_sha256
        or candidate.get("candidate_reexecuted") is not False
        or candidate.get("additional_candidate_model_calls") != 0
        or validation.get("artifact_valid") is not False
        or validation.get("content_sha256")
        != spec.source_validator_operation_sha256
    ):
        raise ValueError("validator-learning candidate/validator authority changed")
    validator_path = Path(spec.source_validator_receipt_path).resolve(strict=True)
    expected_validator_path = (
        validator_root / "runs" / candidate["run_id"] / "validator.json"
    ).resolve(strict=True)
    if (
        validator_path != expected_validator_path
        or validator_path.is_symlink()
        or _file_sha256(validator_path) != spec.source_validator_file_sha256
    ):
        raise ValueError("validator-learning validator receipt file changed")

    client = CoreControlV2Client(core_authority)
    try:
        attempt = client.json(
            "GET",
            f"/v2/internal/training-attempts/{candidate['core_task_id']}/"
            f"{candidate['core_attempt_id']}",
        )
        task = client.json("GET", f"/v2/tasks/{candidate['core_task_id']}")
        dataset = client.json(
            "GET", f"/v2/internal/training-feedback/datasets/{spec.source_dataset_id}"
        )
        project = client.json("GET", f"/v2/projects/{candidate['core_project_id']}")
        successor = client.json(
            "GET",
            "/v2/internal/training-successors/"
            + str(candidate["successor_transition_id"]),
        )
    finally:
        client.close()
    execution = attempt.get("execution_receipt", {})
    session_result = attempt.get("session_result", {})
    agent = session_result.get("metadata", {}).get("agent", {})
    isolation = (
        session_result.get("metadata", {})
        .get("openevo", {})
        .get("credential_isolation", {})
    )
    checks = {
        "session": execution.get("session_id")
        == session_result.get("session_id")
        == dataset.get("session_id")
        == spec.source_session_id,
        "completed": execution.get("terminal_status") == "COMPLETED"
        and session_result.get("status") == "COMPLETED",
        "dataset": dataset.get("completed_dataset_id") == spec.source_dataset_id
        and dataset.get("completed_dataset_revision") == spec.source_dataset_revision,
        "session_result": execution.get("session_result_sha256")
        == spec.source_session_result_sha256,
        "task_attempt": task.get("authoritative_attempt_id")
        == candidate["core_attempt_id"],
        "model": execution.get("model_ref") == spec.expected_model
        and agent.get("model_name") == spec.expected_model
        and execution.get("harness_id") == spec.expected_harness
        and execution.get("capture_mode") == spec.expected_capture_mode
        and isolation.get("codex_version") == spec.expected_codex_version,
        "successor": successor.get("transition", {}).get("state") == "failed"
        and successor.get("artifacts") == []
        and not isinstance(successor.get("commit"), dict),
    }
    if not all(checks.values()):
        raise ValueError(
            "validator-learning Core authority failed: "
            + ",".join(sorted(key for key, passed in checks.items() if not passed))
        )
    active_head = project.get("active_project_head")
    if not isinstance(active_head, dict):
        raise TypeError("validator-learning source project lacks an active head")

    registry = _ValidatorLearningRegistry(
        supervisor_root / "validator-learning-authority"
    )
    registry.reserve(spec)
    ports = build_production_ports(
        config, target_root, core_authority=core_authority
    )
    baseline = ports.composite.register_baseline(
        project_id=candidate["core_project_id"], project_head=active_head
    )
    if baseline.get("composite_id") != "c000":
        raise ValueError("validator-learning baseline registration failed")
    operations = ProductionTrainingOperations(
        config=config,
        experiment_run_id=spec.target_namespace,
        receipt_root=target_root / "operations",
        ports=ports,
    )
    identity = SupervisorIdentity(
        protocol_sha256=_file_sha256(config.path),
        core_identity_sha256=str(
            config.require("source_identity.openevo_core_source_tree_sha256")
        ),
        adapter_identity_sha256=str(
            config.require("source_identity.adapter_tree_sha256")
        ),
    )
    source_reference = {
        "source_candidate_namespace": spec.source_candidate_namespace,
        "source_validator_namespace": spec.source_validator_namespace,
        "task_id": spec.task_id,
        "attempt_index": 0,
        "session_id": spec.source_session_id,
        "dataset_id": spec.source_dataset_id,
        "dataset_revision": spec.source_dataset_revision,
        "session_result_sha256": spec.source_session_result_sha256,
        "candidate_state_sha256": spec.source_candidate_state_sha256,
        "validator_state_sha256": spec.source_validator_state_sha256,
        "validator_operation_sha256": spec.source_validator_operation_sha256,
        "validator_file_sha256": spec.source_validator_file_sha256,
        "candidate_adapter_identity": spec.source_candidate_adapter_identity,
        "candidate_protocol_identity": spec.source_candidate_protocol_identity,
        "validator_adapter_identity": spec.source_validator_adapter_identity,
        "validator_protocol_identity": spec.source_validator_protocol_identity,
        "candidate_reexecuted": False,
        "additional_candidate_model_calls": 0,
    }
    receipt = {
        "schema_version": 1,
        "operation": "community_validator_failure_source_reference",
        "operation_id": spec.operation_id,
        "source_key": spec.source_key,
        "source": source_reference,
        "migration": {
            "namespace": spec.target_namespace,
            "namespace_type": "community_validator_failure_learning",
            "executor_adapter_identity": identity.adapter_identity_sha256,
            "executor_core_identity": identity.core_identity_sha256,
            "protocol_identity": identity.protocol_sha256,
            "core_generation": core_authority.generation,
            "release_identity": core_authority.release_identity,
            "candidate_reexecuted": False,
            "additional_candidate_model_calls": 0,
            "source_mutated": False,
        },
        "verification": {
            "core_checks": checks,
            "candidate_tree_before_sha256": candidate_before,
            "validator_tree_before_sha256": validator_before,
            "candidate_database_sha256": source_candidate["database_sha256"],
            "validator_database_sha256": source_validator["database_sha256"],
            "validator_receipt_path": os.fspath(validator_path),
            "baseline_composite_sha256": baseline["composite_sha256"],
        },
    }
    receipt["content_sha256"] = canonical_sha256(receipt)
    target_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    receipt_path = target_root / "validator-learning-source-reference.json"
    if receipt_path.exists():
        if json.loads(receipt_path.read_text(encoding="utf-8")) != receipt:
            raise ValueError("validator-learning source receipt conflicts")
    else:
        atomic_write_json(receipt_path, receipt)
        os.chmod(receipt_path, 0o600)
    supervisor = CommunityTrainingSupervisor(
        store=TrainingStateStore(target_root),
        experiment_id=spec.target_namespace,
        identity=identity,
        operations=operations,
        task_ids=(spec.task_id,),
        validator_failure_policy=config.community_validator_failure_policy,
    )
    state = supervisor.initialize_validator_failure_learning(
        source_reference=source_reference,
        candidate=candidate,
        validation=validation,
        source_receipt_sha256=receipt["content_sha256"],
    )
    if (
        _tree_sha256(candidate_root) != candidate_before
        or _tree_sha256(validator_root) != validator_before
    ):
        raise ValueError("validator-learning source namespace changed")
    receipt["migration"].update(
        {"initial_state_sha256": state["_state_sha256"], "state_revision": state["_revision"]}
    )
    receipt["content_sha256"] = canonical_sha256(
        {key: value for key, value in receipt.items() if key != "content_sha256"}
    )
    # The source-reference file was sealed before the state transition.  A
    # second receipt records the resulting namespace identity append-only.
    completed_path = target_root / "validator-learning-initialization.json"
    completed = {**receipt, "supervisor_verify": supervisor.verify()}
    completed["content_sha256"] = canonical_sha256(
        {key: value for key, value in completed.items() if key != "content_sha256"}
    )
    if completed_path.exists():
        if json.loads(completed_path.read_text(encoding="utf-8")) != completed:
            raise ValueError("validator-learning initialization receipt conflicts")
    else:
        atomic_write_json(completed_path, completed)
        os.chmod(completed_path, 0o600)
    registry.complete(spec, completed)
    return completed


def replace_validator_learning_pre_effect_namespace(
    *,
    config: ExperimentConfig,
    spec: ValidatorLearningPreEffectReplacementSpec,
    core_authority: ManagedCoreControlAuthority,
) -> dict[str, Any]:
    """Replace one identity-stale namespace before its first external effect.

    The source stays append-only and cannot be resumed by this operation.  The
    replacement is admitted only when the sole pending effect is an attachment
    intent that Core proves never materialized.
    """

    spec.validate()
    if config.community_validator_failure_policy is None:
        raise ValueError("Community validator-failure learning is not enabled")
    supervisor_root = config.experiment_root / "supervisor"
    source_root = supervisor_root / spec.source_namespace
    target_root = supervisor_root / spec.target_namespace
    source_before = _tree_sha256(source_root)
    source = _immutable_supervisor_snapshot(supervisor_root, spec.source_namespace)
    state = source["state"]
    effects = source["effects"]
    planned = [
        item
        for item in effects
        if item["kind"] == "feedback-attachment" and item["status"] == "planned"
    ]
    candidate = state.get("active_candidate_receipt")
    validation = state.get("active_validation_receipt")
    if (
        source["stage"] != "VALIDATOR_FEEDBACK_PENDING"
        or source["state_sha256"] != spec.source_state_sha256
        or source["protocol_sha256"] != spec.source_protocol_identity
        or source["core_identity_sha256"] != spec.source_core_identity
        or source["adapter_identity_sha256"] != spec.source_adapter_identity
        or source["attempt_rows"] != 1
        or source["active_resources"] != 0
        or len(effects) != 1
        or len(planned) != 1
        or not isinstance(candidate, dict)
        or not isinstance(validation, dict)
        or state.get("candidate_reexecuted") is not False
        or state.get("additional_candidate_model_calls") != 0
        or state.get("attachment_ids") != []
        or state.get("evolution_job_ids") != []
        or validation.get("artifact_valid") is not False
    ):
        raise ValueError("validator-learning pre-effect source is not replaceable")

    client = CoreControlV2Client(core_authority)
    try:
        attachments = client.json(
            "GET",
            "/v2/internal/training-feedback/sessions/"
            + str(candidate["session_id"])
            + "/attachments",
        )
        attempt = client.json(
            "GET",
            f"/v2/internal/training-attempts/{candidate['core_task_id']}/"
            f"{candidate['core_attempt_id']}",
        )
        dataset = client.json(
            "GET", f"/v2/internal/training-feedback/datasets/{candidate['dataset_id']}"
        )
        project = client.json("GET", f"/v2/projects/{candidate['core_project_id']}")
    finally:
        client.close()
    execution = attempt.get("execution_receipt", {})
    if (
        attachments.get("attachments") != []
        or execution.get("session_id") != candidate.get("session_id")
        or execution.get("terminal_status") != "COMPLETED"
        or execution.get("session_result_sha256") != candidate.get("session_result_id")
        or dataset.get("completed_dataset_id") != candidate.get("dataset_id")
        or dataset.get("completed_dataset_revision") != candidate.get("dataset_revision")
    ):
        raise ValueError("validator-learning pre-effect Core authority is not closed")
    active_head = project.get("active_project_head")
    if not isinstance(active_head, dict):
        raise TypeError("validator-learning replacement project head is absent")

    registry = _ValidatorLearningReplacementRegistry(
        supervisor_root / "validator-learning-authority"
    )
    registry.reserve(spec)
    ports = build_production_ports(config, target_root, core_authority=core_authority)
    baseline = ports.composite.register_baseline(
        project_id=candidate["core_project_id"], project_head=active_head
    )
    if baseline.get("composite_id") != "c000":
        raise ValueError("validator-learning replacement baseline failed")
    identity = SupervisorIdentity(
        protocol_sha256=_file_sha256(config.path),
        core_identity_sha256=str(
            config.require("source_identity.openevo_core_source_tree_sha256")
        ),
        adapter_identity_sha256=str(config.require("source_identity.adapter_tree_sha256")),
    )
    source_reference = dict(state["validator_failure_learning_source"])
    source_reference.update(
        {
            "pre_effect_replacement_source": spec.source_namespace,
            "pre_effect_replacement_state_sha256": spec.source_state_sha256,
            "candidate_reexecuted": False,
            "additional_candidate_model_calls": 0,
        }
    )
    receipt = {
        "schema_version": 1,
        "operation": "community_validator_learning_pre_effect_replacement",
        "operation_id": spec.operation_id,
        "source": {
            "namespace": spec.source_namespace,
            "state_sha256": spec.source_state_sha256,
            "protocol_identity": spec.source_protocol_identity,
            "core_identity": spec.source_core_identity,
            "adapter_identity": spec.source_adapter_identity,
            "stage": source["stage"],
            "planned_attachment_intents": 1,
            "completed_external_effects": 0,
            "model_calls": 0,
        },
        "replacement": {
            "namespace": spec.target_namespace,
            "protocol_identity": identity.protocol_sha256,
            "core_identity": identity.core_identity_sha256,
            "adapter_identity": identity.adapter_identity_sha256,
            "core_generation": core_authority.generation,
            "release_identity": core_authority.release_identity,
            "candidate_reexecuted": False,
            "additional_candidate_model_calls": 0,
        },
        "verification": {
            "source_tree_before_sha256": source_before,
            "core_attachment_count": 0,
            "session_sealed": True,
            "dataset_sealed": True,
            "baseline_composite_sha256": baseline["composite_sha256"],
        },
    }
    receipt["content_sha256"] = canonical_sha256(receipt)
    target_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    source_receipt = target_root / "validator-learning-pre-effect-replacement.json"
    if source_receipt.exists():
        if json.loads(source_receipt.read_text(encoding="utf-8")) != receipt:
            raise ValueError("validator-learning replacement source receipt conflicts")
    else:
        atomic_write_json(source_receipt, receipt)
        os.chmod(source_receipt, 0o600)
    operations = ProductionTrainingOperations(
        config=config,
        experiment_run_id=spec.target_namespace,
        receipt_root=target_root / "operations",
        ports=ports,
    )
    supervisor = CommunityTrainingSupervisor(
        store=TrainingStateStore(target_root),
        experiment_id=spec.target_namespace,
        identity=identity,
        operations=operations,
        task_ids=("Astronomy_004",),
        validator_failure_policy=config.community_validator_failure_policy,
    )
    initialized = supervisor.initialize_validator_failure_learning(
        source_reference=source_reference,
        candidate=candidate,
        validation=validation,
        source_receipt_sha256=receipt["content_sha256"],
    )
    if _tree_sha256(source_root) != source_before:
        raise ValueError("validator-learning pre-effect source namespace changed")
    completed = {
        **receipt,
        "replacement": {
            **receipt["replacement"],
            "initial_state_sha256": initialized["_state_sha256"],
            "state_revision": initialized["_revision"],
        },
        "supervisor_verify": supervisor.verify(),
    }
    completed["content_sha256"] = canonical_sha256(
        {key: value for key, value in completed.items() if key != "content_sha256"}
    )
    completed_path = target_root / "validator-learning-pre-effect-initialization.json"
    if completed_path.exists():
        if json.loads(completed_path.read_text(encoding="utf-8")) != completed:
            raise ValueError("validator-learning replacement initialization conflicts")
    else:
        atomic_write_json(completed_path, completed)
        os.chmod(completed_path, 0o600)
    registry.complete(spec, completed)
    return completed


def replace_validator_learning_post_feedback_pre_evolution_namespace(
    *,
    config: ExperimentConfig,
    spec: ValidatorLearningPreEffectReplacementSpec,
    core_authority: ManagedCoreControlAuthority,
) -> dict[str, Any]:
    """Replace a scope-mismatched feedback run before any evolution job.

    This narrow append-only repair accepts one durable attachment whose public
    benchmark scope was incorrectly used as the Core science-task scope.  The
    failed successor must expose no commit, artifact, or method job authority.
    The source stays immutable; the replacement starts again at the feedback
    boundary and creates a new attachment under the corrected identity.
    """

    spec.validate()
    if config.community_validator_failure_policy is None:
        raise ValueError("Community validator-failure learning is not enabled")
    supervisor_root = config.experiment_root / "supervisor"
    source_root = supervisor_root / spec.source_namespace
    target_root = supervisor_root / spec.target_namespace
    source_before = _tree_sha256(source_root)
    source = _immutable_supervisor_snapshot(supervisor_root, spec.source_namespace)
    state = source["state"]
    effects = source["effects"]
    completed_feedback = [
        item
        for item in effects
        if item["kind"] == "feedback-attachment" and item["status"] == "completed"
    ]
    planned_evolution = [
        item
        for item in effects
        if item["kind"] == "evolution" and item["status"] == "planned"
    ]
    candidate = state.get("active_candidate_receipt")
    validation = state.get("active_validation_receipt")
    attachment_receipt = state.get("active_attachment_receipt")
    public_task_id = state.get("task_ids", [None])[0]
    if (
        source["stage"] != "EVOLUTION_RUNNING"
        or source["state_sha256"] != spec.source_state_sha256
        or source["protocol_sha256"] != spec.source_protocol_identity
        or source["core_identity_sha256"] != spec.source_core_identity
        or source["adapter_identity_sha256"] != spec.source_adapter_identity
        or source["attempt_rows"] != 1
        or source["active_resources"] != 0
        or len(effects) != 2
        or len(completed_feedback) != 1
        or len(planned_evolution) != 1
        or not isinstance(candidate, dict)
        or not isinstance(validation, dict)
        or not isinstance(attachment_receipt, dict)
        or not isinstance(public_task_id, str)
        or state.get("candidate_reexecuted") is not False
        or state.get("additional_candidate_model_calls") != 0
        or len(state.get("attachment_ids", [])) != 1
        or state.get("evolution_job_ids") != []
        or validation.get("artifact_valid") is not False
        or attachment_receipt.get("attachment_id") != state["attachment_ids"][0]
        or attachment_receipt.get("feedback_source") != "artifact_validator"
        or attachment_receipt.get("judge_calls") != 0
    ):
        raise ValueError(
            "validator-learning post-feedback source is not replaceable"
        )

    transition_id = attachment_receipt.get("successor_transition_id")
    if not isinstance(transition_id, str) or not transition_id:
        raise ValueError("validator-learning source lacks a successor transition")
    client = CoreControlV2Client(core_authority)
    try:
        listed = client.json(
            "GET",
            "/v2/internal/training-feedback/sessions/"
            + str(candidate["session_id"])
            + "/attachments",
        )
        attempt = client.json(
            "GET",
            f"/v2/internal/training-attempts/{candidate['core_task_id']}/"
            f"{candidate['core_attempt_id']}",
        )
        dataset = client.json(
            "GET", f"/v2/internal/training-feedback/datasets/{candidate['dataset_id']}"
        )
        project = client.json("GET", f"/v2/projects/{candidate['core_project_id']}")
        successor = client.json(
            "GET", f"/v2/internal/training-successors/{transition_id}"
        )
    finally:
        client.close()
    attachments = listed.get("attachments")
    exact = [
        item
        for item in attachments
        if isinstance(item, dict)
        and item.get("attachment_id") == attachment_receipt["attachment_id"]
    ] if isinstance(attachments, list) else []
    execution = attempt.get("execution_receipt", {})
    transition = successor.get("transition", {})
    transition_attempts = successor.get("attempts", [])
    if (
        len(exact) != 1
        or exact[0].get("status") != "sealed"
        or exact[0].get("session_id") != candidate.get("session_id")
        or exact[0].get("dataset_id") != candidate.get("dataset_id")
        or exact[0].get("dataset_revision") != candidate.get("dataset_revision")
        or exact[0].get("task_id") != dataset.get("task_id")
        or exact[0].get("task_scope_id") != public_task_id
        or exact[0].get("task_scope_id") == candidate.get("core_task_id")
        or execution.get("session_id") != candidate.get("session_id")
        or execution.get("terminal_status") != "COMPLETED"
        or execution.get("session_result_sha256") != candidate.get("session_result_id")
        or dataset.get("completed_dataset_id") != candidate.get("dataset_id")
        or dataset.get("completed_dataset_revision") != candidate.get("dataset_revision")
        or transition.get("state") != "failed"
        or transition.get("transition", {}).get("successor_transition_id")
        != transition_id
        or not isinstance(transition_attempts, list)
        or len(transition_attempts) < 2
        or any(item.get("state") != "failed" for item in transition_attempts)
        or successor.get("commit") is not None
        or successor.get("artifacts") != []
    ):
        raise ValueError(
            "validator-learning post-feedback Core authority is not closed"
        )
    active_head = project.get("active_project_head")
    if not isinstance(active_head, dict):
        raise TypeError("validator-learning replacement project head is absent")

    registry = _ValidatorLearningReplacementRegistry(
        supervisor_root / "validator-learning-authority"
    )
    registry.reserve(spec)
    ports = build_production_ports(config, target_root, core_authority=core_authority)
    baseline = ports.composite.register_baseline(
        project_id=candidate["core_project_id"], project_head=active_head
    )
    if baseline.get("composite_id") != "c000":
        raise ValueError("validator-learning replacement baseline failed")
    identity = SupervisorIdentity(
        protocol_sha256=_file_sha256(config.path),
        core_identity_sha256=str(
            config.require("source_identity.openevo_core_source_tree_sha256")
        ),
        adapter_identity_sha256=str(config.require("source_identity.adapter_tree_sha256")),
    )
    source_reference = dict(state["validator_failure_learning_source"])
    source_reference.update(
        {
            "post_feedback_replacement_source": spec.source_namespace,
            "post_feedback_replacement_state_sha256": spec.source_state_sha256,
            "superseded_attachment_id": attachment_receipt["attachment_id"],
            "superseded_attachment_scope_id": exact[0]["task_scope_id"],
            "candidate_reexecuted": False,
            "additional_candidate_model_calls": 0,
        }
    )
    receipt = {
        "schema_version": 1,
        "operation": "community_validator_learning_post_feedback_pre_evolution_replacement",
        "operation_id": spec.operation_id,
        "source": {
            "namespace": spec.source_namespace,
            "state_sha256": spec.source_state_sha256,
            "protocol_identity": spec.source_protocol_identity,
            "core_identity": spec.source_core_identity,
            "adapter_identity": spec.source_adapter_identity,
            "stage": source["stage"],
            "attachment_id": attachment_receipt["attachment_id"],
            "attachment_sha256": attachment_receipt["attachment_sha256"],
            "attachment_scope_id": exact[0]["task_scope_id"],
            "successor_transition_id": transition_id,
            "failed_transition_attempts": len(transition_attempts),
            "completed_evolution_jobs": 0,
            "reflector_model_calls": 0,
        },
        "replacement": {
            "namespace": spec.target_namespace,
            "protocol_identity": identity.protocol_sha256,
            "core_identity": identity.core_identity_sha256,
            "adapter_identity": identity.adapter_identity_sha256,
            "core_generation": core_authority.generation,
            "release_identity": core_authority.release_identity,
            "candidate_reexecuted": False,
            "additional_candidate_model_calls": 0,
        },
        "verification": {
            "source_tree_before_sha256": source_before,
            "session_sealed": True,
            "dataset_sealed": True,
            "source_attachment_sealed": True,
            "source_scope_mismatch_proven": True,
            "successor_commit_absent": True,
            "successor_artifacts_absent": True,
            "baseline_composite_sha256": baseline["composite_sha256"],
        },
    }
    receipt["content_sha256"] = canonical_sha256(receipt)
    target_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    source_receipt = target_root / "validator-learning-post-feedback-replacement.json"
    if source_receipt.exists():
        if json.loads(source_receipt.read_text(encoding="utf-8")) != receipt:
            raise ValueError("validator-learning replacement source receipt conflicts")
    else:
        atomic_write_json(source_receipt, receipt)
        os.chmod(source_receipt, 0o600)
    operations = ProductionTrainingOperations(
        config=config,
        experiment_run_id=spec.target_namespace,
        receipt_root=target_root / "operations",
        ports=ports,
    )
    supervisor = CommunityTrainingSupervisor(
        store=TrainingStateStore(target_root),
        experiment_id=spec.target_namespace,
        identity=identity,
        operations=operations,
        task_ids=(public_task_id,),
        validator_failure_policy=config.community_validator_failure_policy,
    )
    initialized = supervisor.initialize_validator_failure_learning(
        source_reference=source_reference,
        candidate=candidate,
        validation=validation,
        source_receipt_sha256=receipt["content_sha256"],
    )
    if _tree_sha256(source_root) != source_before:
        raise ValueError("validator-learning post-feedback source namespace changed")
    completed = {
        **receipt,
        "replacement": {
            **receipt["replacement"],
            "initial_state_sha256": initialized["_state_sha256"],
            "state_revision": initialized["_revision"],
        },
        "supervisor_verify": supervisor.verify(),
    }
    completed["content_sha256"] = canonical_sha256(
        {key: value for key, value in completed.items() if key != "content_sha256"}
    )
    completed_path = target_root / "validator-learning-post-feedback-initialization.json"
    if completed_path.exists():
        if json.loads(completed_path.read_text(encoding="utf-8")) != completed:
            raise ValueError("validator-learning replacement initialization conflicts")
    else:
        atomic_write_json(completed_path, completed)
        os.chmod(completed_path, 0o600)
    registry.complete(spec, completed)
    return completed


__all__ = [
    "ValidatorFailureLearningSpec",
    "ValidatorLearningPreEffectReplacementSpec",
    "initialize_validator_failure_learning",
    "replace_validator_learning_post_feedback_pre_evolution_namespace",
    "replace_validator_learning_pre_effect_namespace",
]
