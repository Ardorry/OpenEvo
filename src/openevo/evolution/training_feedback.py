"""Immutable post-run evaluator feedback for completed-task evolution.

The original ``openevo.session_completed`` event and its dataset stay immutable.
This module publishes a separately hashed evaluator attachment, then builds a
derived evolution-only dataset view containing only global feedback.  Hard
ground-truth details remain available exclusively through a same-task overlay.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from typing import Any, Final, Literal, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openevo.evolution.models import (
    ArtifactRegisterRequest,
    ArtifactResponse,
    ArtifactState,
    ArtifactType,
    DatasetCreateResponse,
)


_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_SOFT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "artifact_root_sha256",
        "artifact_valid",
        "completed",
        "cost_total_usd",
        "exit_code",
        "generic_failure_tags",
        "runtime_bucket_seconds",
        "total_score",
    }
)
_FORBIDDEN_FEEDBACK_FRAGMENTS: Final[tuple[str, ...]] = (
    "checklist",
    "judge_reason",
    "raw_judge",
    "raw_request",
    "raw_response",
    "rubric",
    "target_image",
    "target_paper",
)
_MAX_ATTACHMENT_BYTES: Final[int] = 4 * 1024 * 1024
_MAX_DATASET_VIEW_BYTES: Final[int] = 64 * 1024 * 1024


class FeedbackClass(StrEnum):
    HARD_GT = "HARD_GT"
    SOFT_JUDGE = "SOFT_JUDGE"
    MIXED = "MIXED"


class FeedbackAuthority(StrEnum):
    EVALUATOR_ONLY = "evaluator_only"


class SealedSessionEvidence(BaseModel):
    """Store-issued evidence binding one immutable completed session dataset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    session_id: str
    task_id: str
    dataset_id: str
    dataset_artifact_id: str
    session_result_sha256: str
    dataset_manifest_sha256: str
    completed: bool
    sealed: bool

    _ids = field_validator(
        "event_id", "session_id", "task_id", "dataset_id", "dataset_artifact_id"
    )(lambda value: _bounded_id(value))
    _hashes = field_validator("session_result_sha256", "dataset_manifest_sha256")(
        lambda value: _sha256(value)
    )

    @model_validator(mode="after")
    def _require_completed_sealed(self) -> "SealedSessionEvidence":
        if self.completed is not True or self.sealed is not True:
            raise ValueError("training feedback requires a completed and sealed session")
        return self


class TrainingFeedbackAttachment(BaseModel):
    """Canonical immutable evaluator supplement; hash excludes only itself."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "openevo.training_feedback_attachment.v2"
    attachment_id: str
    revision: int = Field(ge=1)
    session_id: str
    task_id: str
    task_scope_id: str
    dataset_id: str
    dataset_revision: str
    producer: str
    authority_id: str = "legacy_process_local"
    authority: FeedbackAuthority
    feedback_class: FeedbackClass
    global_feedback: dict[str, Any]
    task_local_feedback: dict[str, Any]
    created_at: str
    source_session_result_sha256: str
    source_dataset_manifest_sha256: str
    content_sha256: str
    status: Literal["sealed"] = "sealed"

    @property
    def completed_dataset_id(self) -> str:
        """Compatibility spelling used by the durable Core-control API."""

        return self.dataset_id

    @property
    def completed_dataset_revision(self) -> str:
        """Compatibility spelling used by the durable Core-control API."""

        return self.dataset_revision

    _ids = field_validator(
        "attachment_id",
        "session_id",
        "task_id",
        "task_scope_id",
        "dataset_id",
        "dataset_revision",
        "producer",
        "authority_id",
    )(lambda value: _bounded_id(value))
    _hashes = field_validator(
        "source_session_result_sha256",
        "source_dataset_manifest_sha256",
        "content_sha256",
    )(lambda value: _sha256(value))

    @model_validator(mode="after")
    def _validate_attachment(self) -> "TrainingFeedbackAttachment":
        _validate_feedback_layers(
            self.feedback_class,
            self.global_feedback,
            self.task_local_feedback,
        )
        _parse_utc(self.created_at)
        payload = self.model_dump(mode="json", exclude={"content_sha256"})
        if _canonical_sha256(payload) != self.content_sha256:
            raise ValueError("training feedback attachment content hash is invalid")
        return self


class TrainingFeedbackAttachmentCreateRequest(BaseModel):
    """Closed Core-control request; caller identity comes from service auth."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openevo.training_feedback_create.v1"] = (
        "openevo.training_feedback_create.v1"
    )
    idempotency_key: str
    session_id: str
    task_id: str
    task_scope_id: str
    completed_dataset_id: str
    completed_dataset_revision: str
    producer: str = "trusted_evaluator"
    feedback_class: FeedbackClass
    global_feedback: dict[str, Any]
    task_local_feedback: dict[str, Any]
    created_at: str | None = None

    _ids = field_validator(
        "idempotency_key",
        "session_id",
        "task_id",
        "task_scope_id",
        "completed_dataset_id",
        "completed_dataset_revision",
        "producer",
    )(lambda value: _bounded_id(value))

    @model_validator(mode="after")
    def _layers(self) -> "TrainingFeedbackAttachmentCreateRequest":
        _validate_feedback_layers(
            self.feedback_class,
            self.global_feedback,
            self.task_local_feedback,
        )
        if self.created_at is not None:
            _parse_utc(self.created_at)
        return self


class TrainingFeedbackAttachmentList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    attachments: tuple[TrainingFeedbackAttachment, ...]


def training_feedback_attachment_id_for_idempotency_key(
    idempotency_key: str,
) -> str:
    """Return the durable attachment identity used for idempotent recovery."""

    key = _bounded_id(idempotency_key)
    return f"tfa_{hashlib.sha256(key.encode()).hexdigest()[:32]}"


class EvolutionDatasetViewResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openevo.training_feedback_resolve.v1"] = (
        "openevo.training_feedback_resolve.v1"
    )
    completed_dataset_id: str
    completed_dataset_revision: str
    task_id: str
    task_scope_id: str
    attachment_ids: tuple[str, ...] = Field(min_length=1, max_length=32)

    _ids = field_validator(
        "completed_dataset_id",
        "completed_dataset_revision",
        "task_id",
        "task_scope_id",
    )(lambda value: _bounded_id(value))

    @field_validator("attachment_ids")
    @classmethod
    def _attachment_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        parsed = tuple(_bounded_id(item) for item in value)
        if parsed != tuple(sorted(parsed)) or len(parsed) != len(set(parsed)):
            raise ValueError("attachment IDs must be sorted and unique")
        return parsed


class EvolutionDatasetViewReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "openevo.training_feedback_dataset_view.v1"
    resolution_id: str = "legacy_resolution"
    source_dataset_id: str
    source_dataset_manifest_sha256: str
    source_session_result_sha256: str
    attachment_ids: tuple[str, ...]
    attachment_sha256: tuple[str, ...]
    records_sha256: str
    manifest_sha256: str
    task_local_overlay_sha256: str | None = None

    _ids = field_validator("resolution_id", "source_dataset_id")(
        lambda value: _bounded_id(value)
    )
    _hashes = field_validator(
        "source_dataset_manifest_sha256",
        "source_session_result_sha256",
        "records_sha256",
        "manifest_sha256",
    )(lambda value: _sha256(value))


class RegisteredTrainingFeedbackView(BaseModel):
    """Core receipt binding an attachment to its registered evolution dataset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attachment: TrainingFeedbackAttachment
    dataset_view: EvolutionDatasetViewReceipt
    dataset_artifact: ArtifactResponse

    @model_validator(mode="after")
    def _same_authority(self) -> "RegisteredTrainingFeedbackView":
        manifest = self.dataset_artifact.manifest
        if (
            self.dataset_artifact.type is not ArtifactType.DATASET
            or self.attachment.attachment_id
            not in manifest.get("training_feedback_attachment_ids", [])
            or self.dataset_view.manifest_sha256
            != _canonical_sha256(manifest)
        ):
            raise ValueError("registered feedback dataset authority is inconsistent")
        return self


class CompletedDatasetAuthority(BaseModel):
    """Read-only durable identity of one sealed completed-task dataset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openevo.completed_dataset_authority.v1"] = (
        "openevo.completed_dataset_authority.v1"
    )
    completed_dataset_id: str
    completed_dataset_revision: str
    dataset_artifact_id: str
    dataset_manifest_sha256: str
    task_id: str
    session_id: str
    source_event_id: str
    source_session_result_sha256: str

    _ids = field_validator(
        "completed_dataset_id",
        "completed_dataset_revision",
        "dataset_artifact_id",
        "task_id",
        "session_id",
    )(lambda value: _bounded_id(value))
    _source_event = field_validator("source_event_id")(
        lambda value: _bounded_event_id(value)
    )
    _hashes = field_validator(
        "dataset_manifest_sha256", "source_session_result_sha256"
    )(lambda value: _sha256(value))


