"""Closed canonical entry/state schemas and deterministic target projections."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes, sha256_bytes

SelectedTarget = Literal["text_memory", "skill_bundle"]
EntryStatus = Literal["provisional", "active", "retired"]
StateKind = Literal["generation_zero", "active", "provisional"]

_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_UID = _SHA256
_ENTRY_ID = re.compile(r"entry-[0-9a-f]{24}\Z", re.ASCII)
_CORE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,511}\Z", re.ASCII)
_RUN_ID = re.compile(r"stv3-temperature-safe-evolve-v2-[A-Za-z0-9._:-]{8,160}\Z", re.ASCII)
_ANSWER_MAPPING = re.compile(
    r"\b(?:answer|option|choice|prediction)\s*(?:is|=|:|->|maps?\s+to)?\s*[ABCD]\b",
    re.IGNORECASE | re.ASCII,
)
_STANDALONE_LABEL = re.compile(r"(?m)^\s*(?:[-*]\s*)?[ABCD][.)]?\s*$", re.ASCII)


class SafeArtifactError(RuntimeError):
    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class CanonicalEntryV2(_FrozenModel):
    entry_id: str
    entry_type: Literal["category_rule", "reasoning_step"]
    content: str = Field(min_length=8, max_length=900)
    applicability: str = Field(min_length=3, max_length=500)
    exclusion_conditions: str = Field(min_length=3, max_length=500)
    support_uid_refs: tuple[str, ...]
    contradiction_uid_refs: tuple[str, ...] = ()
    supporting_batches: tuple[int, ...]
    first_seen_batch: int = Field(ge=1, le=4)
    last_evaluated_block: int = Field(ge=1, le=6)
    positive_flip_refs: tuple[str, ...] = ()
    negative_flip_refs: tuple[str, ...] = ()
    support_count: int = Field(ge=0, le=100)
    contradiction_count: int = Field(ge=0, le=100)
    positive_flip_count: int = Field(ge=0, le=100)
    negative_flip_count: int = Field(ge=0, le=100)
    confidence: float = Field(ge=0.0, le=1.0)
    status: EntryStatus
    retirement_reason: str | None = Field(default=None, max_length=240)
    source_packet_sha256: str

    @field_validator("entry_id")
    @classmethod
    def _entry_id(cls, value: str) -> str:
        if _ENTRY_ID.fullmatch(value) is None:
            raise ValueError("entry ID is invalid")
        return value

    @field_validator(
        "support_uid_refs",
        "contradiction_uid_refs",
        "positive_flip_refs",
        "negative_flip_refs",
    )
    @classmethod
    def _uid_refs(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if (
            type(values) is not tuple
            or tuple(sorted(values)) != values
            or len(set(values)) != len(values)
            or any(_UID.fullmatch(value) is None for value in values)
        ):
            raise ValueError("entry UID references are invalid")
        return values

    @field_validator("supporting_batches")
    @classmethod
    def _batches(cls, values: tuple[int, ...]) -> tuple[int, ...]:
        if (
            not values
            or tuple(sorted(values)) != values
            or len(set(values)) != len(values)
            or any(type(value) is not int or not 1 <= value <= 4 for value in values)
        ):
            raise ValueError("supporting batches are invalid")
        return values

    @field_validator("source_packet_sha256")
    @classmethod
    def _source(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("source packet digest is invalid")
        return value

    @model_validator(mode="after")
    def _closure(self) -> CanonicalEntryV2:
        if (
            self.support_count != len(self.support_uid_refs)
            or self.contradiction_count != len(self.contradiction_uid_refs)
            or self.positive_flip_count != len(self.positive_flip_refs)
            or self.negative_flip_count != len(self.negative_flip_refs)
            or set(self.support_uid_refs) & set(self.contradiction_uid_refs)
            or set(self.positive_flip_refs) & set(self.negative_flip_refs)
            or self.first_seen_batch not in self.supporting_batches
            or self.last_evaluated_block < self.first_seen_batch
            or (self.status == "retired") != bool(self.retirement_reason)
            or _ANSWER_MAPPING.search(self.content)
            or _STANDALONE_LABEL.search(self.content)
            or _UID.search(self.content)
            or _UID.search(self.applicability)
            or _UID.search(self.exclusion_conditions)
        ):
            raise ValueError("canonical entry closure is invalid")
        if self.status != "retired" and self.support_count < 2:
            raise ValueError("runtime entry needs two independent supports")
        if self.entry_type == "category_rule" and self.content.lstrip().startswith(
            tuple("123456789")
        ):
            raise ValueError("memory entry looks like a workflow step")
        return self

    def content_identity(self) -> str:
        return sha256_bytes(
            canonical_json_bytes(
                {
                    "entry_type": self.entry_type,
                    "content": self.content,
                    "applicability": self.applicability,
                    "exclusion_conditions": self.exclusion_conditions,
                }
            )
        )

    def __repr__(self) -> str:
        return f"CanonicalEntryV2(entry_id={self.entry_id!r}, <content-and-UIDs-redacted>)"

    __str__ = __repr__


class SafeArtifactStateV2(_FrozenModel):
    schema_version: Literal["TemperatureSafeArtifactStateV2"] = "TemperatureSafeArtifactStateV2"
    run_id: str
    fold_id: Literal["R0", "R1", "R2", "R3"]
    selected_target: SelectedTarget
    state_kind: StateKind
    batch_index: int = Field(ge=0, le=4)
    predecessor_state_sha256: str | None
    prior_evidence_sha256: str | None
    source_packet_sha256: str | None
    entries: tuple[CanonicalEntryV2, ...] = ()
    projection: str
    projection_sha256: str
    projection_utf8_bytes: int = Field(ge=0, le=8192)
    core_artifact_id: str | None = None
    core_job_id: str | None = None
    core_validation_receipt_sha256: str | None = None
    core_payload_sha256: str | None = None
    core_payload_utf8_bytes: int | None = Field(default=None, ge=1, le=8192)
    promoted: bool = False

    @field_validator("run_id")
    @classmethod
    def _run(cls, value: str) -> str:
        if _RUN_ID.fullmatch(value) is None:
            raise ValueError("run ID is invalid")
        return value

    @field_validator(
        "predecessor_state_sha256",
        "prior_evidence_sha256",
        "source_packet_sha256",
        "core_validation_receipt_sha256",
        "core_payload_sha256",
    )
    @classmethod
    def _digest_fields(cls, value: str | None) -> str | None:
        if value is not None and _SHA256.fullmatch(value) is None:
            raise ValueError("state digest field is invalid")
        return value

    @field_validator("core_artifact_id", "core_job_id")
    @classmethod
    def _core_ids(cls, value: str | None) -> str | None:
        if value is not None and _CORE_ID.fullmatch(value) is None:
            raise ValueError("Core ID is invalid")
        return value

    @model_validator(mode="after")
    def _closure(self) -> SafeArtifactStateV2:
        encoded = self.projection.encode("utf-8")
        if (
            len(encoded) != self.projection_utf8_bytes
            or sha256_bytes(encoded) != self.projection_sha256
            or tuple(sorted(self.entries, key=lambda item: item.entry_id)) != self.entries
            or len({entry.entry_id for entry in self.entries}) != len(self.entries)
            or len([entry for entry in self.entries if entry.status in {"active", "provisional"}])
            > 6
            or any(
                entry.entry_type
                != ("category_rule" if self.selected_target == "text_memory" else "reasoning_step")
                for entry in self.entries
            )
        ):
            raise ValueError("artifact state content closure is invalid")
        core_values = (
            self.core_artifact_id,
            self.core_job_id,
            self.core_validation_receipt_sha256,
            self.core_payload_sha256,
            self.core_payload_utf8_bytes,
        )
        if self.state_kind == "generation_zero":
            if (
                self.batch_index != 0
                or self.predecessor_state_sha256 is not None
                or self.prior_evidence_sha256 is not None
                or self.source_packet_sha256 is not None
                or self.entries
                or self.projection
                or any(core_values)
                or self.promoted
            ):
                raise ValueError("generation-zero state is not empty")
        else:
            if (
                self.batch_index < 1
                or self.predecessor_state_sha256 is None
                or self.source_packet_sha256 is None
                or any(value is None for value in core_values)
                or (self.state_kind == "active") != self.promoted
                or (
                    self.state_kind == "active"
                    and any(entry.status == "provisional" for entry in self.entries)
                )
            ):
                raise ValueError("successor state closure is invalid")
        expected = render_full_projection_v2(
            selected_target=self.selected_target,
            entries=self.runtime_entries,
        )
        if expected != self.projection:
            raise ValueError("artifact projection differs from canonical entries")
        return self

    @property
    def runtime_entries(self) -> tuple[CanonicalEntryV2, ...]:
        statuses = {"active", "provisional"} if self.state_kind == "provisional" else {"active"}
        return tuple(entry for entry in self.entries if entry.status in statuses)

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.model_dump(mode="json")))

    @classmethod
    def generation_zero(
        cls,
        *,
        run_id: str,
        fold_id: Literal["R0", "R1", "R2", "R3"],
        selected_target: SelectedTarget,
    ) -> SafeArtifactStateV2:
        empty = ""
        return cls(
            run_id=run_id,
            fold_id=fold_id,
            selected_target=selected_target,
            state_kind="generation_zero",
            batch_index=0,
            predecessor_state_sha256=None,
            prior_evidence_sha256=None,
            source_packet_sha256=None,
            entries=(),
            projection=empty,
            projection_sha256=sha256_bytes(empty.encode()),
            projection_utf8_bytes=0,
        )

    def __repr__(self) -> str:
        return (
            "SafeArtifactStateV2(<content-and-lineage-redacted>; "
            f"kind={self.state_kind!r}, batch={self.batch_index})"
        )

    __str__ = __repr__


def render_full_projection_v2(
    *, selected_target: SelectedTarget, entries: tuple[CanonicalEntryV2, ...]
) -> str:
    """Render the Core-owned full candidate artifact without truncation."""

    if selected_target == "text_memory":
        if not entries:
            return ""
        lines = ["# Temperature Prediction Memory", "", "## Conditional Rules", ""]
        for entry in entries:
            lines.extend(
                (
                    f"- {entry.content}",
                    f"  - Applies: {entry.applicability}",
                    f"  - Exclude: {entry.exclusion_conditions}",
                )
            )
    else:
        if not entries:
            return ""
        lines = ["# Temperature Prediction Skill", "", "## Workflow", ""]
        for index, entry in enumerate(entries, start=1):
            lines.append(f"{index}. {entry.content}")
            lines.append(f"   Apply when: {entry.applicability}")
            lines.append(f"   Skip when: {entry.exclusion_conditions}")
        lines.extend(
            (
                "",
                "## Validation Checks",
                "",
                "- Recheck units, regime, and explicit operational constraints before answering.",
            )
        )
    result = "\n".join(lines).rstrip() + "\n"
    maximum = 4096 if selected_target == "text_memory" else 1536
    if len(result.encode("utf-8")) > maximum:
        raise SafeArtifactError("SAFE_ARTIFACT_FULL_PROJECTION_TOO_LARGE")
    return result


def validate_reflector_entries_v2(
    entries: tuple[CanonicalEntryV2, ...],
    *,
    selected_target: SelectedTarget,
    allowed_train_uids: frozenset[str],
    forbidden_normalized_questions: tuple[str, ...],
) -> None:
    """Apply controller-side schema, evidence, role and leakage validation."""

    if type(entries) is not tuple or any(type(value) is not CanonicalEntryV2 for value in entries):
        raise TypeError("entries must use exact canonical DTOs")
    if len({entry.entry_id for entry in entries}) != len(entries):
        raise SafeArtifactError("SAFE_ARTIFACT_DUPLICATE_ENTRY_ID")
    expected_type = "category_rule" if selected_target == "text_memory" else "reasoning_step"
    for entry in entries:
        refs = {
            *entry.support_uid_refs,
            *entry.contradiction_uid_refs,
            *entry.positive_flip_refs,
            *entry.negative_flip_refs,
        }
        if not refs.issubset(allowed_train_uids):
            raise SafeArtifactError("SAFE_ARTIFACT_EVIDENCE_OUTSIDE_TRAIN")
        if entry.entry_type != expected_type:
            raise SafeArtifactError("SAFE_ARTIFACT_TARGET_ROLE_VIOLATION")
        normalized_content = " ".join(entry.content.casefold().split())
        if any(
            len(question) >= 32
            and (normalized_content in question or question in normalized_content)
            for question in forbidden_normalized_questions
        ):
            raise SafeArtifactError("SAFE_ARTIFACT_QUESTION_MEMORIZATION")
    runtime = tuple(entry for entry in entries if entry.status != "retired")
    if len(runtime) > 6:
        raise SafeArtifactError("SAFE_ARTIFACT_ACTIVE_ENTRY_LIMIT")
    render_full_projection_v2(selected_target=selected_target, entries=runtime)


__all__ = [
    "CanonicalEntryV2",
    "EntryStatus",
    "SafeArtifactError",
    "SafeArtifactStateV2",
    "SelectedTarget",
    "StateKind",
    "render_full_projection_v2",
    "validate_reflector_entries_v2",
]
