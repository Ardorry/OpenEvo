"""Crash-safe SQLite authority for the Community training supervisor."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .transition_engine import TrainingStage, require_transition


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TrainingStateStore:
    """Single-experiment durable state with fenced idempotent transitions."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(os.path.abspath(root))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = os.stat(self.root, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise ValueError("training state root is not supervisor-owned")
        os.chmod(self.root, 0o700)
        self.receipts_root = self.root / "receipts"
        self.receipts_root.mkdir(mode=0o700, exist_ok=True)
        receipt_metadata = os.stat(self.receipts_root, follow_symlinks=False)
        if (
            not stat.S_ISDIR(receipt_metadata.st_mode)
            or receipt_metadata.st_uid != os.geteuid()
        ):
            raise ValueError("training receipt root is not supervisor-owned")
        os.chmod(self.receipts_root, 0o700)
        self.db_path = self.root / "training-supervisor.sqlite3"
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
            raise ValueError("training state database is not supervisor-owned")
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
                CREATE TABLE IF NOT EXISTS experiment_state (
                    experiment_id TEXT PRIMARY KEY,
                    protocol_sha256 TEXT NOT NULL,
                    core_identity_sha256 TEXT NOT NULL,
                    adapter_identity_sha256 TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    state_json BLOB NOT NULL,
                    state_sha256 TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision >= 1),
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transitions (
                    idempotency_key TEXT PRIMARY KEY,
                    request_sha256 TEXT NOT NULL,
                    experiment_id TEXT NOT NULL,
                    source_stage TEXT NOT NULL,
                    target_stage TEXT NOT NULL,
                    state_sha256 TEXT NOT NULL,
                    receipt_json BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(experiment_id) REFERENCES experiment_state(experiment_id)
                );
                CREATE TABLE IF NOT EXISTS side_effects (
                    idempotency_key TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('planned', 'completed', 'failed')),
                    receipt_json BLOB,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(experiment_id) REFERENCES experiment_state(experiment_id)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    experiment_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    attempt_index INTEGER NOT NULL,
                    receipt_sha256 TEXT NOT NULL,
                    receipt_json BLOB NOT NULL,
                    PRIMARY KEY(experiment_id, task_id, attempt_index),
                    FOREIGN KEY(experiment_id) REFERENCES experiment_state(experiment_id)
                );
                CREATE TABLE IF NOT EXISTS owned_resources (
                    resource_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    identity_json BLOB NOT NULL,
                    identity_sha256 TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK(active IN (0, 1)),
                    created_at TEXT NOT NULL,
                    released_at TEXT,
                    FOREIGN KEY(experiment_id) REFERENCES experiment_state(experiment_id)
                );
                CREATE TABLE IF NOT EXISTS budget_reservations (
                    idempotency_key TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    category TEXT NOT NULL CHECK(category IN (
                        'candidate_model_calls',
                        'reflector_model_calls',
                        'judge_operations'
                    )),
                    units INTEGER NOT NULL CHECK(units > 0),
                    limit_units INTEGER NOT NULL CHECK(limit_units > 0),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(experiment_id) REFERENCES experiment_state(experiment_id)
                );
                """
            )
            connection.commit()

    def initialize_experiment(
        self,
        *,
        experiment_id: str,
        protocol_sha256: str,
        core_identity_sha256: str,
        adapter_identity_sha256: str,
        initial_state: dict[str, Any],
    ) -> dict[str, Any]:
        state = dict(initial_state)
        state["experiment_id"] = experiment_id
        state["stage"] = TrainingStage.INITIALIZED.value
        state["last_transition"] = None
        state["updated_at"] = _now()
        digest = canonical_sha256(state)
        payload = canonical_bytes(state)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM experiment_state WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            if row is not None:
                connection.commit()
                observed = self._state_from_row(row)
                self.verify_identity(
                    observed,
                    protocol_sha256=protocol_sha256,
                    core_identity_sha256=core_identity_sha256,
                    adapter_identity_sha256=adapter_identity_sha256,
                )
                return observed
            connection.execute(
                "INSERT INTO experiment_state(experiment_id, protocol_sha256, "
                "core_identity_sha256, adapter_identity_sha256, stage, state_json, "
                "state_sha256, revision, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (
                    experiment_id,
                    protocol_sha256,
                    core_identity_sha256,
                    adapter_identity_sha256,
                    TrainingStage.INITIALIZED.value,
                    payload,
                    digest,
                    state["updated_at"],
                ),
            )
            connection.commit()
        return state

    def load(self, experiment_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM experiment_state WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
        if row is None:
            raise ValueError("unknown training experiment")
        return self._state_from_row(row)

    @staticmethod
    def _state_from_row(row: sqlite3.Row) -> dict[str, Any]:
        state = json.loads(row["state_json"])
        if (
            not isinstance(state, dict)
            or canonical_sha256(state) != row["state_sha256"]
            or state.get("stage") != row["stage"]
        ):
            raise ValueError("training supervisor state hash is invalid")
        state["_state_sha256"] = row["state_sha256"]
        state["_revision"] = int(row["revision"])
        state["_protocol_sha256"] = row["protocol_sha256"]
        state["_core_identity_sha256"] = row["core_identity_sha256"]
        state["_adapter_identity_sha256"] = row["adapter_identity_sha256"]
        return state

    @staticmethod
    def verify_identity(
        state: dict[str, Any],
        *,
        protocol_sha256: str,
        core_identity_sha256: str,
        adapter_identity_sha256: str,
    ) -> None:
        expected = (
            protocol_sha256,
            core_identity_sha256,
            adapter_identity_sha256,
        )
        observed = (
            state.get("_protocol_sha256"),
            state.get("_core_identity_sha256"),
            state.get("_adapter_identity_sha256"),
        )
        if observed != expected:
            raise ValueError("training supervisor source or protocol identity drifted")

    def transition(
        self,
        *,
        experiment_id: str,
        idempotency_key: str,
        source: TrainingStage,
        target: TrainingStage,
        updates: dict[str, Any],
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        require_transition(source, target)
        request = {
            "experiment_id": experiment_id,
            "source": source.value,
            "target": target.value,
            "updates": updates,
            "receipt": receipt,
        }
        request_sha = canonical_sha256(request)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT request_sha256, state_sha256, receipt_json FROM transitions "
                "WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if prior is not None:
                if prior["request_sha256"] != request_sha:
                    raise ValueError("transition idempotency key conflicts")
                prior_receipt = json.loads(prior["receipt_json"])
                connection.commit()
                self._publish_receipt(idempotency_key, prior_receipt)
                return self.load(experiment_id)
            row = connection.execute(
                "SELECT * FROM experiment_state WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            if row is None or row["stage"] != source.value:
                raise ValueError("training transition source stage changed")
            state = json.loads(row["state_json"])
            state.update(json.loads(canonical_bytes(updates)))
            state["stage"] = target.value
            state["last_transition"] = idempotency_key
            state["updated_at"] = _now()
            state_sha = canonical_sha256(state)
            updated = connection.execute(
                "UPDATE experiment_state SET stage = ?, state_json = ?, state_sha256 = ?, "
                "revision = revision + 1, updated_at = ? WHERE experiment_id = ? "
                "AND revision = ?",
                (
                    target.value,
                    canonical_bytes(state),
                    state_sha,
                    state["updated_at"],
                    experiment_id,
                    row["revision"],
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("training transition lost its state fence")
            closed_receipt = {
                **receipt,
                "idempotency_key": idempotency_key,
                "source_stage": source.value,
                "target_stage": target.value,
                "state_sha256": state_sha,
            }
            connection.execute(
                "INSERT INTO transitions(idempotency_key, request_sha256, experiment_id, "
                "source_stage, target_stage, state_sha256, receipt_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    idempotency_key,
                    request_sha,
                    experiment_id,
                    source.value,
                    target.value,
                    state_sha,
                    canonical_bytes(closed_receipt),
                    _now(),
                ),
            )
            connection.commit()
        self._publish_receipt(idempotency_key, closed_receipt)
        return self.load(experiment_id)

    def plan_side_effect(
        self,
        *,
        experiment_id: str,
        idempotency_key: str,
        kind: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        digest = canonical_sha256(request)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM side_effects WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO side_effects(idempotency_key, experiment_id, kind, "
                    "request_sha256, status, receipt_json, updated_at) "
                    "VALUES (?, ?, ?, ?, 'planned', NULL, ?)",
                    (idempotency_key, experiment_id, kind, digest, _now()),
                )
                connection.commit()
                return {"status": "planned", "request_sha256": digest}
            if row["request_sha256"] != digest or row["kind"] != kind:
                raise ValueError("side-effect idempotency key conflicts")
            connection.commit()
            return {
                "status": row["status"],
                "request_sha256": digest,
                "receipt": None if row["receipt_json"] is None else json.loads(row["receipt_json"]),
            }

    def complete_side_effect(
        self,
        *,
        idempotency_key: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        payload = canonical_bytes(receipt)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, receipt_json FROM side_effects WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                raise ValueError("side effect was not planned")
            if row["status"] == "completed":
                existing = json.loads(row["receipt_json"])
                if canonical_bytes(existing) != payload:
                    raise ValueError("completed side-effect receipt conflicts")
                connection.commit()
                return existing
            if row["status"] != "planned":
                raise ValueError("failed side effect cannot be completed")
            connection.execute(
                "UPDATE side_effects SET status = 'completed', receipt_json = ?, "
                "updated_at = ? WHERE idempotency_key = ?",
                (payload, _now(), idempotency_key),
            )
            connection.commit()
        self._publish_receipt(f"side-effect-{idempotency_key}", receipt)
        return receipt

    def fail_side_effect(
        self,
        *,
        idempotency_key: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        """Seal a proven terminal side-effect failure exactly once.

        This is intentionally unavailable for ambiguous transport failures.  A
        caller must provide a typed receipt whose ``terminal_proven`` flag is
        true; otherwise the planned intent remains recoverable and fail-closed.
        """

        if receipt.get("terminal_proven") is not True:
            raise ValueError("side-effect failure is not proven terminal")
        payload = canonical_bytes(receipt)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, receipt_json FROM side_effects "
                "WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                raise ValueError("side effect was not planned")
            if row["status"] == "failed":
                existing = json.loads(row["receipt_json"])
                if canonical_bytes(existing) != payload:
                    raise ValueError("failed side-effect receipt conflicts")
                connection.commit()
                return existing
            if row["status"] != "planned":
                raise ValueError("completed side effect cannot be failed")
            connection.execute(
                "UPDATE side_effects SET status = 'failed', receipt_json = ?, "
                "updated_at = ? WHERE idempotency_key = ?",
                (payload, _now(), idempotency_key),
            )
            connection.commit()
        self._publish_receipt(f"side-effect-{idempotency_key}-failed", receipt)
        return receipt

    def side_effect(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM side_effects WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return {
            "idempotency_key": idempotency_key,
            "kind": row["kind"],
            "request_sha256": row["request_sha256"],
            "status": row["status"],
            "receipt": None if row["receipt_json"] is None else json.loads(row["receipt_json"]),
        }

    def side_effects_for_experiment(
        self, experiment_id: str
    ) -> list[dict[str, Any]]:
        """Return the closed side-effect ledger without changing its state."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT idempotency_key, kind, request_sha256, status, receipt_json "
                "FROM side_effects WHERE experiment_id = ? ORDER BY idempotency_key",
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

    def reserve_budget(
        self,
        *,
        experiment_id: str,
        idempotency_key: str,
        category: str,
        units: int,
        limit_units: int,
    ) -> dict[str, Any]:
        """Atomically reserve a closed model-operation budget.

        Reservations happen before a billed side-effect intent is persisted.
        Replaying the same idempotency key returns the original reservation;
        changing its category, units or limit fails closed.
        """

        if category not in {
            "candidate_model_calls",
            "reflector_model_calls",
            "judge_operations",
        }:
            raise ValueError("unknown training budget category")
        if type(units) is not int or type(limit_units) is not int:
            raise TypeError("training budget units must be integers")
        if units <= 0 or limit_units <= 0:
            raise ValueError("training budget units must be positive")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                "SELECT 1 FROM experiment_state WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            if state is None:
                raise ValueError("budget reservation has no experiment authority")
            prior = connection.execute(
                "SELECT experiment_id, category, units, limit_units "
                "FROM budget_reservations WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if prior is not None:
                if (
                    prior["experiment_id"] != experiment_id
                    or prior["category"] != category
                    or int(prior["units"]) != units
                    or int(prior["limit_units"]) != limit_units
                ):
                    raise ValueError("training budget idempotency key conflicts")
                used = connection.execute(
                    "SELECT COALESCE(SUM(units), 0) AS used "
                    "FROM budget_reservations WHERE experiment_id = ? AND category = ?",
                    (experiment_id, category),
                ).fetchone()["used"]
                connection.commit()
                return {
                    "category": category,
                    "reserved_units": units,
                    "used_units": int(used),
                    "limit_units": limit_units,
                    "idempotency_key": idempotency_key,
                    "recovered": True,
                }
            used = int(
                connection.execute(
                    "SELECT COALESCE(SUM(units), 0) AS used "
                    "FROM budget_reservations WHERE experiment_id = ? AND category = ?",
                    (experiment_id, category),
                ).fetchone()["used"]
            )
            if used + units > limit_units:
                connection.commit()
                raise ValueError(f"training budget exhausted: {category}")
            connection.execute(
                "INSERT INTO budget_reservations(idempotency_key, experiment_id, "
                "category, units, limit_units, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    idempotency_key,
                    experiment_id,
                    category,
                    units,
                    limit_units,
                    _now(),
                ),
            )
            connection.commit()
        return {
            "category": category,
            "reserved_units": units,
            "used_units": used + units,
            "limit_units": limit_units,
            "idempotency_key": idempotency_key,
            "recovered": False,
        }

    def budget_usage(self, experiment_id: str) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT category, COALESCE(SUM(units), 0) AS used "
                "FROM budget_reservations WHERE experiment_id = ? GROUP BY category",
                (experiment_id,),
            ).fetchall()
        observed = {row["category"]: int(row["used"]) for row in rows}
        return {
            category: observed.get(category, 0)
            for category in (
                "candidate_model_calls",
                "reflector_model_calls",
                "judge_operations",
            )
        }

    def transition_receipts_for_experiment(
        self, experiment_id: str
    ) -> list[dict[str, Any]]:
        """Return immutable transition receipts in creation order."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT receipt_json FROM transitions WHERE experiment_id = ? "
                "ORDER BY created_at, idempotency_key",
                (experiment_id,),
            ).fetchall()
        return [json.loads(row["receipt_json"]) for row in rows]

    def record_attempt(self, experiment_id: str, receipt: dict[str, Any]) -> None:
        task_id = str(receipt["task_id"])
        attempt_index = int(receipt["attempt_index"])
        payload = canonical_bytes(receipt)
        digest = hashlib.sha256(payload).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT receipt_sha256 FROM attempts WHERE experiment_id = ? "
                "AND task_id = ? AND attempt_index = ?",
                (experiment_id, task_id, attempt_index),
            ).fetchone()
            if prior is None:
                connection.execute(
                    "INSERT INTO attempts VALUES (?, ?, ?, ?, ?)",
                    (experiment_id, task_id, attempt_index, digest, payload),
                )
            elif prior["receipt_sha256"] != digest:
                raise ValueError("attempt receipt is immutable")
            connection.commit()

    def attempts_for_task(self, experiment_id: str, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT receipt_json, receipt_sha256 FROM attempts WHERE experiment_id = ? "
                "AND task_id = ? ORDER BY attempt_index",
                (experiment_id, task_id),
            ).fetchall()
        result = []
        for row in rows:
            payload = bytes(row["receipt_json"])
            if hashlib.sha256(payload).hexdigest() != row["receipt_sha256"]:
                raise ValueError("attempt receipt hash is invalid")
            result.append(json.loads(payload))
        return result

    def register_resource(
        self,
        *,
        experiment_id: str,
        resource_id: str,
        kind: str,
        identity: dict[str, Any],
    ) -> None:
        payload = canonical_bytes(identity)
        digest = hashlib.sha256(payload).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT identity_sha256, active FROM owned_resources WHERE resource_id = ?",
                (resource_id,),
            ).fetchone()
            if prior is None:
                connection.execute(
                    "INSERT INTO owned_resources VALUES (?, ?, ?, ?, ?, 1, ?, NULL)",
                    (resource_id, experiment_id, kind, payload, digest, _now()),
                )
            elif prior["identity_sha256"] != digest:
                raise ValueError("owned resource identity conflicts")
            connection.commit()

    def active_resources(self, experiment_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM owned_resources WHERE experiment_id = ? AND active = 1 "
                "ORDER BY resource_id",
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

    def release_resource(self, resource_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE owned_resources SET active = 0, released_at = ? "
                "WHERE resource_id = ? AND active = 1",
                (_now(), resource_id),
            )
            if updated.rowcount not in {0, 1}:
                raise ValueError("owned resource release lost its fence")
            connection.commit()

    def _publish_receipt(self, name: str, receipt: dict[str, Any]) -> None:
        safe = hashlib.sha256(name.encode("utf-8")).hexdigest()
        path = self.receipts_root / f"{safe}.json"
        payload = canonical_bytes(receipt)
        if path.exists():
            observed = path.read_bytes()
            if observed != payload:
                raise ValueError("transition receipt mirror conflicts")
            return
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
                    raise OSError("short transition receipt write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory = os.open(
            self.receipts_root,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY,
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


__all__ = ["TrainingStateStore", "canonical_bytes", "canonical_sha256"]
