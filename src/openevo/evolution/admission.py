"""Core-owned admission decisions for native evolution proposals.

Evolution methods create immutable proposal artifacts.  This service is the
separate authority that records update/keep/reject and changes registry
promotion state.  Benchmark adapters may provide validation evidence but cannot
mint the process-local admission capability or manufacture registry revisions.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openevo.evolution.models import (
    ArtifactContentAdmissionReceipt,
    ArtifactResponse,
    ArtifactType,
)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_TASK_LOCAL_PRESERVATION_SCHEMA = "openevo.task_local_preservation.v1"
_TASK_LOCAL_PRESERVATION_SCOPE = "next_session_only"


def task_local_preservation_allows_candidate_source_reuse(
    config: Mapping[str, object],
) -> bool:
    """Return the closed one-session exception to generic source reuse.

    Candidate and public evidence are reusable only when a verified reflector
    target declares this exact scope and waits for post-evaluator feedback.
    Ordinary global artifacts retain their source-literal ban.
    """

    value = config.get("task_local_preservation")
    if value is None:
        return False
    if (
        not isinstance(value, dict)
        or set(value) != {"schema_version", "scope"}
        or value.get("schema_version") != _TASK_LOCAL_PRESERVATION_SCHEMA
        or value.get("scope") != _TASK_LOCAL_PRESERVATION_SCOPE
        or config.get("training_feedback_required") is not True
    ):
        raise ValueError("task_local_preservation config is invalid")
    return True


class ProposalAction(StrEnum):
    UPDATE = "update"
    KEEP = "keep"
    REJECT = "reject"


class NativeArtifactProposalSet(BaseModel):
    """Closed proposal inventory handed to a Core-owned admission policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str
    target_id: str
    artifact_type: ArtifactType
    parent_artifact_id: str | None = None
    proposals: tuple[ArtifactResponse, ...] = Field(min_length=1, max_length=128)

    _ids = field_validator("job_id", "target_id", "parent_artifact_id")(
        lambda value: None if value is None else _identifier(value)
    )

    @model_validator(mode="after")
    def _sealed_unpromoted_proposals(self) -> NativeArtifactProposalSet:
        proposal_ids = tuple(item.artifact_id for item in self.proposals)
        if (
            len(proposal_ids) != len(set(proposal_ids))
            or self.parent_artifact_id in proposal_ids
            or any(
                item.type is not self.artifact_type
                or item.state.value != "sealed"
                or item.promoted is not False
                or any(not math.isfinite(value) for value in item.scores.values())
                for item in self.proposals
            )
        ):
            raise ValueError("native artifact proposal inventory is invalid")
        return self


