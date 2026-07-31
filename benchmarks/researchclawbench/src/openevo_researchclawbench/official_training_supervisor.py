"""Durable, restart-safe supervisor for the frozen official 40-task run.

This state machine is deliberately disjoint from Community training.  It can
only be initialized from a Core-authoritative ``FINAL_FROZEN`` receipt and its
operation surface contains candidate, validator, and one unified scorer only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from .official_training_operations import (
    OFFICIAL_DISABLED_POLICY,
    OfficialTrainingOperations,
)
from .training_state_store import canonical_bytes, canonical_sha256

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TASK_ID = re.compile(r"^[A-Za-z][A-Za-z0-9]*_[0-9]{3}$")
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


class OfficialTrainingStage(StrEnum):
    INITIALIZED = "INITIALIZED"
    TASK_READY = "TASK_READY"
    CANDIDATE_RUNNING = "CANDIDATE_RUNNING"
    CANDIDATE_SEALED = "CANDIDATE_SEALED"
    VALIDATION_PENDING = "VALIDATION_PENDING"
    TASK_TERMINAL = "TASK_TERMINAL"
    NEXT_TASK_READY = "NEXT_TASK_READY"
    ALL_CANDIDATES_SEALED = "ALL_CANDIDATES_SEALED"
    SCORING_PENDING = "SCORING_PENDING"
    SCORING_RUNNING = "SCORING_RUNNING"
    COMPLETE = "COMPLETE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


_ALLOWED: dict[OfficialTrainingStage, frozenset[OfficialTrainingStage]] = {
    OfficialTrainingStage.INITIALIZED: frozenset({OfficialTrainingStage.TASK_READY}),
    OfficialTrainingStage.TASK_READY: frozenset(
        {
            OfficialTrainingStage.CANDIDATE_RUNNING,
            OfficialTrainingStage.BLOCKED,
            OfficialTrainingStage.FAILED,
        }
    ),
    OfficialTrainingStage.CANDIDATE_RUNNING: frozenset(
        {
            OfficialTrainingStage.CANDIDATE_SEALED,
            OfficialTrainingStage.BLOCKED,
            OfficialTrainingStage.FAILED,
        }
    ),
    OfficialTrainingStage.CANDIDATE_SEALED: frozenset(
        {OfficialTrainingStage.VALIDATION_PENDING}
    ),
    OfficialTrainingStage.VALIDATION_PENDING: frozenset(
        {
            OfficialTrainingStage.TASK_TERMINAL,
            OfficialTrainingStage.BLOCKED,
            OfficialTrainingStage.FAILED,
        }
    ),
    OfficialTrainingStage.TASK_TERMINAL: frozenset(
        {
            OfficialTrainingStage.NEXT_TASK_READY,
            OfficialTrainingStage.ALL_CANDIDATES_SEALED,
        }
    ),
    OfficialTrainingStage.NEXT_TASK_READY: frozenset(
        {OfficialTrainingStage.TASK_READY}
    ),
    OfficialTrainingStage.ALL_CANDIDATES_SEALED: frozenset(
        {OfficialTrainingStage.SCORING_PENDING}
    ),
    OfficialTrainingStage.SCORING_PENDING: frozenset(
        {OfficialTrainingStage.SCORING_RUNNING}
    ),
    OfficialTrainingStage.SCORING_RUNNING: frozenset(
        {
            OfficialTrainingStage.COMPLETE,
            OfficialTrainingStage.BLOCKED,
            OfficialTrainingStage.FAILED,
        }
    ),
    OfficialTrainingStage.COMPLETE: frozenset(),
    OfficialTrainingStage.BLOCKED: frozenset(),
    OfficialTrainingStage.FAILED: frozenset(),
}


def _require_transition(
    source: OfficialTrainingStage, target: OfficialTrainingStage
) -> None:
    if target not in _ALLOWED[source]:
        raise ValueError(
            f"invalid official training transition: {source.value}->{target.value}"
        )


@dataclass(frozen=True)
class OfficialTrainingIdentity:
    protocol_sha256: str
    core_identity_sha256: str
    adapter_identity_sha256: str

    def __post_init__(self) -> None:
        for value in (
            self.protocol_sha256,
            self.core_identity_sha256,
            self.adapter_identity_sha256,
        ):
            if _SHA256.fullmatch(value) is None:
                raise ValueError("official training identity contains an invalid digest")


@dataclass(frozen=True)
class OfficialFrozenRunPlan:
    task_ids: tuple[str, ...]
    split_config_path: str
    split_config_sha256: str
    source_state_sha256: str
    freeze_receipt_sha256: str
    freeze_receipt_id: str
    freeze_authority_sha256: str
    frozen_composite_id: str
    frozen_composite_sha256: str
    frozen_registry_artifacts: dict[str, dict[str, str]]
    core_project_id: str
    core_project_head_id: str
    core_project_head_manifest_sha256: str
    identity: OfficialTrainingIdentity

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": "official_frozen_run",
            "task_ids": list(self.task_ids),
            "split_config_path": self.split_config_path,
            "split_config_sha256": self.split_config_sha256,
            "source_state_sha256": self.source_state_sha256,
            "freeze_receipt_sha256": self.freeze_receipt_sha256,
            "freeze_receipt_id": self.freeze_receipt_id,
            "freeze_authority_sha256": self.freeze_authority_sha256,
            "frozen_composite_id": self.frozen_composite_id,
            "frozen_composite_sha256": self.frozen_composite_sha256,
            "frozen_registry_artifacts": self.frozen_registry_artifacts,
            "core_project_id": self.core_project_id,
            "core_project_head_id": self.core_project_head_id,
            "core_project_head_manifest_sha256": self.core_project_head_manifest_sha256,
            "protocol_sha256": self.identity.protocol_sha256,
            "core_identity_sha256": self.identity.core_identity_sha256,
            "adapter_identity_sha256": self.identity.adapter_identity_sha256,
            "official_policy": dict(OFFICIAL_DISABLED_POLICY),
            "official_candidate_run_count": 40,
            "attempts_per_task": 1,
            "runs_per_attempt": 1,
            "pass_at_k": 1,
            "scoring_gate": "ALL_40_CANDIDATE_RUNS_SEALED",
        }

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.to_dict())


def load_official_frozen_plan(
    *,
    split_config: str | Path,
    expected_split_sha256: str,
    official_task_root: str | Path,
    final_state: dict[str, Any],
    identity: OfficialTrainingIdentity,
) -> OfficialFrozenRunPlan:
    """Build the official plan from the frozen public split and final receipt."""

    split_path = Path(os.path.abspath(split_config))
    task_root = Path(os.path.abspath(official_task_root))
    if (
        split_path.is_symlink()
        or not split_path.is_file()
        or task_root.is_symlink()
        or not task_root.is_dir()
    ):
        raise ValueError("official split or task root is unsafe")
    split_sha256 = _file_sha256(split_path)
    if (
        _SHA256.fullmatch(expected_split_sha256) is None
        or split_sha256 != expected_split_sha256
    ):
        raise ValueError("official split identity drifted")
    payload = yaml.safe_load(split_path.read_text(encoding="utf-8"))
    tasks = payload.get("official_test_tasks") if isinstance(payload, dict) else None
    if (
        not isinstance(tasks, list)
        or len(tasks) != 40
        or len(set(tasks)) != 40
        or any(not isinstance(task, str) or _TASK_ID.fullmatch(task) is None for task in tasks)
    ):
        raise ValueError("official split does not contain 40 distinct task IDs")
    for task_id in tasks:
        task_path = task_root / task_id
        if task_path.is_symlink() or not task_path.is_dir():
            raise ValueError(f"official task is unavailable: {task_id}")

    if final_state.get("stage") != "FINAL_FROZEN":
        raise ValueError("official run requires a FINAL_FROZEN source state")
    source_state_sha256 = final_state.get("_state_sha256")
    freeze = final_state.get("final_freeze_receipt")
    if (
        not isinstance(source_state_sha256, str)
        or _SHA256.fullmatch(source_state_sha256) is None
        or not isinstance(freeze, dict)
    ):
        raise ValueError("official run lacks an immutable final state authority")
    if (
        final_state.get("_protocol_sha256") != identity.protocol_sha256
        or final_state.get("_core_identity_sha256") != identity.core_identity_sha256
        or final_state.get("_adapter_identity_sha256") != identity.adapter_identity_sha256
    ):
        raise ValueError("official source state identity drifted")
    required_false = {
        "evolution_enabled",
        "reflector_enabled",
        "training_feedback_attachment_enabled",
        "teacher_enabled",
        "task_local_overlay_enabled",
        "cross_task_state_updates",
    }
    if (
        freeze.get("core_authoritative") is not True
        or freeze.get("frozen") is not True
        or any(freeze.get(field) is not False for field in required_false)
        or freeze.get("feedback_released", freeze.get("official_feedback_released"))
        is not False
    ):
        raise ValueError("official source is not a Core-authoritative frozen receipt")
    for key, expected in (
        ("protocol_sha256", identity.protocol_sha256),
        ("core_identity_sha256", identity.core_identity_sha256),
        ("adapter_identity_sha256", identity.adapter_identity_sha256),
    ):
        if freeze.get(key) != expected:
            raise ValueError(f"official freeze {key} drifted")
    string_fields = (
        "freeze_receipt_id",
        "composite_id",
        "core_project_id",
        "core_project_head_id",
    )
    if any(not isinstance(freeze.get(key), str) or not freeze[key] for key in string_fields):
        raise ValueError("official freeze lacks Core registry identity")
    digest_fields = (
        "authority_sha256",
        "composite_sha256",
        "core_project_head_manifest_sha256",
    )
    if any(
        not isinstance(freeze.get(key), str) or _SHA256.fullmatch(freeze[key]) is None
        for key in digest_fields
    ):
        raise ValueError("official freeze contains an invalid Core digest")
    artifacts: dict[str, dict[str, str]] = {}
    for artifact_type in ("agent_system", "text_memory", "skill_bundle"):
        artifact = freeze.get(artifact_type)
        if (
            not isinstance(artifact, dict)
            or not isinstance(artifact.get("registry_id"), str)
            or not artifact["registry_id"]
            or not isinstance(artifact.get("sha256"), str)
            or _SHA256.fullmatch(artifact["sha256"]) is None
            or artifact.get("frozen") is not True
        ):
            raise ValueError(f"official freeze lacks frozen {artifact_type} authority")
        artifacts[artifact_type] = {
            "registry_id": artifact["registry_id"],
            "sha256": artifact["sha256"],
        }
    freeze_sha256 = canonical_sha256(freeze)
    claimed_freeze_sha256 = final_state.get("final_freeze_receipt_sha256")
    if claimed_freeze_sha256 is not None and claimed_freeze_sha256 != freeze_sha256:
        raise ValueError("official freeze receipt hash drifted")
    return OfficialFrozenRunPlan(
        task_ids=tuple(tasks),
        split_config_path=os.fspath(split_path),
        split_config_sha256=split_sha256,
        source_state_sha256=source_state_sha256,
        freeze_receipt_sha256=freeze_sha256,
        freeze_receipt_id=freeze["freeze_receipt_id"],
        freeze_authority_sha256=freeze["authority_sha256"],
        frozen_composite_id=freeze["composite_id"],
        frozen_composite_sha256=freeze["composite_sha256"],
        frozen_registry_artifacts=artifacts,
        core_project_id=freeze["core_project_id"],
        core_project_head_id=freeze["core_project_head_id"],
        core_project_head_manifest_sha256=freeze[
            "core_project_head_manifest_sha256"
        ],
        identity=identity,
    )


class OfficialTrainingStateStore:
    """SQLite authority dedicated to one or more official frozen runs."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(os.path.abspath(root))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = os.stat(self.root, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise ValueError("official state root is not supervisor-owned")
        os.chmod(self.root, 0o700)
        self.db_path = self.root / "official-training-supervisor.sqlite3"
        descriptor = os.open(
            self.db_path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
        ):
            os.close(descriptor)
            raise ValueError("official state database is unsafe")
        os.close(descriptor)
        os.chmod(self.db_path, 0o600)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS official_state (
                    experiment_id TEXT PRIMARY KEY,
                    plan_sha256 TEXT NOT NULL,
                    protocol_sha256 TEXT NOT NULL,
                    core_identity_sha256 TEXT NOT NULL,
                    adapter_identity_sha256 TEXT NOT NULL,
                    freeze_receipt_sha256 TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    state_json BLOB NOT NULL,
                    state_sha256 TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS official_transitions (
                    idempotency_key TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    source_stage TEXT NOT NULL,
                    target_stage TEXT NOT NULL,
                    receipt_json BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(experiment_id) REFERENCES official_state(experiment_id)
                );
                CREATE TABLE IF NOT EXISTS official_side_effects (
                    idempotency_key TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('planned', 'completed')),
                    receipt_json BLOB,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(experiment_id) REFERENCES official_state(experiment_id)
                );
                CREATE TABLE IF NOT EXISTS official_task_runs (
                    experiment_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    task_index INTEGER NOT NULL,
                    receipt_sha256 TEXT NOT NULL,
                    receipt_json BLOB NOT NULL,
                    PRIMARY KEY(experiment_id, task_id),
                    UNIQUE(experiment_id, task_index),
                    FOREIGN KEY(experiment_id) REFERENCES official_state(experiment_id)
                );
                CREATE TABLE IF NOT EXISTS official_owned_resources (
                    resource_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    identity_json BLOB NOT NULL,
                    identity_sha256 TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK(active IN (0, 1)),
                    FOREIGN KEY(experiment_id) REFERENCES official_state(experiment_id)
                );
                """
            )
            connection.commit()

    @staticmethod
    def _state_from_row(row: sqlite3.Row) -> dict[str, Any]:
        state = json.loads(row["state_json"])
        if (
            not isinstance(state, dict)
            or canonical_sha256(state) != row["state_sha256"]
            or state.get("stage") != row["stage"]
        ):
            raise ValueError("official supervisor state hash is invalid")
        return {
            **state,
            "_state_sha256": row["state_sha256"],
            "_revision": int(row["revision"]),
            "_plan_sha256": row["plan_sha256"],
            "_protocol_sha256": row["protocol_sha256"],
            "_core_identity_sha256": row["core_identity_sha256"],
            "_adapter_identity_sha256": row["adapter_identity_sha256"],
            "_freeze_receipt_sha256": row["freeze_receipt_sha256"],
        }

    def initialize_experiment(
        self,
        *,
        experiment_id: str,
        plan: OfficialFrozenRunPlan,
        initial_state: dict[str, Any],
    ) -> dict[str, Any]:
        state = {
            **initial_state,
            "experiment_id": experiment_id,
            "stage": OfficialTrainingStage.INITIALIZED.value,
            "updated_at": _now(),
            "last_transition": None,
        }
        payload = canonical_bytes(state)
        digest = canonical_sha256(state)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM official_state WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            if row is not None:
                connection.commit()
                observed = self._state_from_row(row)
                self.verify_identity(observed, plan)
                return observed
            connection.execute(
                "INSERT INTO official_state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (
                    experiment_id,
                    plan.sha256,
                    plan.identity.protocol_sha256,
                    plan.identity.core_identity_sha256,
                    plan.identity.adapter_identity_sha256,
                    plan.freeze_receipt_sha256,
                    OfficialTrainingStage.INITIALIZED.value,
                    payload,
                    digest,
                    state["updated_at"],
                ),
            )
            connection.commit()
        return self.load(experiment_id)

    def load(self, experiment_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM official_state WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
        if row is None:
            raise ValueError("unknown official training experiment")
        return self._state_from_row(row)

    @staticmethod
    def verify_identity(
        state: dict[str, Any], plan: OfficialFrozenRunPlan
    ) -> None:
        expected = (
            plan.sha256,
            plan.identity.protocol_sha256,
            plan.identity.core_identity_sha256,
            plan.identity.adapter_identity_sha256,
            plan.freeze_receipt_sha256,
        )
        observed = (
            state.get("_plan_sha256"),
            state.get("_protocol_sha256"),
            state.get("_core_identity_sha256"),
            state.get("_adapter_identity_sha256"),
            state.get("_freeze_receipt_sha256"),
        )
        if observed != expected:
            raise ValueError("official plan, source, or freeze identity drifted")

    def transition(
        self,
        *,
        experiment_id: str,
        idempotency_key: str,
        source: OfficialTrainingStage,
        target: OfficialTrainingStage,
        updates: dict[str, Any],
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        _require_transition(source, target)
        request = {
            "experiment_id": experiment_id,
            "source": source.value,
            "target": target.value,
            "updates": updates,
            "receipt": receipt,
        }
        request_sha256 = canonical_sha256(request)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT request_sha256 FROM official_transitions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if prior is not None:
                if prior["request_sha256"] != request_sha256:
                    raise ValueError("official transition idempotency key conflicts")
                connection.commit()
                return self.load(experiment_id)
            row = connection.execute(
                "SELECT * FROM official_state WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            if row is None or row["stage"] != source.value:
                raise ValueError("official transition source stage changed")
            state = json.loads(row["state_json"])
            state.update(json.loads(canonical_bytes(updates)))
            state["stage"] = target.value
            state["last_transition"] = idempotency_key
            state["updated_at"] = _now()
            state_sha256 = canonical_sha256(state)
            updated = connection.execute(
                "UPDATE official_state SET stage = ?, state_json = ?, state_sha256 = ?, "
                "revision = revision + 1, updated_at = ? WHERE experiment_id = ? "
                "AND revision = ?",
                (
                    target.value,
                    canonical_bytes(state),
                    state_sha256,
                    state["updated_at"],
                    experiment_id,
                    row["revision"],
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("official transition lost its state fence")
            closed_receipt = {
                **receipt,
                "idempotency_key": idempotency_key,
                "source_stage": source.value,
                "target_stage": target.value,
                "state_sha256": state_sha256,
            }
            connection.execute(
                "INSERT INTO official_transitions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    idempotency_key,
                    experiment_id,
                    request_sha256,
                    source.value,
                    target.value,
                    canonical_bytes(closed_receipt),
                    _now(),
                ),
            )
            connection.commit()
        return self.load(experiment_id)

    def plan_side_effect(
        self,
        *,
        experiment_id: str,
        idempotency_key: str,
        kind: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        request_sha256 = canonical_sha256(request)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM official_side_effects WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO official_side_effects VALUES (?, ?, ?, ?, 'planned', NULL, ?)",
                    (idempotency_key, experiment_id, kind, request_sha256, _now()),
                )
                connection.commit()
                return {"status": "planned", "request_sha256": request_sha256}
            if row["kind"] != kind or row["request_sha256"] != request_sha256:
                raise ValueError("official side-effect idempotency key conflicts")
            connection.commit()
            return {
                "status": row["status"],
                "request_sha256": row["request_sha256"],
                "receipt": (
                    None
                    if row["receipt_json"] is None
                    else json.loads(row["receipt_json"])
                ),
            }

    def complete_side_effect(
        self, *, idempotency_key: str, receipt: dict[str, Any]
    ) -> dict[str, Any]:
        payload = canonical_bytes(receipt)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, receipt_json FROM official_side_effects "
                "WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                raise ValueError("official side effect was not planned")
            if row["status"] == "completed":
                prior = json.loads(row["receipt_json"])
                if canonical_bytes(prior) != payload:
                    raise ValueError("official side-effect receipt conflicts")
                connection.commit()
                return prior
            connection.execute(
                "UPDATE official_side_effects SET status = 'completed', "
                "receipt_json = ?, updated_at = ? WHERE idempotency_key = ?",
                (payload, _now(), idempotency_key),
            )
            connection.commit()
        return receipt

    def side_effects(self, experiment_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM official_side_effects WHERE experiment_id = ? "
                "ORDER BY idempotency_key",
                (experiment_id,),
            ).fetchall()
        return [
            {
                "idempotency_key": row["idempotency_key"],
                "kind": row["kind"],
                "request_sha256": row["request_sha256"],
                "status": row["status"],
                "receipt": (
                    None
                    if row["receipt_json"] is None
                    else json.loads(row["receipt_json"])
                ),
            }
            for row in rows
        ]

    def record_task_run(
        self, experiment_id: str, manifest: dict[str, Any]
    ) -> None:
        payload = canonical_bytes(manifest)
        digest = hashlib.sha256(payload).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT receipt_sha256 FROM official_task_runs "
                "WHERE experiment_id = ? AND task_id = ?",
                (experiment_id, manifest["task_id"]),
            ).fetchone()
            if prior is None:
                connection.execute(
                    "INSERT INTO official_task_runs VALUES (?, ?, ?, ?, ?)",
                    (
                        experiment_id,
                        manifest["task_id"],
                        manifest["task_index"],
                        digest,
                        payload,
                    ),
                )
            elif prior["receipt_sha256"] != digest:
                raise ValueError("official task run receipt is immutable")
            connection.commit()

    def task_runs(self, experiment_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT receipt_json, receipt_sha256 FROM official_task_runs "
                "WHERE experiment_id = ? ORDER BY task_index",
                (experiment_id,),
            ).fetchall()
        result = []
        for row in rows:
            payload = bytes(row["receipt_json"])
            if hashlib.sha256(payload).hexdigest() != row["receipt_sha256"]:
                raise ValueError("official task run receipt hash is invalid")
            result.append(json.loads(payload))
        return result

    def register_owned_resources(
        self, experiment_id: str, resources: object
    ) -> None:
        if resources is None:
            return
        if not isinstance(resources, list):
            raise TypeError("official operation owned resources are invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in resources:
                if (
                    not isinstance(item, dict)
                    or not isinstance(item.get("resource_id"), str)
                    or not isinstance(item.get("kind"), str)
                    or not isinstance(item.get("identity"), dict)
                    or type(item.get("active")) is not bool
                ):
                    raise ValueError("official operation returned an untyped resource")
                identity = canonical_bytes(item["identity"])
                digest = hashlib.sha256(identity).hexdigest()
                prior = connection.execute(
                    "SELECT identity_sha256 FROM official_owned_resources "
                    "WHERE resource_id = ?",
                    (item["resource_id"],),
                ).fetchone()
                if prior is None:
                    connection.execute(
                        "INSERT INTO official_owned_resources VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            item["resource_id"],
                            experiment_id,
                            item["kind"],
                            identity,
                            digest,
                            1 if item["active"] else 0,
                        ),
                    )
                elif prior["identity_sha256"] != digest:
                    raise ValueError("official owned resource identity conflicts")
            connection.commit()

    def active_resources(self, experiment_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT resource_id, kind, identity_json FROM official_owned_resources "
                "WHERE experiment_id = ? AND active = 1 ORDER BY resource_id",
                (experiment_id,),
            ).fetchall()
        return [
            {
                "resource_id": row["resource_id"],
                "kind": row["kind"],
                "identity": json.loads(row["identity_json"]),
            }
            for row in rows
        ]


class OfficialTrainingSupervisor:
    def __init__(
        self,
        *,
        store: OfficialTrainingStateStore,
        experiment_id: str,
        plan: OfficialFrozenRunPlan,
        operations: OfficialTrainingOperations,
    ) -> None:
        if _RUN_ID.fullmatch(experiment_id) is None:
            raise ValueError("official experiment ID is unsafe")
        if len(plan.task_ids) != 40 or len(set(plan.task_ids)) != 40:
            raise ValueError("official supervisor requires 40 distinct tasks")
        self.store = store
        self.experiment_id = experiment_id
        self.plan = plan
        self.operations = operations

    def _initial_state(self) -> dict[str, Any]:
        return {
            "schema_version": "openevo.researchclawbench.official_training_state.v1",
            "namespace_type": "official_frozen_test",
            "plan_sha256": self.plan.sha256,
            "task_ids": list(self.plan.task_ids),
            "current_task_index": 0,
            "attempt_index": 0,
            "candidate_run_count": 0,
            "validator_terminal_count": 0,
            "candidate_session_ids": [],
            "completed_dataset_ids": [],
            "active_candidate_receipt": None,
            "task_manifests": [],
            "scorer_receipt": None,
            "failure_reason": None,
            "frozen_composite_id": self.plan.frozen_composite_id,
            "frozen_composite_sha256": self.plan.frozen_composite_sha256,
            "freeze_receipt_sha256": self.plan.freeze_receipt_sha256,
            "freeze_receipt_id": self.plan.freeze_receipt_id,
            "freeze_authority_sha256": self.plan.freeze_authority_sha256,
            **OFFICIAL_DISABLED_POLICY,
        }

    def initialize(self) -> dict[str, Any]:
        self.store.initialize_experiment(
            experiment_id=self.experiment_id,
            plan=self.plan,
            initial_state=self._initial_state(),
        )
        return self.status()

    def status(self) -> dict[str, Any]:
        state = self.store.load(self.experiment_id)
        self.store.verify_identity(state, self.plan)
        return state

    def _key(self, suffix: str) -> str:
        return f"{self.experiment_id}:official:{suffix}"

    def _transition(
        self,
        source: OfficialTrainingStage,
        target: OfficialTrainingStage,
        suffix: str,
        *,
        updates: dict[str, Any] | None = None,
        receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.store.transition(
            experiment_id=self.experiment_id,
            idempotency_key=self._key(suffix),
            source=source,
            target=target,
            updates=updates or {},
            receipt=receipt or {"transition": suffix},
        )

    def _effect(
        self,
        *,
        kind: str,
        suffix: str,
        request: dict[str, Any],
        execute: Callable[[dict[str, Any], str], dict[str, Any]],
    ) -> dict[str, Any]:
        key = self._key(suffix)
        planned = self.store.plan_side_effect(
            experiment_id=self.experiment_id,
            idempotency_key=key,
            kind=kind,
            request=request,
        )
        if planned["status"] == "completed":
            receipt = planned["receipt"]
            if not isinstance(receipt, dict):
                raise ValueError("official completed side effect lacks a receipt")
            return receipt
        receipt = execute(request, key)
        if not isinstance(receipt, dict):
            raise TypeError("official operation returned a non-object receipt")
        self.store.register_owned_resources(
            self.experiment_id, receipt.get("owned_resources")
        )
        return self.store.complete_side_effect(
            idempotency_key=key,
            receipt=json.loads(canonical_bytes(receipt)),
        )

    def _candidate_request(self, state: dict[str, Any]) -> dict[str, Any]:
        index = int(state["current_task_index"])
        task_id = self.plan.task_ids[index]
        run_suffix = hashlib.sha256(self.experiment_id.encode()).hexdigest()[:12]
        return {
            "experiment_id": self.experiment_id,
            "task_id": task_id,
            "task_index": index,
            "attempt_index": 0,
            "run_id": f"{task_id}_a0_official_{run_suffix}",
            "fresh_workspace": True,
            "resume_in_place": False,
            "frozen_protocol_sha256": self.plan.identity.protocol_sha256,
            "freeze_receipt_sha256": self.plan.freeze_receipt_sha256,
            "freeze_receipt_id": self.plan.freeze_receipt_id,
            "freeze_authority_sha256": self.plan.freeze_authority_sha256,
            "frozen_composite_id": self.plan.frozen_composite_id,
            "frozen_composite_sha256": self.plan.frozen_composite_sha256,
            "frozen_registry_artifacts": self.plan.frozen_registry_artifacts,
            "core_project_id": self.plan.core_project_id,
            "core_project_head_id": self.plan.core_project_head_id,
            "core_project_head_manifest_sha256": self.plan.core_project_head_manifest_sha256,
            "official_policy": dict(OFFICIAL_DISABLED_POLICY),
        }

    def run_next(self) -> dict[str, Any]:
        state = self.status()
        stage = OfficialTrainingStage(state["stage"])
        if stage in {
            OfficialTrainingStage.COMPLETE,
            OfficialTrainingStage.BLOCKED,
            OfficialTrainingStage.FAILED,
        }:
            raise RuntimeError(f"official supervisor is terminal at {stage.value}")
        if stage is OfficialTrainingStage.INITIALIZED:
            return self._transition(stage, OfficialTrainingStage.TASK_READY, "task-0-ready")
        if stage is OfficialTrainingStage.TASK_READY:
            request = self._candidate_request(state)
            readiness = self.operations.candidate_readiness(request)
            if readiness.get("model_started") is not False:
                raise ValueError("official pre-intent readiness started a model")
            return self._transition(
                stage,
                OfficialTrainingStage.CANDIDATE_RUNNING,
                f"task-{state['current_task_index']}-candidate-intent",
                receipt={
                    "operation": "official_candidate_intent",
                    "task_id": request["task_id"],
                    "run_id": request["run_id"],
                    "preflight_sha256": canonical_sha256(readiness),
                    "official_policy": dict(OFFICIAL_DISABLED_POLICY),
                },
            )
        if stage is OfficialTrainingStage.CANDIDATE_RUNNING:
            request = self._candidate_request(state)
            candidate = self._effect(
                kind="official-candidate",
                suffix=f"task-{state['current_task_index']}-candidate",
                request=request,
                execute=self.operations.run_candidate,
            )
            if (
                type(candidate.get("completed")) is not bool
                or candidate.get("sealed") is not True
                or candidate.get("task_id") != request["task_id"]
                or candidate.get("run_id") != request["run_id"]
                or candidate.get("frozen_composite_sha256")
                != self.plan.frozen_composite_sha256
            ):
                raise ValueError("official candidate receipt is incomplete")
            return self._transition(
                stage,
                OfficialTrainingStage.CANDIDATE_SEALED,
                f"task-{state['current_task_index']}-candidate-sealed",
                updates={
                    "candidate_run_count": state["candidate_run_count"] + 1,
                    "candidate_session_ids": [
                        *state["candidate_session_ids"],
                        candidate["session_id"],
                    ],
                    "completed_dataset_ids": [
                        *state["completed_dataset_ids"],
                        candidate["dataset_id"],
                    ],
                    "active_candidate_receipt": candidate,
                },
                receipt=candidate,
            )
        if stage is OfficialTrainingStage.CANDIDATE_SEALED:
            return self._transition(
                stage,
                OfficialTrainingStage.VALIDATION_PENDING,
                f"task-{state['current_task_index']}-validation-pending",
            )
        if stage is OfficialTrainingStage.VALIDATION_PENDING:
            candidate = state["active_candidate_receipt"]
            request = {
                **self._candidate_request(state),
                "candidate": candidate,
            }
            validation = self._effect(
                kind="official-validation",
                suffix=f"task-{state['current_task_index']}-validation",
                request=request,
                execute=self.operations.validate_candidate,
            )
            task_id = self.plan.task_ids[state["current_task_index"]]
            manifest = {
                "task_id": task_id,
                "task_index": state["current_task_index"],
                "attempt_index": 0,
                "run_id": candidate["run_id"],
                "session_id": candidate["session_id"],
                "dataset_id": candidate["dataset_id"],
                "dataset_revision": candidate["dataset_revision"],
                "candidate_output_root": candidate["candidate_output_root"],
                "runtime_seconds": candidate["runtime_seconds"],
                "sealed": True,
                "completed": candidate["completed"],
                "artifact_valid": validation["artifact_valid"],
                "artifact_root_sha256": validation["artifact_root_sha256"],
                "validator_receipt_id": validation["validator_receipt_id"],
                "validator_receipt_sha256": validation[
                    "validator_receipt_sha256"
                ],
                "task_terminal_status": (
                    "CANDIDATE_FAILED_TERMINAL"
                    if candidate["completed"] is False
                    else (
                        "SEALED_VALID"
                        if validation["artifact_valid"]
                        else "VALIDATOR_FAILED_TERMINAL"
                    )
                ),
                "frozen_protocol_sha256": self.plan.identity.protocol_sha256,
                "freeze_receipt_sha256": self.plan.freeze_receipt_sha256,
                "frozen_composite_id": self.plan.frozen_composite_id,
                "frozen_composite_sha256": self.plan.frozen_composite_sha256,
                **OFFICIAL_DISABLED_POLICY,
            }
            self.store.record_task_run(self.experiment_id, manifest)
            manifests = self.store.task_runs(self.experiment_id)
            return self._transition(
                stage,
                OfficialTrainingStage.TASK_TERMINAL,
                f"task-{state['current_task_index']}-terminal",
                updates={
                    "validator_terminal_count": state["validator_terminal_count"] + 1,
                    "task_manifests": manifests,
                    "active_candidate_receipt": None,
                },
                receipt={
                    "operation": "official_task_terminal",
                    "manifest_sha256": canonical_sha256(manifest),
                    "task_terminal_status": manifest["task_terminal_status"],
                    "judge_invoked": False,
                    "same_attempt_retry_allowed": False,
                },
            )
        if stage is OfficialTrainingStage.TASK_TERMINAL:
            if state["current_task_index"] + 1 == len(self.plan.task_ids):
                self._require_all_task_runs(state)
                return self._transition(
                    stage,
                    OfficialTrainingStage.ALL_CANDIDATES_SEALED,
                    "all-40-candidates-sealed",
                    receipt={
                        "operation": "official_all_candidates_sealed",
                        "run_set_sha256": canonical_sha256(state["task_manifests"]),
                        "official_task_count": 40,
                    },
                )
            return self._transition(
                stage,
                OfficialTrainingStage.NEXT_TASK_READY,
                f"task-{state['current_task_index']}-next-ready",
                updates={"current_task_index": state["current_task_index"] + 1},
            )
        if stage is OfficialTrainingStage.NEXT_TASK_READY:
            return self._transition(
                stage,
                OfficialTrainingStage.TASK_READY,
                f"task-{state['current_task_index']}-ready",
            )
        if stage is OfficialTrainingStage.ALL_CANDIDATES_SEALED:
            self._require_all_task_runs(state)
            return self._transition(
                stage,
                OfficialTrainingStage.SCORING_PENDING,
                "unified-scoring-pending",
            )
        if stage is OfficialTrainingStage.SCORING_PENDING:
            self._require_all_task_runs(state)
            return self._transition(
                stage,
                OfficialTrainingStage.SCORING_RUNNING,
                "unified-scorer-intent",
                receipt={
                    "operation": "official_unified_scorer_intent",
                    "run_set_sha256": canonical_sha256(state["task_manifests"]),
                    "scorer_call_count_authorized": 1,
                    "feedback_released": False,
                },
            )
        if stage is OfficialTrainingStage.SCORING_RUNNING:
            self._require_all_task_runs(state)
            request = {
                "experiment_id": self.experiment_id,
                "run_manifests": state["task_manifests"],
                "frozen_protocol_sha256": self.plan.identity.protocol_sha256,
                "freeze_receipt_sha256": self.plan.freeze_receipt_sha256,
                "frozen_composite_id": self.plan.frozen_composite_id,
                "frozen_composite_sha256": self.plan.frozen_composite_sha256,
                "official_policy": dict(OFFICIAL_DISABLED_POLICY),
                "runs_per_attempt": 1,
                "pass_at_k": 1,
                "scoring_mode": "unified_after_all_40_sealed",
            }
            score = self._effect(
                kind="official-unified-scorer",
                suffix="unified-scorer",
                request=request,
                execute=self.operations.score_all,
            )
            return self._transition(
                stage,
                OfficialTrainingStage.COMPLETE,
                "official-run-complete",
                updates={"scorer_receipt": score},
                receipt=score,
            )
        raise ValueError(f"unhandled official stage: {stage.value}")

    def _require_all_task_runs(self, state: dict[str, Any]) -> None:
        manifests = self.store.task_runs(self.experiment_id)
        if (
            len(manifests) != 40
            or tuple(item.get("task_id") for item in manifests) != self.plan.task_ids
            or state.get("candidate_run_count") != 40
            or state.get("validator_terminal_count") != 40
            or len(state.get("candidate_session_ids", [])) != 40
            or len(set(state.get("candidate_session_ids", []))) != 40
            or len(state.get("completed_dataset_ids", [])) != 40
            or len(set(state.get("completed_dataset_ids", []))) != 40
            or canonical_sha256(manifests)
            != canonical_sha256(state.get("task_manifests"))
        ):
            raise RuntimeError("OFFICIAL_SCORER_WITHHELD_UNTIL_40_RUNS_SEALED")
        if any(
            item.get("task_index") != index
            or item.get("session_id") != state["candidate_session_ids"][index]
            or item.get("dataset_id") != state["completed_dataset_ids"][index]
            or item.get("sealed") is not True
            or item.get("frozen_composite_id") != self.plan.frozen_composite_id
            or item.get("frozen_composite_sha256")
            != self.plan.frozen_composite_sha256
            or any(item.get(key) is not expected for key, expected in OFFICIAL_DISABLED_POLICY.items())
            for index, item in enumerate(manifests)
        ):
            raise ValueError("official task run changed the frozen protocol")

    def verify(self) -> dict[str, Any]:
        state = self.status()
        manifests = self.store.task_runs(self.experiment_id)
        side_effects = self.store.side_effects(self.experiment_id)
        completed_candidates = sum(
            1 for item in side_effects if item["kind"] == "official-candidate" and item["status"] == "completed"
        )
        completed_validators = sum(
            1 for item in side_effects if item["kind"] == "official-validation" and item["status"] == "completed"
        )
        scorer_effects = [
            item for item in side_effects if item["kind"] == "official-unified-scorer"
        ]
        planned_effects = [item for item in side_effects if item["status"] == "planned"]
        if (
            state["task_ids"] != list(self.plan.task_ids)
            or state["plan_sha256"] != self.plan.sha256
            or state["frozen_composite_id"] != self.plan.frozen_composite_id
            or state["frozen_composite_sha256"]
            != self.plan.frozen_composite_sha256
            or any(state[key] is not expected for key, expected in OFFICIAL_DISABLED_POLICY.items())
            or len(manifests) != state["validator_terminal_count"]
            or completed_candidates != state["candidate_run_count"]
            or completed_validators != state["validator_terminal_count"]
            or len(set(state["candidate_session_ids"]))
            != len(state["candidate_session_ids"])
            or len(set(state["completed_dataset_ids"]))
            != len(state["completed_dataset_ids"])
            or len(scorer_effects) > 1
            or len(planned_effects) > 1
        ):
            raise ValueError("official supervisor consistency verification failed")
        stage = OfficialTrainingStage(state["stage"])
        if stage in {
            OfficialTrainingStage.ALL_CANDIDATES_SEALED,
            OfficialTrainingStage.SCORING_PENDING,
            OfficialTrainingStage.SCORING_RUNNING,
            OfficialTrainingStage.COMPLETE,
        }:
            self._require_all_task_runs(state)
        if stage is OfficialTrainingStage.COMPLETE:
            if (
                len(scorer_effects) != 1
                or state.get("scorer_receipt") is None
                or planned_effects
            ):
                raise ValueError("official completion lacks exactly one scorer receipt")
        elif scorer_effects and stage is not OfficialTrainingStage.SCORING_RUNNING:
            raise ValueError("official scorer was invoked before the scoring gate")
        allowed_planned_kind = {
            OfficialTrainingStage.CANDIDATE_RUNNING: "official-candidate",
            OfficialTrainingStage.VALIDATION_PENDING: "official-validation",
            OfficialTrainingStage.SCORING_RUNNING: "official-unified-scorer",
        }.get(stage)
        if planned_effects and planned_effects[0]["kind"] != allowed_planned_kind:
            raise ValueError("official planned side effect does not match the state")
        active = self.store.active_resources(self.experiment_id)
        if stage is OfficialTrainingStage.COMPLETE and active:
            raise ValueError("official completion has active owned resources")
        return {
            "status": "PASS",
            "stage": stage.value,
            "state_sha256": state["_state_sha256"],
            "state_revision": state["_revision"],
            "plan_sha256": self.plan.sha256,
            "freeze_receipt_sha256": self.plan.freeze_receipt_sha256,
            "official_tasks_expected": 40,
            "candidate_runs_completed": state["candidate_run_count"],
            "validator_terminals_completed": state["validator_terminal_count"],
            "task_manifests": len(manifests),
            "scorer_operation_count": len(scorer_effects),
            "active_owned_resources": active,
            "feedback_released": False,
            "evolution_operations_available": False,
        }

    def resume(self) -> dict[str, Any]:
        return self.run_next()

    def run_until_complete(self, *, max_transitions: int = 1000) -> dict[str, Any]:
        for _ in range(max_transitions):
            state = self.status()
            if state["stage"] in {
                OfficialTrainingStage.COMPLETE.value,
                OfficialTrainingStage.BLOCKED.value,
                OfficialTrainingStage.FAILED.value,
            }:
                return state
            self.run_next()
        raise ValueError("official transition budget was exhausted")


__all__ = [
    "OfficialFrozenRunPlan",
    "OfficialTrainingIdentity",
    "OfficialTrainingStage",
    "OfficialTrainingStateStore",
    "OfficialTrainingSupervisor",
    "load_official_frozen_plan",
]
