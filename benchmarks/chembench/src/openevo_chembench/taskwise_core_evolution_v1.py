"""Global taskwise online evolution through the real OpenEvo Core lifecycle.

Each update is a complete current-task trajectory prefix.  The bridge ingests
that prefix as Core events, creates a new immutable dataset artifact and
plan-bound job, dispatches the registered ``text_memory_expel_reflector``
method, accepts only its typed Core artifact, validates and promotes it, and
finally resolves the artifact through Core context projection.

The lineage is one global stream across tasks.  Task ``N`` update 1 must name
task ``N-1`` update 2 as its predecessor.  Adapter-local reflector/artifact
classes are intentionally absent.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openevo.evolution.context_materialization import MaterializedContext
from openevo.evolution.context_projection import ContextProjectionResolveRequest
from openevo.evolution.framework import (
    DescriptorKind,
    EvolutionExecutionProfile,
    EvolutionTargetSelection,
    RuntimeDestinationRoots,
    TargetConsumptionLimits,
    canonical_digest,
    canonical_json,
    load_verified_framework_registry,
)
from openevo.evolution.framework.builtins import (
    VerifiedExecutableRegistry,
    require_verified_executable_registry,
)
from openevo.evolution.models import (
    ArtifactResponse,
    ArtifactState,
    ArtifactType,
    DatasetCreateRequest,
    EventIngestRequest,
)
from openevo.evolution.planned_jobs import (
    PlanBoundJobCreateRequest,
    PlannedInputBinding,
)
from openevo.evolution.store import EvolutionStore
from openevo.evolution.worker import run_once

from openevo_chembench.core_evolution_v2 import (
    CoreArtifactValidationReceiptV2,
    _StoreWorkerClient,
    _read_bounded_regular_file,
    _resolved_text_memory,
    _safe_core_file_path,
)
from openevo_chembench.frozen_runtime_v2 import (
    CoreResolvedTextMemoryV2,
    _issue_core_resolved_text_memory_v2,
)
from openevo_chembench.reflector_execution_boundary_v2 import (
    ReflectorBoundaryStatusV2,
    ReflectorExecutionBoundaryV2,
    TASKWISE_SOURCE_SPLIT,
)
from openevo_chembench.taskwise_config_v1 import (
    TASKWISE_MEMORY_LIMITS_V1,
    TaskwiseMemoryLimitsV1,
)
from openevo_chembench.taskwise_feedback_v1 import (
    TaskwiseSafeSignalCodeV1,
)
from openevo_chembench.taskwise_trajectory_v1 import (
    TaskwiseTrajectoryV1,
    ordered_safe_feedback_digest,
    ordered_taskwise_trajectory_digest,
)


PROTOCOL_ID = "taskwise_online_evolution_v1"
BRIDGE_ID = "openevo_core_taskwise_text_memory_v1"
METHOD_ID = "text_memory_expel_reflector"
TARGET_ID = "text_memory"
MODEL = "gpt-5.5"
ABSOLUTE_MAX_MEMORY_FILE_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
_EVENT_TYPE = "openevo.session_completed"
_EVENT_SOURCE = "chembench4k.taskwise_core.v1"
_DATASET_PURPOSE = "chembench4k_taskwise_trajectory_v1"
_JOB_PREFIX = "chembench4k.taskwise.text_memory.v1"
_PRIVATE_INPUT_DIRECTORY = "private_taskwise_reflector_inputs"
_PRIVATE_FAILURE_DIRECTORY = "private_core_failure_diagnostics"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_CLOSED_CODE_RE = re.compile(r"[A-Z][A-Z0-9_]{0,95}\Z", re.ASCII)
_CORE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z", re.ASCII)
_DIAGNOSTIC_BASENAME_RE = re.compile(
    r"taskwise_core_failure_[0-9a-f]{32}\.json\Z",
    re.ASCII,
)
_CORE_FAILURE_STAGES = frozenset(
    {
        "TYPED_ARTIFACT",
        "ARTIFACT_LINEAGE",
        "ARTIFACT_VALIDATION",
        "ARTIFACT_PROMOTION",
        "CONTEXT_RESOLUTION",
        "PRIVATE_CHECKPOINT",
    }
)
_CORE_STAGE_ALLOWED_FINDINGS = {
    "TYPED_ARTIFACT": frozenset({"TASKWISE_TYPED_ARTIFACT_INVALID"}),
    "ARTIFACT_LINEAGE": frozenset(
        {
            "TASKWISE_CORE_LINEAGE_INVALID",
            "TASKWISE_PREDECESSOR_BINDING_INVALID",
        }
    ),
    "ARTIFACT_VALIDATION": frozenset({"TASKWISE_ARTIFACT_VALIDATION_FAILED"}),
    "ARTIFACT_PROMOTION": frozenset({"TASKWISE_ARTIFACT_PROMOTION_FAILED"}),
    "CONTEXT_RESOLUTION": frozenset({"TASKWISE_CONTEXT_RESOLUTION_FAILED"}),
    "PRIVATE_CHECKPOINT": frozenset(
        {
            "TASKWISE_PRIVATE_CHECKPOINT_FAILED",
            "TASKWISE_PRIVATE_CHECKPOINT_UNSAFE",
        }
    ),
}
_CORE_EXCEPTION_CLASSES = frozenset(
    {
        "TASKWISE_CORE_EVOLUTION_ERROR",
        "OS_ERROR",
        "TYPE_ERROR",
        "VALUE_ERROR",
        "RUNTIME_ERROR",
        "OTHER_ERROR",
    }
)
_MEMORY_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[^\x00-\x7f]|\S", re.ASCII)
_MEMORY_H1 = "# General Chemistry Memory"
_MEMORY_H2_RE = re.compile(r"## ([^\r\n]+)\Z", re.ASCII)
_MEMORY_BULLET_RE = re.compile(r"- \S", re.ASCII)
_MEMORY_CONTINUATION_RE = re.compile(r" {2,}\S", re.ASCII)
_PATH_OR_BENCHMARK_RE = re.compile(
    r"(?:chembench(?:4k)?|ai4chem|opencompass|"
    r"(?:^|[\\/])(?:dev|test|results?)[\\/]|"
    r"\.(?:json|jsonl|parquet|sqlite3?)\b)",
    re.IGNORECASE | re.MULTILINE,
)
_ANSWER_MAP_RE = re.compile(
    r"(?:the\s+)?(?:correct\s+)?answer\s*(?:is|=|:)\s*[A-Z]\b|"
    r"(?:choose|select|pick)\s+(?:option\s+)?[A-Z]\b|"
    r"(?:question|item|uid|index)\s*[^\n]{0,48}(?:->|=|:)\s*[A-Z]\b",
    re.IGNORECASE,
)


class TaskwiseCoreEvolutionError(RuntimeError):
    """Closed failure carrying no benchmark or artifact content."""

    def __init__(
        self,
        finding_code: str,
        *,
        diagnostic_receipt: str | None = None,
    ) -> None:
        if _CLOSED_CODE_RE.fullmatch(finding_code) is None:
            raise ValueError("finding_code must use the closed taskwise vocabulary")
        if diagnostic_receipt is not None and (
            type(diagnostic_receipt) is not str
            or _DIAGNOSTIC_BASENAME_RE.fullmatch(diagnostic_receipt) is None
            or "/" in diagnostic_receipt
            or "\\" in diagnostic_receipt
        ):
            raise ValueError("diagnostic_receipt must be a safe basename or None")
        self.finding_code = finding_code
        self.diagnostic_receipt = diagnostic_receipt
        super().__init__(finding_code)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class TaskwiseCoreFailureReceiptV1(_FrozenModel):
    """Private content-free evidence for a failed Core update stage."""

    schema_version: Literal["chembench4k_taskwise_core_failure_v1"] = (
        "chembench4k_taskwise_core_failure_v1"
    )
    protocol_id: Literal["taskwise_online_evolution_v1"] = PROTOCOL_ID
    bridge_id: Literal["openevo_core_taskwise_text_memory_v1"] = BRIDGE_ID
    stage: str
    finding_code: str
    exception_class: str
    errno: int | None
    task_index: int = Field(ge=0)
    round_index: Literal[0, 1]
    update_index: Literal[1, 2]
    job_id: str = Field(min_length=1, max_length=256)
    artifact_id: str | None = Field(default=None, min_length=1, max_length=256)
    predecessor_artifact_id: str | None = Field(default=None, min_length=1, max_length=256)
    trajectory_digest: str
    safe_feedback_digest: str
    validator_input_digest: str
    dataset_manifest_sha256: str
    plan_digest: str
    reflector_input_digest: str | None
    reflector_receipt_digest: str | None
    reflector_event_stream_digest: str | None
    artifact_payload_sha256: str | None
    artifact_lineage_sha256: str | None
    memory_inspection_sha256: str | None
    validation_receipt_sha256: str | None
    core_artifact_manifest_sha256: str | None
    context_resolution_digest: str | None

    @field_validator(
        "trajectory_digest",
        "safe_feedback_digest",
        "validator_input_digest",
        "dataset_manifest_sha256",
        "plan_digest",
        "reflector_input_digest",
        "reflector_receipt_digest",
        "reflector_event_stream_digest",
        "artifact_payload_sha256",
        "artifact_lineage_sha256",
        "memory_inspection_sha256",
        "validation_receipt_sha256",
        "core_artifact_manifest_sha256",
        "context_resolution_digest",
    )
    @classmethod
    def _digests(cls, value: str | None) -> str | None:
        if value is not None and _SHA256_RE.fullmatch(value) is None:
            raise ValueError("Core failure receipt digest must be SHA-256")
        return value

    @field_validator("stage")
    @classmethod
    def _stage(cls, value: str) -> str:
        if value not in _CORE_FAILURE_STAGES:
            raise ValueError("Core failure stage is outside the closed vocabulary")
        return value

    @field_validator("job_id", "artifact_id", "predecessor_artifact_id")
    @classmethod
    def _core_identifiers(cls, value: str | None) -> str | None:
        if value is not None and _CORE_ID_RE.fullmatch(value) is None:
            raise ValueError("Core failure identifier is outside the safe vocabulary")
        return value

    @field_validator("finding_code")
    @classmethod
    def _finding(cls, value: str) -> str:
        if _CLOSED_CODE_RE.fullmatch(value) is None:
            raise ValueError("Core failure finding is outside the closed vocabulary")
        return value

    @field_validator("exception_class")
    @classmethod
    def _exception_class(cls, value: str) -> str:
        if value not in _CORE_EXCEPTION_CLASSES:
            raise ValueError("Core exception class is outside the closed vocabulary")
        return value

    @model_validator(mode="after")
    def _sequence(self) -> TaskwiseCoreFailureReceiptV1:
        if self.update_index != self.round_index + 1:
            raise ValueError("Core failure receipt update does not match its round")
        if self.finding_code not in _CORE_STAGE_ALLOWED_FINDINGS[self.stage]:
            raise ValueError("Core failure finding is invalid for its stage")
        if self.errno is not None and self.errno < 0:
            raise ValueError("Core failure errno must be non-negative")
        return self


class TaskwiseMemoryInspectionV1(_FrozenModel):
    """Content-free deterministic measurements for one candidate memory."""

    schema_version: Literal["chembench4k_taskwise_memory_inspection_v1"] = (
        "chembench4k_taskwise_memory_inspection_v1"
    )
    memory_limits_sha256: str
    token_estimator_id: Literal["ascii_word_or_unicode_codepoint_v1"]
    parser_id: Literal["exact_markdown_sections_v1"]
    utf8_byte_count: int = Field(ge=0)
    estimated_token_count: int = Field(ge=0)
    section_item_counts: tuple[int, ...]
    total_section_items: int = Field(ge=0)
    max_section_items: int = Field(ge=0)
    finding_codes: tuple[str, ...]

    @field_validator("memory_limits_sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("memory limits digest must be SHA-256")
        return value

    @field_validator("finding_codes")
    @classmethod
    def _findings(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(set(values))) or any(
            re.fullmatch(r"[a-z][a-z0-9_]{0,95}", value) is None for value in values
        ):
            raise ValueError("memory findings must be a sorted closed set")
        return values

    @model_validator(mode="after")
    def _counts(self) -> TaskwiseMemoryInspectionV1:
        if (
            len(self.section_item_counts) != len(TASKWISE_MEMORY_LIMITS_V1.required_sections)
            or any(count < 0 for count in self.section_item_counts)
            or sum(self.section_item_counts) != self.total_section_items
            or max(self.section_item_counts, default=0) != self.max_section_items
        ):
            raise ValueError("memory section measurements are inconsistent")
        return self

    @property
    def passed(self) -> bool:
        return not self.finding_codes

    @property
    def digest(self) -> str:
        return canonical_digest(self)

    def to_public_payload(self) -> dict[str, object]:
        """Return aggregate-only evidence with no memory content."""

        return {
            "memory_limits_sha256": self.memory_limits_sha256,
            "token_estimator_id": self.token_estimator_id,
            "parser_id": self.parser_id,
            "utf8_byte_count": self.utf8_byte_count,
            "estimated_token_count": self.estimated_token_count,
            "section_item_counts": list(self.section_item_counts),
            "total_section_items": self.total_section_items,
            "max_section_items": self.max_section_items,
            "inspection_sha256": self.digest,
        }


def inspect_taskwise_text_memory_v1(
    payload: bytes,
    *,
    limits: TaskwiseMemoryLimitsV1 = TASKWISE_MEMORY_LIMITS_V1,
) -> TaskwiseMemoryInspectionV1:
    """Parse exact Markdown and count bytes/tokens/items without model heuristics."""

    if type(payload) is not bytes:
        raise TypeError("memory payload must be exact bytes")
    if type(limits) is not TaskwiseMemoryLimitsV1:
        raise TypeError("memory limits must be exact TaskwiseMemoryLimitsV1")
    findings: set[str] = set()
    utf8_byte_count = len(payload)
    if utf8_byte_count > limits.max_utf8_bytes:
        findings.add("memory_bytes_exceeded")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        text = ""
        findings.add("memory_invalid_utf8")
    estimated_token_count = len(_MEMORY_TOKEN_RE.findall(text))
    if estimated_token_count > limits.max_estimated_tokens:
        findings.add("memory_tokens_exceeded")
    if not text.strip() or "\x00" in text or "\r" in text:
        findings.add("memory_structure_invalid")

    items_by_section: list[list[str]] = [[] for _section in limits.required_sections]
    expected_section_index = 0
    current_section_index: int | None = None
    saw_nonempty_preamble = False
    for line in text.split("\n"):
        if not line:
            continue
        heading = _MEMORY_H2_RE.fullmatch(line)
        if heading is not None:
            if (
                expected_section_index >= len(limits.required_sections)
                or heading.group(1) != limits.required_sections[expected_section_index]
            ):
                findings.add("memory_required_sections_invalid")
                current_section_index = None
                continue
            current_section_index = expected_section_index
            expected_section_index += 1
            continue
        if current_section_index is None:
            if not saw_nonempty_preamble and expected_section_index == 0 and line == _MEMORY_H1:
                saw_nonempty_preamble = True
                continue
            findings.add("memory_structure_invalid")
            saw_nonempty_preamble = True
            continue
        if _MEMORY_BULLET_RE.match(line) is not None:
            items_by_section[current_section_index].append(line[2:].strip())
        elif (
            not items_by_section[current_section_index]
            or _MEMORY_CONTINUATION_RE.match(line) is None
        ):
            findings.add("memory_structure_invalid")
        else:
            items_by_section[current_section_index][-1] += f" {line.strip()}"

    counts = [len(items) for items in items_by_section]
    if expected_section_index != len(limits.required_sections):
        findings.add("memory_required_sections_invalid")
    if any(count == 0 for count in counts):
        findings.add("memory_section_empty")
    if any(count > limits.max_items_per_section for count in counts):
        findings.add("memory_section_items_exceeded")
    normalized_items = [
        _normalize_rule(item)
        for section_items in items_by_section
        for item in section_items
        if _normalize_rule(item)
    ]
    if len(normalized_items) != len(set(normalized_items)):
        findings.add("memory_duplicate_rule")
    return TaskwiseMemoryInspectionV1(
        memory_limits_sha256=limits.digest,
        token_estimator_id=limits.token_estimator_id,
        parser_id=limits.parser_id,
        utf8_byte_count=utf8_byte_count,
        estimated_token_count=estimated_token_count,
        section_item_counts=tuple(counts),
        total_section_items=sum(counts),
        max_section_items=max(counts, default=0),
        finding_codes=tuple(sorted(findings)),
    )


class TaskwiseCorePredecessorV1(_FrozenModel):
    """Content-free identity of the exact global stream head."""

    task_uid: str
    task_index: int = Field(ge=0)
    round_index: Literal[0, 1]
    update_index: Literal[1, 2]
    global_update_ordinal: int = Field(ge=1)
    core_artifact_id: str = Field(min_length=1, max_length=256)
    artifact_payload_sha256: str
    artifact_lineage_sha256: str
    core_context_id: str = Field(min_length=1, max_length=256)
    context_resolution_digest: str
    resolved_memory_sha256: str

    @field_validator(
        "task_uid",
        "artifact_payload_sha256",
        "artifact_lineage_sha256",
        "context_resolution_digest",
        "resolved_memory_sha256",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("predecessor digest must be SHA-256")
        return value

    @model_validator(mode="after")
    def _sequence(self) -> TaskwiseCorePredecessorV1:
        if self.update_index != self.round_index + 1:
            raise ValueError("predecessor update does not match its round")
        return self


class TaskwiseArtifactLineageReceiptV1(_FrozenModel):
    """Private evidence projection using the protocol's exact lineage keys."""

    schema_version: Literal["chembench4k_taskwise_artifact_lineage_v1"] = (
        "chembench4k_taskwise_artifact_lineage_v1"
    )
    protocol_id: Literal["taskwise_online_evolution_v1"] = PROTOCOL_ID
    task_uid: str
    task_index: int = Field(ge=0)
    round_index: Literal[0, 1]
    predecessor_artifact_id: str | None
    predecessor_memory_sha256: str | None
    trajectory_ids: tuple[str, ...]
    trajectory_digest: str
    safe_feedback_digest: str
    memory_limits_sha256: str
    memory_inspection_sha256: str
    token_estimator_id: Literal["ascii_word_or_unicode_codepoint_v1"]
    utf8_byte_count: int = Field(ge=0)
    estimated_token_count: int = Field(ge=0)
    section_item_counts: tuple[int, ...]
    evolution_plan_id: str
    evolution_job_id: str
    core_artifact_id: str
    artifact_payload_sha256: str
    context_resolution_digest: str

    @field_validator(
        "task_uid",
        "predecessor_memory_sha256",
        "trajectory_digest",
        "safe_feedback_digest",
        "memory_limits_sha256",
        "memory_inspection_sha256",
        "artifact_payload_sha256",
        "context_resolution_digest",
    )
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None and _SHA256_RE.fullmatch(value) is None:
            raise ValueError("lineage receipt digest must be SHA-256")
        return value

    @field_validator("trajectory_ids")
    @classmethod
    def _trajectory_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if (
            not values
            or any(_SHA256_RE.fullmatch(value) is None for value in values)
            or len(values) != len(set(values))
        ):
            raise ValueError("lineage trajectory ids must be unique SHA-256 values")
        return values

    @property
    def digest(self) -> str:
        return canonical_digest(self)

    @model_validator(mode="after")
    def _memory_measurements(self) -> TaskwiseArtifactLineageReceiptV1:
        if (
            self.memory_limits_sha256 != TASKWISE_MEMORY_LIMITS_V1.digest
            or self.token_estimator_id != TASKWISE_MEMORY_LIMITS_V1.token_estimator_id
            or self.utf8_byte_count > TASKWISE_MEMORY_LIMITS_V1.max_utf8_bytes
            or self.estimated_token_count > TASKWISE_MEMORY_LIMITS_V1.max_estimated_tokens
            or len(self.section_item_counts) != len(TASKWISE_MEMORY_LIMITS_V1.required_sections)
            or any(
                count < 1 or count > TASKWISE_MEMORY_LIMITS_V1.max_items_per_section
                for count in self.section_item_counts
            )
        ):
            raise ValueError("lineage receipt memory measurements violate protocol limits")
        return self


