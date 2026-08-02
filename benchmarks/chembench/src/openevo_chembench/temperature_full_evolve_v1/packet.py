"""Private batch-supervision packet for Temperature full-evolve v1.

The controller keeps execution provenance in :class:`BatchSupervisedPacketV1`
but projects only the fields needed for supervised reflection into
:class:`ReflectorVisibleBatchPacketV1`.  Test identities are never serialized
into either packet.  A separate, non-serializable seal proves that the frozen
Train and Test UID sets are disjoint before a prompt can be rendered.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openevo_chembench.chembench4k_evaluation import PrivateChemBench4KEvaluation
from openevo_chembench.chembench4k_models import PrivateChemBench4KTask
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    sha256_bytes,
)
from openevo_chembench.temperature_full_evolve_v1.config import (
    BATCH_SIZE,
    CATEGORY,
    PROTOCOL_ID,
    TEST_COUNT,
    TRAIN_COUNT,
)
from openevo_chembench.temperature_full_evolve_v1.evidence import (
    RuleEvidenceIndexV1,
    evidence_uid_closure,
)

PACKET_SCHEMA = "TemperatureBatchSupervisedPacketV1"
VISIBLE_PACKET_SCHEMA = "TemperatureReflectorVisibleBatchPacketV1"
PACKET_MAX_UTF8_BYTES = 512 * 1024
VISIBLE_PACKET_MAX_UTF8_BYTES = 448 * 1024
CASE_COMPLETION_MAX_UTF8_BYTES = 8 * 1024
QUESTION_MAX_UTF8_BYTES = 32 * 1024
OPTION_MAX_UTF8_BYTES = 8 * 1024
# A state above the 6 KiB target must be compressed before it is admitted as
# the current batch context.  The separate 8 KiB hard ceiling remains a Core
# generation failure boundary, not a looser prompt-admission budget.
ARTIFACT_TOTAL_MAX_UTF8_BYTES = 6 * 1024
TARGET_ORDER = ("text_memory", "skill_bundle", "agent_system")
TARGET_MAX_UTF8_BYTES = {
    "text_memory": 4096,
    "skill_bundle": 1536,
    "agent_system": 1024,
}

_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_GIT_COMMIT = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_RUNTIME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,191}\Z", re.ASCII)
_ARTIFACT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}\Z", re.ASCII)
_CHOICES = ("A", "B", "C", "D")
_SEAL_TOKEN = object()


class TemperatureBatchPacketError(RuntimeError):
    """Closed packet construction or Train/Test isolation failure."""


class _PrivateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<private-batch-data>)"

    __str__ = __repr__


def _bounded_text(
    value: object,
    *,
    field_name: str,
    maximum_utf8_bytes: int,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str or "\x00" in value:
        raise ValueError(f"{field_name} must be NUL-free text")
    if (not allow_empty and not value.strip()) or len(value.encode("utf-8")) > maximum_utf8_bytes:
        raise ValueError(f"{field_name} is empty or exceeds its UTF-8 budget")
    return value


def _require_sha256(value: object, field_name: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256")
    return value


def _uid_set_sha256(values: frozenset[str]) -> str:
    return sha256_bytes(canonical_json_bytes(sorted(values)))


class ReflectorTrainCaseV1(_PrivateModel):
    """Exactly the current Train evidence visible to the Reflector."""

    uid: str
    batch_position: int = Field(ge=1, le=BATCH_SIZE)
    question: str
    options: tuple[str, str, str, str]
    ground_truth: Literal["A", "B", "C", "D"]
    correct_option: str
    candidate_completion: str
    official_prediction: str | None
    official_parse_status: Literal[
        "parsed",
        "no_uppercase",
        "first_uppercase_not_choice",
    ]
    strict_prediction: Literal["A", "B", "C", "D"] | None
    strict_parse_status: Literal["parsed", "strict_format_mismatch"]
    correct: bool

    @field_validator("uid")
    @classmethod
    def _uid(cls, value: str) -> str:
        return _require_sha256(value, "case UID")

    @field_validator("question")
    @classmethod
    def _question(cls, value: str) -> str:
        return _bounded_text(
            value,
            field_name="question",
            maximum_utf8_bytes=QUESTION_MAX_UTF8_BYTES,
        )

    @field_validator("options")
    @classmethod
    def _options(cls, values: tuple[str, str, str, str]) -> tuple[str, str, str, str]:
        if type(values) is not tuple or len(values) != 4:
            raise ValueError("options must contain exactly A, B, C, and D")
        return tuple(
            _bounded_text(
                value,
                field_name=f"option {label}",
                maximum_utf8_bytes=OPTION_MAX_UTF8_BYTES,
            )
            for label, value in zip(_CHOICES, values, strict=True)
        )  # type: ignore[return-value]

    @field_validator("candidate_completion")
    @classmethod
    def _completion(cls, value: str) -> str:
        return _bounded_text(
            value,
            field_name="candidate completion",
            maximum_utf8_bytes=CASE_COMPLETION_MAX_UTF8_BYTES,
        )

    @field_validator("official_prediction")
    @classmethod
    def _official_prediction(cls, value: str | None) -> str | None:
        if value is not None and (len(value) != 1 or not value.isascii() or not value.isupper()):
            raise ValueError("official prediction must be one ASCII uppercase character or null")
        return value

    @model_validator(mode="after")
    def _derived(self) -> ReflectorTrainCaseV1:
        correct_option = self.options[_CHOICES.index(self.ground_truth)]
        expected_official_status = (
            "no_uppercase"
            if self.official_prediction is None
            else "parsed"
            if self.official_prediction in _CHOICES
            else "first_uppercase_not_choice"
        )
        if (
            self.correct_option != correct_option
            or self.correct != (self.official_prediction == self.ground_truth)
            or self.official_parse_status != expected_official_status
            or (self.strict_parse_status == "parsed") != (self.strict_prediction is not None)
        ):
            raise ValueError("case answer, parser, or correctness fields are inconsistent")
        return self


class BatchTrainCaseV1(ReflectorTrainCaseV1):
    """Visible current-Train evidence plus immutable execution provenance."""

    train_ordinal: int = Field(ge=0, lt=TRAIN_COUNT)
    task_request_sha256: str
    accepted_completion_sha256: str
    transcript_sha256: str

    @field_validator(
        "task_request_sha256",
        "accepted_completion_sha256",
        "transcript_sha256",
    )
    @classmethod
    def _digests(cls, value: str, info: Any) -> str:
        return _require_sha256(value, str(info.field_name))

    @model_validator(mode="after")
    def _completion_digest(self) -> BatchTrainCaseV1:
        if hashlib.sha256(self.candidate_completion.encode("utf-8")).hexdigest() != (
            self.accepted_completion_sha256
        ):
            raise ValueError("accepted completion digest does not match the completion")
        return self

    @classmethod
    def from_evaluation(
        cls,
        *,
        task: PrivateChemBench4KTask,
        evaluation: PrivateChemBench4KEvaluation,
        train_ordinal: int,
        batch_position: int,
        task_request_sha256: str,
        transcript_sha256: str,
    ) -> BatchTrainCaseV1:
        if type(task) is not PrivateChemBench4KTask or type(evaluation) is not (
            PrivateChemBench4KEvaluation
        ):
            raise TypeError("case construction requires exact private task and evaluation DTOs")
        if (
            task.uid != evaluation.task_uid
            or task.category != CATEGORY
            or evaluation.category != CATEGORY
            or task.target != evaluation.target
        ):
            raise TemperatureBatchPacketError("CASE_EVALUATION_IDENTITY_MISMATCH")
        completion = evaluation.raw_completion
        return cls(
            uid=task.uid,
            batch_position=batch_position,
            question=task.question,
            options=(task.A, task.B, task.C, task.D),
            ground_truth=task.target,
            correct_option=(task.A, task.B, task.C, task.D)[_CHOICES.index(task.target)],
            candidate_completion=completion,
            official_prediction=evaluation.official.prediction or None,
            official_parse_status=evaluation.official.status.value,
            strict_prediction=evaluation.strict.prediction,  # type: ignore[arg-type]
            strict_parse_status=evaluation.strict.status.value,
            correct=evaluation.correct,
            train_ordinal=train_ordinal,
            task_request_sha256=task_request_sha256,
            accepted_completion_sha256=hashlib.sha256(completion.encode("utf-8")).hexdigest(),
            transcript_sha256=transcript_sha256,
        )

    def to_reflector_case(self) -> ReflectorTrainCaseV1:
        return ReflectorTrainCaseV1.model_validate(
            {
                key: value
                for key, value in self.model_dump(mode="json").items()
                if key
                not in {
                    "train_ordinal",
                    "task_request_sha256",
                    "accepted_completion_sha256",
                    "transcript_sha256",
                }
            }
        )


class ReflectorArtifactContentV1(_PrivateModel):
    target_id: Literal["text_memory", "skill_bundle", "agent_system"]
    content: str

    @model_validator(mode="after")
    def _budget(self) -> ReflectorArtifactContentV1:
        _bounded_text(
            self.content,
            field_name=f"{self.target_id} content",
            maximum_utf8_bytes=TARGET_MAX_UTF8_BYTES[self.target_id],
            allow_empty=True,
        )
        return self


class ReflectorArtifactSnapshotV1(ReflectorArtifactContentV1):
    """Current Core artifact content with content-free lineage identities."""

    artifact_id: str | None
    predecessor_artifact_id: str | None
    artifact_payload_sha256: str | None
    context_resolution_digest: str | None
    resolved_content_sha256: str
    utf8_byte_count: int = Field(ge=0)

    @field_validator("artifact_id", "predecessor_artifact_id")
    @classmethod
    def _artifact_ids(cls, value: str | None) -> str | None:
        if value is not None and _ARTIFACT_ID.fullmatch(value) is None:
            raise ValueError("artifact identity is invalid")
        return value

    @field_validator(
        "artifact_payload_sha256",
        "context_resolution_digest",
        "resolved_content_sha256",
    )
    @classmethod
    def _artifact_digests(cls, value: str | None, info: Any) -> str | None:
        if value is None and info.field_name != "resolved_content_sha256":
            return None
        return _require_sha256(value, str(info.field_name))

    @model_validator(mode="after")
    def _identity(self) -> ReflectorArtifactSnapshotV1:
        encoded = self.content.encode("utf-8")
        empty = not encoded
        if (
            self.utf8_byte_count != len(encoded)
            or self.resolved_content_sha256 != hashlib.sha256(encoded).hexdigest()
            or empty
            != (
                self.artifact_id is None
                and self.predecessor_artifact_id is None
                and self.artifact_payload_sha256 is None
                and self.context_resolution_digest is None
            )
            or (
                not empty
                and (
                    self.artifact_id is None
                    or self.artifact_payload_sha256 is None
                    or self.context_resolution_digest is None
                )
            )
        ):
            raise ValueError("artifact content and identity fields are inconsistent")
        return self

    @property
    def is_generation_zero(self) -> bool:
        return self.artifact_id is None and self.content == ""

    @classmethod
    def generation_zero(
        cls,
        target_id: Literal["text_memory", "skill_bundle", "agent_system"],
    ) -> ReflectorArtifactSnapshotV1:
        return cls(
            target_id=target_id,
            content="",
            artifact_id=None,
            predecessor_artifact_id=None,
            artifact_payload_sha256=None,
            context_resolution_digest=None,
            resolved_content_sha256=hashlib.sha256(b"").hexdigest(),
            utf8_byte_count=0,
        )

    def to_reflector_content(self) -> ReflectorArtifactContentV1:
        return ReflectorArtifactContentV1(target_id=self.target_id, content=self.content)


class BatchAggregateDiagnosticV1(_PrivateModel):
    """Aggregate-only pre/post diagnostic from one earlier Train batch."""

    batch_index: int = Field(ge=1, le=TRAIN_COUNT // BATCH_SIZE)
    task_count: Literal[25] = BATCH_SIZE
    pre_correct: int = Field(ge=0, le=BATCH_SIZE)
    post_correct: int = Field(ge=0, le=BATCH_SIZE)
    both_correct: int = Field(ge=0, le=BATCH_SIZE)
    pre_only_correct: int = Field(ge=0, le=BATCH_SIZE)
    post_only_correct: int = Field(ge=0, le=BATCH_SIZE)
    both_wrong: int = Field(ge=0, le=BATCH_SIZE)
    pre_parser_success: int = Field(ge=0, le=BATCH_SIZE)
    post_parser_success: int = Field(ge=0, le=BATCH_SIZE)
    accuracy_delta_percentage_points: float
    mcnemar_exact_p: float = Field(ge=0.0, le=1.0)
    artifact_total_utf8_bytes: int = Field(ge=0, le=ARTIFACT_TOTAL_MAX_UTF8_BYTES)
    candidate_call_count: Literal[50] = 50
    reflector_accepted_synthesis_count: Literal[1] = 1
    core_job_count: Literal[3] = 3

    @model_validator(mode="after")
    def _transitions(self) -> BatchAggregateDiagnosticV1:
        expected_delta = 100.0 * (self.post_correct - self.pre_correct) / BATCH_SIZE
        if (
            self.both_correct + self.pre_only_correct + self.post_only_correct + self.both_wrong
            != BATCH_SIZE
            or self.both_correct + self.pre_only_correct != self.pre_correct
            or self.both_correct + self.post_only_correct != self.post_correct
            or abs(self.accuracy_delta_percentage_points - expected_delta) > 1e-12
        ):
            raise ValueError("batch aggregate transitions are inconsistent")
        return self


class ReflectorVisibleBatchPacketV1(_PrivateModel):
    """The strict Train-only payload serialized into the model instruction."""

    schema_version: Literal["TemperatureReflectorVisibleBatchPacketV1"] = VISIBLE_PACKET_SCHEMA
    protocol_id: Literal["chembench_temperature_full_evolve_v1"] = PROTOCOL_ID
    category: Literal["Temperature_Prediction"] = CATEGORY
    batch_index: int = Field(ge=1, le=TRAIN_COUNT // BATCH_SIZE)
    batch_size: Literal[25] = BATCH_SIZE
    current_train_cases: tuple[ReflectorTrainCaseV1, ...] = Field(
        min_length=BATCH_SIZE,
        max_length=BATCH_SIZE,
    )
    current_artifacts: tuple[ReflectorArtifactContentV1, ...] = Field(
        min_length=3,
        max_length=3,
    )
    prior_evidence: RuleEvidenceIndexV1
    prior_batch_diagnostics: tuple[BatchAggregateDiagnosticV1, ...]

    @model_validator(mode="after")
    def _closed(self) -> ReflectorVisibleBatchPacketV1:
        if (
            tuple(case.batch_position for case in self.current_train_cases)
            != tuple(range(1, BATCH_SIZE + 1))
            or len({case.uid for case in self.current_train_cases}) != BATCH_SIZE
            or tuple(item.target_id for item in self.current_artifacts) != TARGET_ORDER
            or sum(len(item.content.encode("utf-8")) for item in self.current_artifacts)
            > ARTIFACT_TOTAL_MAX_UTF8_BYTES
            or self.prior_evidence.batch_index != self.batch_index - 1
            or tuple(item.batch_index for item in self.prior_batch_diagnostics)
            != tuple(range(1, self.batch_index))
        ):
            raise ValueError("visible batch packet sequence or target closure is invalid")
        if self.utf8_byte_count > VISIBLE_PACKET_MAX_UTF8_BYTES:
            raise ValueError("visible batch packet exceeds its UTF-8 budget")
        return self

    @property
    def utf8_byte_count(self) -> int:
        return len(canonical_json_bytes(self.model_dump(mode="json")))

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.model_dump(mode="json")))


class BatchSupervisedPacketV1(_PrivateModel):
    """Full private batch packet retained by the controller for audit and lineage."""

    schema_version: Literal["TemperatureBatchSupervisedPacketV1"] = PACKET_SCHEMA
    protocol_id: Literal["chembench_temperature_full_evolve_v1"] = PROTOCOL_ID
    run_id: str
    source_commit: str
    split_sha256: str
    config_sha256: str
    dataset_sha256: str
    batch_index: int = Field(ge=1, le=TRAIN_COUNT // BATCH_SIZE)
    batch_size: Literal[25] = BATCH_SIZE
    all_train_uid_set_sha256: str
    seen_train_uid_set_sha256: str
    current_batch_uid_order_sha256: str
    current_train_cases: tuple[BatchTrainCaseV1, ...] = Field(
        min_length=BATCH_SIZE,
        max_length=BATCH_SIZE,
    )
    current_artifacts: tuple[ReflectorArtifactSnapshotV1, ...] = Field(
        min_length=3,
        max_length=3,
    )
    prior_evidence: RuleEvidenceIndexV1
    prior_batch_diagnostics: tuple[BatchAggregateDiagnosticV1, ...]

    @field_validator("run_id")
    @classmethod
    def _run_id(cls, value: str) -> str:
        if _RUNTIME_ID.fullmatch(value) is None:
            raise ValueError("run ID is invalid")
        return value

    @field_validator("source_commit")
    @classmethod
    def _source_commit(cls, value: str) -> str:
        if _GIT_COMMIT.fullmatch(value) is None:
            raise ValueError("source_commit must be a lowercase Git commit")
        return value

    @field_validator(
        "split_sha256",
        "config_sha256",
        "dataset_sha256",
        "all_train_uid_set_sha256",
        "seen_train_uid_set_sha256",
        "current_batch_uid_order_sha256",
    )
    @classmethod
    def _identity_digests(cls, value: str, info: Any) -> str:
        return _require_sha256(value, str(info.field_name))

    @model_validator(mode="after")
    def _closed(self) -> BatchSupervisedPacketV1:
        expected_ordinals = tuple(
            range((self.batch_index - 1) * BATCH_SIZE, self.batch_index * BATCH_SIZE)
        )
        current_uids = tuple(case.uid for case in self.current_train_cases)
        if (
            tuple(case.batch_position for case in self.current_train_cases)
            != tuple(range(1, BATCH_SIZE + 1))
            or tuple(case.train_ordinal for case in self.current_train_cases) != expected_ordinals
            or len(set(current_uids)) != BATCH_SIZE
            or self.current_batch_uid_order_sha256
            != sha256_bytes(canonical_json_bytes(list(current_uids)))
            or tuple(item.target_id for item in self.current_artifacts) != TARGET_ORDER
            or self.prior_evidence.batch_index != self.batch_index - 1
            or tuple(item.batch_index for item in self.prior_batch_diagnostics)
            != tuple(range(1, self.batch_index))
            or (
                self.batch_index == 1
                and not all(item.is_generation_zero for item in self.current_artifacts)
            )
            or (
                self.batch_index > 1
                and any(item.is_generation_zero for item in self.current_artifacts)
            )
        ):
            raise ValueError("private batch packet sequence, lineage, or C0 state is invalid")
        if self.utf8_byte_count > PACKET_MAX_UTF8_BYTES:
            raise ValueError("private batch packet exceeds its UTF-8 budget")
        # Construct the visible DTO now so its independent schema and budget are
        # checked before any prompt can be built.
        self.to_reflector_packet()
        return self

    @property
    def utf8_byte_count(self) -> int:
        return len(canonical_json_bytes(self.model_dump(mode="json")))

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.model_dump(mode="json")))

    def to_reflector_packet(self) -> ReflectorVisibleBatchPacketV1:
        return ReflectorVisibleBatchPacketV1(
            batch_index=self.batch_index,
            current_train_cases=tuple(
                case.to_reflector_case() for case in self.current_train_cases
            ),
            current_artifacts=tuple(
                artifact.to_reflector_content() for artifact in self.current_artifacts
            ),
            prior_evidence=self.prior_evidence,
            prior_batch_diagnostics=self.prior_batch_diagnostics,
        )


def build_batch_supervised_packet_v1(
    *,
    run_id: str,
    source_commit: str,
    split_sha256: str,
    config_sha256: str,
    dataset_sha256: str,
    batch_index: int,
    current_train_cases: tuple[BatchTrainCaseV1, ...],
    current_artifacts: tuple[ReflectorArtifactSnapshotV1, ...],
    prior_evidence: RuleEvidenceIndexV1,
    prior_batch_diagnostics: tuple[BatchAggregateDiagnosticV1, ...],
    all_train_uids: frozenset[str],
    seen_train_uids: frozenset[str],
) -> BatchSupervisedPacketV1:
    """Close the 25-item packet while keeping future Train items out of evidence."""

    if (
        type(all_train_uids) is not frozenset
        or type(seen_train_uids) is not frozenset
        or len(all_train_uids) != TRAIN_COUNT
        or len(seen_train_uids) != batch_index * BATCH_SIZE
        or any(_SHA256.fullmatch(uid) is None for uid in (*all_train_uids, *seen_train_uids))
        or not seen_train_uids <= all_train_uids
    ):
        raise TemperatureBatchPacketError("TRAIN_UID_SCOPE_INVALID")
    current_uids = frozenset(case.uid for case in current_train_cases)
    prior_seen = seen_train_uids - current_uids
    if (
        len(current_uids) != BATCH_SIZE
        or not current_uids <= seen_train_uids
        or not evidence_uid_closure(prior_evidence) <= prior_seen
    ):
        raise TemperatureBatchPacketError("BATCH_OR_EVIDENCE_UID_SCOPE_INVALID")
    return BatchSupervisedPacketV1(
        run_id=run_id,
        source_commit=source_commit,
        split_sha256=split_sha256,
        config_sha256=config_sha256,
        dataset_sha256=dataset_sha256,
        batch_index=batch_index,
        all_train_uid_set_sha256=_uid_set_sha256(all_train_uids),
        seen_train_uid_set_sha256=_uid_set_sha256(seen_train_uids),
        current_batch_uid_order_sha256=sha256_bytes(
            canonical_json_bytes([case.uid for case in current_train_cases])
        ),
        current_train_cases=current_train_cases,
        current_artifacts=current_artifacts,
        prior_evidence=prior_evidence,
        prior_batch_diagnostics=prior_batch_diagnostics,
    )


@dataclass(frozen=True, slots=True, init=False, repr=False)
class SealedBatchSupervisedPacketV1:
    """Non-serializable proof that Test UIDs cannot enter reflection."""

    packet: BatchSupervisedPacketV1
    test_isolation_receipt_sha256: str
    _all_train_uids: frozenset[str]
    _seen_train_uids: frozenset[str]
    _test_uids: frozenset[str]

    def __init__(
        self,
        *,
        packet: BatchSupervisedPacketV1,
        test_isolation_receipt_sha256: str,
        all_train_uids: frozenset[str],
        seen_train_uids: frozenset[str],
        test_uids: frozenset[str],
        _issuer_token: object,
    ) -> None:
        if _issuer_token is not _SEAL_TOKEN:
            raise TypeError("Test-isolated packet seals are issued only by the trusted factory")
        object.__setattr__(self, "packet", packet)
        object.__setattr__(self, "test_isolation_receipt_sha256", test_isolation_receipt_sha256)
        object.__setattr__(self, "_all_train_uids", all_train_uids)
        object.__setattr__(self, "_seen_train_uids", seen_train_uids)
        object.__setattr__(self, "_test_uids", test_uids)

    def __repr__(self) -> str:
        return "SealedBatchSupervisedPacketV1(<private-train-test-closure>)"

    __str__ = __repr__

    @property
    def current_batch_uids(self) -> frozenset[str]:
        return frozenset(case.uid for case in self.packet.current_train_cases)

    def require_reflector_evidence_scope(self, referenced_uids: frozenset[str]) -> None:
        if (
            type(referenced_uids) is not frozenset
            or referenced_uids & self._test_uids
            or not referenced_uids <= self._seen_train_uids
        ):
            raise TemperatureBatchPacketError("REFLECTOR_TEST_OR_UNSEEN_UID_REFERENCE")


def seal_batch_supervised_packet_v1(
    packet: BatchSupervisedPacketV1,
    *,
    all_train_uids: frozenset[str],
    seen_train_uids: frozenset[str],
    test_uids: frozenset[str],
) -> SealedBatchSupervisedPacketV1:
    """Issue a seal only after exact Train/Test UID isolation is proven."""

    if type(packet) is not BatchSupervisedPacketV1:
        raise TypeError("packet must be an exact BatchSupervisedPacketV1")
    if (
        type(all_train_uids) is not frozenset
        or type(seen_train_uids) is not frozenset
        or type(test_uids) is not frozenset
        or len(all_train_uids) != TRAIN_COUNT
        or len(test_uids) != TEST_COUNT
        or len(seen_train_uids) != packet.batch_index * BATCH_SIZE
        or any(
            _SHA256.fullmatch(uid) is None
            for uid in (*all_train_uids, *seen_train_uids, *test_uids)
        )
        or packet.all_train_uid_set_sha256 != _uid_set_sha256(all_train_uids)
        or packet.seen_train_uid_set_sha256 != _uid_set_sha256(seen_train_uids)
        or not seen_train_uids <= all_train_uids
        or bool(all_train_uids & test_uids)
        or bool(frozenset(case.uid for case in packet.current_train_cases) & test_uids)
        or bool(evidence_uid_closure(packet.prior_evidence) & test_uids)
    ):
        raise TemperatureBatchPacketError("TRAIN_TEST_UID_ISOLATION_VIOLATION")
    receipt = {
        "schema_version": "TemperatureBatchTestIsolationReceiptV1",
        "packet_sha256": packet.digest,
        "all_train_uid_set_sha256": _uid_set_sha256(all_train_uids),
        "seen_train_uid_set_sha256": _uid_set_sha256(seen_train_uids),
        "test_uid_set_sha256": _uid_set_sha256(test_uids),
        "train_count": len(all_train_uids),
        "seen_train_count": len(seen_train_uids),
        "test_count": len(test_uids),
        "overlap_count": 0,
        "test_uids_serialized_to_reflector": False,
    }
    return SealedBatchSupervisedPacketV1(
        packet=packet,
        test_isolation_receipt_sha256=sha256_bytes(canonical_json_bytes(receipt)),
        all_train_uids=all_train_uids,
        seen_train_uids=seen_train_uids,
        test_uids=test_uids,
        _issuer_token=_SEAL_TOKEN,
    )


__all__ = [
    "ARTIFACT_TOTAL_MAX_UTF8_BYTES",
    "PACKET_MAX_UTF8_BYTES",
    "PACKET_SCHEMA",
    "VISIBLE_PACKET_MAX_UTF8_BYTES",
    "VISIBLE_PACKET_SCHEMA",
    "BatchAggregateDiagnosticV1",
    "BatchSupervisedPacketV1",
    "BatchTrainCaseV1",
    "ReflectorArtifactContentV1",
    "ReflectorArtifactSnapshotV1",
    "ReflectorTrainCaseV1",
    "ReflectorVisibleBatchPacketV1",
    "SealedBatchSupervisedPacketV1",
    "TemperatureBatchPacketError",
    "build_batch_supervised_packet_v1",
    "seal_batch_supervised_packet_v1",
]