class NativeArtifactAdmissionSelection(BaseModel):
    """Core policy result; registry mutation remains owned by admission service."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: ProposalAction
    selected_artifact_id: str | None = None
    reason: str = Field(min_length=1, max_length=4096)

    _selected = field_validator("selected_artifact_id")(
        lambda value: None if value is None else _identifier(value)
    )

    @model_validator(mode="after")
    def _action_shape(self) -> NativeArtifactAdmissionSelection:
        if self.action is ProposalAction.UPDATE:
            if self.selected_artifact_id is None:
                raise ValueError("update admission must select one proposal")
        elif self.selected_artifact_id is not None:
            raise ValueError("keep/reject admission cannot select a proposal")
        return self


class DeterministicNativeArtifactAdmissionPolicy:
    """Select one structurally valid proposal without benchmark-side mutation.

    Native workers may emit several proposals (GEPA is the first built-in case).
    The policy ranks a closed proposal inventory by generic artifact scores and
    uses the immutable proposal order as the final tie-break.  It does not
    inspect benchmark identities, evaluator-private payloads, or filesystem
    paths.  Alternate Core-owned policies may return ``keep`` or ``reject``;
    those actions are still applied by :class:`NativeArtifactAdmissionService`.
    """

    _SCORE_PRIORITY = (
        "heldout_reward_delta",
        "quality",
        "static_guardrail_score",
    )

    def decide(
        self,
        proposal_set: NativeArtifactProposalSet,
    ) -> NativeArtifactAdmissionSelection:
        proposal_set = NativeArtifactProposalSet.model_validate(proposal_set)
        score_key = next(
            (
                key
                for key in self._SCORE_PRIORITY
                if any(key in item.scores for item in proposal_set.proposals)
            ),
            None,
        )
        if score_key is None:
            selected = proposal_set.proposals[0]
            reason = "Core selected the first proposal from the sealed native order"
        else:
            selected = max(
                enumerate(proposal_set.proposals),
                key=lambda pair: (
                    pair[1].scores.get(score_key, float("-inf")),
                    -pair[0],
                ),
            )[1]
            reason = (
                "Core selected the highest sealed native proposal by "
                f"{score_key} with stable worker-order tie-breaking"
            )
        return NativeArtifactAdmissionSelection(
            action=ProposalAction.UPDATE,
            selected_artifact_id=selected.artifact_id,
            reason=reason,
        )


class ArtifactContentAdmissionBasis(BaseModel):
    """Private exact-match basis; values are never copied into a receipt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_ids: tuple[str, ...] = Field(default=(), max_length=128)
    file_names: tuple[str, ...] = Field(default=(), max_length=512)
    entities: tuple[str, ...] = Field(default=(), max_length=512)
    values: tuple[str, ...] = Field(default=(), max_length=512)
    dois: tuple[str, ...] = Field(default=(), max_length=128)
    report_sentences: tuple[str, ...] = Field(default=(), max_length=512)
    evaluator_terms: tuple[str, ...] = Field(default=(), max_length=128)
    absolute_paths: tuple[str, ...] = Field(default=(), max_length=128)
    candidate_source_reuse_authorized: bool = False

    @field_validator(
        "task_ids",
        "file_names",
        "entities",
        "values",
        "dois",
        "report_sentences",
        "evaluator_terms",
        "absolute_paths",
    )
    @classmethod
    def _bounded_literals(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if (
            any(not item or len(item.encode("utf-8")) > 4096 for item in normalized)
            or normalized != tuple(sorted(set(normalized), key=lambda item: item.casefold()))
        ):
            raise ValueError("content admission literals must be sorted and unique")
        return normalized

    @property
    def content_sha256(self) -> str:
        return _canonical_sha256(self.model_dump(mode="json"))


_DOI_PATTERN = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)
_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])/(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]+"
)
_DEFAULT_EVALUATOR_TERMS = (
    "_score.json",
    "checklist keyword",
    "judge reasoning",
    "raw judge response",
    "rubric mode",
    "target_study",
)

# This is the immutable, Core-owned root presented to every managed science
# runtime.  Naming the root alone is transferable runtime guidance, not a
# task-local filesystem disclosure.  Descendants remain protected: a proposal
# that names even one file or directory below this root still fails closed.
_TRANSFERABLE_MANAGED_RUNTIME_ROOTS = frozenset({"/openevo/session/workspace"})