class TaskwiseCoreUpdateResultV1(_FrozenModel):
    """Core evidence for one approved global update."""

    schema_version: Literal["chembench4k_taskwise_core_update_v1"] = (
        "chembench4k_taskwise_core_update_v1"
    )
    protocol_id: Literal["taskwise_online_evolution_v1"] = PROTOCOL_ID
    bridge_id: Literal["openevo_core_taskwise_text_memory_v1"] = BRIDGE_ID
    task_uid: str
    task_index: int = Field(ge=0)
    round_index: Literal[0, 1]
    update_index: Literal[1, 2]
    global_update_ordinal: int = Field(ge=1)
    predecessor: TaskwiseCorePredecessorV1 | None
    trajectory_ids: tuple[str, ...]
    trajectory_digest: str
    safe_feedback_digest: str
    validator_input_digest: str
    memory_limits_sha256: str
    memory_inspection: TaskwiseMemoryInspectionV1
    dataset_id: str
    dataset_artifact_id: str
    dataset_manifest_sha256: str
    configured_max_records: Literal[1, 2]
    records_visible_to_reflector: Literal[1, 2]
    reflector_input_digest: str
    plan_id: str
    plan_digest: str
    job_id: str
    job_state: Literal["COMPLETED"] = "COMPLETED"
    method_id: Literal["text_memory_expel_reflector"] = METHOD_ID
    method_descriptor_digest: str
    method_identity_digest: str
    core_artifact_id: str
    core_artifact_manifest_sha256: str
    artifact_payload_sha256: str
    artifact_lineage_sha256: str
    validation_receipt: CoreArtifactValidationReceiptV2
    validation_receipt_sha256: str
    reflector_execution_receipt_sha256: str | None
    reflector_event_stream_sha256: str | None
    core_context_id: str
    context_resolution_digest: str
    resolved_memory: str
    resolved_memory_sha256: str

    @field_validator(
        "task_uid",
        "trajectory_digest",
        "safe_feedback_digest",
        "validator_input_digest",
        "memory_limits_sha256",
        "dataset_manifest_sha256",
        "reflector_input_digest",
        "plan_digest",
        "method_descriptor_digest",
        "method_identity_digest",
        "core_artifact_manifest_sha256",
        "artifact_payload_sha256",
        "artifact_lineage_sha256",
        "validation_receipt_sha256",
        "reflector_execution_receipt_sha256",
        "reflector_event_stream_sha256",
        "context_resolution_digest",
        "resolved_memory_sha256",
    )
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None and _SHA256_RE.fullmatch(value) is None:
            raise ValueError("update digest must be SHA-256")
        return value

    @field_validator("trajectory_ids")
    @classmethod
    def _trajectory_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if (
            not values
            or any(_SHA256_RE.fullmatch(value) is None for value in values)
            or len(values) != len(set(values))
        ):
            raise ValueError("trajectory ids must be unique SHA-256 values")
        return values

    @model_validator(mode="after")
    def _sequence_and_memory(self) -> TaskwiseCoreUpdateResultV1:
        if (
            self.update_index != self.round_index + 1
            or len(self.trajectory_ids) != self.update_index
            or self.configured_max_records != self.update_index
            or self.records_visible_to_reflector != self.update_index
        ):
            raise ValueError("update evidence does not bind the complete round prefix")
        if (self.predecessor is None) != (self.global_update_ordinal == 1):
            raise ValueError("only the first global update can omit a predecessor")
        if self.predecessor is not None and (
            self.predecessor.global_update_ordinal + 1 != self.global_update_ordinal
        ):
            raise ValueError("predecessor is not the immediate global update")
        if self.global_update_ordinal != self.task_index * 2 + self.update_index:
            raise ValueError("global update ordinal does not match stream position")
        if self.predecessor is None:
            if self.task_index != 0 or self.round_index != 0 or self.update_index != 1:
                raise ValueError("the global stream must start at task zero update one")
        elif self.predecessor.update_index == 1:
            if (
                self.task_uid != self.predecessor.task_uid
                or self.task_index != self.predecessor.task_index
                or self.round_index != 1
                or self.update_index != 2
            ):
                raise ValueError("update two must immediately follow update one")
        elif (
            self.task_uid == self.predecessor.task_uid
            or self.task_index != self.predecessor.task_index + 1
            or self.round_index != 0
            or self.update_index != 1
        ):
            raise ValueError("the next task must start from the prior task final memory")
        if _sha256_bytes(self.resolved_memory.encode("utf-8")) != self.resolved_memory_sha256:
            raise ValueError("resolved memory digest mismatch")
        if (
            self.memory_limits_sha256 != TASKWISE_MEMORY_LIMITS_V1.digest
            or self.memory_inspection.memory_limits_sha256 != self.memory_limits_sha256
            or not self.memory_inspection.passed
        ):
            raise ValueError("memory inspection is not an approved protocol-limit result")
        if (
            self.validation_receipt.artifact_id != self.core_artifact_id
            or self.validation_receipt.artifact_payload_sha256 != self.artifact_payload_sha256
            or not self.validation_receipt.passed
            or self.validation_receipt.finding_codes
            or self.validation_receipt.digest != self.validation_receipt_sha256
        ):
            raise ValueError("validation receipt is not bound to the artifact")
        return self

    def predecessor_identity(self) -> TaskwiseCorePredecessorV1:
        return TaskwiseCorePredecessorV1(
            task_uid=self.task_uid,
            task_index=self.task_index,
            round_index=self.round_index,
            update_index=self.update_index,
            global_update_ordinal=self.global_update_ordinal,
            core_artifact_id=self.core_artifact_id,
            artifact_payload_sha256=self.artifact_payload_sha256,
            artifact_lineage_sha256=self.artifact_lineage_sha256,
            core_context_id=self.core_context_id,
            context_resolution_digest=self.context_resolution_digest,
            resolved_memory_sha256=self.resolved_memory_sha256,
        )

    def required_lineage_receipt(self) -> TaskwiseArtifactLineageReceiptV1:
        """Expose the exact closed lineage contract required by the protocol."""

        return TaskwiseArtifactLineageReceiptV1(
            protocol_id=self.protocol_id,
            task_uid=self.task_uid,
            task_index=self.task_index,
            round_index=self.round_index,
            predecessor_artifact_id=(
                None if self.predecessor is None else self.predecessor.core_artifact_id
            ),
            predecessor_memory_sha256=(
                None if self.predecessor is None else self.predecessor.resolved_memory_sha256
            ),
            trajectory_ids=self.trajectory_ids,
            trajectory_digest=self.trajectory_digest,
            safe_feedback_digest=self.safe_feedback_digest,
            memory_limits_sha256=self.memory_limits_sha256,
            memory_inspection_sha256=self.memory_inspection.digest,
            token_estimator_id=self.memory_inspection.token_estimator_id,
            utf8_byte_count=self.memory_inspection.utf8_byte_count,
            estimated_token_count=self.memory_inspection.estimated_token_count,
            section_item_counts=self.memory_inspection.section_item_counts,
            evolution_plan_id=self.plan_id,
            evolution_job_id=self.job_id,
            core_artifact_id=self.core_artifact_id,
            artifact_payload_sha256=self.artifact_payload_sha256,
            context_resolution_digest=self.context_resolution_digest,
        )