class ResolvedEvolutionDatasetView(BaseModel):
    """Durable receipt used by evolution jobs without mutating source data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openevo.resolved_evolution_dataset_view.v1"] = (
        "openevo.resolved_evolution_dataset_view.v1"
    )
    completed_dataset_id: str
    completed_dataset_revision: str
    attachments: tuple[TrainingFeedbackAttachment, ...]
    dataset_view: EvolutionDatasetViewReceipt
    dataset_artifact: ArtifactResponse
    resolved_view_sha256: str

    _ids = field_validator("completed_dataset_id", "completed_dataset_revision")(
        lambda value: _bounded_id(value)
    )
    _hash = field_validator("resolved_view_sha256")(lambda value: _sha256(value))

    @model_validator(mode="after")
    def _consistent(self) -> "ResolvedEvolutionDatasetView":
        expected = _canonical_sha256(
            {
                "completed_dataset_id": self.completed_dataset_id,
                "completed_dataset_revision": self.completed_dataset_revision,
                "attachment_ids": [item.attachment_id for item in self.attachments],
                "attachment_sha256": [item.content_sha256 for item in self.attachments],
                "dataset_view": self.dataset_view.model_dump(mode="json"),
                "dataset_artifact_id": self.dataset_artifact.artifact_id,
            }
        )
        if expected != self.resolved_view_sha256:
            raise ValueError("resolved evolution dataset view hash is invalid")
        return self


class TrainingFeedbackRegistry(Protocol):
    """Narrow store surface used by the trusted Core feedback service."""

    def get_dataset(self, dataset_id: str) -> DatasetCreateResponse: ...

    def get_artifact(self, artifact_id: str) -> ArtifactResponse: ...

    def register_artifact(self, request: ArtifactRegisterRequest) -> ArtifactResponse: ...

    def get_dataset_artifact_by_create_identity(
        self, create_identity: str
    ) -> ArtifactResponse | None: ...


class _EvaluatorAuthorityCapability:
    """Process-local capability; never serialized into an adapter request."""

    __slots__ = ("_nonce", "authority_id", "producer")

    def __init__(self, producer: str, authority_id: str) -> None:
        self.producer = _bounded_id(producer)
        self.authority_id = _bounded_id(authority_id)
        self._nonce = secrets.token_bytes(32)


class TrainingFeedbackAttachmentStore:
    """SQLite-backed immutable store with a process-local authority capability.

    SQLite provides cross-process locking and crash recovery.  Canonical JSON
    mirrors are immutable audit receipts, not the source of state.  The only
    caller allowed to invoke :meth:`attach` holds the non-serializable Core
    capability issued by this store instance.
    """

    def __init__(self, root: Path, *, official_mode: bool = False) -> None:
        self.root = Path(os.path.abspath(root))
        self.official_mode = official_mode
        self._authority: _EvaluatorAuthorityCapability | None = None
        self._initialize_root()
        self._db_path = self.root / "training-feedback.sqlite3"
        self._initialize_database()

    def issue_evaluator_authority(self, *, producer: str) -> object:
        if self.official_mode:
            raise ValueError("official frozen mode forbids training feedback authority")
        producer = _bounded_id(producer)
        if self._authority is not None:
            if self._authority.producer == producer:
                return self._authority
            raise ValueError("evaluator authority was already issued")
        authority_id = f"eva_{_canonical_sha256({'producer': producer, 'store': self._store_id()})[:32]}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT producer, active FROM evaluator_authorities WHERE authority_id = ?",
                (authority_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO evaluator_authorities(authority_id, producer, active, created_at) "
                    "VALUES (?, ?, 1, ?)",
                    (authority_id, producer, datetime.now(UTC).isoformat()),
                )
            elif existing != (producer, 1):
                raise ValueError("persisted evaluator authority is inconsistent")
            connection.commit()
        self._authority = _EvaluatorAuthorityCapability(producer, authority_id)
        return self._authority

    def attach(
        self,
        *,
        authority: object,
        session: SealedSessionEvidence,
        dataset_revision: str,
        feedback_class: FeedbackClass,
        global_feedback: Mapping[str, Any],
        task_local_feedback: Mapping[str, Any],
        task_scope_id: str | None = None,
        created_at: str | None = None,
        idempotency_key: str | None = None,
    ) -> TrainingFeedbackAttachment:
        if self.official_mode:
            raise ValueError("official frozen mode forbids training feedback attachments")
        if authority is not self._authority or not isinstance(
            authority, _EvaluatorAuthorityCapability
        ):
            raise PermissionError("trusted evaluator authority is required")
        session = SealedSessionEvidence.model_validate(session)
        dataset_revision = _bounded_id(dataset_revision)
        scope = _bounded_id(task_scope_id or session.task_id)
        feedback_class = FeedbackClass(feedback_class)
        global_copy = _json_copy(global_feedback)
        local_copy = _json_copy(task_local_feedback)
        _validate_feedback_layers(feedback_class, global_copy, local_copy)
        request_identity = {
            "session": session.model_dump(mode="json"),
            "dataset_revision": dataset_revision,
            "producer": authority.producer,
            "authority_id": authority.authority_id,
            "feedback_class": feedback_class.value,
            "global_feedback": global_copy,
            "task_local_feedback": local_copy,
            "task_scope_id": scope,
        }
        request_sha256 = _canonical_sha256(request_identity)
        idempotency_key = _bounded_id(
            idempotency_key or f"feedback_{request_sha256}"
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            authority_row = connection.execute(
                "SELECT producer, active FROM evaluator_authorities WHERE authority_id = ?",
                (authority.authority_id,),
            ).fetchone()
            if authority_row != (authority.producer, 1):
                raise PermissionError("trusted evaluator authority is invalid")
            existing = connection.execute(
                "SELECT request_sha256, payload_json FROM attachments "
                "WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing[0] != request_sha256:
                    raise ValueError("training feedback idempotency key conflicts")
                connection.commit()
                return self._validate_stored(existing[1])
            revision = int(
                connection.execute(
                    "SELECT COALESCE(MAX(revision), 0) + 1 FROM attachments "
                    "WHERE session_id = ?",
                    (session.session_id,),
                ).fetchone()[0]
            )
            attachment_id = training_feedback_attachment_id_for_idempotency_key(
                idempotency_key
            )
            mirror_path = self._attachment_path(attachment_id)
            mirror_exists = mirror_path.exists()
            if mirror_exists:
                attachment = self._validate_stored(
                    _read_private_regular(
                        mirror_path,
                        max_bytes=_MAX_ATTACHMENT_BYTES,
                    )
                )
                if (
                    attachment.attachment_id != attachment_id
                    or attachment.revision != revision
                    or attachment.session_id != session.session_id
                    or attachment.task_id != session.task_id
                    or attachment.task_scope_id != scope
                    or attachment.dataset_id != session.dataset_id
                    or attachment.dataset_revision != dataset_revision
                    or attachment.producer != authority.producer
                    or attachment.authority_id != authority.authority_id
                    or attachment.feedback_class is not feedback_class
                    or attachment.global_feedback != global_copy
                    or attachment.task_local_feedback != local_copy
                    or attachment.source_session_result_sha256
                    != session.session_result_sha256
                    or attachment.source_dataset_manifest_sha256
                    != session.dataset_manifest_sha256
                    or created_at is not None
                    and attachment.created_at != created_at
                ):
                    raise ValueError("orphan training feedback mirror conflicts")
            else:
                timestamp = created_at or datetime.now(UTC).isoformat()
                _parse_utc(timestamp)
                body: dict[str, Any] = {
                    "schema_version": "openevo.training_feedback_attachment.v2",
                    "attachment_id": attachment_id,
                    "revision": revision,
                    "session_id": session.session_id,
                    "task_id": session.task_id,
                    "task_scope_id": scope,
                    "dataset_id": session.dataset_id,
                    "dataset_revision": dataset_revision,
                    "producer": authority.producer,
                    "authority_id": authority.authority_id,
                    "authority": FeedbackAuthority.EVALUATOR_ONLY.value,
                    "feedback_class": feedback_class.value,
                    "global_feedback": global_copy,
                    "task_local_feedback": local_copy,
                    "created_at": timestamp,
                    "source_session_result_sha256": session.session_result_sha256,
                    "source_dataset_manifest_sha256": session.dataset_manifest_sha256,
                    "status": "sealed",
                }
                body["content_sha256"] = _canonical_sha256(body)
                attachment = TrainingFeedbackAttachment.model_validate(body)
            payload = _canonical_bytes(attachment.model_dump(mode="json"))
            if len(payload) > _MAX_ATTACHMENT_BYTES:
                raise ValueError("training feedback attachment exceeds its size limit")
            if not mirror_exists:
                self._publish(attachment)
            try:
                connection.execute(
                    "INSERT INTO attachments(attachment_id, idempotency_key, request_sha256, "
                    "session_id, task_id, dataset_id, dataset_revision, producer, authority_id, "
                    "revision, content_sha256, payload_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        attachment.attachment_id,
                        idempotency_key,
                        request_sha256,
                        attachment.session_id,
                        attachment.task_id,
                        attachment.dataset_id,
                        attachment.dataset_revision,
                        attachment.producer,
                        attachment.authority_id,
                        attachment.revision,
                        attachment.content_sha256,
                        payload,
                        attachment.created_at,
                    ),
                )
                connection.commit()
            except BaseException:
                # The immutable mirror may survive a crash before commit.  A
                # retry validates and adopts it only when its identity matches.
                connection.rollback()
                raise
        return attachment

    def get(self, attachment_id: str) -> TrainingFeedbackAttachment:
        attachment_id = _bounded_id(attachment_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM attachments WHERE attachment_id = ?",
                (attachment_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown training feedback attachment: {attachment_id}")
        attachment = self._validate_stored(row[0])
        mirror = self._validate_stored(
            _read_private_regular(
                self._attachment_path(attachment_id),
                max_bytes=_MAX_ATTACHMENT_BYTES,
            )
        )
        if mirror != attachment:
            raise ValueError("training feedback attachment mirror was tampered")
        self._validate_authority(attachment)
        return attachment

    def list_for_session(self, session_id: str) -> tuple[TrainingFeedbackAttachment, ...]:
        session_id = _bounded_id(session_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT attachment_id FROM attachments WHERE session_id = ? "
                "ORDER BY revision, attachment_id",
                (session_id,),
            ).fetchall()
        return tuple(self.get(str(row[0])) for row in rows)

    def task_local_overlay(
        self,
        attachment_id: str,
        *,
        next_task_scope_id: str,
    ) -> dict[str, Any]:
        attachment = self.get(attachment_id)
        if attachment.task_scope_id != next_task_scope_id:
            raise ValueError("task-local feedback cannot cross task boundaries")
        return _json_copy(attachment.task_local_feedback)

    def _initialize_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        opened = os.stat(self.root, follow_symlinks=False)
        if not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.geteuid():
            raise ValueError("training feedback root is not Core-owned")
        if stat.S_IMODE(opened.st_mode) & 0o077:
            os.chmod(self.root, 0o700)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=30.0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize_database(self) -> None:
        descriptor = os.open(
            self._db_path,
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
            raise ValueError("training feedback database is not a private regular file")
        os.close(descriptor)
        os.chmod(self._db_path, 0o600)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evaluator_authorities (
                    authority_id TEXT PRIMARY KEY,
                    producer TEXT NOT NULL UNIQUE,
                    active INTEGER NOT NULL CHECK(active IN (0, 1)),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attachments (
                    attachment_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_sha256 TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    dataset_revision TEXT NOT NULL,
                    producer TEXT NOT NULL,
                    authority_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision > 0),
                    content_sha256 TEXT NOT NULL UNIQUE,
                    payload_json BLOB NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, revision),
                    FOREIGN KEY(authority_id) REFERENCES evaluator_authorities(authority_id)
                );
                CREATE INDEX IF NOT EXISTS idx_feedback_session
                    ON attachments(session_id, revision);
                CREATE TABLE IF NOT EXISTS resolved_views (
                    resolution_id TEXT PRIMARY KEY,
                    request_sha256 TEXT NOT NULL,
                    receipt_json BLOB NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            if connection.execute(
                "SELECT value FROM metadata WHERE key = 'store_id'"
            ).fetchone() is None:
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES ('store_id', ?)",
                    (f"tfs_{secrets.token_hex(16)}",),
                )
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES "
                "('schema_version', 'openevo.training_feedback_store.v1')"
            )
            connection.commit()

    def _store_id(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'store_id'"
            ).fetchone()
        if row is None:
            raise ValueError("training feedback store identity is missing")
        return _bounded_id(row[0])

    @staticmethod
    def _validate_stored(payload: bytes | str) -> TrainingFeedbackAttachment:
        try:
            attachment = TrainingFeedbackAttachment.model_validate_json(payload)
        except ValueError as exc:
            raise ValueError("training feedback store contains invalid authority") from exc
        return attachment

    def _validate_authority(self, attachment: TrainingFeedbackAttachment) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT producer, active FROM evaluator_authorities "
                "WHERE authority_id = ?",
                (attachment.authority_id,),
            ).fetchone()
        if row != (attachment.producer, 1):
            raise PermissionError("training feedback authority is no longer valid")

    def get_resolution(self, resolution_id: str) -> ResolvedEvolutionDatasetView | None:
        resolution_id = _bounded_id(resolution_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT receipt_json FROM resolved_views WHERE resolution_id = ?",
                (resolution_id,),
            ).fetchone()
        if row is None:
            return None
        return ResolvedEvolutionDatasetView.model_validate_json(row[0])

    def put_resolution(
        self,
        *,
        resolution_id: str,
        request_sha256: str,
        receipt: ResolvedEvolutionDatasetView,
    ) -> ResolvedEvolutionDatasetView:
        resolution_id = _bounded_id(resolution_id)
        request_sha256 = _sha256(request_sha256)
        receipt = ResolvedEvolutionDatasetView.model_validate(receipt)
        if receipt.dataset_view.resolution_id != resolution_id:
            raise ValueError("resolved view identity is inconsistent")
        payload = _canonical_bytes(receipt.model_dump(mode="json"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT request_sha256, receipt_json FROM resolved_views "
                "WHERE resolution_id = ?",
                (resolution_id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != request_sha256:
                    raise ValueError("resolved view identity conflicts")
                connection.commit()
                observed = ResolvedEvolutionDatasetView.model_validate_json(existing[1])
                if observed != receipt:
                    raise ValueError("resolved view receipt conflicts")
                return observed
            connection.execute(
                "INSERT INTO resolved_views(resolution_id, request_sha256, receipt_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    resolution_id,
                    request_sha256,
                    payload,
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.commit()
        return receipt

    def _attachment_path(self, attachment_id: str) -> Path:
        return self.root / f"{attachment_id}.json"

    def _publish(self, attachment: TrainingFeedbackAttachment) -> None:
        path = self._attachment_path(attachment.attachment_id)
        payload = _canonical_bytes(attachment.model_dump(mode="json"))
        if len(payload) > _MAX_ATTACHMENT_BYTES:
            raise ValueError("training feedback attachment exceeds its size limit")
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
                    raise OSError("short training feedback mirror write")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(path.parent)


class TrainingFeedbackEvolutionService:
    """Trusted Core facade for attach, derive, and registry publication.

    The capability returned by :meth:`issue_evaluator_authority` is deliberately
    process-local to this Evolution service instance.  The attachments and
    resolved views it authorizes are durable.  Cross-process callers reach this
    facade through the generation-bound internal Evolution API; they cannot
    serialize, choose, or mint evaluator authority themselves.
    """

    def __init__(
        self,
        *,
        registry: TrainingFeedbackRegistry,
        root: Path,
        official_mode: bool = False,
    ) -> None:
        self._registry = registry
        self._root = Path(os.path.abspath(root))
        self._attachments = TrainingFeedbackAttachmentStore(
            self._root / "attachments",
            official_mode=official_mode,
        )
        self._views_root = self._root / "dataset_views"
        self._views_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._views_root, 0o700)
        self._official_mode = official_mode

    def issue_evaluator_authority(self, *, producer: str) -> object:
        return self._attachments.issue_evaluator_authority(producer=producer)

    def create_training_feedback_attachment(
        self,
        *,
        authority: object,
        request: TrainingFeedbackAttachmentCreateRequest,
    ) -> TrainingFeedbackAttachment:
        if self._official_mode:
            raise ValueError("official frozen mode forbids training feedback")
        request = TrainingFeedbackAttachmentCreateRequest.model_validate(request)
        dataset = self._registry.get_dataset(request.completed_dataset_id)
        source_artifact = self._registry.get_artifact(dataset.artifact_id)
        expected_revision = f"{source_artifact.artifact_id}.v{source_artifact.version}"
        if (
            source_artifact.type is not ArtifactType.DATASET
            or request.completed_dataset_revision != expected_revision
        ):
            raise ValueError("completed dataset revision does not match durable authority")
        source_manifest_path = _local_file_uri(source_artifact.uri)
        session = _sealed_session_from_source(
            source_manifest_path,
            dataset=dataset,
            session_id=request.session_id,
            task_id=request.task_id,
        )
        return self._attachments.attach(
            authority=authority,
            session=session,
            dataset_revision=request.completed_dataset_revision,
            feedback_class=request.feedback_class,
            global_feedback=request.global_feedback,
            task_local_feedback=request.task_local_feedback,
            task_scope_id=request.task_scope_id,
            created_at=request.created_at,
            idempotency_key=request.idempotency_key,
        )

    def get_training_feedback_attachment(
        self, attachment_id: str
    ) -> TrainingFeedbackAttachment:
        return self._attachments.get(attachment_id)

    def get_completed_dataset_authority(
        self, dataset_id: str
    ) -> CompletedDatasetAuthority:
        """Resolve a dataset revision without trusting an adapter guess."""

        dataset = self._registry.get_dataset(_bounded_id(dataset_id))
        artifact = self._registry.get_artifact(dataset.artifact_id)
        manifest = artifact.manifest
        source = manifest.get("source_event_evidence")
        if (
            artifact.type is not ArtifactType.DATASET
            or artifact.state is not ArtifactState.ACTIVE
            or artifact.promoted is not True
            or manifest.get("dataset_id") != dataset.dataset_id
            or not isinstance(source, dict)
            or source.get("event_type") != "openevo.session_completed"
            or not isinstance(source.get("source_event_id"), str)
            or not isinstance(source.get("task_id"), str)
            or not isinstance(source.get("session_id"), str)
            or not isinstance(source.get("session_result_sha256"), str)
        ):
            raise ValueError("completed dataset authority is not sealed")
        return CompletedDatasetAuthority(
            completed_dataset_id=dataset.dataset_id,
            completed_dataset_revision=f"{artifact.artifact_id}.v{artifact.version}",
            dataset_artifact_id=artifact.artifact_id,
            dataset_manifest_sha256=_canonical_sha256(manifest),
            task_id=source["task_id"],
            session_id=source["session_id"],
            source_event_id=source["source_event_id"],
            source_session_result_sha256=source["session_result_sha256"],
        )

    def list_training_feedback_attachments_for_session(
        self, session_id: str
    ) -> TrainingFeedbackAttachmentList:
        return TrainingFeedbackAttachmentList(
            session_id=_bounded_id(session_id),
            attachments=self._attachments.list_for_session(session_id),
        )

    def resolve_evolution_dataset_view(
        self,
        request: EvolutionDatasetViewResolveRequest,
    ) -> ResolvedEvolutionDatasetView:
        if self._official_mode:
            raise ValueError("official frozen mode forbids training feedback")
        request = EvolutionDatasetViewResolveRequest.model_validate(request)
        attachments = tuple(
            self._attachments.get(attachment_id)
            for attachment_id in request.attachment_ids
        )
        dataset = self._registry.get_dataset(request.completed_dataset_id)
        source_artifact = self._registry.get_artifact(dataset.artifact_id)
        expected_revision = f"{source_artifact.artifact_id}.v{source_artifact.version}"
        if (
            source_artifact.type is not ArtifactType.DATASET
            or request.completed_dataset_revision != expected_revision
            or any(
                attachment.dataset_id != request.completed_dataset_id
                or attachment.dataset_revision != request.completed_dataset_revision
                or attachment.task_id != request.task_id
                or attachment.task_scope_id != request.task_scope_id
                or attachment.authority is not FeedbackAuthority.EVALUATOR_ONLY
                or attachment.status != "sealed"
                for attachment in attachments
            )
        ):
            raise ValueError("training feedback resolution authority is inconsistent")
        identity = {
            "schema_version": request.schema_version,
            "completed_dataset_id": request.completed_dataset_id,
            "completed_dataset_revision": request.completed_dataset_revision,
            "task_id": request.task_id,
            "task_scope_id": request.task_scope_id,
            "attachment_ids": list(request.attachment_ids),
            "attachment_sha256": [item.content_sha256 for item in attachments],
        }
        request_sha256 = _canonical_sha256(identity)
        resolution_id = f"tfv_{request_sha256}"
        existing = self._attachments.get_resolution(resolution_id)
        source_manifest_path = _local_file_uri(source_artifact.uri)
        output_dir = self._views_root / resolution_id
        manifest_path = output_dir / "manifest.json"
        if existing is not None:
            observed_artifact = self._registry.get_artifact(
                existing.dataset_artifact.artifact_id
            )
            observed_view = validate_training_feedback_dataset_view(
                source_manifest_path=source_manifest_path,
                attachments=attachments,
                output_dir=output_dir,
                task_id=request.task_id,
                resolution_id=resolution_id,
            )
            if (
                observed_artifact != existing.dataset_artifact
                or observed_view != existing.dataset_view
                or existing.completed_dataset_id != request.completed_dataset_id
                or existing.completed_dataset_revision
                != request.completed_dataset_revision
                or existing.attachments != attachments
            ):
                raise ValueError("persisted evolution dataset view authority drifted")
            return existing
        artifact = None
        lookup = getattr(
            self._registry,
            "get_dataset_artifact_by_create_identity",
            None,
        )
        if callable(lookup):
            artifact = lookup(resolution_id)
        if artifact is None:
            if output_dir.exists():
                # A prior process may have published the immutable view before
                # its registry call returned.  Validate instead of overwriting.
                view = validate_training_feedback_dataset_view(
                    source_manifest_path=source_manifest_path,
                    attachments=attachments,
                    output_dir=output_dir,
                    task_id=request.task_id,
                    resolution_id=resolution_id,
                )
            else:
                view = build_training_feedback_dataset_view(
                    source_manifest_path=source_manifest_path,
                    attachments=attachments,
                    output_dir=output_dir,
                    task_id=request.task_id,
                    resolution_id=resolution_id,
                )
            manifest = json.loads(
                _read_private_regular(
                    manifest_path,
                    max_bytes=_MAX_DATASET_VIEW_BYTES,
                )
            )
            artifact = self._registry.register_artifact(
                ArtifactRegisterRequest(
                    type=ArtifactType.DATASET,
                    name=f"training-feedback-{dataset.dataset_id}-{request_sha256[:12]}",
                    uri=manifest_path.as_uri(),
                    manifest=manifest,
                    lineage={
                        "source_dataset_id": dataset.dataset_id,
                        "source_dataset_artifact_id": dataset.artifact_id,
                        "training_feedback_attachment_ids": list(request.attachment_ids),
                    },
                    compatibility={
                        "purpose": "completed_task_training_feedback",
                        "authority": FeedbackAuthority.EVALUATOR_ONLY.value,
                    },
                    tags=["training-feedback", "resolved-view"],
                    promoted=False,
                )
            )
        else:
            view = validate_training_feedback_dataset_view(
                source_manifest_path=source_manifest_path,
                attachments=attachments,
                output_dir=output_dir,
                task_id=request.task_id,
                resolution_id=resolution_id,
            )
        receipt_body = {
            "completed_dataset_id": request.completed_dataset_id,
            "completed_dataset_revision": request.completed_dataset_revision,
            "attachment_ids": [item.attachment_id for item in attachments],
            "attachment_sha256": [item.content_sha256 for item in attachments],
            "dataset_view": view.model_dump(mode="json"),
            "dataset_artifact_id": artifact.artifact_id,
        }
        resolved = ResolvedEvolutionDatasetView(
            completed_dataset_id=request.completed_dataset_id,
            completed_dataset_revision=request.completed_dataset_revision,
            attachments=attachments,
            dataset_view=view,
            dataset_artifact=artifact,
            resolved_view_sha256=_canonical_sha256(receipt_body),
        )
        return self._attachments.put_resolution(
            resolution_id=resolution_id,
            request_sha256=request_sha256,
            receipt=resolved,
        )

    def attach_and_register(
        self,
        *,
        authority: object,
        dataset_id: str,
        session_id: str,
        task_id: str,
        task_scope_id: str,
        feedback_class: FeedbackClass,
        global_feedback: Mapping[str, Any],
        task_local_feedback: Mapping[str, Any],
        created_at: str | None = None,
    ) -> RegisteredTrainingFeedbackView:
        dataset = self._registry.get_dataset(_bounded_id(dataset_id))
        source_artifact = self._registry.get_artifact(dataset.artifact_id)
        dataset_revision = f"{source_artifact.artifact_id}.v{source_artifact.version}"
        attachment = self.create_training_feedback_attachment(
            authority=authority,
            request=TrainingFeedbackAttachmentCreateRequest(
                idempotency_key=(
                    "legacy_"
                    + _canonical_sha256(
                        {
                            "dataset_id": dataset_id,
                            "session_id": session_id,
                            "task_id": task_id,
                            "task_scope_id": task_scope_id,
                            "feedback_class": FeedbackClass(feedback_class).value,
                            "global_feedback": _json_copy(global_feedback),
                            "task_local_feedback": _json_copy(task_local_feedback),
                        }
                    )
                ),
                session_id=session_id,
                task_id=task_id,
                task_scope_id=task_scope_id,
                completed_dataset_id=dataset_id,
                completed_dataset_revision=dataset_revision,
                feedback_class=feedback_class,
                global_feedback=_json_copy(global_feedback),
                task_local_feedback=_json_copy(task_local_feedback),
                created_at=created_at,
            ),
        )
        resolved = self.resolve_evolution_dataset_view(
            EvolutionDatasetViewResolveRequest(
                completed_dataset_id=dataset_id,
                completed_dataset_revision=dataset_revision,
                task_id=task_id,
                task_scope_id=task_scope_id,
                attachment_ids=(attachment.attachment_id,),
            )
        )
        return RegisteredTrainingFeedbackView(
            attachment=attachment,
            dataset_view=resolved.dataset_view,
            dataset_artifact=resolved.dataset_artifact,
        )

    def task_local_overlay(
        self,
        attachment_id: str,
        *,
        next_task_scope_id: str,
    ) -> dict[str, Any]:
        return self._attachments.task_local_overlay(
            attachment_id,
            next_task_scope_id=next_task_scope_id,
        )


def build_training_feedback_dataset_view(
    *,
    source_manifest_path: Path,
    attachments: tuple[TrainingFeedbackAttachment, ...],
    output_dir: Path,
    task_id: str,
    official_mode: bool = False,
    resolution_id: str = "legacy_resolution",
) -> EvolutionDatasetViewReceipt:
    """Publish a derived dataset containing global feedback and no hard overlay."""

    if official_mode:
        raise ValueError("official frozen mode forbids training feedback dataset views")
    if not attachments:
        raise ValueError("training feedback dataset view requires an attachment")
    resolution_id = _bounded_id(resolution_id)
    source_manifest_path = Path(os.path.abspath(source_manifest_path))
    manifest_bytes = _read_private_regular(
        source_manifest_path,
        max_bytes=_MAX_DATASET_VIEW_BYTES,
        require_private=False,
    )
    source_manifest = json.loads(manifest_bytes)
    if not isinstance(source_manifest, dict):
        raise ValueError("source dataset manifest is invalid")
    source_manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    records_name = source_manifest.get("records_path")
    if not isinstance(records_name, str) or Path(records_name).name != records_name:
        raise ValueError("source dataset records path is unsafe")
    records_path = source_manifest_path.with_name(records_name)
    records_bytes = _read_private_regular(
        records_path,
        max_bytes=_MAX_DATASET_VIEW_BYTES,
        require_private=False,
    )
    records = [json.loads(line) for line in records_bytes.splitlines() if line.strip()]
    if any(not isinstance(record, dict) for record in records):
        raise ValueError("source dataset records are invalid")

    dataset_id = _bounded_id(str(source_manifest.get("dataset_id") or ""))
    source_evidence = source_manifest.get("source_event_evidence")
    if not isinstance(source_evidence, dict):
        raise ValueError("source dataset lacks sealed single-session evidence")
    source_session_sha256 = _sha256(str(source_evidence.get("session_result_sha256") or ""))
    session_id = _bounded_id(str(source_evidence.get("session_id") or ""))
    if source_evidence.get("task_id") != task_id:
        raise ValueError("source dataset task does not match the requested view")

    for attachment in attachments:
        attachment = TrainingFeedbackAttachment.model_validate(attachment)
        if (
            attachment.session_id != session_id
            or attachment.task_id != task_id
            or attachment.dataset_id != dataset_id
            or attachment.source_session_result_sha256 != source_session_sha256
            or attachment.source_dataset_manifest_sha256 != source_manifest_sha256
        ):
            raise ValueError("training feedback attachment does not bind the source dataset")

    records = _records_with_global_training_feedback(
        records=records,
        attachments=attachments,
        session_id=session_id,
        task_id=task_id,
    )

    output_dir = Path(os.path.abspath(output_dir))
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(output_dir, 0o700)
    derived_records_bytes = b"".join(
        _canonical_line(record_value) for record_value in records
    )
    records_sha256 = hashlib.sha256(derived_records_bytes).hexdigest()
    derived_manifest = {
        **source_manifest,
        "create_identity": resolution_id,
        "dataset_id": resolution_id,
        "derived_from_dataset_id": dataset_id,
        "source_dataset_manifest_sha256": source_manifest_sha256,
        "source_session_result_sha256": source_session_sha256,
        "training_feedback_attachment_ids": [item.attachment_id for item in attachments],
        "training_feedback_attachment_sha256": [item.content_sha256 for item in attachments],
        "records_path": "records.jsonl",
        "records_uri": (output_dir / "records.jsonl").as_uri(),
        "records_byte_size": len(derived_records_bytes),
        "records_sha256": records_sha256,
    }
    manifest_payload = _canonical_pretty_bytes(derived_manifest)
    manifest_sha256 = _canonical_sha256(derived_manifest)
    _publish_bytes(output_dir / "records.jsonl", derived_records_bytes)
    _publish_bytes(output_dir / "manifest.json", manifest_payload)
    overlay = [item.task_local_feedback for item in attachments if item.task_local_feedback]
    return EvolutionDatasetViewReceipt(
        resolution_id=resolution_id,
        source_dataset_id=dataset_id,
        source_dataset_manifest_sha256=source_manifest_sha256,
        source_session_result_sha256=source_session_sha256,
        attachment_ids=tuple(item.attachment_id for item in attachments),
        attachment_sha256=tuple(item.content_sha256 for item in attachments),
        records_sha256=records_sha256,
        manifest_sha256=manifest_sha256,
        task_local_overlay_sha256=(
            _canonical_sha256(overlay) if overlay else None
        ),
    )


def _records_with_global_training_feedback(
    *,
    records: list[Any],
    attachments: tuple[TrainingFeedbackAttachment, ...],
    session_id: str,
    task_id: str,
) -> list[dict[str, Any]]:
    """Deterministically project only global feedback into one session record."""

    copied = _json_copy(records)
    if not isinstance(copied, list) or any(
        not isinstance(record, dict) for record in copied
    ):
        raise ValueError("source dataset records are invalid")
    matching = [record for record in copied if record.get("session_id") == session_id]
    if len(matching) != 1 or matching[0].get("task_id") != task_id:
        raise ValueError("source dataset does not contain exactly one bound session")
    record = matching[0]
    payload = record.setdefault("payload", {})
    if not isinstance(payload, dict):
        raise ValueError("source dataset payload is invalid")
    evolution_feedback = payload.setdefault("evolution_feedback", {})
    if not isinstance(evolution_feedback, dict):
        raise ValueError("source evolution feedback is invalid")
    evolution_feedback["training_attachments"] = [
        {
            "feedback_id": attachment.attachment_id,
            "feedback_class": attachment.feedback_class.value,
            **_json_copy(attachment.global_feedback),
        }
        for attachment in attachments
    ]
    return copied


def validate_training_feedback_dataset_view(
    *,
    source_manifest_path: Path,
    attachments: tuple[TrainingFeedbackAttachment, ...],
    output_dir: Path,
    task_id: str,
    resolution_id: str,
) -> EvolutionDatasetViewReceipt:
    """Validate an already-published immutable view after a process crash."""

    resolution_id = _bounded_id(resolution_id)
    output_dir = Path(os.path.abspath(output_dir))
    manifest_path = output_dir / "manifest.json"
    records_path = output_dir / "records.jsonl"
    manifest_bytes = _read_private_regular(
        manifest_path,
        max_bytes=_MAX_DATASET_VIEW_BYTES,
    )
    records_bytes = _read_private_regular(
        records_path,
        max_bytes=_MAX_DATASET_VIEW_BYTES,
    )
    try:
        manifest = json.loads(manifest_bytes)
        records = [json.loads(line) for line in records_bytes.splitlines() if line.strip()]
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("published training feedback view is invalid") from exc
    source_bytes = _read_private_regular(
        Path(os.path.abspath(source_manifest_path)),
        max_bytes=_MAX_DATASET_VIEW_BYTES,
        require_private=False,
    )
    source_manifest = json.loads(source_bytes)
    source_evidence = source_manifest.get("source_event_evidence")
    source_records_name = source_manifest.get("records_path")
    if (
        not isinstance(source_records_name, str)
        or Path(source_records_name).name != source_records_name
    ):
        raise ValueError("source dataset records path is unsafe")
    source_records_path = Path(os.path.abspath(source_manifest_path)).with_name(
        source_records_name
    )
    source_records_bytes = _read_private_regular(
        source_records_path,
        max_bytes=_MAX_DATASET_VIEW_BYTES,
        require_private=False,
    )
    try:
        source_records = [
            json.loads(line) for line in source_records_bytes.splitlines() if line.strip()
        ]
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("source dataset records are invalid") from exc
    if not isinstance(source_evidence, dict):
        raise ValueError("source dataset lacks sealed single-session evidence")
    source_session_id = _bounded_id(str(source_evidence.get("session_id") or ""))
    expected_records = _records_with_global_training_feedback(
        records=source_records,
        attachments=attachments,
        session_id=source_session_id,
        task_id=task_id,
    )
    expected_records_bytes = b"".join(
        _canonical_line(record) for record in expected_records
    )
    if (
        not isinstance(manifest, dict)
        or manifest.get("create_identity") != resolution_id
        or manifest.get("derived_from_dataset_id") != source_manifest.get("dataset_id")
        or manifest.get("source_dataset_manifest_sha256")
        != hashlib.sha256(source_bytes).hexdigest()
        or manifest.get("source_session_result_sha256")
        != source_evidence.get("session_result_sha256")
        or manifest.get("training_feedback_attachment_ids")
        != [item.attachment_id for item in attachments]
        or manifest.get("training_feedback_attachment_sha256")
        != [item.content_sha256 for item in attachments]
        or manifest.get("records_path") != "records.jsonl"
        or manifest.get("records_uri") != records_path.as_uri()
        or manifest.get("records_byte_size") != len(records_bytes)
        or manifest.get("records_sha256") != hashlib.sha256(records_bytes).hexdigest()
        or not records
        or records_bytes != expected_records_bytes
    ):
        raise ValueError("published training feedback view authority is inconsistent")
    for attachment in attachments:
        if attachment.task_id != task_id:
            raise ValueError("task-local feedback leaked into the evolution dataset view")
    overlay = [item.task_local_feedback for item in attachments if item.task_local_feedback]
    return EvolutionDatasetViewReceipt(
        resolution_id=resolution_id,
        source_dataset_id=_bounded_id(str(source_manifest.get("dataset_id") or "")),
        source_dataset_manifest_sha256=hashlib.sha256(source_bytes).hexdigest(),
        source_session_result_sha256=_sha256(
            str(source_evidence.get("session_result_sha256") or "")
        ),
        attachment_ids=tuple(item.attachment_id for item in attachments),
        attachment_sha256=tuple(item.content_sha256 for item in attachments),
        records_sha256=hashlib.sha256(records_bytes).hexdigest(),
        manifest_sha256=_canonical_sha256(manifest),
        task_local_overlay_sha256=_canonical_sha256(overlay) if overlay else None,
    )


def _sealed_session_from_source(
    manifest_path: Path,
    *,
    dataset: DatasetCreateResponse,
    session_id: str,
    task_id: str,
) -> SealedSessionEvidence:
    manifest_bytes = _read_private_regular(
        manifest_path,
        max_bytes=_MAX_DATASET_VIEW_BYTES,
        require_private=False,
    )
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("source dataset manifest is invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("dataset_id") != dataset.dataset_id:
        raise ValueError("source dataset manifest identity is invalid")
    evidence = manifest.get("source_event_evidence")
    query = manifest.get("query")
    if (
        not isinstance(evidence, dict)
        or evidence.get("session_id") != session_id
        or evidence.get("task_id") != task_id
        or not isinstance(query, dict)
        or query.get("session_id") != session_id
        or query.get("task_id") != task_id
        or query.get("status") != ["COMPLETED"]
        or query.get("event_types") != ["openevo.session_completed"]
    ):
        raise ValueError("training feedback requires one completed bound session dataset")
    return SealedSessionEvidence(
        event_id=_bounded_id(str(evidence.get("event_id") or "")),
        session_id=session_id,
        task_id=task_id,
        dataset_id=dataset.dataset_id,
        dataset_artifact_id=dataset.artifact_id,
        session_result_sha256=_sha256(
            str(evidence.get("session_result_sha256") or "")
        ),
        dataset_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        completed=True,
        sealed=True,
    )


def _local_file_uri(uri: str) -> Path:
    from urllib.parse import unquote, urlparse

    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise ValueError("training feedback requires a local Core dataset artifact")
    path = Path(os.path.abspath(unquote(parsed.path)))
    if not path.is_absolute() or path.name != "manifest.json":
        raise ValueError("source dataset artifact URI is not canonical")
    return path


def _validate_feedback_layers(
    feedback_class: FeedbackClass,
    global_feedback: Mapping[str, Any],
    task_local_feedback: Mapping[str, Any],
) -> None:
    global_copy = _json_copy(global_feedback)
    task_copy = _json_copy(task_local_feedback)
    _reject_forbidden_feedback_keys(global_copy)
    _reject_forbidden_feedback_keys(task_copy)
    if feedback_class is FeedbackClass.SOFT_JUDGE:
        unknown = set(global_copy).difference(_SOFT_KEYS)
        if unknown:
            raise ValueError("soft judge feedback contains non-allowlisted fields")
        if task_copy:
            raise ValueError("soft judge feedback cannot create a task-local overlay")
    elif feedback_class is FeedbackClass.HARD_GT:
        if not task_copy:
            raise ValueError("hard-GT feedback requires task-local feedback")
    elif feedback_class is FeedbackClass.MIXED:
        if not task_copy:
            raise ValueError("mixed feedback requires task-local hard feedback")
        soft = global_copy.get("soft_judge", global_copy)
        if not isinstance(soft, dict) or set(soft).difference(_SOFT_KEYS):
            raise ValueError("mixed global feedback must use the soft-judge allowlist")


def _reject_forbidden_feedback_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(fragment in normalized for fragment in _FORBIDDEN_FEEDBACK_FRAGMENTS):
                raise ValueError("feedback contains evaluator-private fields")
            _reject_forbidden_feedback_keys(child)
    elif isinstance(value, list):
        for child in value:
            _reject_forbidden_feedback_keys(child)


def _bounded_id(value: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value.strip()) is None:
        raise ValueError("identifier is invalid")
    return value.strip()


def _bounded_event_id(value: str) -> str:
    """Event source identities may use the Core ``namespace:id`` form."""

    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", value.strip()) is None
    ):
        raise ValueError("event identifier is invalid")
    return value.strip()


def _sha256(value: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError("sha256 is invalid")
    return value


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("created_at must be timezone-aware")
    return parsed


def _json_copy(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("feedback must be finite JSON") from exc


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _canonical_pretty_bytes(value: Any) -> bytes:
    return json.dumps(value, indent=2, sort_keys=True, allow_nan=False).encode("utf-8")


def _canonical_line(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, allow_nan=False).encode("utf-8") + b"\n"


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _read_private_regular(
    path: Path,
    *,
    max_bytes: int,
    require_private: bool = True,
) -> bytes:
    opened = os.stat(path, follow_symlinks=False)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or opened.st_size > max_bytes
        or (require_private and stat.S_IMODE(opened.st_mode) & 0o077)
    ):
        raise ValueError("feedback authority file is invalid")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        content = os.read(descriptor, max_bytes + 1)
        if len(content) != opened.st_size:
            raise ValueError("feedback authority file changed while read")
        return content
    finally:
        os.close(descriptor)


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
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "CompletedDatasetAuthority",
    "EvolutionDatasetViewReceipt",
    "EvolutionDatasetViewResolveRequest",
    "FeedbackAuthority",
    "FeedbackClass",
    "RegisteredTrainingFeedbackView",
    "ResolvedEvolutionDatasetView",
    "SealedSessionEvidence",
    "TrainingFeedbackAttachment",
    "TrainingFeedbackAttachmentCreateRequest",
    "TrainingFeedbackAttachmentList",
    "TrainingFeedbackAttachmentStore",
    "TrainingFeedbackEvolutionService",
    "TrainingFeedbackRegistry",
    "build_training_feedback_dataset_view",
    "validate_training_feedback_dataset_view",
]