def artifact_content_admission_receipt(
    *,
    basis: ArtifactContentAdmissionBasis,
    payloads: Mapping[str, Mapping[str, str]],
    source_payloads: Mapping[str, Mapping[str, str]] | None = None,
) -> ArtifactContentAdmissionReceipt:
    """Scan verified UTF-8 proposal payloads and return only redacted counts."""

    basis = ArtifactContentAdmissionBasis.model_validate(basis)
    if not payloads or any(not files for files in payloads.values()):
        raise ValueError("content admission requires verified proposal text")
    proposal_ids = tuple(sorted(payloads))
    finding_categories: set[str] = set()
    finding_count = 0
    scanned_file_count = 0
    scanned_byte_count = 0

    source_payloads = source_payloads or {}
    # These two authorities deliberately have different scopes:
    #
    # * ``source_payloads_for_literal_scan`` controls which source literals can
    #   collide with proposal text.  A sealed candidate/public source may be
    #   reusable in the closed task-local preservation scope.
    # * ``source_payloads`` is the complete provenance authority.  Reuse never
    #   changes which artifacts actually produced the proposal.
    #
    # Do not derive provenance from the filtered scan set.
    # The one-session scope may retain candidate/public evidence. Its exact
    # task/evaluator basis below still blocks private feedback, task IDs,
    # protected paths, Judge markers, and every classified literal.
    candidate_source_literals = _sealed_source_literals(source_payloads)
    source_payloads_for_literal_scan = (
        {} if basis.candidate_source_reuse_authorized else source_payloads
    )
    source_literals = _sealed_source_literals(source_payloads_for_literal_scan)

    def direct_literals(category: str, literals: tuple[str, ...]) -> tuple[str, ...]:
        """Keep evaluator-only literals unless baseline evidence already used them.

        In the bounded preservation scope, a candidate can legitimately name a
        public input, file, quantitative observation, DOI, or report sentence
        that happens also to occur in evaluator-private GT.  That overlap is
        candidate provenance, not a route by which the Reflector learned GT:
        the source artifact already sealed it before evaluator attachment.
        Task IDs, evaluator markers, and absolute paths remain non-transferable
        even when the baseline transcript contains them.
        """

        if (
            not basis.candidate_source_reuse_authorized
            or category
            not in {"file_name", "entity", "value", "doi", "report_sentence"}
        ):
            return literals
        reusable = {
            item.casefold()
            for item in candidate_source_literals[category]
        }
        return tuple(item for item in literals if item.casefold() not in reusable)

    exact_by_category = {
        "task_id": _merged_literals(basis.task_ids, source_literals["task_id"]),
        "file_name": _merged_literals(
            direct_literals("file_name", basis.file_names),
            source_literals["file_name"],
        ),
        "entity": _merged_literals(
            direct_literals("entity", basis.entities),
            source_literals["entity"],
        ),
        "value": _merged_literals(
            direct_literals("value", basis.values),
            source_literals["value"],
        ),
        "doi": _merged_literals(
            direct_literals("doi", basis.dois),
            source_literals["doi"],
        ),
        "report_sentence": _merged_literals(
            direct_literals("report_sentence", basis.report_sentences),
            source_literals["report_sentence"],
        ),
        "evaluator_term": tuple(
            sorted(
                {*basis.evaluator_terms, *_DEFAULT_EVALUATOR_TERMS},
                key=lambda item: item.casefold(),
            )
        ),
        "absolute_path": _merged_literals(
            basis.absolute_paths,
            tuple(
                item
                for item in source_literals["absolute_path"]
                if item not in _TRANSFERABLE_MANAGED_RUNTIME_ROOTS
            ),
        ),
    }
    for artifact_id in proposal_ids:
        files = payloads[artifact_id]
        for relative_path in sorted(files):
            text = files[relative_path]
            if not isinstance(text, str):
                raise TypeError("content admission payload must be verified UTF-8 text")
            scanned_file_count += 1
            scanned_byte_count += len(text.encode("utf-8"))
            folded = text.casefold()
            for category, literals in exact_by_category.items():
                for literal in literals:
                    if _protected_literal_present(folded, literal):
                        finding_categories.add(category)
                        finding_count += 1
            if _DOI_PATTERN.search(text):
                finding_categories.add("doi")
                finding_count += 1
            for match in _ABSOLUTE_PATH_PATTERN.finditer(text):
                if match.group(0) not in _TRANSFERABLE_MANAGED_RUNTIME_ROOTS:
                    finding_categories.add("absolute_path")
                    finding_count += 1

    body: dict[str, Any] = {
        "schema_version": "openevo.artifact_content_admission.v1",
        "basis_sha256": basis.content_sha256,
        "proposal_artifact_ids": list(proposal_ids),
        "source_artifact_ids": sorted(source_payloads),
        "source_payload_sha256": (
            _source_payload_sha256(source_payloads)
            if source_payloads
            else None
        ),
        "scanned_file_count": scanned_file_count,
        "scanned_byte_count": scanned_byte_count,
        "finding_count": finding_count,
        "finding_categories": sorted(finding_categories),
        "passed": finding_count == 0,
    }
    body["content_sha256"] = _canonical_sha256(body)
    return ArtifactContentAdmissionReceipt.model_validate(body)


def _merged_literals(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {item for group in groups for item in group},
            key=lambda item: item.casefold(),
        )
    )


def _source_payload_sha256(
    source_payloads: Mapping[str, Mapping[str, str]],
) -> str:
    inventory = {
        artifact_id: {
            path: hashlib.sha256(text.encode("utf-8")).hexdigest()
            for path, text in sorted(files.items())
        }
        for artifact_id, files in sorted(source_payloads.items())
    }
    return _canonical_sha256(inventory)


