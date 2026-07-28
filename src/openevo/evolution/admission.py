"""Core-owned admission decisions for native evolution proposals.

Evolution methods create immutable proposal artifacts.  This service is the
separate authority that records update/keep/reject and changes registry
promotion state.  Benchmark adapters may provide validation evidence but cannot
mint the process-local admission capability or manufacture registry revisions.
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
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openevo.evolution.models import ArtifactResponse, ArtifactType


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class ProposalAction(StrEnum):
    UPDATE = "update"
    KEEP = "keep"
    REJECT = "reject"


class ArtifactAdmissionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    validator_id: str
    schema_version: str
    passed: bool
    report_sha256: str

    _ids = field_validator("validator_id", "schema_version")(
        lambda value: _identifier(value)
    )
    _hash = field_validator("report_sha256")(lambda value: _sha256(value))


class ArtifactProposalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str
    job_id: str
    artifact_type: ArtifactType
    action: ProposalAction
    parent_artifact_id: str | None = None
    selected_artifact_id: str | None = None
    reason: str = Field(min_length=1, max_length=4096)
    admission: ArtifactAdmissionEvidence

    _ids = field_validator(
        "decision_id", "job_id", "parent_artifact_id", "selected_artifact_id"
    )(lambda value: None if value is None else _identifier(value))

    @model_validator(mode="after")
    def _action_shape(self) -> "ArtifactProposalDecisionRequest":
        if not self.admission.passed:
            if self.action is not ProposalAction.REJECT:
                raise ValueError("failed admission evidence requires reject")
        if self.action is ProposalAction.UPDATE:
            if self.selected_artifact_id is None:
                raise ValueError("update requires one selected proposal")
        elif self.selected_artifact_id is not None:
            raise ValueError("keep/reject cannot select a proposal")
        if self.action in {ProposalAction.UPDATE, ProposalAction.KEEP} and (
            self.parent_artifact_id is None
        ):
            raise ValueError("update/keep requires a parent registry artifact")
        return self


class ArtifactProposalDecisionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "openevo.artifact_proposal_decision.v1"
    decision_id: str
    job_id: str
    artifact_type: ArtifactType
    action: ProposalAction
    parent_artifact_id: str | None
    proposal_artifact_ids: tuple[str, ...]
    selected_artifact_id: str | None
    rejected_artifact_ids: tuple[str, ...]
    active_artifact_id: str | None
    reason: str
    admission: ArtifactAdmissionEvidence
    created_at: str
    content_sha256: str

    @model_validator(mode="after")
    def _content_addressed(self) -> "ArtifactProposalDecisionReceipt":
        payload = self.model_dump(mode="json", exclude={"content_sha256"})
        if _canonical_sha256(payload) != self.content_sha256:
            raise ValueError("proposal decision receipt hash is invalid")
        return self


class ArtifactAdmissionRegistry(Protocol):
    def get_internal_job_result(self, job_id: str) -> dict[str, Any]: ...

    def get_artifact(self, artifact_id: str) -> ArtifactResponse: ...

    def update_artifact_promotion(
        self,
        artifact_id: str,
        *,
        promoted: bool,
    ) -> ArtifactResponse: ...


class _AdmissionAuthority:
    __slots__ = ("_nonce", "producer")

    def __init__(self, producer: str) -> None:
        self.producer = _identifier(producer)
        self._nonce = secrets.token_bytes(32)


class NativeArtifactAdmissionService:
    """Apply one immutable decision to all target proposals from one native job."""

    def __init__(self, *, registry: ArtifactAdmissionRegistry, root: Path) -> None:
        self._registry = registry
        self.root = Path(os.path.abspath(root))
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        opened = os.stat(self.root, follow_symlinks=False)
        if not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.geteuid():
            raise ValueError("artifact admission root is not Core-owned")
        self._authority: _AdmissionAuthority | None = None

    def issue_admission_authority(self, *, producer: str) -> object:
        if self._authority is not None:
            raise ValueError("artifact admission authority was already issued")
        self._authority = _AdmissionAuthority(producer)
        return self._authority

    def decide(
        self,
        *,
        authority: object,
        request: ArtifactProposalDecisionRequest,
    ) -> ArtifactProposalDecisionReceipt:
        if authority is not self._authority or not isinstance(authority, _AdmissionAuthority):
            raise PermissionError("Core artifact admission authority is required")
        request = ArtifactProposalDecisionRequest.model_validate(request)
        result = self._registry.get_internal_job_result(request.job_id)
        if result.get("job_id") != request.job_id or result.get("state") != "succeeded":
            raise ValueError("proposal decision requires a succeeded native job")
        raw_ids = result.get("artifact_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ValueError("native evolution job has no registered proposals")
        artifacts = [self._registry.get_artifact(str(item)) for item in raw_ids]
        proposals = [item for item in artifacts if item.type is request.artifact_type]
        if not proposals:
            raise ValueError("native evolution job has no proposal of the requested type")
        proposal_ids = tuple(item.artifact_id for item in proposals)
        if len(proposal_ids) != len(set(proposal_ids)):
            raise ValueError("native evolution job returned duplicate proposals")
        if any(item.promoted for item in proposals):
            raise ValueError("proposal promotion occurred before Core admission")

        parent: ArtifactResponse | None = None
        if request.parent_artifact_id is not None:
            parent = self._registry.get_artifact(request.parent_artifact_id)
            if parent.type is not request.artifact_type or parent.artifact_id in proposal_ids:
                raise ValueError("parent artifact authority is invalid")

        selected: ArtifactResponse | None = None
        if request.action is ProposalAction.UPDATE:
            if request.selected_artifact_id not in proposal_ids:
                raise ValueError("selected artifact is not a proposal from this job")
            selected = next(
                item for item in proposals if item.artifact_id == request.selected_artifact_id
            )
            parent_was_promoted = bool(parent is not None and parent.promoted)
            if parent_was_promoted and parent is not None:
                self._registry.update_artifact_promotion(parent.artifact_id, promoted=False)
            try:
                selected = self._registry.update_artifact_promotion(
                    selected.artifact_id,
                    promoted=True,
                )
            except BaseException:
                if parent_was_promoted and parent is not None:
                    self._registry.update_artifact_promotion(parent.artifact_id, promoted=True)
                raise
            if not selected.promoted:
                raise RuntimeError("registry did not promote the selected successor")
            active_artifact_id = selected.artifact_id
        else:
            active_artifact_id = parent.artifact_id if parent is not None else None

        rejected = tuple(
            artifact_id
            for artifact_id in proposal_ids
            if artifact_id != request.selected_artifact_id
        )
        body: dict[str, Any] = {
            "schema_version": "openevo.artifact_proposal_decision.v1",
            "decision_id": request.decision_id,
            "job_id": request.job_id,
            "artifact_type": request.artifact_type.value,
            "action": request.action.value,
            "parent_artifact_id": request.parent_artifact_id,
            "proposal_artifact_ids": list(proposal_ids),
            "selected_artifact_id": request.selected_artifact_id,
            "rejected_artifact_ids": list(rejected),
            "active_artifact_id": active_artifact_id,
            "reason": request.reason,
            "admission": request.admission.model_dump(mode="json"),
            "created_at": datetime.now(UTC).isoformat(),
        }
        body["content_sha256"] = _canonical_sha256(body)
        receipt = ArtifactProposalDecisionReceipt.model_validate(body)
        self._publish(receipt)
        return receipt

    def _publish(self, receipt: ArtifactProposalDecisionReceipt) -> None:
        path = self.root / f"{receipt.decision_id}.json"
        payload = json.dumps(
            receipt.model_dump(mode="json"),
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            if os.write(descriptor, payload) != len(payload):
                raise OSError("short artifact admission receipt write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _identifier(value: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value.strip()) is None:
        raise ValueError("identifier is invalid")
    return value.strip()


def _sha256(value: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise ValueError("sha256 is invalid")
    return value


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "ArtifactAdmissionEvidence",
    "ArtifactProposalDecisionReceipt",
    "ArtifactProposalDecisionRequest",
    "NativeArtifactAdmissionService",
    "ProposalAction",
]