@dataclass(frozen=True, slots=True, repr=False)
class TaskwiseCoreUpdateRequestV1:
    """Trusted controller request for one complete current-task prefix."""

    task_uid: str
    task_index: int
    round_index: Literal[0, 1]
    update_index: Literal[1, 2]
    trajectories: tuple[TaskwiseTrajectoryV1, ...]
    predecessor: TaskwiseCorePredecessorV1 | None
    validator_forbidden_literals: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return "TaskwiseCoreUpdateRequestV1(<trajectory-prefix-and-validator-input>)"

    __str__ = __repr__

    def __post_init__(self) -> None:
        if type(self.task_uid) is not str or _SHA256_RE.fullmatch(self.task_uid) is None:
            raise ValueError("task_uid must be a lowercase SHA-256")
        if (
            isinstance(self.task_index, bool)
            or not isinstance(self.task_index, int)
            or self.task_index < 0
        ):
            raise ValueError("task_index must be non-negative")
        if self.round_index not in (0, 1) or self.update_index not in (1, 2):
            raise ValueError("taskwise updates support only rounds 0 and 1")
        if self.update_index != self.round_index + 1:
            raise ValueError("update_index must immediately follow round_index")
        if (
            not isinstance(self.trajectories, tuple)
            or len(self.trajectories) != self.update_index
            or any(type(item) is not TaskwiseTrajectoryV1 for item in self.trajectories)
            or tuple(item.round_index for item in self.trajectories)
            != tuple(range(self.update_index))
            or any(
                item.task_uid != self.task_uid or item.task_index != self.task_index
                for item in self.trajectories
            )
            or len({item.trajectory_id for item in self.trajectories}) != self.update_index
        ):
            raise ValueError("trajectories must be the complete ordered current-task prefix")
        # Invoke shared digest validators now, before any Core write.
        ordered_taskwise_trajectory_digest(self.trajectories)
        ordered_safe_feedback_digest(self.trajectories)
        if self.predecessor is not None and type(self.predecessor) is not (
            TaskwiseCorePredecessorV1
        ):
            raise TypeError("predecessor must be exact TaskwiseCorePredecessorV1")
        if not isinstance(self.validator_forbidden_literals, tuple) or any(
            type(value) is not str or not value.strip()
            for value in self.validator_forbidden_literals
        ):
            raise TypeError("validator_forbidden_literals must be non-empty strings")


ReflectorBoundaryFactoryV1 = Callable[
    [Path, str, int],
    ReflectorExecutionBoundaryV2,
]


class TaskwiseTextMemoryValidatorV1:
    """Validate one Core candidate against cumulative taskwise leakage inputs."""

    __slots__ = (
        "_expected_lineage",
        "_expected_record_count",
        "_forbidden_literals",
        "_memory_limits",
    )

    def __init__(
        self,
        *,
        expected_lineage: dict[str, Any],
        expected_record_count: int,
        forbidden_literals: tuple[str, ...],
        memory_limits: TaskwiseMemoryLimitsV1,
    ) -> None:
        if type(memory_limits) is not TaskwiseMemoryLimitsV1:
            raise TypeError("memory_limits must be exact TaskwiseMemoryLimitsV1")
        self._expected_lineage = json.loads(_canonical_json(expected_lineage))
        self._expected_record_count = expected_record_count
        self._forbidden_literals = forbidden_literals
        self._memory_limits = memory_limits

    def validate(
        self,
        *,
        artifact: ArtifactResponse,
        payload: bytes,
        payload_sha256: str,
        lineage: dict[str, Any],
    ) -> CoreArtifactValidationReceiptV2:
        if type(artifact) is not ArtifactResponse:
            raise TypeError("artifact must be exact Core ArtifactResponse")
        if type(payload) is not bytes or not isinstance(lineage, dict):
            raise TypeError("validator inputs use invalid types")
        findings: set[str] = set()
        if _sha256_bytes(payload) != payload_sha256:
            findings.add("payload_digest_mismatch")
        if (
            artifact.type is not ArtifactType.TEXT_MEMORY
            or artifact.state is not ArtifactState.ACTIVE
        ):
            findings.add("invalid_artifact_type_or_state")
        if artifact.promoted:
            findings.add("already_promoted")
        inspection = inspect_taskwise_text_memory_v1(payload, limits=self._memory_limits)
        findings.update(inspection.finding_codes)
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            text = ""
        manifest = artifact.manifest
        expected_prior_count = (
            0 if self._expected_lineage["predecessor_artifact_id"] is None else 1
        )
        if (
            not isinstance(manifest, dict)
            or manifest.get("method") != METHOD_ID
            or manifest.get("record_count") != self._expected_record_count
            or manifest.get("reflected_record_count") != self._expected_record_count
            or manifest.get("prior_memory_count") != expected_prior_count
            or manifest.get("required_sections") != list(self._memory_limits.required_sections)
        ):
            findings.add("invalid_core_manifest")
        for key, value in self._expected_lineage.items():
            if lineage.get(key) != value:
                findings.add("invalid_predecessor_lineage")
                break
        execution = lineage.get("openevo_execution")
        if not isinstance(execution, dict):
            findings.add("invalid_core_execution_lineage")
        normalized_candidate = _normalize(text)
        candidate_ngrams = {
            normalized_candidate[index : index + 24]
            for index in range(max(0, len(normalized_candidate) - 23))
        }
        for literal in self._forbidden_literals:
            stripped = literal.strip()
            if len(stripped) >= 8 and stripped.casefold() in text.casefold():
                findings.add("leak_forbidden_literal")
            normalized_literal = _normalize(stripped)
            if len(normalized_literal) >= 12 and normalized_literal in normalized_candidate:
                findings.add("leak_normalized_literal")
            if _sequential_ngram_overlap(normalized_literal, candidate_ngrams):
                findings.add("leak_literal_ngram")
        if _PATH_OR_BENCHMARK_RE.search(text):
            findings.add("leak_path_or_benchmark_marker")
        if _ANSWER_MAP_RE.search(text):
            findings.add("leak_answer_map")
        finding_codes = tuple(sorted(findings))
        return CoreArtifactValidationReceiptV2(
            validator_id="chembench4k_taskwise_text_memory_validator_v1",
            artifact_id=artifact.artifact_id,
            artifact_payload_sha256=payload_sha256,
            passed=not finding_codes,
            finding_codes=finding_codes,
        )