def _sealed_source_literals(
    source_payloads: Mapping[str, Mapping[str, str]],
) -> dict[str, tuple[str, ...]]:
    """Derive bounded private literals from verified sealed dataset payloads."""

    texts: list[str] = []
    report_texts: list[str] = []
    entities: set[str] = set()
    total_bytes = 0

    def walk(value: object, *, key: str = "", report: bool = False) -> None:
        nonlocal total_bytes
        if total_bytes >= 4 * 1024 * 1024:
            return
        normalized_key = key.strip().lower().replace("-", "_")
        report = report or normalized_key in {
            "candidate_report",
            "report_content",
            "report_markdown",
            "response",
            "response_messages",
        }
        if isinstance(value, str):
            encoded = value.encode("utf-8")
            remaining = 4 * 1024 * 1024 - total_bytes
            text = encoded[:remaining].decode("utf-8", errors="ignore")
            total_bytes += len(text.encode("utf-8"))
            texts.append(text)
            if report:
                report_texts.append(text)
            if normalized_key in {
                "entity",
                "entities",
                "target_entity",
                "title",
                "article_title",
                "paper_title",
            } and 4 <= len(text) <= 512:
                entities.add(" ".join(text.split()))
            return
        if isinstance(value, dict):
            for nested_key, nested in value.items():
                walk(nested, key=str(nested_key), report=report)
            return
        if isinstance(value, list | tuple):
            for nested in value:
                walk(nested, key=key, report=report)

    for files in source_payloads.values():
        for path, text in files.items():
            parsed_values: list[object] = []
            if path.endswith(".jsonl"):
                for line in text.splitlines():
                    try:
                        parsed_values.append(json.loads(line))
                    except ValueError:
                        continue
            elif path.endswith(".json"):
                try:
                    parsed_values.append(json.loads(text))
                except ValueError:
                    pass
            if parsed_values:
                for value in parsed_values:
                    walk(value)
            else:
                walk(text)

    joined = "\n".join(texts)
    report_sentences: set[str] = set()
    for text in report_texts:
        for sentence in re.split(r"(?<=[.!?])\s+|[\r\n]+", text):
            normalized = " ".join(sentence.split())
            if 80 <= len(normalized) <= 320:
                report_sentences.add(normalized)
            if len(report_sentences) >= 512:
                break
    return {
        "task_id": tuple(
            sorted(set(re.findall(r"\b[A-Za-z][A-Za-z0-9-]{1,48}_[0-9]{3,}\b", joined)))[:128]
        ),
        "file_name": tuple(
            sorted(
                set(
                    re.findall(
                        r"(?<![A-Za-z0-9_.-])([A-Za-z0-9][A-Za-z0-9_.-]{1,127}\."
                        r"(?:csv|tsv|xlsx?|jsonl?|npy|npz|nc|hdf5?|parquet|txt|"
                        r"png|svg|pdf|md|py|r))\b",
                        joined,
                        flags=re.IGNORECASE,
                    )
                )
            )[:512]
        ),
        "entity": tuple(sorted(entities)[:512]),
        "value": tuple(
            sorted(
                set(
                    re.findall(
                        r"(?<![A-Za-z0-9.,])[-+]?(?:\d+\.\d{6,}|\.\d{6,}|\d{6,})"
                        r"(?![A-Za-z0-9]|[.,]\d)",
                        joined,
                    )
                )
            )[:512]
        ),
        "doi": tuple(sorted(set(_DOI_PATTERN.findall(joined)))[:128]),
        "report_sentence": tuple(sorted(report_sentences)[:512]),
        "absolute_path": tuple(
            sorted(
                {
                    item
                    for item in _ABSOLUTE_PATH_PATTERN.findall(joined)
                    if item not in _TRANSFERABLE_MANAGED_RUNTIME_ROOTS
                }
            )[:128]
        ),
    }


def _protected_literal_present(folded_text: str, literal: str) -> bool:
    normalized = literal.strip().casefold()
    if not normalized:
        return False
    if len(normalized) < 4 and not any(character.isdigit() for character in normalized):
        return False
    return normalized in folded_text


class NativeArtifactAdmissionPolicy(Protocol):
    def decide(
        self,
        proposal_set: NativeArtifactProposalSet,
    ) -> NativeArtifactAdmissionSelection: ...


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
    genesis: bool = False
    selected_artifact_id: str | None = None
    reason: str = Field(min_length=1, max_length=4096)
    admission: ArtifactAdmissionEvidence
    content_admission_basis: ArtifactContentAdmissionBasis

    _ids = field_validator(
        "decision_id", "job_id", "parent_artifact_id", "selected_artifact_id"
    )(lambda value: None if value is None else _identifier(value))

    @model_validator(mode="after")
    def _action_shape(self) -> ArtifactProposalDecisionRequest:
        if (
            not self.admission.passed
            and self.action is not ProposalAction.REJECT
        ):
            raise ValueError("failed admission evidence requires reject")
        if self.action is ProposalAction.UPDATE:
            if self.selected_artifact_id is None:
                raise ValueError("update requires one selected proposal")
        elif self.selected_artifact_id is not None:
            raise ValueError("keep/reject cannot select a proposal")
        if self.genesis and (
            self.action is not ProposalAction.UPDATE
            or self.parent_artifact_id is not None
        ):
            raise ValueError("genesis is only valid for a parentless update")
        if self.action is ProposalAction.UPDATE and (
            self.parent_artifact_id is None and not self.genesis
        ):
            raise ValueError("parentless update requires genesis authority")
        if self.action is ProposalAction.KEEP and self.parent_artifact_id is None:
            raise ValueError("keep requires a parent registry artifact")
        return self


class ArtifactProposalDecisionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "openevo.artifact_proposal_decision.v1"
    decision_id: str
    job_id: str
    artifact_type: ArtifactType
    action: ProposalAction
    parent_artifact_id: str | None
    genesis: bool = False
    proposal_artifact_ids: tuple[str, ...]
    selected_artifact_id: str | None
    rejected_artifact_ids: tuple[str, ...]
    active_artifact_id: str | None
    reason: str
    admission: ArtifactAdmissionEvidence
    content_admission: ArtifactContentAdmissionReceipt
    created_at: str
    content_sha256: str

    @model_validator(mode="after")
    def _content_addressed(self) -> ArtifactProposalDecisionReceipt:
        payload = self.model_dump(mode="json", exclude={"content_sha256"})
        if _canonical_sha256(payload) != self.content_sha256:
            raise ValueError("proposal decision receipt hash is invalid")
        return self


class ArtifactAdmissionRegistry(Protocol):
    def get_internal_job_result(self, job_id: str) -> dict[str, Any]: ...

    def get_artifact(self, artifact_id: str) -> ArtifactResponse: ...

    def apply_internal_artifact_admission(
        self,
        request: dict[str, Any],
    ) -> ArtifactProposalDecisionReceipt | dict[str, Any]: ...


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
        receipt = ArtifactProposalDecisionReceipt.model_validate(
            self._registry.apply_internal_artifact_admission(
                request.model_dump(mode="json")
            )
        )
        if not _receipt_matches_request(receipt, request):
            raise ValueError("registry admission receipt differs from the request")
        self._publish_idempotent(receipt)
        return receipt

    def _read_existing(
        self,
        decision_id: str,
    ) -> ArtifactProposalDecisionReceipt | None:
        path = self.root / f"{decision_id}.json"
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != os.geteuid()
                or metadata.st_size > 1_048_576
            ):
                raise ValueError("artifact admission receipt identity is invalid")
            payload = os.read(descriptor, metadata.st_size + 1)
            if len(payload) != metadata.st_size:
                raise ValueError("artifact admission receipt read was incomplete")
        finally:
            os.close(descriptor)
        try:
            return ArtifactProposalDecisionReceipt.model_validate_json(payload)
        except ValueError as exc:
            raise ValueError("artifact admission receipt is invalid") from exc

    def _publish_idempotent(self, receipt: ArtifactProposalDecisionReceipt) -> None:
        existing = self._read_existing(receipt.decision_id)
        if existing is not None:
            if existing != receipt:
                raise ValueError("artifact admission decision mirror changed")
            return
        path = self.root / f"{receipt.decision_id}.json"
        payload = json.dumps(
            receipt.model_dump(mode="json"),
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        temporary = self.root / f".{receipt.decision_id}.{secrets.token_hex(16)}.pending"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            if os.write(descriptor, payload) != len(payload):
                raise OSError("short artifact admission receipt write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            existing = self._read_existing(receipt.decision_id)
            if existing != receipt:
                raise ValueError("artifact admission decision mirror changed")
        finally:
            temporary.unlink(missing_ok=True)
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _receipt_matches_request(
    receipt: ArtifactProposalDecisionReceipt,
    request: ArtifactProposalDecisionRequest,
) -> bool:
    return (
        receipt.decision_id == request.decision_id
        and receipt.job_id == request.job_id
        and receipt.artifact_type is request.artifact_type
        and receipt.action is request.action
        and receipt.parent_artifact_id == request.parent_artifact_id
        and receipt.genesis is request.genesis
        and receipt.selected_artifact_id == request.selected_artifact_id
        and receipt.reason == request.reason
        and receipt.admission == request.admission
        and receipt.content_admission.basis_sha256
        == request.content_admission_basis.content_sha256
    )


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
    "ArtifactContentAdmissionBasis",
    "ArtifactProposalDecisionReceipt",
    "ArtifactProposalDecisionRequest",
    "DeterministicNativeArtifactAdmissionPolicy",
    "NativeArtifactAdmissionPolicy",
    "NativeArtifactAdmissionSelection",
    "NativeArtifactAdmissionService",
    "NativeArtifactProposalSet",
    "ProposalAction",
    "artifact_content_admission_receipt",
]
