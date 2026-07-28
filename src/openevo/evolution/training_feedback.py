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
import stat
from typing import Any, Final, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openevo.evolution.models import (
    ArtifactRegisterRequest,
    ArtifactResponse,
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

    schema_version: str = "openevo.training_feedback_attachment.v1"
    attachment_id: str
    revision: int = Field(ge=1)
    session_id: str
    task_id: str
    task_scope_id: str
    dataset_id: str
    dataset_revision: str
    producer: str
    authority: FeedbackAuthority
    feedback_class: FeedbackClass
    global_feedback: dict[str, Any]
    task_local_feedback: dict[str, Any]
    created_at: str
    source_session_result_sha256: str
    source_dataset_manifest_sha256: str
    content_sha256: str

    _ids = field_validator(
        "attachment_id",
        "session_id",
        "task_id",
        "task_scope_id",
        "dataset_id",
        "dataset_revision",
        "producer",
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


class EvolutionDatasetViewReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "openevo.training_feedback_dataset_view.v1"
    source_dataset_id: str
    source_dataset_manifest_sha256: str
    source_session_result_sha256: str
    attachment_ids: tuple[str, ...]
    attachment_sha256: tuple[str, ...]
    records_sha256: str
    manifest_sha256: str
    task_local_overlay_sha256: str | None = None

    _ids = field_validator("source_dataset_id")(lambda value: _bounded_id(value))
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


class TrainingFeedbackRegistry(Protocol):
    """Narrow store surface used by the trusted Core feedback service."""

    def get_dataset(self, dataset_id: str) -> DatasetCreateResponse: ...

    def get_artifact(self, artifact_id: str) -> ArtifactResponse: ...

    def register_artifact(self, request: ArtifactRegisterRequest) -> ArtifactResponse: ...


class _EvaluatorAuthorityCapability:
    """Process-local capability; never serialized into an adapter request."""

    __slots__ = ("_nonce", "producer")

    def __init__(self, producer: str) -> None:
        self.producer = _bounded_id(producer)
        self._nonce = secrets.token_bytes(32)


class TrainingFeedbackAttachmentStore:
    """Core-owned immutable attachment store with one evaluator capability."""

    def __init__(self, root: Path, *, official_mode: bool = False) -> None:
        self.root = Path(os.path.abspath(root))
        self.official_mode = official_mode
        self._authority: _EvaluatorAuthorityCapability | None = None
        self._initialize_root()

    def issue_evaluator_authority(self, *, producer: str) -> object:
        if self.official_mode:
            raise ValueError("official frozen mode forbids training feedback authority")
        if self._authority is not None:
            raise ValueError("evaluator authority was already issued")
        self._authority = _EvaluatorAuthorityCapability(producer)
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
    ) -> TrainingFeedbackAttachment:
        if self.official_mode:
            raise ValueError("official frozen mode forbids training feedback attachments")
        if authority is not self._authority or not isinstance(
            authority, _EvaluatorAuthorityCapability
        ):
            raise PermissionError("trusted evaluator authority is required")
        session = SealedSessionEvidence.model_validate(session)
        revision = self._next_revision(session.session_id)
        timestamp = created_at or datetime.now(UTC).isoformat()
        body: dict[str, Any] = {
            "schema_version": "openevo.training_feedback_attachment.v1",
            "attachment_id": f"tfa_{secrets.token_hex(16)}",
            "revision": revision,
            "session_id": session.session_id,
            "task_id": session.task_id,
            "task_scope_id": _bounded_id(task_scope_id or session.task_id),
            "dataset_id": session.dataset_id,
            "dataset_revision": _bounded_id(dataset_revision),
            "producer": authority.producer,
            "authority": FeedbackAuthority.EVALUATOR_ONLY.value,
            "feedback_class": FeedbackClass(feedback_class).value,
            "global_feedback": _json_copy(global_feedback),
            "task_local_feedback": _json_copy(task_local_feedback),
            "created_at": timestamp,
            "source_session_result_sha256": session.session_result_sha256,
            "source_dataset_manifest_sha256": session.dataset_manifest_sha256,
        }
        body["content_sha256"] = _canonical_sha256(body)
        attachment = TrainingFeedbackAttachment.model_validate(body)
        self._publish(attachment)
        return attachment

    def get(self, attachment_id: str) -> TrainingFeedbackAttachment:
        path = self._attachment_path(_bounded_id(attachment_id))
        payload = _read_private_regular(path, max_bytes=_MAX_ATTACHMENT_BYTES)
        attachment = TrainingFeedbackAttachment.model_validate_json(payload)
        if attachment.attachment_id != attachment_id:
            raise ValueError("attachment path identity does not match its content")
        return attachment

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

    def _next_revision(self, session_id: str) -> int:
        revisions = []
        for path in self.root.glob("tfa_*.json"):
            try:
                attachment = TrainingFeedbackAttachment.model_validate_json(
                    _read_private_regular(path, max_bytes=_MAX_ATTACHMENT_BYTES)
                )
            except (OSError, ValueError):
                raise ValueError("training feedback store contains invalid authority")
            if attachment.session_id == session_id:
                revisions.append(attachment.revision)
        return max(revisions, default=0) + 1

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
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class TrainingFeedbackEvolutionService:
    """Trusted Core facade for attach, derive, and registry publication.

    The capability returned by :meth:`issue_evaluator_authority` is deliberately
    process-local.  A benchmark adapter can ask a trusted supervisor to invoke
    this service, but it cannot serialize or mint evaluator authority itself.
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
        if self._official_mode:
            raise ValueError("official frozen mode forbids training feedback")
        dataset = self._registry.get_dataset(_bounded_id(dataset_id))
        source_artifact = self._registry.get_artifact(dataset.artifact_id)
        if source_artifact.type is not ArtifactType.DATASET:
            raise ValueError("source dataset registry artifact has the wrong type")
        source_manifest_path = _local_file_uri(source_artifact.uri)
        session = _sealed_session_from_source(
            source_manifest_path,
            dataset=dataset,
            session_id=session_id,
            task_id=task_id,
        )
        attachment = self._attachments.attach(
            authority=authority,
            session=session,
            dataset_revision=f"{source_artifact.artifact_id}.v{source_artifact.version}",
            feedback_class=feedback_class,
            global_feedback=global_feedback,
            task_local_feedback=task_local_feedback,
            task_scope_id=task_scope_id,
            created_at=created_at,
        )
        output_dir = self._views_root / attachment.attachment_id
        view = build_training_feedback_dataset_view(
            source_manifest_path=source_manifest_path,
            attachments=(attachment,),
            output_dir=output_dir,
            task_id=task_id,
        )
        manifest_path = output_dir / "manifest.json"
        manifest = json.loads(
            _read_private_regular(
                manifest_path,
                max_bytes=_MAX_DATASET_VIEW_BYTES,
            )
        )
        artifact = self._registry.register_artifact(
            ArtifactRegisterRequest(
                type=ArtifactType.DATASET,
                name=f"training-feedback-{dataset.dataset_id}-{attachment.revision}",
                uri=manifest_path.as_uri(),
                manifest=manifest,
                lineage={
                    "source_dataset_id": dataset.dataset_id,
                    "source_dataset_artifact_id": dataset.artifact_id,
                    "training_feedback_attachment_ids": [attachment.attachment_id],
                },
                compatibility={
                    "purpose": "completed_task_training_feedback",
                    "authority": FeedbackAuthority.EVALUATOR_ONLY.value,
                },
                tags=["training-feedback", attachment.feedback_class.value.lower()],
                promoted=False,
            )
        )
        return RegisteredTrainingFeedbackView(
            attachment=attachment,
            dataset_view=view,
            dataset_artifact=artifact,
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
) -> EvolutionDatasetViewReceipt:
    """Publish a derived dataset containing global feedback and no hard overlay."""

    if official_mode:
        raise ValueError("official frozen mode forbids training feedback dataset views")
    if not attachments:
        raise ValueError("training feedback dataset view requires an attachment")
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

    matching = [record for record in records if record.get("session_id") == session_id]
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

    output_dir = Path(os.path.abspath(output_dir))
    output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    os.chmod(output_dir, 0o700)
    derived_records_bytes = b"".join(
        _canonical_line(record_value) for record_value in records
    )
    records_sha256 = hashlib.sha256(derived_records_bytes).hexdigest()
    derived_manifest = {
        **source_manifest,
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
        written = os.write(descriptor, payload)
        if written != len(payload):
            raise OSError("short write")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "EvolutionDatasetViewReceipt",
    "FeedbackAuthority",
    "FeedbackClass",
    "RegisteredTrainingFeedbackView",
    "SealedSessionEvidence",
    "TrainingFeedbackAttachment",
    "TrainingFeedbackAttachmentStore",
    "TrainingFeedbackEvolutionService",
    "TrainingFeedbackRegistry",
    "build_training_feedback_dataset_view",
]