class TaskwiseCoreEvolutionBridgeV1:
    """Global Core stream whose head advances only after context resolution."""

    def __init__(
        self,
        *,
        db_path: str | Path,
        artifact_root: str | Path,
        executable_registry: VerifiedExecutableRegistry,
        reflector_boundary_factory: ReflectorBoundaryFactoryV1 | None = None,
        checkpoint_path: str | Path | None = None,
        memory_limits: TaskwiseMemoryLimitsV1 = TASKWISE_MEMORY_LIMITS_V1,
    ) -> None:
        if type(memory_limits) is not TaskwiseMemoryLimitsV1:
            raise TypeError("memory_limits must be exact TaskwiseMemoryLimitsV1")
        self._registry = require_verified_executable_registry(executable_registry)
        self._require_method()
        self._memory_limits = memory_limits
        db = Path(db_path).resolve()
        db.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db.parent.chmod(0o700)
        self._private_failure_root = db.parent / _PRIVATE_FAILURE_DIRECTORY
        _prepare_private_directory(self._private_failure_root)
        self._stream_lock_path = db.with_name("taskwise_core_stream_v1.lock")
        self._stream_lock_fd: int | None = self._acquire_stream_lock(self._stream_lock_path)
        self._closed = False
        resolved_checkpoint = (
            db.with_name("taskwise_core_lineage_checkpoints_v1.jsonl")
            if checkpoint_path is None
            else Path(checkpoint_path)
        )
        self._checkpoint_path = resolved_checkpoint.resolve()
        self._checkpoint_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._checkpoint_path.parent.chmod(0o700)
        self._head: TaskwiseCoreUpdateResultV1 | None = None
        self._history: dict[str, TaskwiseCoreUpdateResultV1] = {}
        self._validator_inputs: dict[str, tuple[str, ...]] = {}
        self._seen_forbidden_literals: tuple[str, ...] = ()
        self._terminal_finding: str | None = None
        try:
            self._store = EvolutionStore(
                db_path=db,
                artifact_root=artifact_root,
                executable_registry=self._registry,
            )
            self._store.initialize()
            self._reflector_boundary_factory = reflector_boundary_factory
            self._restore_private_checkpoints()
        except Exception:
            self.close()
            raise

    @staticmethod
    def _acquire_stream_lock(path: Path) -> int:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            raise TaskwiseCoreEvolutionError("TASKWISE_STREAM_LOCK_UNAVAILABLE") from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            ):
                raise TaskwiseCoreEvolutionError("TASKWISE_STREAM_LOCK_UNSAFE")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise TaskwiseCoreEvolutionError("TASKWISE_STREAM_LOCK_HELD") from exc
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def close(self) -> None:
        """Release the process-exclusive stream writer lock."""

        if getattr(self, "_closed", True):
            return
        descriptor = self._stream_lock_fd
        self._stream_lock_fd = None
        self._closed = True
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def __enter__(self) -> TaskwiseCoreEvolutionBridgeV1:
        if self._closed:
            raise RuntimeError("taskwise Core bridge is closed")
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def apply_update(
        self,
        request: TaskwiseCoreUpdateRequestV1,
        *,
        test_only_allow_synthetic_reflector: bool = False,
        lease_seconds: int = 600,
    ) -> TaskwiseCoreUpdateResultV1:
        """Execute one immutable Core update; no adapter artifact is accepted."""

        if type(request) is not TaskwiseCoreUpdateRequestV1:
            raise TypeError("request must be exact TaskwiseCoreUpdateRequestV1")
        if self._closed:
            raise TaskwiseCoreEvolutionError("TASKWISE_STREAM_CLOSED")
        if self._terminal_finding is not None:
            raise TaskwiseCoreEvolutionError("TASKWISE_STREAM_TERMINAL")
        if test_only_allow_synthetic_reflector and self._reflector_boundary_factory is not None:
            raise ValueError("reflector execution mode is ambiguous")
        if not test_only_allow_synthetic_reflector and self._reflector_boundary_factory is None:
            raise TaskwiseCoreEvolutionError("TASKWISE_REFLECTOR_BOUNDARY_REQUIRED")
        self._require_next_predecessor(request)

        trajectory_digest = ordered_taskwise_trajectory_digest(request.trajectories)
        safe_feedback_digest = ordered_safe_feedback_digest(request.trajectories)
        global_update_ordinal = 1 if self._head is None else self._head.global_update_ordinal + 1
        policy_version = f"{PROTOCOL_ID}.{global_update_ordinal}.{trajectory_digest[:20]}"
        for trajectory in request.trajectories:
            self._store.ingest_event(
                self._event_request(
                    trajectory=trajectory,
                    policy_version=policy_version,
                )
            )
        dataset = self._store.create_dataset(
            DatasetCreateRequest(
                name=(
                    f"Taskwise trajectory prefix task {request.task_index} "
                    f"update {request.update_index}"
                ),
                purpose=_DATASET_PURPOSE,
                query={
                    "event_types": [_EVENT_TYPE],
                    "status": ["COMPLETED"],
                    "policy_version": policy_version,
                },
                limits={
                    "max_events": request.update_index,
                    "max_traces": request.update_index,
                },
            )
        )
        if (
            dataset.event_count != request.update_index
            or dataset.trace_count != request.update_index
        ):
            self._terminal_finding = "TASKWISE_DATASET_CARDINALITY_INVALID"
            raise TaskwiseCoreEvolutionError(self._terminal_finding)
        dataset_artifact = self._store.get_artifact(dataset.artifact_id)
        dataset_manifest_path = _safe_core_file_path(
            dataset_artifact.uri,
            allowed_root=self._store.files.root,
        )
        dataset_manifest_sha256 = _sha256_bytes(
            _read_bounded_regular_file(
                dataset_manifest_path,
                maximum=MAX_MANIFEST_BYTES,
            )
        )
        forbidden_literals = _deduplicate_literals(
            (
                *self._seen_forbidden_literals,
                *request.validator_forbidden_literals,
                request.task_uid,
                *(
                    literal
                    for trajectory in request.trajectories
                    for literal in _trajectory_forbidden_literals(trajectory)
                ),
            )
        )
        validator_input_digest = _validator_input_digest(forbidden_literals)
        expected_lineage = self._expected_lineage(
            request=request,
            global_update_ordinal=global_update_ordinal,
            trajectory_digest=trajectory_digest,
            safe_feedback_digest=safe_feedback_digest,
            validator_input_digest=validator_input_digest,
            dataset_artifact_id=dataset.artifact_id,
        )
        plan = self._create_job(
            request=request,
            dataset_artifact_id=dataset.artifact_id,
            expected_lineage=expected_lineage,
            trajectory_digest=trajectory_digest,
            forbidden_literals=forbidden_literals,
            validator_input_digest=validator_input_digest,
        )
        try:
            try:
                execution = self._run_and_register(
                    request=request,
                    job_id=plan["job_id"],
                    job_type=plan["job_type"],
                    trajectory_digest=trajectory_digest,
                    lease_seconds=lease_seconds,
                    test_only_allow_synthetic_reflector=(test_only_allow_synthetic_reflector),
                )
            except TaskwiseCoreEvolutionError as exc:
                if exc.finding_code == "TASKWISE_TYPED_ARTIFACT_INVALID":
                    self._raise_private_core_failure(
                        stage="TYPED_ARTIFACT",
                        default_finding="TASKWISE_TYPED_ARTIFACT_INVALID",
                        cause=exc,
                        request=request,
                        job_id=plan["job_id"],
                        artifact_id=None,
                        trajectory_digest=trajectory_digest,
                        safe_feedback_digest=safe_feedback_digest,
                        validator_input_digest=validator_input_digest,
                        dataset_manifest_sha256=dataset_manifest_sha256,
                        plan_digest=plan["plan_digest"],
                        reflector_input_digest=None,
                        reflector_receipt_digest=None,
                        reflector_event_stream_digest=None,
                        artifact_payload_sha256=None,
                        artifact_lineage_sha256=None,
                        memory_inspection_sha256=None,
                        validation_receipt_sha256=None,
                        core_artifact_manifest_sha256=None,
                        context_resolution_digest=None,
                    )
                raise
            result = self._validate_promote_resolve(
                request=request,
                global_update_ordinal=global_update_ordinal,
                trajectory_digest=trajectory_digest,
                safe_feedback_digest=safe_feedback_digest,
                validator_input_digest=validator_input_digest,
                dataset_id=dataset.dataset_id,
                dataset_artifact_id=dataset.artifact_id,
                dataset_manifest_sha256=dataset_manifest_sha256,
                plan_id=plan["plan_id"],
                plan_digest=plan["plan_digest"],
                job_id=plan["job_id"],
                artifact_id=execution["artifact_id"],
                expected_lineage=expected_lineage,
                forbidden_literals=forbidden_literals,
                reflector_input_digest=execution["reflector_input_digest"],
                reflector_receipt_digest=execution["reflector_receipt_digest"],
                reflector_event_stream_digest=execution["reflector_event_stream_digest"],
            )
        except Exception:
            self._terminal_finding = "TASKWISE_CORE_UPDATE_FAILED"
            raise
        try:
            self._append_private_checkpoint(
                result=result,
                validator_forbidden_literals=forbidden_literals,
            )
        except Exception as exc:
            self._terminal_finding = "TASKWISE_PRIVATE_CHECKPOINT_FAILED"
            self._raise_private_core_failure(
                stage="PRIVATE_CHECKPOINT",
                default_finding="TASKWISE_PRIVATE_CHECKPOINT_FAILED",
                cause=exc,
                request=request,
                job_id=result.job_id,
                artifact_id=result.core_artifact_id,
                trajectory_digest=result.trajectory_digest,
                safe_feedback_digest=result.safe_feedback_digest,
                validator_input_digest=result.validator_input_digest,
                dataset_manifest_sha256=result.dataset_manifest_sha256,
                plan_digest=result.plan_digest,
                reflector_input_digest=result.reflector_input_digest,
                reflector_receipt_digest=result.reflector_execution_receipt_sha256,
                reflector_event_stream_digest=result.reflector_event_stream_sha256,
                artifact_payload_sha256=result.artifact_payload_sha256,
                artifact_lineage_sha256=result.artifact_lineage_sha256,
                memory_inspection_sha256=result.memory_inspection.digest,
                validation_receipt_sha256=result.validation_receipt_sha256,
                core_artifact_manifest_sha256=result.core_artifact_manifest_sha256,
                context_resolution_digest=result.context_resolution_digest,
            )
        self._accept_verified_checkpoint(
            result=result,
            validator_forbidden_literals=forbidden_literals,
        )
        return result

    def _append_private_checkpoint(
        self,
        *,
        result: TaskwiseCoreUpdateResultV1,
        validator_forbidden_literals: tuple[str, ...],
    ) -> None:
        lineage_receipt = result.required_lineage_receipt()
        payload = {
            "schema_version": "taskwise_core_private_checkpoint_v1",
            "result": result.model_dump(mode="json"),
            "required_lineage": lineage_receipt.model_dump(mode="json"),
            "required_lineage_sha256": lineage_receipt.digest,
            "validator_forbidden_literals": list(validator_forbidden_literals),
        }
        encoded = (_canonical_json(payload) + "\n").encode("utf-8")
        if self._checkpoint_path.exists():
            metadata = self._checkpoint_path.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise TaskwiseCoreEvolutionError("TASKWISE_PRIVATE_CHECKPOINT_UNSAFE")
        descriptor = os.open(
            self._checkpoint_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        with os.fdopen(descriptor, "ab", buffering=0) as stream:
            stream.write(encoded)
            os.fsync(stream.fileno())
        self._checkpoint_path.chmod(0o600)

    def _restore_private_checkpoints(self) -> None:
        if not self._checkpoint_path.exists():
            if self._taskwise_job_count() != 0:
                raise TaskwiseCoreEvolutionError("TASKWISE_PRIVATE_CHECKPOINT_MISSING")
            return
        metadata = self._checkpoint_path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise TaskwiseCoreEvolutionError("TASKWISE_PRIVATE_CHECKPOINT_UNSAFE")
        try:
            raw = self._checkpoint_path.read_bytes()
            if raw and not raw.endswith(b"\n"):
                raise ValueError
            encoded_lines = raw.splitlines(keepends=True)
            rows = [json.loads(line) for line in encoded_lines]
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise TaskwiseCoreEvolutionError("TASKWISE_PRIVATE_CHECKPOINT_INVALID") from exc
        prior_literals: set[str] = set()
        for expected_ordinal, (encoded, row) in enumerate(
            zip(encoded_lines, rows, strict=True),
            start=1,
        ):
            if (
                not isinstance(row, dict)
                or set(row)
                != {
                    "schema_version",
                    "result",
                    "required_lineage",
                    "required_lineage_sha256",
                    "validator_forbidden_literals",
                }
                or row["schema_version"] != "taskwise_core_private_checkpoint_v1"
                or encoded != (_canonical_json(row) + "\n").encode("utf-8")
            ):
                raise TaskwiseCoreEvolutionError("TASKWISE_PRIVATE_CHECKPOINT_INVALID")
            try:
                result = TaskwiseCoreUpdateResultV1.model_validate(row["result"])
                lineage_receipt = TaskwiseArtifactLineageReceiptV1.model_validate(
                    row["required_lineage"]
                )
                literals = _deduplicate_literals(tuple(row["validator_forbidden_literals"]))
            except (TypeError, ValueError) as exc:
                raise TaskwiseCoreEvolutionError("TASKWISE_PRIVATE_CHECKPOINT_INVALID") from exc
            if (
                result.global_update_ordinal != expected_ordinal
                or result.global_update_ordinal != result.task_index * 2 + result.update_index
                or (result.predecessor is None) != (expected_ordinal == 1)
                or (
                    self._head is not None
                    and result.predecessor != self._head.predecessor_identity()
                )
                or lineage_receipt != result.required_lineage_receipt()
                or row["required_lineage_sha256"] != lineage_receipt.digest
                or not prior_literals.issubset(set(literals))
            ):
                raise TaskwiseCoreEvolutionError("TASKWISE_PRIVATE_CHECKPOINT_CHAIN_INVALID")
            self.verify_update_result(
                result,
                validator_forbidden_literals=literals,
            )
            self._accept_verified_checkpoint(
                result=result,
                validator_forbidden_literals=literals,
            )
            prior_literals = set(literals)
        if self._taskwise_job_count() != len(rows):
            raise TaskwiseCoreEvolutionError("TASKWISE_PRIVATE_CHECKPOINT_COUNT_MISMATCH")

    def _accept_verified_checkpoint(
        self,
        *,
        result: TaskwiseCoreUpdateResultV1,
        validator_forbidden_literals: tuple[str, ...],
    ) -> None:
        self._head = result
        self._history[result.core_artifact_id] = result
        self._validator_inputs[result.core_artifact_id] = validator_forbidden_literals
        self._seen_forbidden_literals = validator_forbidden_literals

    def _taskwise_job_count(self) -> int:
        with self._store.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE job_type LIKE ?",
                (f"{_JOB_PREFIX}.%",),
            ).fetchone()
        return int(row[0])

    def current_head(self) -> TaskwiseCoreUpdateResultV1 | None:
        """Return the verified global head, or ``None`` before the first update."""

        if self._closed:
            raise TaskwiseCoreEvolutionError("TASKWISE_STREAM_CLOSED")
        if self._head is not None:
            self.verify_update_result(self._head)
        return self._head

    def verify_update_result(
        self,
        result: TaskwiseCoreUpdateResultV1,
        *,
        validator_forbidden_literals: tuple[str, ...] | None = None,
    ) -> TaskwiseCoreUpdateResultV1:
        """Reopen Core job/artifact/context state and recompute its bindings."""

        if type(result) is not TaskwiseCoreUpdateResultV1:
            raise TypeError("result must be exact TaskwiseCoreUpdateResultV1")
        if self._closed:
            raise TaskwiseCoreEvolutionError("TASKWISE_STREAM_CLOSED")
        forbidden = (
            self._validator_inputs.get(result.core_artifact_id)
            if validator_forbidden_literals is None
            else _deduplicate_literals(validator_forbidden_literals)
        )
        if forbidden is None:
            raise TaskwiseCoreEvolutionError("TASKWISE_VALIDATOR_INPUT_REQUIRED")
        with self._store.connect() as connection:
            job_row = connection.execute(
                "SELECT state, method, plan_id, method_identity_digest, config_json "
                "FROM jobs WHERE job_id = ?",
                (result.job_id,),
            ).fetchone()
            artifact_row = connection.execute(
                "SELECT type, state, promoted, manifest_path FROM artifacts WHERE artifact_id = ?",
                (result.core_artifact_id,),
            ).fetchone()
            context_row = connection.execute(
                "SELECT cm.manifest_json, c.response_json, "
                "c.selected_artifact_ids_json "
                "FROM context_materializations AS cm "
                "JOIN contexts AS c USING(context_id) "
                "WHERE cm.context_id = ?",
                (result.core_context_id,),
            ).fetchone()
        if (
            job_row is None
            or job_row["state"] != "succeeded"
            or job_row["method"] != METHOD_ID
            or job_row["plan_id"] != result.plan_id
            or job_row["method_identity_digest"] != result.method_identity_digest
        ):
            raise TaskwiseCoreEvolutionError("TASKWISE_JOB_IDENTITY_DRIFT")
        try:
            job_config = json.loads(str(job_row["config_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise TaskwiseCoreEvolutionError("TASKWISE_JOB_IDENTITY_DRIFT") from exc
        if (
            not isinstance(job_config, dict)
            or not isinstance(job_config.get("lineage"), dict)
            or job_config["lineage"].get("validator_input_digest") != result.validator_input_digest
            or job_config.get("forbidden_literals") != {"taskwise_public_input": list(forbidden)}
            or job_config["lineage"].get("memory_limits") != self._memory_limits.to_payload()
            or result.memory_limits_sha256 != self._memory_limits.digest
            or _validator_input_digest(forbidden) != result.validator_input_digest
        ):
            raise TaskwiseCoreEvolutionError("TASKWISE_VALIDATOR_INPUT_DRIFT")
        if (
            artifact_row is None
            or artifact_row["type"] != "text_memory"
            or artifact_row["state"] != "active"
            or artifact_row["promoted"] != 1
        ):
            raise TaskwiseCoreEvolutionError("TASKWISE_HEAD_STATE_INVALID")
        artifact = self._store.get_artifact(result.core_artifact_id)
        payload_path = _safe_core_file_path(
            artifact.uri,
            allowed_root=self._store.files.root,
        )
        payload = _read_bounded_regular_file(
            payload_path,
            maximum=ABSOLUTE_MAX_MEMORY_FILE_BYTES,
        )
        lineage = self._artifact_lineage(result.core_artifact_id)
        manifest_path = Path(str(artifact_row["manifest_path"]))
        if (
            _sha256_bytes(payload) != result.artifact_payload_sha256
            or canonical_digest(lineage) != result.artifact_lineage_sha256
            or _sha256_bytes(
                _read_bounded_regular_file(
                    manifest_path,
                    maximum=MAX_MANIFEST_BYTES,
                )
            )
            != result.core_artifact_manifest_sha256
        ):
            raise TaskwiseCoreEvolutionError("TASKWISE_HEAD_IDENTITY_DRIFT")
        validator = TaskwiseTextMemoryValidatorV1(
            expected_lineage=self._expected_lineage_from_result(result),
            expected_record_count=result.update_index,
            forbidden_literals=forbidden,
            memory_limits=self._memory_limits,
        )
        recomputed = validator.validate(
            artifact=artifact.model_copy(update={"promoted": False}),
            payload=payload,
            payload_sha256=result.artifact_payload_sha256,
            lineage=lineage,
        )
        if recomputed != result.validation_receipt:
            raise TaskwiseCoreEvolutionError("TASKWISE_VALIDATION_RECEIPT_DRIFT")
        inspection = inspect_taskwise_text_memory_v1(payload, limits=self._memory_limits)
        if inspection != result.memory_inspection:
            raise TaskwiseCoreEvolutionError("TASKWISE_MEMORY_INSPECTION_DRIFT")
        if context_row is None:
            raise TaskwiseCoreEvolutionError("TASKWISE_CONTEXT_IDENTITY_DRIFT")
        try:
            materialized = MaterializedContext.model_validate_json(
                str(context_row["manifest_json"])
            )
            response = MaterializedContext.model_validate_json(str(context_row["response_json"]))
            selected_ids = json.loads(str(context_row["selected_artifact_ids_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TaskwiseCoreEvolutionError("TASKWISE_CONTEXT_IDENTITY_DRIFT") from exc
        if (
            response != materialized
            or materialized.context_id != result.core_context_id
            or selected_ids != [result.core_artifact_id]
            or materialized.selection.artifact_ids != (result.core_artifact_id,)
            or canonical_digest(materialized) != result.context_resolution_digest
        ):
            raise TaskwiseCoreEvolutionError("TASKWISE_CONTEXT_IDENTITY_DRIFT")
        resolved = _resolved_text_memory(
            materialized,
            expected_artifact_id=result.core_artifact_id,
        )
        if (
            resolved != result.resolved_memory
            or _sha256_bytes(resolved.encode("utf-8")) != result.resolved_memory_sha256
        ):
            raise TaskwiseCoreEvolutionError("TASKWISE_CONTEXT_IDENTITY_DRIFT")
        return result

    def issue_runtime_memory(
        self,
        result: TaskwiseCoreUpdateResultV1,
    ) -> CoreResolvedTextMemoryV2:
        """Issue the capability DTO only after current Core-state verification."""

        self.verify_update_result(result)
        return _issue_core_resolved_text_memory_v2(
            core_artifact_id=result.core_artifact_id,
            artifact_payload_sha256=result.artifact_payload_sha256,
            context_resolution_digest=result.context_resolution_digest,
            resolved_memory_sha256=result.resolved_memory_sha256,
            markdown=result.resolved_memory,
        )

    def _require_next_predecessor(self, request: TaskwiseCoreUpdateRequestV1) -> None:
        head = self._head
        if head is None:
            if request.predecessor is not None:
                raise TaskwiseCoreEvolutionError("TASKWISE_LINEAGE_FORK")
            if request.task_index != 0 or request.round_index != 0 or request.update_index != 1:
                raise TaskwiseCoreEvolutionError("TASKWISE_LINEAGE_JUMP")
            return
        self.verify_update_result(head)
        expected_predecessor = head.predecessor_identity()
        if request.predecessor != expected_predecessor:
            predecessor_id = (
                None if request.predecessor is None else request.predecessor.core_artifact_id
            )
            if predecessor_id in self._history:
                raise TaskwiseCoreEvolutionError("TASKWISE_LINEAGE_ROLLBACK")
            raise TaskwiseCoreEvolutionError("TASKWISE_LINEAGE_FORK")
        if head.update_index == 1:
            expected_position = (
                head.task_uid,
                head.task_index,
                1,
                2,
            )
        else:
            expected_position = (
                request.task_uid,
                head.task_index + 1,
                0,
                1,
            )
            if request.task_uid == head.task_uid:
                raise TaskwiseCoreEvolutionError("TASKWISE_LINEAGE_FORK")
        actual_position = (
            request.task_uid,
            request.task_index,
            request.round_index,
            request.update_index,
        )
        if actual_position == expected_position:
            return
        if request.task_index < expected_position[1] or (
            request.task_index == head.task_index and request.update_index <= head.update_index
        ):
            raise TaskwiseCoreEvolutionError("TASKWISE_LINEAGE_ROLLBACK")
        raise TaskwiseCoreEvolutionError("TASKWISE_LINEAGE_JUMP")

    def _event_request(
        self,
        *,
        trajectory: TaskwiseTrajectoryV1,
        policy_version: str,
    ) -> EventIngestRequest:
        safe_feedback = trajectory.safe_feedback.to_evolution_payload()
        satisfactory = TaskwiseSafeSignalCodeV1.CORRECT in trajectory.safe_feedback.codes
        reward = 1.0 if satisfactory else 0.0
        task_id = f"task-{trajectory.task_index:08d}"
        session_id = f"task-{trajectory.task_index:08d}-round-{trajectory.round_index}"
        trajectory_payload = {
            "status": "COMPLETED",
            "metadata": {
                "builder": BRIDGE_ID,
                "capture_mode": "transcript",
                "token_level_metrics_available": False,
                "trajectory_id": trajectory.trajectory_id,
                "safe_feedback_digest": trajectory.safe_feedback.digest,
            },
            "traces": [
                {
                    "prompt_ids": [],
                    "response_ids": [],
                    "loss_mask": [],
                    "prompt_messages": [{"role": "user", "content": trajectory.public_prompt}],
                    "response_messages": [
                        {"role": "assistant", "content": trajectory.raw_completion}
                    ],
                    "finish_reason": "transcript",
                    "response_logprobs": None,
                    "reward": reward,
                    "metadata": {
                        "capture_mode": "transcript",
                        "token_level_metrics_available": False,
                        "verifier": {
                            "summary": {code.value: 1 for code in trajectory.safe_feedback.codes}
                        },
                    },
                }
            ],
        }
        return EventIngestRequest(
            source=_EVENT_SOURCE,
            event_type=_EVENT_TYPE,
            source_event_id=(f"{BRIDGE_ID}:{policy_version}:{trajectory.trajectory_id}"),
            task_id=task_id,
            session_id=session_id,
            policy_version=policy_version,
            rollout_step=trajectory.round_index,
            agent={"harness": "codex", "model_name": MODEL},
            base_model=MODEL,
            reward=reward,
            status="COMPLETED",
            payload={
                "session_result": {
                    "session_id": session_id,
                    "task_id": task_id,
                    "status": "COMPLETED",
                    "trajectory": trajectory_payload,
                    "metadata": {
                        "protocol_id": PROTOCOL_ID,
                        "bridge_id": BRIDGE_ID,
                        "task_index": trajectory.task_index,
                        "round_index": trajectory.round_index,
                        "trajectory_id": trajectory.trajectory_id,
                        "safe_feedback_digest": trajectory.safe_feedback.digest,
                        "evolution_feedback": safe_feedback,
                    },
                    "evolution_feedback": safe_feedback,
                },
                "evolution_feedback": safe_feedback,
            },
        )

    def _expected_lineage(
        self,
        *,
        request: TaskwiseCoreUpdateRequestV1,
        global_update_ordinal: int,
        trajectory_digest: str,
        safe_feedback_digest: str,
        validator_input_digest: str,
        dataset_artifact_id: str,
    ) -> dict[str, Any]:
        predecessor = request.predecessor
        return {
            "protocol_id": PROTOCOL_ID,
            "bridge_id": BRIDGE_ID,
            "task_uid": request.task_uid,
            "task_index": request.task_index,
            "round_index": request.round_index,
            "update_index": request.update_index,
            "global_update_ordinal": global_update_ordinal,
            "trajectory_ids": [trajectory.trajectory_id for trajectory in request.trajectories],
            "trajectory_digest": trajectory_digest,
            "safe_feedback_digest": safe_feedback_digest,
            "validator_input_digest": validator_input_digest,
            "memory_limits": self._memory_limits.to_payload(),
            "memory_limits_sha256": self._memory_limits.digest,
            "dataset_artifact_id": dataset_artifact_id,
            "predecessor_artifact_id": (
                None if predecessor is None else predecessor.core_artifact_id
            ),
            "predecessor_memory_sha256": (
                None if predecessor is None else predecessor.resolved_memory_sha256
            ),
            "predecessor_payload_sha256": (
                None if predecessor is None else predecessor.artifact_payload_sha256
            ),
            "predecessor_lineage_sha256": (
                None if predecessor is None else predecessor.artifact_lineage_sha256
            ),
            "predecessor_context_resolution_digest": (
                None if predecessor is None else predecessor.context_resolution_digest
            ),
        }

    def _expected_lineage_from_result(
        self,
        result: TaskwiseCoreUpdateResultV1,
    ) -> dict[str, Any]:
        predecessor = result.predecessor
        return {
            "protocol_id": PROTOCOL_ID,
            "bridge_id": BRIDGE_ID,
            "task_uid": result.task_uid,
            "task_index": result.task_index,
            "round_index": result.round_index,
            "update_index": result.update_index,
            "global_update_ordinal": result.global_update_ordinal,
            "trajectory_ids": list(result.trajectory_ids),
            "trajectory_digest": result.trajectory_digest,
            "safe_feedback_digest": result.safe_feedback_digest,
            "validator_input_digest": result.validator_input_digest,
            "memory_limits": self._memory_limits.to_payload(),
            "memory_limits_sha256": result.memory_limits_sha256,
            "dataset_artifact_id": result.dataset_artifact_id,
            "predecessor_artifact_id": (
                None if predecessor is None else predecessor.core_artifact_id
            ),
            "predecessor_memory_sha256": (
                None if predecessor is None else predecessor.resolved_memory_sha256
            ),
            "predecessor_payload_sha256": (
                None if predecessor is None else predecessor.artifact_payload_sha256
            ),
            "predecessor_lineage_sha256": (
                None if predecessor is None else predecessor.artifact_lineage_sha256
            ),
            "predecessor_context_resolution_digest": (
                None if predecessor is None else predecessor.context_resolution_digest
            ),
        }

    def _create_job(
        self,
        *,
        request: TaskwiseCoreUpdateRequestV1,
        dataset_artifact_id: str,
        expected_lineage: dict[str, Any],
        trajectory_digest: str,
        forbidden_literals: tuple[str, ...],
        validator_input_digest: str,
    ) -> dict[str, str]:
        if validator_input_digest != _validator_input_digest(forbidden_literals):
            raise TaskwiseCoreEvolutionError("TASKWISE_VALIDATOR_INPUT_DIGEST_INVALID")
        selection = EvolutionTargetSelection(
            target_id=TARGET_ID,
            enabled=True,
            method_id=METHOD_ID,
            config={
                "max_records": request.update_index,
                "reflector_llm": {
                    "provider": "codex_cli",
                    "model": MODEL,
                    "timeout_seconds": 900.0,
                    "max_tokens": self._memory_limits.max_estimated_tokens,
                },
            },
        )
        identity = _sha256_bytes(
            _canonical_json(
                {
                    "expected_lineage": expected_lineage,
                    "trajectory_digest": trajectory_digest,
                }
            ).encode("utf-8")
        )
        plan = self._registry.snapshot.compile_plan(
            plan_id=f"{_JOB_PREFIX}.{identity[:32]}",
            selections=(selection,),
            profile=self._execution_profile(),
        )
        job_type = f"{_JOB_PREFIX}.{identity[:24]}"
        prior_ids = () if request.predecessor is None else (request.predecessor.core_artifact_id,)
        created = self._store.create_plan_bound_job(
            PlanBoundJobCreateRequest(
                plan=plan,
                target_id=TARGET_ID,
                job_type=job_type,
                input_bindings=(
                    PlannedInputBinding(
                        binding_id="dataset_inputs",
                        artifact_ids=(dataset_artifact_id,),
                    ),
                    PlannedInputBinding(
                        binding_id="prior_target_artifacts",
                        artifact_ids=prior_ids,
                    ),
                ),
                core_config={
                    "name": (
                        f"Taskwise text memory task {request.task_index} "
                        f"update {request.update_index}"
                    ),
                    "promoted": False,
                    "lineage": expected_lineage,
                    "compatibility": {
                        "agent_harness": ["codex"],
                        "auth_mode": ["subscription"],
                        "base_model": [MODEL],
                        "task_tags": [PROTOCOL_ID],
                    },
                    "tags": [
                        PROTOCOL_ID,
                        BRIDGE_ID,
                        f"task-{request.task_index}",
                        f"update-{request.update_index}",
                    ],
                    "forbidden_literals": {
                        "taskwise_public_input": list(forbidden_literals),
                    },
                },
            ),
            snapshot=self._registry.snapshot,
        )
        return {
            "plan_id": plan.plan_id,
            "plan_digest": canonical_digest(plan),
            "job_id": created.job_id,
            "job_type": job_type,
        }

    def _run_and_register(
        self,
        *,
        request: TaskwiseCoreUpdateRequestV1,
        job_id: str,
        job_type: str,
        trajectory_digest: str,
        lease_seconds: int,
        test_only_allow_synthetic_reflector: bool,
    ) -> dict[str, str | None]:
        reflector_input_digest: str
        reflector_receipt_digest: str | None = None
        reflector_event_stream_digest: str | None = None
        client = _StoreWorkerClient(self._store)
        if test_only_allow_synthetic_reflector:
            reflector_input_digest = trajectory_digest
            claimed = run_once(
                client,
                worker_id=(f"{BRIDGE_ID}-task-{request.task_index}-update-{request.update_index}"),
                capabilities=[job_type],
                artifact_root=self._store.files.root,
                lease_seconds=lease_seconds,
                executable_registry=self._registry,
            )
        else:
            records = [
                {
                    "uid": trajectory.trajectory_id,
                    "source_split": TASKWISE_SOURCE_SPLIT,
                    **trajectory.to_core_record(),
                }
                for trajectory in request.trajectories
            ]
            reflector_input_digest = canonical_digest(records)
            private_parent = self._store.files.root / _PRIVATE_INPUT_DIRECTORY
            private_parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            private_parent.chmod(0o700)
            with tempfile.TemporaryDirectory(
                prefix=f"{job_id}.",
                dir=private_parent,
            ) as temporary:
                path = Path(temporary) / "taskwise_records.jsonl"
                encoded = "".join(canonical_json(record) + "\n" for record in records).encode(
                    "utf-8"
                )
                _exclusive_private_write(path, encoded)
                boundary_factory = self._reflector_boundary_factory
                if boundary_factory is None:  # defensive against mutation
                    raise TaskwiseCoreEvolutionError("TASKWISE_REFLECTOR_BOUNDARY_REQUIRED")
                boundary = boundary_factory(
                    path,
                    reflector_input_digest,
                    request.update_index,
                )
                if type(boundary) is not ReflectorExecutionBoundaryV2:
                    raise TypeError("reflector boundary factory returned an untrusted type")
                if (
                    boundary.expected_record_count != request.update_index
                    or boundary.expected_source_split != TASKWISE_SOURCE_SPLIT
                    or boundary.expected_records_sha256 != reflector_input_digest
                ):
                    raise TaskwiseCoreEvolutionError("TASKWISE_REFLECTOR_BOUNDARY_BINDING_INVALID")
                with boundary.activate() as activation:
                    claimed = run_once(
                        client,
                        worker_id=(
                            f"{BRIDGE_ID}-task-{request.task_index}-update-{request.update_index}"
                        ),
                        capabilities=[job_type],
                        artifact_root=self._store.files.root,
                        lease_seconds=lease_seconds,
                        executable_registry=self._registry,
                    )
                receipt = activation.load_receipt_for_audit()
                if receipt.status is ReflectorBoundaryStatusV2.SECURITY_TOOL_USE_VIOLATION:
                    raise TaskwiseCoreEvolutionError(
                        "TASKWISE_REFLECTOR_SECURITY_TOOL_USE_VIOLATION"
                    )
                if (
                    receipt.status is not ReflectorBoundaryStatusV2.COMPLETED
                    or not receipt.cleanup_complete
                    or receipt.event_counts
                    or receipt.retry_allowed
                    or receipt.resume_allowed
                    or receipt.replacement_completion_allowed
                    or receipt.record_count != request.update_index
                    or receipt.ordered_records_sha256 != reflector_input_digest
                ):
                    raise TaskwiseCoreEvolutionError("TASKWISE_REFLECTOR_BOUNDARY_FAILED")
                reflector_receipt_digest = receipt.digest
                reflector_event_stream_digest = receipt.event_stream_sha256
        if not claimed or client.completed is None or client.completed.get("job_id") != job_id:
            raise TaskwiseCoreEvolutionError("TASKWISE_CORE_JOB_FAILED")
        artifact_ids = client.completed.get("artifact_ids")
        if not isinstance(artifact_ids, list) or len(artifact_ids) != 1:
            raise TaskwiseCoreEvolutionError("TASKWISE_TYPED_ARTIFACT_INVALID")
        return {
            "artifact_id": str(artifact_ids[0]),
            "reflector_input_digest": reflector_input_digest,
            "reflector_receipt_digest": reflector_receipt_digest,
            "reflector_event_stream_digest": reflector_event_stream_digest,
        }

    def _validate_promote_resolve(
        self,
        *,
        request: TaskwiseCoreUpdateRequestV1,
        global_update_ordinal: int,
        trajectory_digest: str,
        safe_feedback_digest: str,
        validator_input_digest: str,
        dataset_id: str,
        dataset_artifact_id: str,
        dataset_manifest_sha256: str,
        plan_id: str,
        plan_digest: str,
        job_id: str,
        artifact_id: str | None,
        expected_lineage: dict[str, Any],
        forbidden_literals: tuple[str, ...],
        reflector_input_digest: str | None,
        reflector_receipt_digest: str | None,
        reflector_event_stream_digest: str | None,
    ) -> TaskwiseCoreUpdateResultV1:
        failure_metadata = {
            "request": request,
            "job_id": job_id,
            "artifact_id": artifact_id,
            "trajectory_digest": trajectory_digest,
            "safe_feedback_digest": safe_feedback_digest,
            "validator_input_digest": validator_input_digest,
            "dataset_manifest_sha256": dataset_manifest_sha256,
            "plan_digest": plan_digest,
            "reflector_input_digest": reflector_input_digest,
            "reflector_receipt_digest": reflector_receipt_digest,
            "reflector_event_stream_digest": reflector_event_stream_digest,
            "artifact_payload_sha256": None,
            "artifact_lineage_sha256": None,
            "memory_inspection_sha256": None,
            "validation_receipt_sha256": None,
            "core_artifact_manifest_sha256": None,
            "context_resolution_digest": None,
        }
        try:
            if artifact_id is None or reflector_input_digest is None:
                raise TaskwiseCoreEvolutionError("TASKWISE_TYPED_ARTIFACT_INVALID")
            artifact = self._store.get_artifact(artifact_id)
            if (
                artifact.type is not ArtifactType.TEXT_MEMORY
                or artifact.state is not ArtifactState.ACTIVE
                or artifact.promoted
            ):
                raise TaskwiseCoreEvolutionError("TASKWISE_TYPED_ARTIFACT_INVALID")
        except Exception as exc:
            self._raise_private_core_failure(
                stage="TYPED_ARTIFACT",
                default_finding="TASKWISE_TYPED_ARTIFACT_INVALID",
                cause=exc,
                **failure_metadata,
            )

        try:
            lineage = self._artifact_lineage(artifact_id)
            failure_metadata["artifact_lineage_sha256"] = canonical_digest(lineage)
            execution = lineage.get("openevo_execution")
            if (
                not isinstance(execution, dict)
                or execution.get("job_id") != job_id
                or execution.get("plan_id") != plan_id
                or execution.get("method_id") != METHOD_ID
                or execution.get("method_identity_digest") != self._method_identity_digest()
            ):
                raise TaskwiseCoreEvolutionError("TASKWISE_CORE_LINEAGE_INVALID")
            expected_input_ids = [
                dataset_artifact_id,
                *([] if request.predecessor is None else [request.predecessor.core_artifact_id]),
            ]
            if lineage.get("input_artifact_ids") != expected_input_ids:
                raise TaskwiseCoreEvolutionError("TASKWISE_PREDECESSOR_BINDING_INVALID")
        except Exception as exc:
            self._raise_private_core_failure(
                stage="ARTIFACT_LINEAGE",
                default_finding="TASKWISE_CORE_LINEAGE_INVALID",
                cause=exc,
                **failure_metadata,
            )

        try:
            payload_path = _safe_core_file_path(
                artifact.uri,
                allowed_root=self._store.files.root,
            )
            payload = _read_bounded_regular_file(
                payload_path,
                maximum=ABSOLUTE_MAX_MEMORY_FILE_BYTES,
            )
            payload_sha256 = _sha256_bytes(payload)
            inspection = inspect_taskwise_text_memory_v1(payload, limits=self._memory_limits)
            failure_metadata["artifact_payload_sha256"] = payload_sha256
            failure_metadata["memory_inspection_sha256"] = inspection.digest
            validator = TaskwiseTextMemoryValidatorV1(
                expected_lineage=expected_lineage,
                expected_record_count=request.update_index,
                forbidden_literals=forbidden_literals,
                memory_limits=self._memory_limits,
            )
            receipt = validator.validate(
                artifact=artifact,
                payload=payload,
                payload_sha256=payload_sha256,
                lineage=lineage,
            )
            failure_metadata["validation_receipt_sha256"] = receipt.digest
            if not receipt.passed or receipt.finding_codes:
                raise TaskwiseCoreEvolutionError("TASKWISE_ARTIFACT_VALIDATION_FAILED")
        except Exception as exc:
            self._raise_private_core_failure(
                stage="ARTIFACT_VALIDATION",
                default_finding="TASKWISE_ARTIFACT_VALIDATION_FAILED",
                cause=exc,
                **failure_metadata,
            )

        try:
            promoted = self._store.update_artifact_promotion(
                artifact_id,
                promoted=True,
            )
            if not promoted.promoted:
                raise TaskwiseCoreEvolutionError("TASKWISE_ARTIFACT_PROMOTION_FAILED")
            manifest_path = self._store.files.artifact_manifest_path(
                str(ArtifactType.TEXT_MEMORY),
                artifact_id,
            )
            core_artifact_manifest_sha256 = _sha256_bytes(
                _read_bounded_regular_file(
                    manifest_path,
                    maximum=MAX_MANIFEST_BYTES,
                )
            )
            failure_metadata["core_artifact_manifest_sha256"] = core_artifact_manifest_sha256
        except Exception as exc:
            self._raise_private_core_failure(
                stage="ARTIFACT_PROMOTION",
                default_finding="TASKWISE_ARTIFACT_PROMOTION_FAILED",
                cause=exc,
                **failure_metadata,
            )

        try:
            context = self._store.resolve_materialized_context(
                self._context_request(
                    artifact_id=artifact_id,
                    task_index=request.task_index,
                    update_index=request.update_index,
                )
            )
            failure_metadata["context_resolution_digest"] = canonical_digest(context)
            resolved_memory = _resolved_text_memory(
                context,
                expected_artifact_id=artifact_id,
            )
            return TaskwiseCoreUpdateResultV1(
                task_uid=request.task_uid,
                task_index=request.task_index,
                round_index=request.round_index,
                update_index=request.update_index,
                global_update_ordinal=global_update_ordinal,
                predecessor=request.predecessor,
                trajectory_ids=tuple(
                    trajectory.trajectory_id for trajectory in request.trajectories
                ),
                trajectory_digest=trajectory_digest,
                safe_feedback_digest=safe_feedback_digest,
                validator_input_digest=validator_input_digest,
                memory_limits_sha256=self._memory_limits.digest,
                memory_inspection=inspection,
                dataset_id=dataset_id,
                dataset_artifact_id=dataset_artifact_id,
                dataset_manifest_sha256=dataset_manifest_sha256,
                configured_max_records=request.update_index,
                records_visible_to_reflector=request.update_index,
                reflector_input_digest=reflector_input_digest,
                plan_id=plan_id,
                plan_digest=plan_digest,
                job_id=job_id,
                method_descriptor_digest=canonical_digest(
                    self._registry.snapshot.methods[METHOD_ID]
                ),
                method_identity_digest=self._method_identity_digest(),
                core_artifact_id=artifact_id,
                core_artifact_manifest_sha256=core_artifact_manifest_sha256,
                artifact_payload_sha256=payload_sha256,
                artifact_lineage_sha256=canonical_digest(lineage),
                validation_receipt=receipt,
                validation_receipt_sha256=receipt.digest,
                reflector_execution_receipt_sha256=reflector_receipt_digest,
                reflector_event_stream_sha256=reflector_event_stream_digest,
                core_context_id=context.context_id,
                context_resolution_digest=canonical_digest(context),
                resolved_memory=resolved_memory,
                resolved_memory_sha256=_sha256_bytes(resolved_memory.encode("utf-8")),
            )
        except Exception as exc:
            self._raise_private_core_failure(
                stage="CONTEXT_RESOLUTION",
                default_finding="TASKWISE_CONTEXT_RESOLUTION_FAILED",
                cause=exc,
                **failure_metadata,
            )

    def _raise_private_core_failure(
        self,
        *,
        stage: str,
        default_finding: str,
        cause: Exception,
        request: TaskwiseCoreUpdateRequestV1,
        job_id: str,
        artifact_id: str | None,
        trajectory_digest: str,
        safe_feedback_digest: str,
        validator_input_digest: str,
        dataset_manifest_sha256: str,
        plan_digest: str,
        reflector_input_digest: str | None,
        reflector_receipt_digest: str | None,
        reflector_event_stream_digest: str | None,
        artifact_payload_sha256: str | None,
        artifact_lineage_sha256: str | None,
        memory_inspection_sha256: str | None,
        validation_receipt_sha256: str | None,
        core_artifact_manifest_sha256: str | None,
        context_resolution_digest: str | None,
    ) -> NoReturn:
        """Persist closed stage evidence and re-raise a content-free error."""

        allowed_findings = _CORE_STAGE_ALLOWED_FINDINGS.get(stage, frozenset())
        if default_finding not in allowed_findings:
            raise TaskwiseCoreEvolutionError("TASKWISE_CORE_DIAGNOSTICS_FAILED")
        candidate_finding = (
            cause.finding_code if type(cause) is TaskwiseCoreEvolutionError else None
        )
        finding = candidate_finding if candidate_finding in allowed_findings else default_finding
        receipt = TaskwiseCoreFailureReceiptV1(
            stage=stage,
            finding_code=finding,
            exception_class=_closed_exception_class(cause),
            errno=(cause.errno if isinstance(cause, OSError) else None),
            task_index=request.task_index,
            round_index=request.round_index,
            update_index=request.update_index,
            job_id=job_id,
            artifact_id=artifact_id,
            predecessor_artifact_id=(
                None if request.predecessor is None else request.predecessor.core_artifact_id
            ),
            trajectory_digest=trajectory_digest,
            safe_feedback_digest=safe_feedback_digest,
            validator_input_digest=validator_input_digest,
            dataset_manifest_sha256=dataset_manifest_sha256,
            plan_digest=plan_digest,
            reflector_input_digest=reflector_input_digest,
            reflector_receipt_digest=reflector_receipt_digest,
            reflector_event_stream_digest=reflector_event_stream_digest,
            artifact_payload_sha256=artifact_payload_sha256,
            artifact_lineage_sha256=artifact_lineage_sha256,
            memory_inspection_sha256=memory_inspection_sha256,
            validation_receipt_sha256=validation_receipt_sha256,
            core_artifact_manifest_sha256=core_artifact_manifest_sha256,
            context_resolution_digest=context_resolution_digest,
        )
        encoded = (_canonical_json(receipt.model_dump(mode="json")) + "\n").encode("utf-8")
        basename = f"taskwise_core_failure_{canonical_digest(receipt)[:32]}.json"
        path = self._private_failure_root / basename
        created_by_this_call = False
        try:
            _exclusive_private_write(path, encoded)
            created_by_this_call = True
            metadata = path.lstat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
            ):
                raise OSError("private Core failure receipt permissions are unsafe")
        except Exception as diagnostics_error:
            if created_by_this_call:
                path.unlink(missing_ok=True)
            raise TaskwiseCoreEvolutionError(
                "TASKWISE_CORE_DIAGNOSTICS_FAILED"
            ) from diagnostics_error
        raise TaskwiseCoreEvolutionError(
            finding,
            diagnostic_receipt=basename,
        ) from cause

    def _artifact_lineage(self, artifact_id: str) -> dict[str, Any]:
        with self._store.connect() as connection:
            row = connection.execute(
                "SELECT lineage_json FROM artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise TaskwiseCoreEvolutionError("TASKWISE_CORE_LINEAGE_INVALID")
        try:
            lineage = json.loads(str(row["lineage_json"]))
        except json.JSONDecodeError as exc:
            raise TaskwiseCoreEvolutionError("TASKWISE_CORE_LINEAGE_INVALID") from exc
        if not isinstance(lineage, dict):
            raise TaskwiseCoreEvolutionError("TASKWISE_CORE_LINEAGE_INVALID")
        return lineage

    def _method_identity_digest(self) -> str:
        return self._registry.snapshot.identity_digest_for(
            DescriptorKind.METHOD,
            METHOD_ID,
        )

    def _require_method(self) -> None:
        snapshot = self._registry.snapshot
        if METHOD_ID not in snapshot.methods or METHOD_ID not in self._registry.method_handles:
            raise TaskwiseCoreEvolutionError("TASKWISE_REGISTERED_METHOD_MISSING")
        descriptor = snapshot.methods[METHOD_ID]
        if (
            descriptor.target_id != TARGET_ID
            or descriptor.output_artifact_types != ("text_memory",)
            or tuple(binding.binding_id for binding in descriptor.input_bindings)
            != ("dataset_inputs", "prior_target_artifacts")
        ):
            raise TaskwiseCoreEvolutionError("TASKWISE_REGISTERED_METHOD_INVALID")

    @staticmethod
    def _execution_profile() -> EvolutionExecutionProfile:
        return EvolutionExecutionProfile(
            execution_mode="subscription",
            capture_mode="transcript",
            harness_id="codex",
        )

    def _context_request(
        self,
        *,
        artifact_id: str,
        task_index: int,
        update_index: int,
    ) -> ContextProjectionResolveRequest:
        return ContextProjectionResolveRequest(
            task_id=(f"taskwise-task-{task_index:08d}-after-update-{update_index}"),
            instruction=("Resolve the approved taskwise text memory for the next round."),
            agent={
                "harness": "codex",
                "settings": {"auth_mode": "subscription"},
            },
            base_model=MODEL,
            policy_version=PROTOCOL_ID,
            metadata={
                "task_tags": [PROTOCOL_ID],
                "evolution": {"context_artifact_ids": [artifact_id]},
            },
            execution_profile=self._execution_profile(),
            destination_roots=RuntimeDestinationRoots(
                target_data="/openevo/session/evolution",
                harness_skills="/openevo/session/evolution/skills",
                harness_instruction="/workspace/repository",
            ),
            target_limits={
                TARGET_ID: TargetConsumptionLimits(
                    max_artifacts=1,
                    max_text_chars=self._memory_limits.max_utf8_bytes,
                    max_text_bytes=self._memory_limits.max_utf8_bytes,
                    max_payload_bytes=self._memory_limits.max_utf8_bytes,
                    max_adapters=0,
                )
            },
        )


class TaskwiseCoreUpdatePortAdapterV1:
    """Concrete runner port backed exclusively by the Core bridge above."""

    def __init__(
        self,
        bridge: TaskwiseCoreEvolutionBridgeV1,
        *,
        test_only_allow_synthetic_reflector: bool = False,
    ) -> None:
        if type(bridge) is not TaskwiseCoreEvolutionBridgeV1:
            raise TypeError("bridge must be exact TaskwiseCoreEvolutionBridgeV1")
        if type(test_only_allow_synthetic_reflector) is not bool:
            raise TypeError("synthetic reflector switch must be boolean")
        self._bridge = bridge
        self._test_only_allow_synthetic_reflector = test_only_allow_synthetic_reflector

    def update_text_memory(self, request: Any) -> Any:
        """Translate the runner DTO and return its typed rich Core outcome."""

        from openevo_chembench.taskwise_online_runner_v1 import (
            TaskwiseCoreUpdateOutcomeV1,
            TaskwiseMemoryPublicMetricsV1,
            TaskwiseRunnerCoreUpdateRequestV1,
        )

        if type(request) is not TaskwiseRunnerCoreUpdateRequestV1:
            raise TypeError("request must be exact TaskwiseRunnerCoreUpdateRequestV1")
        head = self._bridge.current_head()
        prior = request.prior_resolved_text_memory
        if head is None:
            if prior is not None:
                raise TaskwiseCoreEvolutionError("TASKWISE_RUNTIME_PREDECESSOR_FORK")
            predecessor = None
        else:
            if prior is None or (
                prior.core_artifact_id != head.core_artifact_id
                or prior.artifact_payload_sha256 != head.artifact_payload_sha256
                or prior.context_resolution_digest != head.context_resolution_digest
                or prior.resolved_memory_sha256 != head.resolved_memory_sha256
            ):
                raise TaskwiseCoreEvolutionError("TASKWISE_RUNTIME_PREDECESSOR_FORK")
            predecessor = head.predecessor_identity()
        result = self._bridge.apply_update(
            TaskwiseCoreUpdateRequestV1(
                task_uid=request.task_uid,
                task_index=request.task_index,
                round_index=request.source_round_index,
                update_index=request.update_index,
                trajectories=request.trajectories,
                predecessor=predecessor,
                validator_forbidden_literals=tuple(
                    literal
                    for trajectory in request.trajectories
                    for literal in _trajectory_forbidden_literals(trajectory)
                ),
            ),
            test_only_allow_synthetic_reflector=(self._test_only_allow_synthetic_reflector),
        )
        memory = self._bridge.issue_runtime_memory(result)
        return TaskwiseCoreUpdateOutcomeV1(
            job_id=result.job_id,
            job_state="COMPLETED",
            core_context_id=result.core_context_id,
            validation_receipt_sha256=result.validation_receipt_sha256,
            memory_metrics=TaskwiseMemoryPublicMetricsV1(
                memory_limits_sha256=result.memory_inspection.memory_limits_sha256,
                inspection_sha256=result.memory_inspection.digest,
                token_estimator_id=result.memory_inspection.token_estimator_id,
                parser_id=result.memory_inspection.parser_id,
                utf8_byte_count=result.memory_inspection.utf8_byte_count,
                estimated_token_count=result.memory_inspection.estimated_token_count,
                section_item_counts=result.memory_inspection.section_item_counts,
                total_section_items=result.memory_inspection.total_section_items,
                max_section_items=result.memory_inspection.max_section_items,
            ),
            resolved_text_memory=memory,
        )

    def resolve_text_memory(self, reference: Any) -> CoreResolvedTextMemoryV2:
        """Resolve only the exact verified global head; historical rollback is forbidden."""

        from openevo_chembench.taskwise_online_runner_v1 import (
            CoreMemoryReferenceV1,
        )

        if type(reference) is not CoreMemoryReferenceV1:
            raise TypeError("reference must be exact CoreMemoryReferenceV1")
        result = self._bridge.current_head()
        if result is None or (
            reference.core_artifact_id != result.core_artifact_id
            or reference.artifact_payload_sha256 != result.artifact_payload_sha256
            or reference.context_resolution_digest != result.context_resolution_digest
            or reference.resolved_memory_sha256 != result.resolved_memory_sha256
        ):
            raise TaskwiseCoreEvolutionError("TASKWISE_RUNTIME_MEMORY_REFERENCE_INVALID")
        return self._bridge.issue_runtime_memory(result)

    def close(self) -> None:
        """Release the benchmark-layer single-writer Core stream lock."""

        self._bridge.close()


def build_taskwise_core_port_at_roots_v1(
    *,
    state_root: Path,
    framework_lock: Path,
    timeout_seconds: int,
    memory_limits: TaskwiseMemoryLimitsV1 = TASKWISE_MEMORY_LIMITS_V1,
) -> TaskwiseCoreUpdatePortAdapterV1:
    """Build the production Core port at caller-owned, explicit private roots.

    Output-directory parity and new-run/resume policy remain controller
    responsibilities.  This helper owns only the Core state subtree and keeps
    both the formal taskwise CLI and bounded infrastructure smokes on the same
    registered-method, reflector-boundary, artifact, and context path.
    """

    if (
        not isinstance(state_root, Path)
        or not state_root.is_absolute()
        or not isinstance(framework_lock, Path)
        or not framework_lock.is_absolute()
    ):
        raise TypeError("Core state and framework lock roots must be absolute Paths")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 0 < timeout_seconds <= 86_400
    ):
        raise ValueError("Core reflector timeout must be positive and bounded")
    if type(memory_limits) is not TaskwiseMemoryLimitsV1:
        raise TypeError("memory_limits must be exact TaskwiseMemoryLimitsV1")
    if not framework_lock.is_file():
        raise TaskwiseCoreEvolutionError("TASKWISE_VERIFIED_FRAMEWORK_LOCK_MISSING")

    capability = ReflectorExecutionBoundaryV2.detect_capability()
    if not capability.available:
        raise TaskwiseCoreEvolutionError("REFLECTOR_FILESYSTEM_ISOLATION_MISSING")
    _prepare_private_state_root(state_root)
    private_audit_root = state_root / "private_reflector_events"

    probe_record = {
        "uid": "0" * 64,
        "source_split": TASKWISE_SOURCE_SPLIT,
    }
    probe_digest = canonical_digest([probe_record])
    with tempfile.TemporaryDirectory(
        prefix=".taskwise-reflector-preflight-",
        dir=state_root,
    ) as temporary:
        probe_path = Path(temporary) / "records.jsonl"
        _exclusive_private_write(probe_path, (_canonical_json(probe_record) + "\n").encode())
        boundary = ReflectorExecutionBoundaryV2(
            dev_artifact_path=probe_path,
            expected_records_sha256=probe_digest,
            expected_record_count=1,
            expected_source_split=TASKWISE_SOURCE_SPLIT,
            private_audit_root=private_audit_root,
            timeout_seconds=timeout_seconds,
        )
        if not boundary.preflight().available:
            raise TaskwiseCoreEvolutionError("REFLECTOR_FILESYSTEM_ISOLATION_MISSING")

    def boundary_factory(
        artifact_path: Path,
        records_sha256: str,
        record_count: int,
    ) -> ReflectorExecutionBoundaryV2:
        return ReflectorExecutionBoundaryV2(
            dev_artifact_path=artifact_path,
            expected_records_sha256=records_sha256,
            expected_record_count=record_count,
            expected_source_split=TASKWISE_SOURCE_SPLIT,
            private_audit_root=private_audit_root,
            timeout_seconds=timeout_seconds,
        )

    bridge = TaskwiseCoreEvolutionBridgeV1(
        db_path=state_root / "evolution.sqlite3",
        artifact_root=state_root / "artifacts",
        executable_registry=load_verified_framework_registry(framework_lock),
        reflector_boundary_factory=boundary_factory,
        checkpoint_path=state_root / "private_lineage_checkpoints.jsonl",
        memory_limits=memory_limits,
    )
    return TaskwiseCoreUpdatePortAdapterV1(bridge)


def _prepare_private_state_root(path: Path) -> None:
    """Create or validate one non-symlink, owner-private Core state root."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise TaskwiseCoreEvolutionError("TASKWISE_CORE_STATE_ROOT_UNSAFE")
    else:
        path.mkdir(mode=0o700)
    path.chmod(0o700)
    metadata = path.lstat()
    if stat.S_IMODE(metadata.st_mode) != 0o700 or (
        hasattr(os, "getuid") and metadata.st_uid != os.getuid()
    ):
        raise TaskwiseCoreEvolutionError("TASKWISE_CORE_STATE_ROOT_UNSAFE")


def _prepare_private_directory(path: Path) -> None:
    """Create one owner-private non-symlink diagnostic directory."""

    if path.exists():
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise TaskwiseCoreEvolutionError("TASKWISE_CORE_DIAGNOSTICS_UNSAFE")
    else:
        path.mkdir(mode=0o700)
    path.chmod(0o700)
    metadata = path.lstat()
    if stat.S_IMODE(metadata.st_mode) != 0o700 or (
        hasattr(os, "getuid") and metadata.st_uid != os.getuid()
    ):
        raise TaskwiseCoreEvolutionError("TASKWISE_CORE_DIAGNOSTICS_UNSAFE")


def _closed_exception_class(exc: Exception) -> str:
    if type(exc) is TaskwiseCoreEvolutionError:
        return "TASKWISE_CORE_EVOLUTION_ERROR"
    if isinstance(exc, OSError):
        return "OS_ERROR"
    if isinstance(exc, TypeError):
        return "TYPE_ERROR"
    if isinstance(exc, ValueError):
        return "VALUE_ERROR"
    if isinstance(exc, RuntimeError):
        return "RUNTIME_ERROR"
    return "OTHER_ERROR"


def _trajectory_forbidden_literals(
    trajectory: TaskwiseTrajectoryV1,
) -> tuple[str, ...]:
    """Derive question/option lines without persisting a second private DTO."""

    values: list[str] = [
        trajectory.task_uid,
        trajectory.public_prompt,
        trajectory.raw_completion,
    ]
    for line in trajectory.public_prompt.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        values.append(stripped)
        for prefix in ("Question:", "A.", "B.", "C.", "D."):
            if stripped.startswith(prefix):
                remainder = stripped[len(prefix) :].strip()
                if remainder:
                    values.append(remainder)
    return _deduplicate_literals(values)


def _deduplicate_literals(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if type(value) is not str:
            raise TypeError("validator literals must be strings")
        stripped = value.strip()
        if not stripped:
            continue
        key = stripped.casefold()
        if key not in seen:
            seen.add(key)
            result.append(stripped)
    return tuple(result)


def _exclusive_private_write(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _normalize_rule(value: str) -> str:
    return "".join(character.casefold() for character in value if character.isalnum())


def _sequential_ngram_overlap(source: str, candidate_ngrams: set[str]) -> bool:
    if len(source) < 24:
        return False
    consecutive = 0
    for index in range(len(source) - 23):
        if source[index : index + 24] in candidate_ngrams:
            consecutive += 1
            if consecutive >= 4:
                return True
        else:
            consecutive = 0
    return False


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validator_input_digest(values: tuple[str, ...]) -> str:
    return _sha256_bytes(_canonical_json(list(values)).encode("utf-8"))


__all__ = [
    "build_taskwise_core_port_at_roots_v1",
    "BRIDGE_ID",
    "METHOD_ID",
    "PROTOCOL_ID",
    "ReflectorBoundaryFactoryV1",
    "TaskwiseArtifactLineageReceiptV1",
    "TaskwiseCoreEvolutionBridgeV1",
    "TaskwiseCoreEvolutionError",
    "TaskwiseCoreFailureReceiptV1",
    "TaskwiseCorePredecessorV1",
    "TaskwiseCoreUpdatePortAdapterV1",
    "TaskwiseCoreUpdateRequestV1",
    "TaskwiseCoreUpdateResultV1",
    "TaskwiseTextMemoryValidatorV1",
]
