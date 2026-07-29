"""Global taskwise online evolution through the real OpenEvo Core lifecycle.

Each update is a complete current-task trajectory prefix.  The bridge ingests
that prefix as Core events, creates a new immutable dataset artifact and
plan-bound job, dispatches the registered ``text_memory_expel_reflector``
method, accepts only its typed Core artifact, validates and promotes it, and
finally resolves the artifact through Core context projection.  Supervised
transfer additionally binds the reflector's closed skill and agent-system
projections and runs each through its own verified plan-bound Core lifecycle.

The lineage is one global stream across tasks.  Task ``N`` update 1 must name
task ``N-1`` update 2 as its predecessor.  Adapter-local reflector/artifact
classes are intentionally absent.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NoReturn, Self

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
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
from openevo_chembench.core_evolution_v2 import (
    CoreArtifactValidationReceiptV2,
    _read_bounded_regular_file,
    _resolved_text_memory,
    _safe_core_file_path,
    _StoreWorkerClient,
)
from openevo_chembench.frozen_runtime_v2 import (
    CoreResolvedTextMemoryV2,
    _issue_core_resolved_text_memory_v2,
)
from openevo_chembench.supervised_transfer_v2.artifacts import (
    CoreResolvedSupervisedContextV2,
    SupervisedAuxiliaryInspectionV2,
    _issue_core_resolved_auxiliary_v2,
    inspect_supervised_auxiliary_artifact_v2,
)
from openevo_chembench.supervised_transfer_v2.memory import (
    SUPERVISED_MEMORY_LIMITS_V2,
    SupervisedMemoryInspectionV2,
    SupervisedMemoryLimitsV2,
    inspect_supervised_category_memory_v2,
)
from openevo_chembench.supervised_transfer_v2.packet import (
    PACKET_INPUT_SCHEMA_DIGEST,
    REFLECTOR_PROMPT_DIGEST,
    SupervisedEvolutionPacketV2,
)
from openevo_chembench.supervised_transfer_v2.reflector_boundary import (
    SUPERVISED_TRAIN_SOURCE_SPLIT,
    TASKWISE_SOURCE_SPLIT,
    ReflectorBoundaryStatusV2,
    ReflectorExecutionBoundaryV2,
    SupervisedStructuredAuxiliaryOutputV2,
)
from openevo_chembench.supervised_transfer_v2.trajectory import (
    SupervisedTrajectoryV2,
    ordered_source_execution_provenance_digest_v2,
    ordered_supervised_safe_feedback_digest_v2,
    ordered_supervised_trajectory_digest_v2,
)
from openevo_chembench.taskwise_config_v1 import (
    TASKWISE_MEMORY_LIMITS_V1,
    TaskwiseMemoryLimitsV1,
)
from openevo_chembench.taskwise_feedback_v1 import (
    TaskwiseSafeSignalCodeV1,
)

PROTOCOL_ID = "chembench_supervised_transfer_v2"
SUPERVISED_PROTOCOL_ID = PROTOCOL_ID
BRIDGE_ID = "openevo_core_supervised_three_target_v2"
METHOD_ID = "text_memory_expel_reflector"
REFLECTOR_PROJECTION_ID = "supervised_reflector_trajectory_projection_v2"
SUPERVISED_REFLECTOR_PROJECTION_ID = "SupervisedEvolutionPacketV2"
TARGET_ID = "text_memory"
_AUXILIARY_TARGET_METHODS = (
    ("skill_bundle", "skill_bundle"),
    ("agent_system", "agent_system"),
)
MODEL = "gpt-5.5"
CORE_LEASE_GRACE_SECONDS = 120
MAX_CORE_LEASE_SECONDS = 86_400
DEFAULT_REFLECTOR_TIMEOUT_SECONDS = 600
ABSOLUTE_MAX_MEMORY_FILE_BYTES = 64 * 1024
_MAX_AUXILIARY_PAYLOAD_BYTES = 24_576
MAX_MANIFEST_BYTES = 1024 * 1024
_EVENT_TYPE = "openevo.session_completed"
_EVENT_SOURCE = "chembench.supervised_transfer_v2"
_DATASET_PURPOSE = "chembench_supervised_trajectory_v2"
_SUPERVISED_DATASET_PURPOSE = "chembench_supervised_evolution_packet_v2"
_JOB_PREFIX = "chembench.supervised_transfer_v2.text_memory"
_PRIVATE_INPUT_DIRECTORY = "private_supervised_v2_reflector_inputs"
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
_AUXILIARY_TERMINAL_FINDINGS = frozenset(
    {
        "TASKWISE_ARTIFACT_VALIDATION_FAILED",
        "TASKWISE_ARTIFACT_PROMOTION_FAILED",
        "TASKWISE_CONTEXT_IDENTITY_DRIFT",
        "TASKWISE_CORE_JOB_FAILED",
        "TASKWISE_CORE_LINEAGE_INVALID",
        "TASKWISE_PREDECESSOR_BINDING_INVALID",
        "TASKWISE_REGISTERED_METHOD_INVALID",
        "TASKWISE_TYPED_ARTIFACT_INVALID",
    }
)
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
_REFLECTOR_PROJECTED_PROMPT = (
    "Task text is sealed. Derive reusable strategy only from the closed "
    "evolution-feedback taxonomy and the general chemistry category."
)
_REFLECTOR_PROJECTED_RESPONSE = (
    "Attempt content is sealed. Outcome evidence is represented only by the "
    "closed taxonomy and reward."
)
_PATH_OR_BENCHMARK_RE = re.compile(
    r"(?:chembench(?:4k)?|ai4chem|opencompass|"
    r"(?<![A-Za-z0-9_])(?:dev|test|results?|test[-_ ]data)[\\/]|"
    r"(?<![A-Za-z0-9_])(?:private[-_ ]manifest|answer[-_ ]cache)"
    r"(?![A-Za-z0-9_])|"
    r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]|"
    r"(?<![A-Za-z0-9_])\\\\[^\\\s]+\\[^\\\s]+|"
    r"\bfile://|"
    r"\.(?:json|jsonl|parquet|sqlite3?|ya?ml)\b)",
    re.IGNORECASE | re.MULTILINE,
)
_POSIX_ABSOLUTE_PATH_RE = re.compile(
    r"/(?:[\w.~+@%=-]+/)+[\w.~+@%=-]+",
    re.UNICODE,
)
_POSIX_PATH_OPENING_BOUNDARY = frozenset("\"'([{:=<")
_OPERATIONAL_IDENTIFIER_RE = re.compile(
    r"(?:(?<![A-Za-z0-9])(?:job|art|ds)_[A-Za-z0-9_.:-]{6,}|"
    r"\btaskwise[-_. ](?:reflector|safe_signal|safe|core|online)"
    r"(?![A-Za-z0-9])|"
    r"\bopenevo_core_taskwise(?![A-Za-z0-9])|"
    r"\b(?:source(?:[\s_.-]+)(?:row|index)|row(?:[\s_.-]+)index)"
    r"\s*(?::|=|#|-)?\s*\d+\b|"
    r"\buid\s*(?::|=|->|→)\s*[A-Za-z0-9][A-Za-z0-9_.:-]{5,}\b)",
    re.IGNORECASE,
)
_OPTION_TOKEN_PREFIX_RE = r"(?<![A-Za-z0-9_])"
_OPTION_CHEMICAL_SUFFIX_RE = r"(?![-‐‑‒–—=][A-Za-z0-9])"
_UPPER_OPTION_TOKEN_RE = rf"{_OPTION_TOKEN_PREFIX_RE}[ABCD]\b{_OPTION_CHEMICAL_SUFFIX_RE}"
_EXPLICIT_OPTION_TOKEN_RE = rf"{_OPTION_TOKEN_PREFIX_RE}[ABCDabcd]\b{_OPTION_CHEMICAL_SUFFIX_RE}"
_TERMINAL_OPTION_TOKEN_RE = (
    rf"{_OPTION_TOKEN_PREFIX_RE}[ABCD]\b{_OPTION_CHEMICAL_SUFFIX_RE}"
    r"(?=\s*(?:\Z|[.,;:!?]))"
)
_ANSWER_MAP_SEPARATOR_RE = r"(?:is|=|:|->|→)"
_ANSWER_MAP_RE = re.compile(
    rf"(?i:(?:the\s+)?(?:correct\s+)?answer\s*{_ANSWER_MAP_SEPARATOR_RE})\s*"
    rf"{_UPPER_OPTION_TOKEN_RE}|"
    rf"(?i:(?:choose|select|pick)\s+option\s*"
    rf"(?:{_ANSWER_MAP_SEPARATOR_RE})?\s*){_EXPLICIT_OPTION_TOKEN_RE}|"
    rf"(?i:(?:choose|select|pick)\s+){_TERMINAL_OPTION_TOKEN_RE}|"
    rf"(?i:(?:return|output)\s*(?:only\s+)?option\s*"
    rf"(?:{_ANSWER_MAP_SEPARATOR_RE})?\s*){_EXPLICIT_OPTION_TOKEN_RE}|"
    rf"(?i:(?:return|output)\s*(?:only\s+)?"
    rf"(?:{_ANSWER_MAP_SEPARATOR_RE})?\s*){_TERMINAL_OPTION_TOKEN_RE}|"
    rf"(?i:(?:final\s+)?(?:response|prediction|letter|choice)\s*"
    rf"{_ANSWER_MAP_SEPARATOR_RE}\s*){_TERMINAL_OPTION_TOKEN_RE}|"
    rf"{_UPPER_OPTION_TOKEN_RE}\s+"
    r"(?i:(?:is|should\s+be|must\s+be)\s+(?:the\s+)?"
    r"(?:correct|selected|chosen)(?:\s+(?:answer|choice|option))?)\b|"
    rf"(?i:\boption\s*(?:{_ANSWER_MAP_SEPARATOR_RE})?\s*)"
    rf"{_EXPLICIT_OPTION_TOKEN_RE}|"
    r"(?i:(?:question|item|index)"
    r"(?:\s+(?:\d+|uid|[A-Za-z0-9_.-]{6,}))?|uid"
    r"(?:\s+[A-Za-z0-9_.:-]{6,})?)\s*"
    rf"(?:->|→|=|:)\s*{_UPPER_OPTION_TOKEN_RE}|"
    r"(?i:(?:question|item|index)"
    r"(?:\s+(?:\d+|uid|[A-Za-z0-9_.-]{6,}))?|uid"
    r"(?:\s+[A-Za-z0-9_.:-]{6,})?)\s+"
    rf"maps?\s+to\s+{_UPPER_OPTION_TOKEN_RE}|"
    rf"{_UPPER_OPTION_TOKEN_RE}\s+"
    r"(?i:(?:is\s+)?(?:the\s+)?(?:correct\s+)?answer)\b",
)
_ANSWER_MAP_WRAPPER_RE = re.compile(r"""[*_`~()[\]{}"'“”‘’]""")
_VALIDATOR_LITERAL_PREFIX = "@chembench-validator-literal-v1:"
_VALIDATOR_LITERAL_KINDS = frozenset(
    {
        "completion",
        "option",
        "question",
        "uid",
    }
)
_NGRAM_WIDTH = 24
_NGRAM_CONSECUTIVE_MATCHES = 4
_NGRAM_CONTIGUOUS_WIDTH = _NGRAM_WIDTH + _NGRAM_CONSECUTIVE_MATCHES - 1


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


class TaskwiseValidatorFindingEvidenceV1(_FrozenModel):
    """Content-free measurements for one deterministic validator finding."""

    schema_version: Literal["chembench4k_taskwise_validator_finding_evidence_v1"] = (
        "chembench4k_taskwise_validator_finding_evidence_v1"
    )
    finding_code: str
    source_kind: Literal[
        "artifact",
        "candidate",
        "completion",
        "lineage",
        "option",
        "question",
        "strict",
        "uid",
    ]
    token_count: int = Field(ge=0)
    char_count: int = Field(ge=0)
    left_token_boundary: bool
    right_token_boundary: bool
    source_ratio_ppm: int = Field(ge=0, le=1_000_000)
    candidate_ratio_ppm: int = Field(ge=0, le=1_000_000)
    segment_sha256: str

    @field_validator("finding_code")
    @classmethod
    def _finding(cls, value: str) -> str:
        if re.fullmatch(r"[a-z][a-z0-9_]{0,95}", value) is None:
            raise ValueError("validator finding code is outside the closed vocabulary")
        return value

    @field_validator("segment_sha256")
    @classmethod
    def _segment_digest(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("validator segment digest must be SHA-256")
        return value


class TaskwiseCoreFailureReceiptV1(_FrozenModel):
    """Private content-free evidence for a failed Core update stage."""

    schema_version: Literal["chembench_supervised_core_failure_v2"] = (
        "chembench_supervised_core_failure_v2"
    )
    protocol_id: Literal["chembench_supervised_transfer_v2"] = PROTOCOL_ID
    bridge_id: Literal["openevo_core_supervised_three_target_v2"] = BRIDGE_ID
    stage: str
    finding_code: str
    exception_class: str
    errno: int | None
    task_index: int = Field(ge=0)
    round_index: Literal[0, 1, 2]
    update_index: Literal[1, 2, 3]
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
    validator_finding_evidence: tuple[TaskwiseValidatorFindingEvidenceV1, ...] = ()

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
        evidence_keys = tuple(
            (
                item.finding_code,
                item.source_kind,
                item.segment_sha256,
            )
            for item in self.validator_finding_evidence
        )
        if evidence_keys != tuple(sorted(set(evidence_keys))):
            raise ValueError("validator finding evidence must be sorted and unique")
        if self.stage != "ARTIFACT_VALIDATION" and self.validator_finding_evidence:
            raise ValueError("validator finding evidence is invalid outside validation")
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
    if any(
        not any(character.isalnum() for character in _security_scan_text(item))
        for section_items in items_by_section
        for item in section_items
    ):
        findings.add("memory_item_alnum_missing")
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
    round_index: Literal[0, 1, 2]
    update_index: Literal[1, 2, 3]
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
    protocol_id: Literal["chembench_supervised_transfer_v2"] = PROTOCOL_ID
    reflector_projection_id: Literal["supervised_reflector_trajectory_projection_v2"] = (
        REFLECTOR_PROJECTION_ID
    )
    task_uid: str
    task_index: int = Field(ge=0)
    round_index: Literal[0, 1, 2]
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


class SupervisedArtifactLineageReceiptV1(_FrozenModel):
    """Private content-free receipt for one answer-supervised category update."""

    schema_version: Literal["chembench_supervised_artifact_lineage_v2"] = (
        "chembench_supervised_artifact_lineage_v2"
    )
    protocol_id: Literal["chembench_supervised_transfer_v2"]
    reflector_projection_id: Literal["SupervisedEvolutionPacketV2"]
    task_uid: str
    task_index: int = Field(ge=0)
    round_index: Literal[0, 1, 2]
    category: str
    supervised_packet_sha256: str
    input_schema_sha256: str
    reflector_prompt_sha256: str
    predecessor_artifact_id: str | None
    predecessor_memory_sha256: str | None
    trajectory_ids: tuple[str, ...]
    trajectory_digest: str
    source_execution_provenance_sha256: str
    safe_feedback_digest: str
    memory_limits_sha256: str
    memory_inspection_sha256: str
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
        "supervised_packet_sha256",
        "input_schema_sha256",
        "reflector_prompt_sha256",
        "predecessor_memory_sha256",
        "trajectory_digest",
        "source_execution_provenance_sha256",
        "safe_feedback_digest",
        "memory_limits_sha256",
        "memory_inspection_sha256",
        "artifact_payload_sha256",
        "context_resolution_digest",
    )
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None and _SHA256_RE.fullmatch(value) is None:
            raise ValueError("supervised lineage receipt digest must be SHA-256")
        return value

    @model_validator(mode="after")
    def _memory(self) -> SupervisedArtifactLineageReceiptV1:
        if (
            self.category not in CHEMBENCH4K_CATEGORIES
            or self.memory_limits_sha256 != SUPERVISED_MEMORY_LIMITS_V2.digest
            or self.input_schema_sha256 != PACKET_INPUT_SCHEMA_DIGEST
            or self.reflector_prompt_sha256 != REFLECTOR_PROMPT_DIGEST
            or self.utf8_byte_count > SUPERVISED_MEMORY_LIMITS_V2.max_utf8_bytes
            or self.estimated_token_count > SUPERVISED_MEMORY_LIMITS_V2.max_estimated_tokens
            or len(self.section_item_counts)
            != len(SUPERVISED_MEMORY_LIMITS_V2.required_sections)
            + len(SUPERVISED_MEMORY_LIMITS_V2.core_compatibility_sections)
        ):
            raise ValueError("supervised lineage receipt violates protocol limits")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self)


class SupervisedAuxiliaryCoreArtifactV2(_FrozenModel):
    """Complete Core identity for one supervised skill or agent-system artifact."""

    schema_version: Literal["SupervisedAuxiliaryCoreArtifactV2"] = (
        "SupervisedAuxiliaryCoreArtifactV2"
    )
    target_id: Literal["skill_bundle", "agent_system"]
    method_id: Literal["skill_bundle", "agent_system"]
    plan_id: str
    plan_digest: str
    job_id: str
    method_descriptor_digest: str
    method_identity_digest: str
    core_artifact_id: str
    core_artifact_manifest_sha256: str
    artifact_payload_sha256: str
    artifact_lineage_sha256: str
    source_component_sha256: str
    inspection: SupervisedAuxiliaryInspectionV2
    inspection_sha256: str
    core_context_id: str
    context_resolution_digest: str
    resolved_content: str
    resolved_content_sha256: str

    @field_validator(
        "plan_digest",
        "method_descriptor_digest",
        "method_identity_digest",
        "core_artifact_manifest_sha256",
        "artifact_payload_sha256",
        "artifact_lineage_sha256",
        "source_component_sha256",
        "inspection_sha256",
        "context_resolution_digest",
        "resolved_content_sha256",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("auxiliary Core digest must be SHA-256")
        return value

    @model_validator(mode="after")
    def _binding(self) -> SupervisedAuxiliaryCoreArtifactV2:
        if (
            self.target_id != self.method_id
            or self.inspection.target_id != self.target_id
            or not self.inspection.passed
            or self.inspection.digest != self.inspection_sha256
            or _sha256_bytes(self.resolved_content.encode("utf-8")) != self.resolved_content_sha256
        ):
            raise ValueError("auxiliary Core artifact binding is invalid")
        return self


class TaskwiseCoreUpdateResultV1(_FrozenModel):
    """Core evidence for one approved global update."""

    schema_version: Literal["chembench_supervised_core_update_v2"] = (
        "chembench_supervised_core_update_v2"
    )
    protocol_id: Literal["chembench_supervised_transfer_v2"] = PROTOCOL_ID
    bridge_id: Literal["openevo_core_supervised_three_target_v2"] = BRIDGE_ID
    task_uid: str
    task_index: int = Field(ge=0)
    round_index: Literal[0, 1, 2]
    update_index: Literal[1, 2, 3]
    global_update_ordinal: int = Field(ge=1)
    predecessor: TaskwiseCorePredecessorV1 | None
    trajectory_ids: tuple[str, ...]
    trajectory_digest: str
    source_execution_provenance_sha256: str
    safe_feedback_digest: str
    validator_input_digest: str
    memory_limits_sha256: str
    memory_inspection: TaskwiseMemoryInspectionV1 | SupervisedMemoryInspectionV2
    dataset_id: str
    dataset_artifact_id: str
    dataset_manifest_sha256: str
    configured_max_records: int = Field(ge=1, le=1024)
    records_visible_to_reflector: int = Field(ge=1, le=1024)
    reflector_input_digest: str
    reflector_timeout_seconds: int = Field(gt=0)
    core_lease_seconds: int = Field(gt=0)
    core_lease_grace_seconds: Literal[120] = CORE_LEASE_GRACE_SECONDS
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
    supervised_packet_sha256: str | None = None
    supervised_category: str | None = None
    supervised_input_schema_sha256: str | None = None
    supervised_reflector_prompt_sha256: str | None = None
    supervised_auxiliary_artifacts: tuple[SupervisedAuxiliaryCoreArtifactV2, ...] = ()

    @field_validator(
        "task_uid",
        "trajectory_digest",
        "source_execution_provenance_sha256",
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
        "supervised_packet_sha256",
        "supervised_input_schema_sha256",
        "supervised_reflector_prompt_sha256",
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
        supervised = self.supervised_packet_sha256 is not None
        if (
            self.update_index != self.round_index + 1
            or len(self.trajectory_ids) != self.update_index
            or self.configured_max_records != self.records_visible_to_reflector
            or (not supervised and self.configured_max_records != self.update_index)
            or self.core_lease_seconds != _core_lease_seconds(self.reflector_timeout_seconds)
        ):
            raise ValueError("update evidence does not bind the complete round prefix")
        if (self.predecessor is None) != (self.global_update_ordinal == 1):
            raise ValueError("only the first global update can omit a predecessor")
        if self.predecessor is not None and (
            self.predecessor.global_update_ordinal + 1 != self.global_update_ordinal
        ):
            raise ValueError("predecessor is not the immediate global update")
        if self.global_update_ordinal != self.task_index * 3 + self.update_index:
            raise ValueError("global update ordinal does not match stream position")
        if self.predecessor is None:
            if self.task_index != 0 or self.round_index != 0 or self.update_index != 1:
                raise ValueError("the global stream must start at task zero update one")
        elif self.predecessor.update_index in (1, 2):
            if (
                self.task_uid != self.predecessor.task_uid
                or self.task_index != self.predecessor.task_index
                or self.round_index != self.predecessor.round_index + 1
                or self.update_index != self.predecessor.update_index + 1
            ):
                raise ValueError("same-task updates must be immediate")
        elif (
            self.task_uid == self.predecessor.task_uid
            or self.task_index != self.predecessor.task_index + 1
            or self.round_index != 0
            or self.update_index != 1
        ):
            raise ValueError("the next task must start from the prior task final memory")
        if _sha256_bytes(self.resolved_memory.encode("utf-8")) != self.resolved_memory_sha256:
            raise ValueError("resolved memory digest mismatch")
        if supervised:
            if (
                type(self.memory_inspection) is not SupervisedMemoryInspectionV2
                or self.memory_limits_sha256 != SUPERVISED_MEMORY_LIMITS_V2.digest
                or self.memory_inspection.memory_limits_sha256 != self.memory_limits_sha256
                or self.supervised_category != self.memory_inspection.category
                or self.supervised_input_schema_sha256 != PACKET_INPUT_SCHEMA_DIGEST
                or self.supervised_reflector_prompt_sha256 != REFLECTOR_PROMPT_DIGEST
                or not self.memory_inspection.passed
                or tuple(artifact.target_id for artifact in self.supervised_auxiliary_artifacts)
                != ("skill_bundle", "agent_system")
                or any(
                    artifact.inspection.category != self.supervised_category
                    for artifact in self.supervised_auxiliary_artifacts
                )
            ):
                raise ValueError("supervised memory inspection is not approved")
        elif (
            type(self.memory_inspection) is not TaskwiseMemoryInspectionV1
            or self.memory_limits_sha256 != TASKWISE_MEMORY_LIMITS_V1.digest
            or self.memory_inspection.memory_limits_sha256 != self.memory_limits_sha256
            or self.supervised_category is not None
            or self.supervised_input_schema_sha256 is not None
            or self.supervised_reflector_prompt_sha256 is not None
            or self.supervised_auxiliary_artifacts
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

    def required_lineage_receipt(
        self,
    ) -> TaskwiseArtifactLineageReceiptV1 | SupervisedArtifactLineageReceiptV1:
        """Expose the exact closed lineage contract required by the protocol."""

        if self.supervised_packet_sha256 is not None:
            if (
                self.supervised_category is None
                or self.supervised_input_schema_sha256 is None
                or self.supervised_reflector_prompt_sha256 is None
                or type(self.memory_inspection) is not SupervisedMemoryInspectionV2
            ):
                raise ValueError("supervised result lineage fields are incomplete")
            return SupervisedArtifactLineageReceiptV1(
                protocol_id=SUPERVISED_PROTOCOL_ID,
                reflector_projection_id=SUPERVISED_REFLECTOR_PROJECTION_ID,
                task_uid=self.task_uid,
                task_index=self.task_index,
                round_index=self.round_index,
                category=self.supervised_category,
                supervised_packet_sha256=self.supervised_packet_sha256,
                input_schema_sha256=self.supervised_input_schema_sha256,
                reflector_prompt_sha256=self.supervised_reflector_prompt_sha256,
                predecessor_artifact_id=(
                    None if self.predecessor is None else self.predecessor.core_artifact_id
                ),
                predecessor_memory_sha256=(
                    None if self.predecessor is None else self.predecessor.resolved_memory_sha256
                ),
                trajectory_ids=self.trajectory_ids,
                trajectory_digest=self.trajectory_digest,
                source_execution_provenance_sha256=(
                    self.source_execution_provenance_sha256
                ),
                safe_feedback_digest=self.safe_feedback_digest,
                memory_limits_sha256=self.memory_limits_sha256,
                memory_inspection_sha256=self.memory_inspection.digest,
                utf8_byte_count=self.memory_inspection.utf8_byte_count,
                estimated_token_count=self.memory_inspection.estimated_token_count,
                section_item_counts=self.memory_inspection.section_item_counts,
                evolution_plan_id=self.plan_id,
                evolution_job_id=self.job_id,
                core_artifact_id=self.core_artifact_id,
                artifact_payload_sha256=self.artifact_payload_sha256,
                context_resolution_digest=self.context_resolution_digest,
            )
        return TaskwiseArtifactLineageReceiptV1(
            protocol_id=self.protocol_id,
            reflector_projection_id=REFLECTOR_PROJECTION_ID,
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
    round_index: Literal[0, 1, 2]
    update_index: Literal[1, 2, 3]
    trajectories: tuple[SupervisedTrajectoryV2, ...]
    predecessor: TaskwiseCorePredecessorV1 | None
    validator_forbidden_literals: tuple[str, ...] = ()
    supervised_packet: SupervisedEvolutionPacketV2 | None = None

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
        if self.round_index not in (0, 1, 2) or self.update_index not in (1, 2, 3):
            raise ValueError("v2 updates support only rounds 0..2")
        if self.update_index != self.round_index + 1:
            raise ValueError("update_index must immediately follow round_index")
        if (
            not isinstance(self.trajectories, tuple)
            or len(self.trajectories) != self.update_index
            or any(type(item) is not SupervisedTrajectoryV2 for item in self.trajectories)
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
        ordered_supervised_trajectory_digest_v2(self.trajectories)
        ordered_supervised_safe_feedback_digest_v2(self.trajectories)
        if self.predecessor is not None and type(self.predecessor) is not (
            TaskwiseCorePredecessorV1
        ):
            raise TypeError("predecessor must be exact TaskwiseCorePredecessorV1")
        if not isinstance(self.validator_forbidden_literals, tuple) or any(
            type(value) is not str or not value.strip()
            for value in self.validator_forbidden_literals
        ):
            raise TypeError("validator_forbidden_literals must be non-empty strings")
        packet = self.supervised_packet
        if packet is not None:
            if type(packet) is not SupervisedEvolutionPacketV2:
                raise TypeError("supervised_packet must be exact SupervisedEvolutionPacketV2")
            current = self.trajectories[-1]
            if (
                packet.task_uid != self.task_uid
                or packet.training_task_ordinal != self.task_index + 1
                or packet.round_index != self.round_index
                or packet.category != current.category
                or packet.model_raw_completion != current.raw_completion
                or tuple(item.trajectory_id for item in packet.round_evidence)
                != tuple(item.trajectory_id for item in self.trajectories)
                or packet.predecessor_artifact_id
                != (None if self.predecessor is None else self.predecessor.core_artifact_id)
            ):
                raise ValueError("supervised packet does not bind the current Core update")
            predecessor_digest = (
                None
                if packet.predecessor_memory is None
                else _sha256_bytes(packet.predecessor_memory.encode("utf-8"))
            )
            if predecessor_digest != (
                None if self.predecessor is None else self.predecessor.resolved_memory_sha256
            ):
                raise ValueError("supervised packet predecessor memory binding differs")


ReflectorBoundaryFactoryV1 = Callable[
    [Path, str, int],
    ReflectorExecutionBoundaryV2,
]


@dataclass(frozen=True, slots=True)
class _ValidatorSpanMatchV1:
    segment: str
    token_count: int
    left_token_boundary: bool
    right_token_boundary: bool
    source_ratio: float
    candidate_ratio: float


class TaskwiseTextMemoryValidatorV1:
    """Validate one Core candidate against cumulative taskwise leakage inputs."""

    __slots__ = (
        "_allowed_evidence_digests",
        "_expected_lineage",
        "_expected_record_count",
        "_finding_evidence",
        "_forbidden_literals",
        "_memory_limits",
        "_supervised_category",
    )

    def __init__(
        self,
        *,
        expected_lineage: dict[str, Any],
        expected_record_count: int,
        forbidden_literals: tuple[str, ...],
        memory_limits: TaskwiseMemoryLimitsV1 | SupervisedMemoryLimitsV2,
        supervised_category: str | None = None,
        allowed_evidence_digests: frozenset[str] | None = None,
    ) -> None:
        if type(memory_limits) not in (TaskwiseMemoryLimitsV1, SupervisedMemoryLimitsV2):
            raise TypeError("memory_limits type is unsupported")
        if (type(memory_limits) is SupervisedMemoryLimitsV2) != (supervised_category is not None):
            raise TypeError("supervised category and limits must be selected together")
        if supervised_category is None:
            if allowed_evidence_digests is not None:
                raise TypeError("taskwise validation cannot bind supervised evidence")
        elif (
            type(allowed_evidence_digests) is not frozenset
            or not allowed_evidence_digests
            or any(_SHA256_RE.fullmatch(value) is None for value in allowed_evidence_digests)
        ):
            raise TypeError("supervised validation requires allowed packet evidence")
        self._expected_lineage = json.loads(_canonical_json(expected_lineage))
        self._expected_record_count = expected_record_count
        self._forbidden_literals = forbidden_literals
        self._finding_evidence: tuple[TaskwiseValidatorFindingEvidenceV1, ...] = ()
        self._allowed_evidence_digests = allowed_evidence_digests
        self._memory_limits = memory_limits
        self._supervised_category = supervised_category

    @property
    def finding_evidence(self) -> tuple[TaskwiseValidatorFindingEvidenceV1, ...]:
        """Return deterministic content-free evidence from the latest validation."""

        return self._finding_evidence

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
        evidence: list[TaskwiseValidatorFindingEvidenceV1] = []
        actual_payload_sha256 = _sha256_bytes(payload)
        if actual_payload_sha256 != payload_sha256:
            findings.add("payload_digest_mismatch")
        if (
            artifact.type is not ArtifactType.TEXT_MEMORY
            or artifact.state is not ArtifactState.ACTIVE
        ):
            findings.add("invalid_artifact_type_or_state")
        if artifact.promoted:
            findings.add("already_promoted")
        if self._supervised_category is None:
            if type(self._memory_limits) is not TaskwiseMemoryLimitsV1:
                raise AssertionError("taskwise validator limit type changed")
            inspection: TaskwiseMemoryInspectionV1 | SupervisedMemoryInspectionV2 = (
                inspect_taskwise_text_memory_v1(payload, limits=self._memory_limits)
            )
            required_core_sections = list(self._memory_limits.required_sections)
            validator_id = "chembench4k_taskwise_text_memory_validator_v1"
        else:
            if type(self._memory_limits) is not SupervisedMemoryLimitsV2:
                raise AssertionError("supervised validator limit type changed")
            inspection = inspect_supervised_category_memory_v2(
                payload,
                category=self._supervised_category,
                limits=self._memory_limits,
                allowed_evidence_digests=self._allowed_evidence_digests,
            )
            required_core_sections = list(self._memory_limits.core_compatibility_sections)
            validator_id = "chembench_supervised_category_memory_validator_v1"
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
            or manifest.get("required_sections") != required_core_sections
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
        for encoded_literal in self._forbidden_literals:
            source_kind, stripped = _decode_validator_literal(encoded_literal)
            if _full_literal_scan_allowed(source_kind, stripped) and _contains_bounded_literal(
                text,
                stripped,
            ):
                findings.add("leak_forbidden_literal")
                evidence.append(
                    _validator_finding_evidence(
                        finding_code="leak_forbidden_literal",
                        source_kind=source_kind,
                        match=_literal_match_evidence(stripped, text),
                    )
                )
            normalized_literal = _normalize(stripped)
            if _full_literal_scan_allowed(
                source_kind,
                normalized_literal,
            ) and _contains_bounded_literal(
                normalized_candidate,
                normalized_literal,
            ):
                findings.add("leak_normalized_literal")
                evidence.append(
                    _validator_finding_evidence(
                        finding_code="leak_normalized_literal",
                        source_kind=source_kind,
                        match=_literal_match_evidence(
                            normalized_literal,
                            normalized_candidate,
                        ),
                    )
                )
            ngram_match = _source_kind_ngram_match(
                source_kind,
                normalized_literal,
                normalized_candidate,
            )
            if ngram_match is not None:
                findings.add("leak_literal_ngram")
                evidence.append(
                    _validator_finding_evidence(
                        finding_code="leak_literal_ngram",
                        source_kind=source_kind,
                        match=ngram_match,
                    )
                )
        security_scan_text = _security_scan_text(text)
        if _contains_path_or_benchmark_marker(security_scan_text):
            findings.add("leak_path_or_benchmark_marker")
        if _OPERATIONAL_IDENTIFIER_RE.search(security_scan_text):
            findings.add("leak_operational_identifier")
        if _contains_answer_map(security_scan_text):
            findings.add("leak_answer_map")
        finding_codes = tuple(sorted(findings))
        evidence_by_code: dict[str, TaskwiseValidatorFindingEvidenceV1] = {}
        for item in sorted(
            evidence,
            key=lambda value: (
                value.finding_code,
                value.source_kind,
                value.segment_sha256,
            ),
        ):
            evidence_by_code.setdefault(item.finding_code, item)
        for finding_code in finding_codes:
            evidence_by_code.setdefault(
                finding_code,
                _artifact_finding_evidence(
                    finding_code=finding_code,
                    payload_sha256=actual_payload_sha256,
                    utf8_byte_count=len(payload),
                    estimated_token_count=inspection.estimated_token_count,
                ),
            )
        self._finding_evidence = tuple(evidence_by_code[code] for code in sorted(evidence_by_code))
        return CoreArtifactValidationReceiptV2(
            validator_id=validator_id,
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
        supervised_reflector_boundary_factory: ReflectorBoundaryFactoryV1 | None = None,
        checkpoint_path: str | Path | None = None,
        memory_limits: TaskwiseMemoryLimitsV1 | SupervisedMemoryLimitsV2 = (
            TASKWISE_MEMORY_LIMITS_V1
        ),
        reflector_timeout_seconds: int = DEFAULT_REFLECTOR_TIMEOUT_SECONDS,
    ) -> None:
        if type(memory_limits) not in (TaskwiseMemoryLimitsV1, SupervisedMemoryLimitsV2):
            raise TypeError("memory_limits must be an exact supported protocol type")
        self._reflector_timeout_seconds = _validate_reflector_timeout_seconds(
            reflector_timeout_seconds
        )
        self._core_lease_seconds = _core_lease_seconds(self._reflector_timeout_seconds)
        self._registry = require_verified_executable_registry(executable_registry)
        self._require_method()
        if type(memory_limits) is SupervisedMemoryLimitsV2:
            self._require_auxiliary_methods()
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
        self._seen_supervised_packet_digests: tuple[str, ...] = ()
        self._terminal_finding: str | None = None
        try:
            self._store = EvolutionStore(
                db_path=db,
                artifact_root=artifact_root,
                executable_registry=self._registry,
            )
            self._store.initialize()
            self._reflector_boundary_factory = reflector_boundary_factory
            self._supervised_reflector_boundary_factory = supervised_reflector_boundary_factory
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

    def __enter__(self) -> Self:
        if self._closed:
            raise RuntimeError("taskwise Core bridge is closed")
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001, S110 - destructor is best effort only
            pass

    def apply_update(
        self,
        request: TaskwiseCoreUpdateRequestV1,
        *,
        test_only_allow_synthetic_reflector: bool = False,
    ) -> TaskwiseCoreUpdateResultV1:
        """Execute one immutable Core update; no adapter artifact is accepted."""

        if type(request) is not TaskwiseCoreUpdateRequestV1:
            raise TypeError("request must be exact TaskwiseCoreUpdateRequestV1")
        if self._closed:
            raise TaskwiseCoreEvolutionError("TASKWISE_STREAM_CLOSED")
        if self._terminal_finding is not None:
            raise TaskwiseCoreEvolutionError("TASKWISE_STREAM_TERMINAL")
        selected_factory = (
            self._supervised_reflector_boundary_factory
            if request.supervised_packet is not None
            else self._reflector_boundary_factory
        )
        if test_only_allow_synthetic_reflector and selected_factory is not None:
            raise ValueError("reflector execution mode is ambiguous")
        if not test_only_allow_synthetic_reflector and selected_factory is None:
            raise TaskwiseCoreEvolutionError("TASKWISE_REFLECTOR_BOUNDARY_REQUIRED")
        self._require_next_predecessor(request)

        trajectory_digest = ordered_supervised_trajectory_digest_v2(request.trajectories)
        source_execution_provenance_sha256 = (
            ordered_source_execution_provenance_digest_v2(request.trajectories)
        )
        safe_feedback_digest = ordered_supervised_safe_feedback_digest_v2(request.trajectories)
        global_update_ordinal = 1 if self._head is None else self._head.global_update_ordinal + 1
        packet = request.supervised_packet
        packet_records = () if packet is None else packet.reflector_records()
        policy_protocol = PROTOCOL_ID if packet is None else packet.protocol_id
        policy_version = f"{policy_protocol}.{global_update_ordinal}.{trajectory_digest[:20]}"
        if packet is None:
            for trajectory in request.trajectories:
                self._store.ingest_event(
                    self._event_request(
                        trajectory=trajectory,
                        policy_version=policy_version,
                    )
                )
            expected_record_count = request.update_index
        else:
            for record in packet_records:
                self._store.ingest_event(
                    self._supervised_event_request(
                        packet=packet,
                        record=record,
                        policy_version=policy_version,
                        source_trajectories=request.trajectories,
                        source_execution_provenance_sha256=(
                            source_execution_provenance_sha256
                        ),
                    )
                )
            expected_record_count = len(packet_records)
        dataset = self._store.create_dataset(
            DatasetCreateRequest(
                name=(
                    "Taskwise safe trajectory prefix"
                    if packet is None
                    else "Supervised Train evolution packet"
                ),
                purpose=(_DATASET_PURPOSE if packet is None else _SUPERVISED_DATASET_PURPOSE),
                query={
                    "event_types": [_EVENT_TYPE],
                    "status": ["COMPLETED"],
                    "policy_version": policy_version,
                },
                limits={
                    "max_events": expected_record_count,
                    "max_traces": expected_record_count,
                },
            )
        )
        if (
            dataset.event_count != expected_record_count
            or dataset.trace_count != expected_record_count
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
            source_execution_provenance_sha256=source_execution_provenance_sha256,
            safe_feedback_digest=safe_feedback_digest,
            validator_input_digest=validator_input_digest,
            dataset_artifact_id=dataset.artifact_id,
            expected_record_count=expected_record_count,
        )
        plan = self._create_job(
            request=request,
            dataset_artifact_id=dataset.artifact_id,
            expected_lineage=expected_lineage,
            trajectory_digest=trajectory_digest,
            forbidden_literals=forbidden_literals,
            validator_input_digest=validator_input_digest,
            expected_record_count=expected_record_count,
        )
        try:
            try:
                execution = self._run_and_register(
                    request=request,
                    job_id=plan["job_id"],
                    job_type=plan["job_type"],
                    trajectory_digest=trajectory_digest,
                    expected_record_count=expected_record_count,
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
                expected_record_count=expected_record_count,
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
                auxiliary_output=execution["auxiliary_output"],
                test_only_allow_synthetic_reflector=test_only_allow_synthetic_reflector,
            )
        except Exception:
            self._terminal_finding = "TASKWISE_CORE_UPDATE_FAILED"
            raise
        try:
            self._append_private_checkpoint(
                result=result,
                validator_forbidden_literals=forbidden_literals,
            )
        except Exception as exc:  # noqa: BLE001 - private fail-closed boundary
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
        literal_delta = _checkpoint_literal_delta(
            self._seen_forbidden_literals,
            validator_forbidden_literals,
        )
        payload = {
            "schema_version": "taskwise_core_private_checkpoint_v2",
            "result": result.model_dump(mode="json"),
            "required_lineage": lineage_receipt.model_dump(mode="json"),
            "required_lineage_sha256": lineage_receipt.digest,
            "validator_forbidden_literals_delta": list(literal_delta),
            "validator_input_digest": result.validator_input_digest,
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
        prior_literals: tuple[str, ...] = ()
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
                    "validator_forbidden_literals_delta",
                    "validator_input_digest",
                }
                or row["schema_version"] != "taskwise_core_private_checkpoint_v2"
                or encoded != (_canonical_json(row) + "\n").encode("utf-8")
            ):
                raise TaskwiseCoreEvolutionError("TASKWISE_PRIVATE_CHECKPOINT_INVALID")
            try:
                result = TaskwiseCoreUpdateResultV1.model_validate(row["result"])
                lineage_payload = row["required_lineage"]
                if (
                    isinstance(lineage_payload, dict)
                    and lineage_payload.get("schema_version")
                    == "chembench_supervised_artifact_lineage_v2"
                ):
                    lineage_receipt: (
                        TaskwiseArtifactLineageReceiptV1 | SupervisedArtifactLineageReceiptV1
                    ) = SupervisedArtifactLineageReceiptV1.model_validate(lineage_payload)
                else:
                    lineage_receipt = TaskwiseArtifactLineageReceiptV1.model_validate(
                        lineage_payload
                    )
                literals = _merge_checkpoint_literal_delta(
                    prior_literals,
                    row["validator_forbidden_literals_delta"],
                )
            except (TypeError, ValueError) as exc:
                raise TaskwiseCoreEvolutionError("TASKWISE_PRIVATE_CHECKPOINT_INVALID") from exc
            if (
                result.global_update_ordinal != expected_ordinal
                or result.global_update_ordinal != result.task_index * 3 + result.update_index
                or (result.predecessor is None) != (expected_ordinal == 1)
                or (
                    self._head is not None
                    and result.predecessor != self._head.predecessor_identity()
                )
                or lineage_receipt != result.required_lineage_receipt()
                or row["required_lineage_sha256"] != lineage_receipt.digest
                or row["validator_input_digest"] != result.validator_input_digest
                or _validator_input_digest(literals) != result.validator_input_digest
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
            prior_literals = literals
        expected_jobs = sum(
            1 + len(result.supervised_auxiliary_artifacts) for result in self._history.values()
        )
        if self._taskwise_job_count() != expected_jobs:
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
        if result.supervised_packet_sha256 is not None:
            self._seen_supervised_packet_digests = tuple(
                dict.fromkeys(
                    (*self._seen_supervised_packet_digests, result.supervised_packet_sha256)
                )
            )

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
            or job_config["lineage"].get("reflector_timeout_seconds")
            != result.reflector_timeout_seconds
            or job_config["lineage"].get("core_lease_seconds") != result.core_lease_seconds
            or job_config["lineage"].get("core_lease_grace_seconds")
            != result.core_lease_grace_seconds
            or result.reflector_timeout_seconds != self._reflector_timeout_seconds
            or result.core_lease_seconds != self._core_lease_seconds
            or "forbidden_literals" in job_config
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
        allowed_evidence = (
            None
            if result.supervised_category is None
            else frozenset(
                (
                    *self._seen_supervised_packet_digests,
                    result.supervised_packet_sha256,
                )
            )
        )
        validator = TaskwiseTextMemoryValidatorV1(
            expected_lineage=self._expected_lineage_from_result(result),
            expected_record_count=result.records_visible_to_reflector,
            forbidden_literals=forbidden,
            memory_limits=self._memory_limits,
            supervised_category=result.supervised_category,
            allowed_evidence_digests=allowed_evidence,
        )
        recomputed = validator.validate(
            artifact=artifact.model_copy(update={"promoted": False}),
            payload=payload,
            payload_sha256=result.artifact_payload_sha256,
            lineage=lineage,
        )
        if recomputed != result.validation_receipt:
            raise TaskwiseCoreEvolutionError("TASKWISE_VALIDATION_RECEIPT_DRIFT")
        if result.supervised_category is None:
            if type(self._memory_limits) is not TaskwiseMemoryLimitsV1:
                raise TaskwiseCoreEvolutionError("TASKWISE_MEMORY_LIMITS_DRIFT")
            inspection: TaskwiseMemoryInspectionV1 | SupervisedMemoryInspectionV2 = (
                inspect_taskwise_text_memory_v1(payload, limits=self._memory_limits)
            )
        else:
            if type(self._memory_limits) is not SupervisedMemoryLimitsV2:
                raise TaskwiseCoreEvolutionError("TASKWISE_MEMORY_LIMITS_DRIFT")
            inspection = inspect_supervised_category_memory_v2(
                payload,
                category=result.supervised_category,
                limits=self._memory_limits,
                allowed_evidence_digests=allowed_evidence,
            )
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
        if result.supervised_packet_sha256 is not None:
            allowed_evidence = frozenset(
                (*self._seen_supervised_packet_digests, result.supervised_packet_sha256)
            )
            prior_result = (
                None
                if result.predecessor is None
                else self._history.get(result.predecessor.core_artifact_id)
            )
            prior_auxiliary = {
                artifact.target_id: artifact
                for artifact in (
                    () if prior_result is None else prior_result.supervised_auxiliary_artifacts
                )
            }
            for auxiliary in result.supervised_auxiliary_artifacts:
                self._verify_auxiliary_artifact(
                    result=result,
                    auxiliary=auxiliary,
                    predecessor=prior_auxiliary.get(auxiliary.target_id),
                    forbidden_literals=forbidden,
                    allowed_evidence=allowed_evidence,
                )
        return result

    def _verify_auxiliary_artifact(
        self,
        *,
        result: TaskwiseCoreUpdateResultV1,
        auxiliary: SupervisedAuxiliaryCoreArtifactV2,
        predecessor: SupervisedAuxiliaryCoreArtifactV2 | None,
        forbidden_literals: tuple[str, ...],
        allowed_evidence: frozenset[str],
    ) -> None:
        with self._store.connect() as connection:
            job_row = connection.execute(
                "SELECT state, method, plan_id, method_identity_digest, config_json "
                "FROM jobs WHERE job_id = ?",
                (auxiliary.job_id,),
            ).fetchone()
            artifact_row = connection.execute(
                "SELECT type, state, promoted, manifest_path FROM artifacts WHERE artifact_id = ?",
                (auxiliary.core_artifact_id,),
            ).fetchone()
            context_row = connection.execute(
                "SELECT cm.manifest_json, c.response_json, "
                "c.selected_artifact_ids_json "
                "FROM context_materializations AS cm "
                "JOIN contexts AS c USING(context_id) "
                "WHERE cm.context_id = ?",
                (auxiliary.core_context_id,),
            ).fetchone()
        if (
            job_row is None
            or job_row["state"] != "succeeded"
            or job_row["method"] != auxiliary.method_id
            or job_row["plan_id"] != auxiliary.plan_id
            or job_row["method_identity_digest"] != auxiliary.method_identity_digest
        ):
            raise TaskwiseCoreEvolutionError("TASKWISE_JOB_IDENTITY_DRIFT")
        try:
            job_config = json.loads(str(job_row["config_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise TaskwiseCoreEvolutionError("TASKWISE_JOB_IDENTITY_DRIFT") from exc
        job_lineage = job_config.get("lineage") if isinstance(job_config, dict) else None
        if (
            not isinstance(job_lineage, dict)
            or job_config.get("content") != auxiliary.resolved_content
            or job_lineage.get("protocol_id") != SUPERVISED_PROTOCOL_ID
            or job_lineage.get("category") != result.supervised_category
            or job_lineage.get("target_id") != auxiliary.target_id
            or job_lineage.get("method_id") != auxiliary.method_id
            or job_lineage.get("supervised_packet_sha256") != result.supervised_packet_sha256
            or job_lineage.get("source_component_sha256") != auxiliary.source_component_sha256
            or job_lineage.get("source_evidence_digests")
            != list(auxiliary.inspection.source_evidence_digests)
            or job_lineage.get("dataset_artifact_id") != result.dataset_artifact_id
            or job_lineage.get("global_update_ordinal") != result.global_update_ordinal
            or job_lineage.get("validator_input_digest") != result.validator_input_digest
            or job_lineage.get("predecessor_artifact_id")
            != (None if predecessor is None else predecessor.core_artifact_id)
            or job_lineage.get("predecessor_payload_sha256")
            != (None if predecessor is None else predecessor.artifact_payload_sha256)
        ):
            raise TaskwiseCoreEvolutionError("TASKWISE_CORE_LINEAGE_INVALID")
        expected_type = auxiliary.target_id
        if (
            artifact_row is None
            or artifact_row["type"] != expected_type
            or artifact_row["state"] != "active"
            or artifact_row["promoted"] != 1
        ):
            raise TaskwiseCoreEvolutionError("TASKWISE_HEAD_STATE_INVALID")
        artifact = self._store.get_artifact(auxiliary.core_artifact_id)
        payload_root = _safe_core_file_path(
            artifact.uri,
            allowed_root=self._store.files.root,
        )
        payload_path = (
            payload_root / "SKILL.md" if auxiliary.target_id == "skill_bundle" else payload_root
        )
        payload = _read_bounded_regular_file(
            payload_path,
            maximum=ABSOLUTE_MAX_MEMORY_FILE_BYTES,
        )
        lineage = self._artifact_lineage(auxiliary.core_artifact_id)
        execution = lineage.get("openevo_execution")
        input_bindings = (
            None if not isinstance(execution, dict) else execution.get("input_bindings")
        )
        expected_prior_ids = [] if predecessor is None else [predecessor.core_artifact_id]
        bindings_valid = (
            isinstance(input_bindings, list)
            and len(input_bindings) == 2
            and isinstance(input_bindings[0], dict)
            and isinstance(input_bindings[1], dict)
            and input_bindings[0].get("binding_id") == "current_dataset"
            and input_bindings[0].get("artifact_ids") == [result.dataset_artifact_id]
            and input_bindings[1].get("binding_id") == "prior_target_artifacts"
            and input_bindings[1].get("artifact_ids") == expected_prior_ids
            and all(
                isinstance(binding, dict)
                and isinstance(binding.get("artifact_digests"), list)
                and len(binding["artifact_digests"]) == len(binding["artifact_ids"])
                and all(
                    isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None
                    for value in binding["artifact_digests"]
                )
                for binding in input_bindings
            )
        )
        if (
            _sha256_bytes(payload) != auxiliary.artifact_payload_sha256
            or canonical_digest(lineage) != auxiliary.artifact_lineage_sha256
            or not isinstance(execution, dict)
            or execution.get("job_id") != auxiliary.job_id
            or execution.get("plan_id") != auxiliary.plan_id
            or execution.get("plan_digest") != auxiliary.plan_digest
            or execution.get("target_id") != auxiliary.target_id
            or execution.get("method_id") != auxiliary.method_id
            or execution.get("method_identity_digest") != auxiliary.method_identity_digest
            or not bindings_valid
        ):
            raise TaskwiseCoreEvolutionError("TASKWISE_CORE_LINEAGE_INVALID")
        inspection = inspect_supervised_auxiliary_artifact_v2(
            payload,
            target_id=auxiliary.target_id,
            category=result.supervised_category or "",
            source_evidence_digests=auxiliary.inspection.source_evidence_digests,
            allowed_evidence_digests=allowed_evidence,
            forbidden_literals=forbidden_literals,
        )
        if inspection != auxiliary.inspection:
            raise TaskwiseCoreEvolutionError("TASKWISE_VALIDATION_RECEIPT_DRIFT")
        manifest_path = Path(str(artifact_row["manifest_path"]))
        if (
            _sha256_bytes(_read_bounded_regular_file(manifest_path, maximum=MAX_MANIFEST_BYTES))
            != auxiliary.core_artifact_manifest_sha256
            or context_row is None
        ):
            raise TaskwiseCoreEvolutionError("TASKWISE_HEAD_IDENTITY_DRIFT")
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
            or materialized.context_id != auxiliary.core_context_id
            or selected_ids != [auxiliary.core_artifact_id]
            or materialized.selection.artifact_ids != (auxiliary.core_artifact_id,)
            or canonical_digest(materialized) != auxiliary.context_resolution_digest
            or payload.decode("utf-8") != auxiliary.resolved_content
            or _sha256_bytes(payload) != auxiliary.resolved_content_sha256
        ):
            raise TaskwiseCoreEvolutionError("TASKWISE_CONTEXT_IDENTITY_DRIFT")

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

    def issue_runtime_context(
        self,
        result: TaskwiseCoreUpdateResultV1,
    ) -> CoreResolvedSupervisedContextV2:
        """Issue the exact approved memory, skill, and agent-system context."""

        self.verify_update_result(result)
        if tuple(artifact.target_id for artifact in result.supervised_auxiliary_artifacts) != (
            "skill_bundle",
            "agent_system",
        ):
            raise TaskwiseCoreEvolutionError("TASKWISE_RUNTIME_CONTEXT_INCOMPLETE")
        by_target = {
            artifact.target_id: artifact for artifact in result.supervised_auxiliary_artifacts
        }
        return CoreResolvedSupervisedContextV2(
            memory=_issue_core_resolved_text_memory_v2(
                core_artifact_id=result.core_artifact_id,
                artifact_payload_sha256=result.artifact_payload_sha256,
                context_resolution_digest=result.context_resolution_digest,
                resolved_memory_sha256=result.resolved_memory_sha256,
                markdown=result.resolved_memory,
            ),
            skill=_issue_core_resolved_auxiliary_v2(
                target_id="skill_bundle",
                core_artifact_id=by_target["skill_bundle"].core_artifact_id,
                artifact_payload_sha256=by_target["skill_bundle"].artifact_payload_sha256,
                context_resolution_digest=by_target["skill_bundle"].context_resolution_digest,
                resolved_content_sha256=by_target["skill_bundle"].resolved_content_sha256,
                markdown=by_target["skill_bundle"].resolved_content,
            ),
            agent_system=_issue_core_resolved_auxiliary_v2(
                target_id="agent_system",
                core_artifact_id=by_target["agent_system"].core_artifact_id,
                artifact_payload_sha256=by_target["agent_system"].artifact_payload_sha256,
                context_resolution_digest=by_target["agent_system"].context_resolution_digest,
                resolved_content_sha256=by_target["agent_system"].resolved_content_sha256,
                markdown=by_target["agent_system"].resolved_content,
            ),
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
        packet = request.supervised_packet
        if (packet is None) != (head.supervised_packet_sha256 is None):
            raise TaskwiseCoreEvolutionError("TASKWISE_LINEAGE_FORK")
        if packet is not None:
            by_target = {value.target_id: value for value in head.supervised_auxiliary_artifacts}
            if set(by_target) != {"skill_bundle", "agent_system"}:
                raise TaskwiseCoreEvolutionError("TASKWISE_HEAD_STATE_INVALID")
            if (
                packet.predecessor_skill_artifact_id != by_target["skill_bundle"].core_artifact_id
                or packet.predecessor_skill != by_target["skill_bundle"].resolved_content
                or packet.predecessor_agent_system_artifact_id
                != by_target["agent_system"].core_artifact_id
                or packet.predecessor_agent_system != by_target["agent_system"].resolved_content
            ):
                raise TaskwiseCoreEvolutionError("TASKWISE_PREDECESSOR_BINDING_INVALID")
        if head.update_index in (1, 2):
            expected_position = (
                head.task_uid,
                head.task_index,
                head.round_index + 1,
                head.update_index + 1,
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
        trajectory: SupervisedTrajectoryV2,
        policy_version: str,
    ) -> EventIngestRequest:
        safe_feedback = trajectory.safe_feedback.to_evolution_payload()
        reflector_projection = trajectory.to_reflector_projection()
        satisfactory = TaskwiseSafeSignalCodeV1.CORRECT in trajectory.safe_feedback.codes
        reward = 1.0 if satisfactory else 0.0
        task_id = "taskwise-current-task"
        session_id = f"taskwise-current-round-{trajectory.round_index}"
        trajectory_payload = {
            "status": "COMPLETED",
            "metadata": {
                "builder": BRIDGE_ID,
                "capture_mode": "safe_taxonomy_projection",
                "token_level_metrics_available": False,
                "reflector_projection": reflector_projection,
            },
            "traces": [
                {
                    "prompt_ids": [],
                    "response_ids": [],
                    "loss_mask": [],
                    "prompt_messages": [
                        {
                            "role": "user",
                            "content": (
                                f"{_REFLECTOR_PROJECTED_PROMPT}\n"
                                f"Category: {trajectory.category}\n"
                                f"Round: {trajectory.round_index}"
                            ),
                        }
                    ],
                    "response_messages": [
                        {"role": "assistant", "content": _REFLECTOR_PROJECTED_RESPONSE}
                    ],
                    "finish_reason": "safe_taxonomy_projection",
                    "response_logprobs": None,
                    "reward": reward,
                    "metadata": {
                        "capture_mode": "safe_taxonomy_projection",
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
                        "round_index": trajectory.round_index,
                        "reflector_projection": reflector_projection,
                        "evolution_feedback": safe_feedback,
                    },
                    "evolution_feedback": safe_feedback,
                },
                "evolution_feedback": safe_feedback,
            },
        )

    def _supervised_event_request(
        self,
        *,
        packet: SupervisedEvolutionPacketV2,
        record: dict[str, Any],
        policy_version: str,
        source_trajectories: tuple[SupervisedTrajectoryV2, ...],
        source_execution_provenance_sha256: str,
    ) -> EventIngestRequest:
        """Ingest one bounded packet part as a real private Core trajectory event."""

        if type(packet) is not SupervisedEvolutionPacketV2 or not isinstance(record, dict):
            raise TypeError("supervised Core event inputs are invalid")
        expected_trajectory_ids = tuple(item.trajectory_id for item in source_trajectories)
        if (
            record.get("source_split") != SUPERVISED_TRAIN_SOURCE_SPLIT
            or record.get("packet_sha256") != packet.digest
            or type(record.get("uid")) is not str
            or type(record.get("content")) is not str
            or record.get("packet_part_count") != len(packet.reflector_records())
            or expected_trajectory_ids
            != tuple(item.trajectory_id for item in packet.round_evidence)
            or source_execution_provenance_sha256
            != ordered_source_execution_provenance_digest_v2(source_trajectories)
        ):
            raise TaskwiseCoreEvolutionError("TASKWISE_SUPERVISED_PACKET_BINDING_INVALID")
        reward = 1.0 if packet.prediction_correct else 0.0
        part = int(record["packet_part"])
        task_id = "supervised-current-train-task"
        session_id = f"supervised-current-round-{packet.round_index}"
        trajectory_payload = {
            "status": "COMPLETED",
            "metadata": {
                "builder": BRIDGE_ID,
                "capture_mode": "supervised_train_answer_projection",
                "token_level_metrics_available": False,
                "packet_sha256": packet.digest,
                "packet_part": part,
                "packet_part_count": record["packet_part_count"],
                "input_schema_sha256": PACKET_INPUT_SCHEMA_DIGEST,
                "reflector_prompt_sha256": REFLECTOR_PROMPT_DIGEST,
                "source_execution_provenance_sha256": (
                    source_execution_provenance_sha256
                ),
            },
            "traces": [
                {
                    "prompt_ids": [],
                    "response_ids": [],
                    "loss_mask": [],
                    "prompt_messages": [{"role": "user", "content": record["content"]}],
                    "response_messages": [
                        {
                            "role": "assistant",
                            "content": "Bound supervised packet part for category reflection.",
                        }
                    ],
                    "finish_reason": "supervised_train_answer_projection",
                    "response_logprobs": None,
                    "reward": reward,
                    "metadata": {
                        "capture_mode": "supervised_train_answer_projection",
                        "token_level_metrics_available": False,
                        "packet_part": part,
                        "source_execution_provenance_sha256": (
                            source_execution_provenance_sha256
                        ),
                    },
                }
            ],
        }
        return EventIngestRequest(
            source="chembench.supervised_transfer.core.v2",
            event_type=_EVENT_TYPE,
            source_event_id=f"{BRIDGE_ID}:{policy_version}:{record['uid']}",
            task_id=task_id,
            session_id=session_id,
            policy_version=policy_version,
            rollout_step=part - 1,
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
                        "protocol_id": packet.protocol_id,
                        "bridge_id": BRIDGE_ID,
                        "packet_sha256": packet.digest,
                        "packet_part": part,
                        "source_execution_provenance_sha256": (
                            source_execution_provenance_sha256
                        ),
                    },
                }
            },
        )

    def _expected_lineage(
        self,
        *,
        request: TaskwiseCoreUpdateRequestV1,
        global_update_ordinal: int,
        trajectory_digest: str,
        source_execution_provenance_sha256: str,
        safe_feedback_digest: str,
        validator_input_digest: str,
        dataset_artifact_id: str,
        expected_record_count: int,
    ) -> dict[str, Any]:
        predecessor = request.predecessor
        lineage = {
            "protocol_id": (
                PROTOCOL_ID
                if request.supervised_packet is None
                else request.supervised_packet.protocol_id
            ),
            "bridge_id": BRIDGE_ID,
            "reflector_projection_id": (
                REFLECTOR_PROJECTION_ID
                if request.supervised_packet is None
                else SUPERVISED_REFLECTOR_PROJECTION_ID
            ),
            "task_uid": request.task_uid,
            "task_index": request.task_index,
            "round_index": request.round_index,
            "update_index": request.update_index,
            "global_update_ordinal": global_update_ordinal,
            "trajectory_ids": [trajectory.trajectory_id for trajectory in request.trajectories],
            "trajectory_digest": trajectory_digest,
            "source_execution_provenance_sha256": (
                source_execution_provenance_sha256
            ),
            "safe_feedback_digest": safe_feedback_digest,
            "validator_input_digest": validator_input_digest,
            "reflector_timeout_seconds": self._reflector_timeout_seconds,
            "core_lease_seconds": self._core_lease_seconds,
            "core_lease_grace_seconds": CORE_LEASE_GRACE_SECONDS,
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
        packet = request.supervised_packet
        if packet is not None:
            lineage.update(
                {
                    "supervised_packet_sha256": packet.digest,
                    "supervised_category": packet.category,
                    "supervised_input_schema_sha256": PACKET_INPUT_SCHEMA_DIGEST,
                    "supervised_reflector_prompt_sha256": REFLECTOR_PROMPT_DIGEST,
                    "records_visible_to_reflector": expected_record_count,
                }
            )
        return lineage

    def _expected_lineage_from_result(
        self,
        result: TaskwiseCoreUpdateResultV1,
    ) -> dict[str, Any]:
        predecessor = result.predecessor
        lineage = {
            "protocol_id": (
                PROTOCOL_ID if result.supervised_packet_sha256 is None else SUPERVISED_PROTOCOL_ID
            ),
            "bridge_id": BRIDGE_ID,
            "reflector_projection_id": (
                REFLECTOR_PROJECTION_ID
                if result.supervised_packet_sha256 is None
                else SUPERVISED_REFLECTOR_PROJECTION_ID
            ),
            "task_uid": result.task_uid,
            "task_index": result.task_index,
            "round_index": result.round_index,
            "update_index": result.update_index,
            "global_update_ordinal": result.global_update_ordinal,
            "trajectory_ids": list(result.trajectory_ids),
            "trajectory_digest": result.trajectory_digest,
            "source_execution_provenance_sha256": (
                result.source_execution_provenance_sha256
            ),
            "safe_feedback_digest": result.safe_feedback_digest,
            "validator_input_digest": result.validator_input_digest,
            "reflector_timeout_seconds": result.reflector_timeout_seconds,
            "core_lease_seconds": result.core_lease_seconds,
            "core_lease_grace_seconds": result.core_lease_grace_seconds,
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
        if result.supervised_packet_sha256 is not None:
            lineage.update(
                {
                    "supervised_packet_sha256": result.supervised_packet_sha256,
                    "supervised_category": result.supervised_category,
                    "supervised_input_schema_sha256": (result.supervised_input_schema_sha256),
                    "supervised_reflector_prompt_sha256": (
                        result.supervised_reflector_prompt_sha256
                    ),
                    "records_visible_to_reflector": result.records_visible_to_reflector,
                }
            )
        return lineage

    def _create_job(
        self,
        *,
        request: TaskwiseCoreUpdateRequestV1,
        dataset_artifact_id: str,
        expected_lineage: dict[str, Any],
        trajectory_digest: str,
        forbidden_literals: tuple[str, ...],
        validator_input_digest: str,
        expected_record_count: int,
    ) -> dict[str, str]:
        if validator_input_digest != _validator_input_digest(forbidden_literals):
            raise TaskwiseCoreEvolutionError("TASKWISE_VALIDATOR_INPUT_DIGEST_INVALID")
        selection = EvolutionTargetSelection(
            target_id=TARGET_ID,
            enabled=True,
            method_id=METHOD_ID,
            config={
                "max_records": expected_record_count,
                "reflector_llm": {
                    "provider": "codex_cli",
                    "model": MODEL,
                    "timeout_seconds": float(self._reflector_timeout_seconds),
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
                        "Taskwise safe text memory update"
                        if request.supervised_packet is None
                        else "Supervised category text memory update"
                    ),
                    "promoted": False,
                    "lineage": expected_lineage,
                    "compatibility": {
                        "agent_harness": ["codex"],
                        "auth_mode": ["subscription"],
                        "base_model": [MODEL],
                        "task_tags": [
                            PROTOCOL_ID
                            if request.supervised_packet is None
                            else request.supervised_packet.protocol_id
                        ],
                    },
                    "tags": [
                        (
                            PROTOCOL_ID
                            if request.supervised_packet is None
                            else request.supervised_packet.protocol_id
                        ),
                        BRIDGE_ID,
                        (
                            "taskwise-safe-projection"
                            if request.supervised_packet is None
                            else "supervised-train-answer-projection"
                        ),
                    ],
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
        expected_record_count: int,
        test_only_allow_synthetic_reflector: bool,
    ) -> dict[str, Any]:
        reflector_input_digest: str
        reflector_receipt_digest: str | None = None
        reflector_event_stream_digest: str | None = None
        auxiliary_output: SupervisedStructuredAuxiliaryOutputV2 | None = None
        client = _StoreWorkerClient(self._store)
        if test_only_allow_synthetic_reflector:
            reflector_input_digest = trajectory_digest
            claimed = run_once(
                client,
                worker_id=(f"{BRIDGE_ID}-task-{request.task_index}-update-{request.update_index}"),
                capabilities=[job_type],
                artifact_root=self._store.files.root,
                lease_seconds=self._core_lease_seconds,
                executable_registry=self._registry,
            )
        else:
            records = (
                _taskwise_reflector_records(request.trajectories)
                if request.supervised_packet is None
                else list(request.supervised_packet.reflector_records())
            )
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
                boundary_factory = (
                    self._reflector_boundary_factory
                    if request.supervised_packet is None
                    else self._supervised_reflector_boundary_factory
                )
                if boundary_factory is None:  # defensive against mutation
                    raise TaskwiseCoreEvolutionError("TASKWISE_REFLECTOR_BOUNDARY_REQUIRED")
                boundary = boundary_factory(
                    path,
                    reflector_input_digest,
                    expected_record_count,
                )
                if type(boundary) is not ReflectorExecutionBoundaryV2:
                    raise TypeError("reflector boundary factory returned an untrusted type")
                if (
                    boundary.expected_record_count != expected_record_count
                    or boundary.expected_source_split
                    != (
                        TASKWISE_SOURCE_SPLIT
                        if request.supervised_packet is None
                        else SUPERVISED_TRAIN_SOURCE_SPLIT
                    )
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
                        lease_seconds=self._core_lease_seconds,
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
                    or receipt.record_count != expected_record_count
                    or receipt.ordered_records_sha256 != reflector_input_digest
                ):
                    raise TaskwiseCoreEvolutionError("TASKWISE_REFLECTOR_BOUNDARY_FAILED")
                reflector_receipt_digest = receipt.digest
                reflector_event_stream_digest = receipt.event_stream_sha256
                if request.supervised_packet is not None:
                    auxiliary_output = activation.load_supervised_auxiliary_output_for_audit()
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
            "auxiliary_output": auxiliary_output,
        }

    def _validate_promote_resolve(
        self,
        *,
        request: TaskwiseCoreUpdateRequestV1,
        global_update_ordinal: int,
        trajectory_digest: str,
        safe_feedback_digest: str,
        expected_record_count: int,
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
        auxiliary_output: SupervisedStructuredAuxiliaryOutputV2 | None,
        test_only_allow_synthetic_reflector: bool,
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
            "validator_finding_evidence": (),
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
        except Exception as exc:  # noqa: BLE001 - typed artifact trust boundary
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
        except Exception as exc:  # noqa: BLE001 - lineage trust boundary
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
            packet = request.supervised_packet
            if packet is None:
                if type(self._memory_limits) is not TaskwiseMemoryLimitsV1:
                    raise TypeError("taskwise stream uses incompatible memory limits")
                inspection: TaskwiseMemoryInspectionV1 | SupervisedMemoryInspectionV2 = (
                    inspect_taskwise_text_memory_v1(payload, limits=self._memory_limits)
                )
            else:
                if type(self._memory_limits) is not SupervisedMemoryLimitsV2:
                    raise TypeError("supervised stream uses incompatible memory limits")
                allowed_evidence = frozenset(
                    (*self._seen_supervised_packet_digests, packet.digest)
                )
                inspection = inspect_supervised_category_memory_v2(
                    payload,
                    category=packet.category,
                    limits=self._memory_limits,
                    allowed_evidence_digests=allowed_evidence,
                )
            failure_metadata["artifact_payload_sha256"] = payload_sha256
            failure_metadata["memory_inspection_sha256"] = inspection.digest
            validator = TaskwiseTextMemoryValidatorV1(
                expected_lineage=expected_lineage,
                expected_record_count=expected_record_count,
                forbidden_literals=forbidden_literals,
                memory_limits=self._memory_limits,
                supervised_category=None if packet is None else packet.category,
                allowed_evidence_digests=(
                    None if packet is None else allowed_evidence
                ),
            )
            receipt = validator.validate(
                artifact=artifact,
                payload=payload,
                payload_sha256=payload_sha256,
                lineage=lineage,
            )
            failure_metadata["validation_receipt_sha256"] = receipt.digest
            failure_metadata["validator_finding_evidence"] = validator.finding_evidence
            if not inspection.passed or not receipt.passed or receipt.finding_codes:
                raise TaskwiseCoreEvolutionError("TASKWISE_ARTIFACT_VALIDATION_FAILED")
        except Exception as exc:  # noqa: BLE001 - validator trust boundary
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
        except Exception as exc:  # noqa: BLE001 - promotion trust boundary
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
                    supervised_protocol=(
                        None
                        if request.supervised_packet is None
                        else request.supervised_packet.protocol_id
                    ),
                )
            )
            failure_metadata["context_resolution_digest"] = canonical_digest(context)
            resolved_memory = _resolved_text_memory(
                context,
                expected_artifact_id=artifact_id,
            )
            if packet is None:
                if auxiliary_output is not None:
                    raise TaskwiseCoreEvolutionError("TASKWISE_TYPED_ARTIFACT_INVALID")
                auxiliary_artifacts: tuple[SupervisedAuxiliaryCoreArtifactV2, ...] = ()
            else:
                if auxiliary_output is None and test_only_allow_synthetic_reflector:
                    auxiliary_output = _synthetic_supervised_auxiliary_output(packet)
                if auxiliary_output is None or auxiliary_output.category != packet.category:
                    raise TaskwiseCoreEvolutionError("TASKWISE_TYPED_ARTIFACT_INVALID")
                auxiliary_artifacts = self._materialize_auxiliary_artifacts(
                    request=request,
                    dataset_artifact_id=dataset_artifact_id,
                    global_update_ordinal=global_update_ordinal,
                    forbidden_literals=forbidden_literals,
                    auxiliary_output=auxiliary_output,
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
                source_execution_provenance_sha256=(
                    ordered_source_execution_provenance_digest_v2(request.trajectories)
                ),
                safe_feedback_digest=safe_feedback_digest,
                validator_input_digest=validator_input_digest,
                memory_limits_sha256=self._memory_limits.digest,
                memory_inspection=inspection,
                dataset_id=dataset_id,
                dataset_artifact_id=dataset_artifact_id,
                dataset_manifest_sha256=dataset_manifest_sha256,
                configured_max_records=expected_record_count,
                records_visible_to_reflector=expected_record_count,
                reflector_input_digest=reflector_input_digest,
                reflector_timeout_seconds=self._reflector_timeout_seconds,
                core_lease_seconds=self._core_lease_seconds,
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
                supervised_packet_sha256=(None if packet is None else packet.digest),
                supervised_category=(None if packet is None else packet.category),
                supervised_input_schema_sha256=(
                    None if packet is None else PACKET_INPUT_SCHEMA_DIGEST
                ),
                supervised_reflector_prompt_sha256=(
                    None if packet is None else REFLECTOR_PROMPT_DIGEST
                ),
                supervised_auxiliary_artifacts=auxiliary_artifacts,
            )
        except TaskwiseCoreEvolutionError as exc:
            # Auxiliary target jobs run inside this outer context-resolution phase,
            # but their own closed failure code must not be mislabeled as a context
            # failure. No successor head/checkpoint is accepted after re-raising.
            if exc.finding_code in _AUXILIARY_TERMINAL_FINDINGS:
                raise
            self._raise_private_core_failure(
                stage="CONTEXT_RESOLUTION",
                default_finding="TASKWISE_CONTEXT_RESOLUTION_FAILED",
                cause=exc,
                **failure_metadata,
            )
        except Exception as exc:  # noqa: BLE001 - context trust boundary
            self._raise_private_core_failure(
                stage="CONTEXT_RESOLUTION",
                default_finding="TASKWISE_CONTEXT_RESOLUTION_FAILED",
                cause=exc,
                **failure_metadata,
            )

    def _materialize_auxiliary_artifacts(
        self,
        *,
        request: TaskwiseCoreUpdateRequestV1,
        dataset_artifact_id: str,
        global_update_ordinal: int,
        forbidden_literals: tuple[str, ...],
        auxiliary_output: SupervisedStructuredAuxiliaryOutputV2,
    ) -> tuple[SupervisedAuxiliaryCoreArtifactV2, ...]:
        """Create verified skill/system jobs from one reflector-bound response."""

        packet = request.supervised_packet
        if packet is None or auxiliary_output.category != packet.category:
            raise TaskwiseCoreEvolutionError("TASKWISE_TYPED_ARTIFACT_INVALID")
        allowed_evidence = frozenset((*self._seen_supervised_packet_digests, packet.digest))
        prior_by_target = {
            artifact.target_id: artifact
            for artifact in (
                () if self._head is None else self._head.supervised_auxiliary_artifacts
            )
        }
        specifications = (
            (
                "skill_bundle",
                "skill_bundle",
                auxiliary_output.skill_markdown,
                auxiliary_output.skill_source_sha256,
                auxiliary_output.skill_evidence_digests,
            ),
            (
                "agent_system",
                "agent_system",
                auxiliary_output.agent_system_markdown,
                auxiliary_output.agent_system_source_sha256,
                auxiliary_output.agent_system_evidence_digests,
            ),
        )
        results: list[SupervisedAuxiliaryCoreArtifactV2] = []
        for target_id, method_id, content, source_digest, evidence_digests in specifications:
            source_evidence_digests = _bind_auxiliary_source_evidence(
                reported_evidence_digests=evidence_digests,
                allowed_evidence_digests=allowed_evidence,
                current_packet_digest=packet.digest,
            )
            prior = prior_by_target.get(target_id)
            results.append(
                self._materialize_one_auxiliary_artifact(
                    request=request,
                    dataset_artifact_id=dataset_artifact_id,
                    global_update_ordinal=global_update_ordinal,
                    forbidden_literals=forbidden_literals,
                    allowed_evidence=allowed_evidence,
                    target_id=target_id,
                    method_id=method_id,
                    content=content,
                    source_component_sha256=source_digest,
                    source_evidence_digests=source_evidence_digests,
                    predecessor=prior,
                )
            )
        return tuple(results)

    def _materialize_one_auxiliary_artifact(
        self,
        *,
        request: TaskwiseCoreUpdateRequestV1,
        dataset_artifact_id: str,
        global_update_ordinal: int,
        forbidden_literals: tuple[str, ...],
        allowed_evidence: frozenset[str],
        target_id: str,
        method_id: str,
        content: str,
        source_component_sha256: str,
        source_evidence_digests: tuple[str, ...],
        predecessor: SupervisedAuxiliaryCoreArtifactV2 | None,
    ) -> SupervisedAuxiliaryCoreArtifactV2:
        if (target_id, method_id) not in _AUXILIARY_TARGET_METHODS:
            raise TaskwiseCoreEvolutionError("TASKWISE_REGISTERED_METHOD_INVALID")
        descriptor = self._registry.snapshot.methods.get(method_id)
        if (
            descriptor is None
            or method_id not in self._registry.method_handles
            or descriptor.target_id != target_id
            or descriptor.output_artifact_types != (target_id,)
            or tuple(binding.binding_id for binding in descriptor.input_bindings)
            != ("current_dataset", "prior_target_artifacts")
        ):
            raise TaskwiseCoreEvolutionError("TASKWISE_REGISTERED_METHOD_INVALID")
        selection_config: dict[str, object] = {"content": content}
        if target_id == "agent_system":
            selection_config["target_path"] = "AGENTS.md"
        selection = EvolutionTargetSelection(
            target_id=target_id,
            enabled=True,
            method_id=method_id,
            config=selection_config,
        )
        identity = canonical_digest(
            {
                "protocol_id": request.supervised_packet.protocol_id,
                "packet_sha256": request.supervised_packet.digest,
                "target_id": target_id,
                "source_component_sha256": source_component_sha256,
                "global_update_ordinal": global_update_ordinal,
                "predecessor_artifact_id": (
                    None if predecessor is None else predecessor.core_artifact_id
                ),
            }
        )
        profile = EvolutionExecutionProfile(
            execution_mode="self_deployed",
            capture_mode="transcript",
            harness_id="codex",
        )
        plan = self._registry.snapshot.compile_plan(
            plan_id=f"{_JOB_PREFIX}.{target_id}.{identity[:32]}",
            selections=(selection,),
            profile=profile,
        )
        job_type = f"{_JOB_PREFIX}.{target_id}.{identity[:24]}"
        prior_ids = () if predecessor is None else (predecessor.core_artifact_id,)
        expected_lineage = {
            "protocol_id": request.supervised_packet.protocol_id,
            "category": request.supervised_packet.category,
            "target_id": target_id,
            "method_id": method_id,
            "supervised_packet_sha256": request.supervised_packet.digest,
            "source_component_sha256": source_component_sha256,
            "source_evidence_digests": list(source_evidence_digests),
            "global_update_ordinal": global_update_ordinal,
            "dataset_artifact_id": dataset_artifact_id,
            "predecessor_artifact_id": (
                None if predecessor is None else predecessor.core_artifact_id
            ),
            "predecessor_payload_sha256": (
                None if predecessor is None else predecessor.artifact_payload_sha256
            ),
            "validator_input_digest": _validator_input_digest(forbidden_literals),
        }
        created = self._store.create_plan_bound_job(
            PlanBoundJobCreateRequest(
                plan=plan,
                target_id=target_id,
                job_type=job_type,
                input_bindings=(
                    PlannedInputBinding(
                        binding_id="current_dataset",
                        artifact_ids=(dataset_artifact_id,),
                    ),
                    PlannedInputBinding(
                        binding_id="prior_target_artifacts",
                        artifact_ids=prior_ids,
                    ),
                ),
                core_config={
                    "name": f"Supervised category {target_id} update",
                    "promoted": False,
                    "lineage": expected_lineage,
                    "compatibility": {
                        "agent_harness": ["codex"],
                        "auth_mode": ["subscription"],
                        "base_model": [MODEL],
                        "task_tags": [request.supervised_packet.protocol_id],
                    },
                    "tags": [
                        request.supervised_packet.protocol_id,
                        BRIDGE_ID,
                        "supervised-multitarget-projection",
                    ],
                },
            ),
            snapshot=self._registry.snapshot,
        )
        client = _StoreWorkerClient(self._store)
        claimed = run_once(
            client,
            worker_id=(
                f"{BRIDGE_ID}-{target_id}-task-{request.task_index}-update-{request.update_index}"
            ),
            capabilities=[job_type],
            artifact_root=self._store.files.root,
            lease_seconds=self._core_lease_seconds,
            executable_registry=self._registry,
        )
        if (
            not claimed
            or client.completed is None
            or client.completed.get("job_id") != created.job_id
            or not isinstance(client.completed.get("artifact_ids"), list)
            or len(client.completed["artifact_ids"]) != 1
        ):
            raise TaskwiseCoreEvolutionError("TASKWISE_CORE_JOB_FAILED")
        artifact_id = str(client.completed["artifact_ids"][0])
        artifact = self._store.get_artifact(artifact_id)
        expected_type = (
            ArtifactType.SKILL_BUNDLE if target_id == "skill_bundle" else ArtifactType.AGENT_SYSTEM
        )
        if (
            artifact.type is not expected_type
            or artifact.state is not ArtifactState.ACTIVE
            or artifact.promoted
        ):
            raise TaskwiseCoreEvolutionError("TASKWISE_TYPED_ARTIFACT_INVALID")
        payload_root = _safe_core_file_path(
            artifact.uri,
            allowed_root=self._store.files.root,
        )
        payload_path = payload_root / "SKILL.md" if target_id == "skill_bundle" else payload_root
        payload = _read_bounded_regular_file(
            payload_path,
            maximum=ABSOLUTE_MAX_MEMORY_FILE_BYTES,
        )
        if payload != content.encode("utf-8"):
            raise TaskwiseCoreEvolutionError("TASKWISE_ARTIFACT_VALIDATION_FAILED")
        inspection = inspect_supervised_auxiliary_artifact_v2(
            payload,
            target_id=target_id,  # type: ignore[arg-type]
            category=request.supervised_packet.category,
            source_evidence_digests=source_evidence_digests,
            allowed_evidence_digests=allowed_evidence,
            forbidden_literals=forbidden_literals,
        )
        if not inspection.passed:
            raise TaskwiseCoreEvolutionError("TASKWISE_ARTIFACT_VALIDATION_FAILED")
        lineage = self._artifact_lineage(artifact_id)
        execution = lineage.get("openevo_execution")
        input_bindings = (
            None if not isinstance(execution, dict) else execution.get("input_bindings")
        )
        bindings_valid = (
            isinstance(input_bindings, list)
            and len(input_bindings) == 2
            and isinstance(input_bindings[0], dict)
            and isinstance(input_bindings[1], dict)
            and input_bindings[0].get("binding_id") == "current_dataset"
            and input_bindings[0].get("artifact_ids") == [dataset_artifact_id]
            and input_bindings[1].get("binding_id") == "prior_target_artifacts"
            and input_bindings[1].get("artifact_ids") == list(prior_ids)
            and all(
                isinstance(binding, dict)
                and isinstance(binding.get("artifact_digests"), list)
                and len(binding["artifact_digests"]) == len(binding["artifact_ids"])
                and all(
                    isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None
                    for value in binding["artifact_digests"]
                )
                for binding in input_bindings
            )
        )
        if (
            not isinstance(execution, dict)
            or execution.get("job_id") != created.job_id
            or execution.get("plan_id") != plan.plan_id
            or execution.get("plan_digest") != canonical_digest(plan)
            or execution.get("target_id") != target_id
            or execution.get("method_id") != method_id
            or execution.get("method_identity_digest")
            != self._registry.snapshot.identity_digest_for(
                DescriptorKind.METHOD,
                method_id,
            )
            or execution.get("registry_snapshot_digest") != plan.registry_snapshot_digest
            or not bindings_valid
        ):
            raise TaskwiseCoreEvolutionError("TASKWISE_CORE_LINEAGE_INVALID")
        promoted = self._store.update_artifact_promotion(artifact_id, promoted=True)
        if not promoted.promoted:
            raise TaskwiseCoreEvolutionError("TASKWISE_ARTIFACT_PROMOTION_FAILED")
        manifest_path = self._store.files.artifact_manifest_path(
            str(expected_type),
            artifact_id,
        )
        context = self._store.resolve_materialized_context(
            ContextProjectionResolveRequest(
                task_id=(
                    f"supervised-{target_id}-task-{request.task_index:08d}-"
                    f"after-update-{request.update_index}"
                ),
                instruction=f"Resolve approved supervised {target_id} for the next session.",
                agent={
                    "harness": "codex",
                    "settings": {"auth_mode": "subscription"},
                },
                base_model=MODEL,
                policy_version=request.supervised_packet.protocol_id,
                metadata={
                    "task_tags": [request.supervised_packet.protocol_id],
                    "evolution": {"context_artifact_ids": [artifact_id]},
                },
                execution_profile=self._execution_profile(),
                destination_roots=RuntimeDestinationRoots(
                    target_data="/openevo/session/evolution",
                    harness_skills="/openevo/session/evolution/skills",
                    harness_instruction="/workspace/repository",
                ),
                target_limits={
                    target_id: TargetConsumptionLimits(
                        max_artifacts=1,
                        max_text_chars=_MAX_AUXILIARY_PAYLOAD_BYTES,
                        max_text_bytes=_MAX_AUXILIARY_PAYLOAD_BYTES,
                        max_payload_bytes=_MAX_AUXILIARY_PAYLOAD_BYTES,
                        max_adapters=0,
                    )
                },
            )
        )
        if (
            context.selection.artifact_ids != (artifact_id,)
            or len(context.projections) != 1
            or context.projections[0].target_id != target_id
            or context.projections[0].artifact_ids != (artifact_id,)
        ):
            raise TaskwiseCoreEvolutionError("TASKWISE_CONTEXT_IDENTITY_DRIFT")
        return SupervisedAuxiliaryCoreArtifactV2(
            target_id=target_id,
            method_id=method_id,
            plan_id=plan.plan_id,
            plan_digest=canonical_digest(plan),
            job_id=created.job_id,
            method_descriptor_digest=canonical_digest(descriptor),
            method_identity_digest=self._registry.snapshot.identity_digest_for(
                DescriptorKind.METHOD,
                method_id,
            ),
            core_artifact_id=artifact_id,
            core_artifact_manifest_sha256=_sha256_bytes(
                _read_bounded_regular_file(manifest_path, maximum=MAX_MANIFEST_BYTES)
            ),
            artifact_payload_sha256=_sha256_bytes(payload),
            artifact_lineage_sha256=canonical_digest(lineage),
            source_component_sha256=source_component_sha256,
            inspection=inspection,
            inspection_sha256=inspection.digest,
            core_context_id=context.context_id,
            context_resolution_digest=canonical_digest(context),
            resolved_content=payload.decode("utf-8"),
            resolved_content_sha256=_sha256_bytes(payload),
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
        validator_finding_evidence: tuple[TaskwiseValidatorFindingEvidenceV1, ...] = (),
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
            validator_finding_evidence=validator_finding_evidence,
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

    def _require_auxiliary_methods(self) -> None:
        snapshot = self._registry.snapshot
        for target_id, method_id in _AUXILIARY_TARGET_METHODS:
            if method_id not in snapshot.methods or method_id not in self._registry.method_handles:
                raise TaskwiseCoreEvolutionError("TASKWISE_REGISTERED_METHOD_MISSING")
            descriptor = snapshot.methods[method_id]
            if (
                descriptor.target_id != target_id
                or descriptor.output_artifact_types != (target_id,)
                or tuple(binding.binding_id for binding in descriptor.input_bindings)
                != ("current_dataset", "prior_target_artifacts")
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
        supervised_protocol: str | None = None,
    ) -> ContextProjectionResolveRequest:
        return ContextProjectionResolveRequest(
            task_id=(f"taskwise-task-{task_index:08d}-after-update-{update_index}"),
            instruction=("Resolve the approved taskwise text memory for the next round."),
            agent={
                "harness": "codex",
                "settings": {"auth_mode": "subscription"},
            },
            base_model=MODEL,
            policy_version=PROTOCOL_ID if supervised_protocol is None else supervised_protocol,
            metadata={
                "task_tags": [PROTOCOL_ID if supervised_protocol is None else supervised_protocol],
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
            task_uid=result.task_uid,
            task_index=result.task_index,
            update_index=result.update_index,
            global_update_ordinal=result.global_update_ordinal,
            predecessor_artifact_id=(
                None if result.predecessor is None else result.predecessor.core_artifact_id
            ),
            predecessor_memory_sha256=(
                None if result.predecessor is None else result.predecessor.resolved_memory_sha256
            ),
            trajectory_ids=result.trajectory_ids,
            trajectory_digest=result.trajectory_digest,
            safe_feedback_digest=result.safe_feedback_digest,
            core_artifact_id=result.core_artifact_id,
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
        or not 0 < timeout_seconds <= MAX_CORE_LEASE_SECONDS - CORE_LEASE_GRACE_SECONDS
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
        reflector_timeout_seconds=timeout_seconds,
    )
    return TaskwiseCoreUpdatePortAdapterV1(bridge)


def build_supervised_core_bridge_at_roots_v2(
    *,
    state_root: Path,
    framework_lock: Path,
    codex_executable: Path,
    auth_source: Path,
    timeout_seconds: int,
    memory_limits: SupervisedMemoryLimitsV2 = SUPERVISED_MEMORY_LIMITS_V2,
) -> TaskwiseCoreEvolutionBridgeV1:
    """Build one category-private supervised stream on the verified Core method."""

    if (
        not isinstance(state_root, Path)
        or not state_root.is_absolute()
        or not isinstance(framework_lock, Path)
        or not framework_lock.is_absolute()
        or not isinstance(codex_executable, Path)
        or not codex_executable.is_absolute()
        or not isinstance(auth_source, Path)
        or not auth_source.is_absolute()
    ):
        raise TypeError("Core state, framework lock, and Codex executable must be absolute Paths")
    if type(memory_limits) is not SupervisedMemoryLimitsV2:
        raise TypeError("supervised memory limits must be exact")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 0 < timeout_seconds <= MAX_CORE_LEASE_SECONDS - CORE_LEASE_GRACE_SECONDS
    ):
        raise ValueError("Core reflector timeout must be positive and bounded")
    if not framework_lock.is_file():
        raise TaskwiseCoreEvolutionError("TASKWISE_VERIFIED_FRAMEWORK_LOCK_MISSING")

    capability = ReflectorExecutionBoundaryV2.detect_capability()
    if not capability.available:
        raise TaskwiseCoreEvolutionError("REFLECTOR_FILESYSTEM_ISOLATION_MISSING")
    _prepare_private_state_root(state_root)
    private_audit_root = state_root / "private_reflector_events"
    probe_record = {
        "uid": "0" * 64,
        "source_split": SUPERVISED_TRAIN_SOURCE_SPLIT,
        "packet_sha256": "0" * 64,
        "packet_part": 1,
        "packet_part_count": 1,
        "field": "preflight",
        "field_part": 1,
        "field_part_count": 1,
        "content": "PACKET_PART 001/001 field=preflight field_part=001/001 value=preflight",
        "reward": 0.0,
    }
    probe_digest = canonical_digest([probe_record])
    with tempfile.TemporaryDirectory(
        prefix=".supervised-reflector-preflight-",
        dir=state_root,
    ) as temporary:
        probe_path = Path(temporary) / "records.jsonl"
        _exclusive_private_write(probe_path, (_canonical_json(probe_record) + "\n").encode())
        boundary = ReflectorExecutionBoundaryV2(
            dev_artifact_path=probe_path,
            expected_records_sha256=probe_digest,
            expected_record_count=1,
            expected_source_split=SUPERVISED_TRAIN_SOURCE_SPLIT,
            private_audit_root=private_audit_root,
            real_codex_binary=codex_executable,
            auth_source=auth_source,
            timeout_seconds=timeout_seconds,
        )
        if not boundary.preflight().available:
            raise TaskwiseCoreEvolutionError("REFLECTOR_FILESYSTEM_ISOLATION_MISSING")

    def supervised_boundary_factory(
        artifact_path: Path,
        records_sha256: str,
        record_count: int,
    ) -> ReflectorExecutionBoundaryV2:
        return ReflectorExecutionBoundaryV2(
            dev_artifact_path=artifact_path,
            expected_records_sha256=records_sha256,
            expected_record_count=record_count,
            expected_source_split=SUPERVISED_TRAIN_SOURCE_SPLIT,
            private_audit_root=private_audit_root,
            real_codex_binary=codex_executable,
            auth_source=auth_source,
            timeout_seconds=timeout_seconds,
        )

    return TaskwiseCoreEvolutionBridgeV1(
        db_path=state_root / "evolution.sqlite3",
        artifact_root=state_root / "artifacts",
        executable_registry=load_verified_framework_registry(framework_lock),
        supervised_reflector_boundary_factory=supervised_boundary_factory,
        checkpoint_path=state_root / "private_lineage_checkpoints_v2.jsonl",
        memory_limits=memory_limits,
        reflector_timeout_seconds=timeout_seconds,
    )


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
    trajectory: SupervisedTrajectoryV2,
) -> tuple[str, ...]:
    """Derive typed private literals without persisting a second private DTO."""

    values: list[str] = [
        _encode_validator_literal("uid", trajectory.task_uid),
        _encode_validator_literal("completion", trajectory.raw_completion),
    ]
    for line in trajectory.public_prompt.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for prefix, source_kind in (
            ("Question:", "question"),
            ("A.", "option"),
            ("B.", "option"),
            ("C.", "option"),
            ("D.", "option"),
        ):
            if stripped.startswith(prefix):
                remainder = stripped[len(prefix) :].strip()
                if remainder:
                    values.append(_encode_validator_literal(source_kind, remainder))
                break
    return _deduplicate_literals(values)


def _bind_auxiliary_source_evidence(
    *,
    reported_evidence_digests: tuple[str, ...],
    allowed_evidence_digests: frozenset[str],
    current_packet_digest: str,
) -> tuple[str, ...]:
    """Bind auxiliary source provenance to the current Core-owned Train packet.

    The model may report a subset of already-seen packet digests while merging a
    predecessor. It cannot author artifact provenance: the current packet is the
    only direct source for this update, while older support remains reachable
    through the predecessor artifact lineage.
    """

    if (
        type(current_packet_digest) is not str
        or _SHA256_RE.fullmatch(current_packet_digest) is None
        or type(reported_evidence_digests) is not tuple
        or not reported_evidence_digests
        or any(
            type(value) is not str or _SHA256_RE.fullmatch(value) is None
            for value in reported_evidence_digests
        )
        or len(set(reported_evidence_digests)) != len(reported_evidence_digests)
        or type(allowed_evidence_digests) is not frozenset
        or current_packet_digest not in allowed_evidence_digests
        # The reflector can echo an opaque supporting-task-set hash that is
        # visible in the approved predecessor memory alongside a real Train
        # packet digest.  Those model-authored values never become artifact
        # provenance: Core binds the auxiliary artifact solely to the current
        # packet below.  Require at least one real, already-authorized packet
        # digest so an entirely invented evidence set still fails closed.
        or set(reported_evidence_digests).isdisjoint(allowed_evidence_digests)
    ):
        raise TaskwiseCoreEvolutionError("TASKWISE_ARTIFACT_VALIDATION_FAILED")
    return (current_packet_digest,)


def _taskwise_reflector_records(
    trajectories: tuple[SupervisedTrajectoryV2, ...],
) -> list[dict[str, object]]:
    """Build the only trajectory projection mounted inside the reflector boundary."""

    ordered_supervised_trajectory_digest_v2(trajectories)
    return [
        {
            "uid": f"taskwise-reflector-record-{trajectory.round_index}",
            "source_split": TASKWISE_SOURCE_SPLIT,
            **trajectory.to_reflector_projection(),
        }
        for trajectory in trajectories
    ]


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


def _checkpoint_literal_delta(
    prior: tuple[str, ...],
    current: tuple[str, ...],
) -> tuple[str, ...]:
    """Return the append-only literal suffix stored by checkpoint schema v2."""

    normalized_prior = _deduplicate_literals(prior)
    normalized_current = _deduplicate_literals(current)
    if (
        normalized_prior != prior
        or normalized_current != current
        or normalized_current[: len(normalized_prior)] != normalized_prior
    ):
        raise TaskwiseCoreEvolutionError("TASKWISE_VALIDATOR_INPUT_DRIFT")
    return normalized_current[len(normalized_prior) :]


def _merge_checkpoint_literal_delta(
    prior: tuple[str, ...],
    delta: object,
) -> tuple[str, ...]:
    """Reconstruct one cumulative validator input without quadratic checkpoint rows."""

    if not isinstance(delta, list) or any(type(item) is not str for item in delta):
        raise ValueError("checkpoint literal delta must be a string list")
    normalized_prior = _deduplicate_literals(prior)
    normalized_delta = _deduplicate_literals(delta)
    if normalized_prior != prior or len(normalized_delta) != len(delta):
        raise ValueError("checkpoint literal delta is not canonical")
    prior_keys = {item.casefold() for item in normalized_prior}
    if any(item.casefold() in prior_keys for item in normalized_delta):
        raise ValueError("checkpoint literal delta overlaps its predecessor")
    return (*normalized_prior, *normalized_delta)


def _validate_reflector_timeout_seconds(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 < value <= MAX_CORE_LEASE_SECONDS - CORE_LEASE_GRACE_SECONDS
    ):
        raise ValueError("reflector timeout must leave bounded Core lease grace")
    return value


def _core_lease_seconds(reflector_timeout_seconds: int) -> int:
    timeout = _validate_reflector_timeout_seconds(reflector_timeout_seconds)
    return timeout + CORE_LEASE_GRACE_SECONDS


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


def _encode_validator_literal(source_kind: str, value: str) -> str:
    """Encode private source-kind metadata inside the private validator stream."""

    if source_kind not in _VALIDATOR_LITERAL_KINDS:
        raise ValueError("validator literal source kind is outside the closed set")
    if type(value) is not str or not value.strip():
        raise TypeError("validator literal must be non-empty text")
    return f"{_VALIDATOR_LITERAL_PREFIX}{source_kind}:{value.strip()}"


def _decode_validator_literal(value: str) -> tuple[str, str]:
    """Decode typed private literals; legacy/controller literals stay strict."""

    if type(value) is not str or not value.strip():
        raise TypeError("validator literal must be non-empty text")
    stripped = value.strip()
    if not stripped.startswith(_VALIDATOR_LITERAL_PREFIX):
        return "strict", stripped
    encoded = stripped[len(_VALIDATOR_LITERAL_PREFIX) :]
    source_kind, separator, literal = encoded.partition(":")
    if not separator or source_kind not in _VALIDATOR_LITERAL_KINDS or not literal.strip():
        raise ValueError("typed validator literal is malformed")
    return source_kind, literal.strip()


def _normalized_tokens(value: str) -> tuple[str, ...]:
    """Tokenize already-normalized text using Unicode alphanumeric boundaries."""

    if type(value) is not str:
        raise TypeError("normalized token input must be a string")
    return tuple(match.group(0) for match in _unicode_token_spans(value))


def _unicode_token_spans(value: str) -> tuple[re.Match[str], ...]:
    """Return deterministic complete-token spans for Unicode alphanumeric text."""

    if type(value) is not str:
        raise TypeError("token span input must be a string")
    return tuple(re.finditer(r"[^\W_]+", value))


def _full_literal_scan_allowed(source_kind: str, value: str) -> bool:
    """Apply source-kind thresholds without weakening UID/controller literals."""

    normalized = _normalize(value)
    if source_kind in {"strict", "uid"}:
        return len(normalized) >= 8
    tokens = _normalized_tokens(normalized)
    longest_token = max(map(len, tokens), default=0)
    if source_kind == "question":
        return len(normalized) >= 20 and (len(tokens) >= 4 or longest_token >= 40)
    if source_kind == "option":
        return len(normalized) >= 24 and (len(tokens) >= 4 or longest_token >= 40)
    if source_kind == "completion":
        return len(normalized) >= 64 and len(tokens) >= 8
    raise ValueError("validator literal source kind is outside the closed set")


def _source_kind_ngram_overlap(
    source_kind: str,
    source: str,
    candidate: str,
) -> bool:
    """Use stricter semantic spans for prompts than for explicit controller inputs."""

    return _source_kind_ngram_match(source_kind, source, candidate) is not None


def _source_kind_ngram_match(
    source_kind: str,
    source: str,
    candidate: str,
) -> _ValidatorSpanMatchV1 | None:
    """Return the first deterministic complete-token span allowed for one source kind."""

    if source_kind in {"strict", "uid"}:
        return _sequential_ngram_match(source, candidate)
    if source_kind in {"question", "option"}:
        minimum_source_ratio = 0.30 if source_kind == "question" else 0.40
        return _sequential_ngram_match(
            source,
            candidate,
            minimum_characters=32,
            minimum_tokens=4,
            long_token_characters=48,
            minimum_source_ratio=minimum_source_ratio,
            minimum_candidate_ratio=0.002,
        )
    if source_kind == "completion":
        return _sequential_ngram_match(
            source,
            candidate,
            minimum_characters=64,
            minimum_tokens=8,
            long_token_characters=80,
            minimum_source_ratio=0.50,
            minimum_candidate_ratio=0.004,
        )
    raise ValueError("validator literal source kind is outside the closed set")


def _normalize(value: str) -> str:
    """NFKC/case/punctuation normalization for leakage comparison."""

    canonical = _security_scan_text(value).casefold()
    normalized: list[str] = []
    pending_separator = False
    for character in canonical:
        if character.isalnum():
            if pending_separator and normalized:
                normalized.append(" ")
            normalized.append(character)
            pending_separator = False
        else:
            pending_separator = bool(normalized)
    return "".join(normalized)


def _security_scan_text(value: str) -> str:
    """Canonicalize compatibility forms and remove invisible format controls."""

    if type(value) is not str:
        raise TypeError("security scan input must be a string")
    canonical = unicodedata.normalize("NFKC", value)
    return "".join(character for character in canonical if unicodedata.category(character) != "Cf")


def _contains_bounded_literal(text: str, literal: str) -> bool:
    """Match an exact literal without accepting a prefix inside a longer token."""

    return _bounded_literal_span(text, literal) is not None


def _bounded_literal_span(text: str, literal: str) -> tuple[int, int] | None:
    """Return the first case-folded match whose two token boundaries are complete."""

    if type(text) is not str or type(literal) is not str or not literal:
        return None
    candidate = text.casefold()
    needle = literal.casefold()
    offset = 0
    while True:
        index = candidate.find(needle, offset)
        if index < 0:
            return None
        end = index + len(needle)
        starts_in_token = (
            index > 0
            and _literal_token_character(needle[0])
            and _literal_token_character(candidate[index - 1])
        )
        ends_in_token = (
            end < len(candidate)
            and _literal_token_character(needle[-1])
            and _literal_token_character(candidate[end])
        )
        if not starts_in_token and not ends_in_token:
            return index, end
        offset = index + 1


def _literal_match_evidence(source: str, candidate: str) -> _ValidatorSpanMatchV1:
    """Measure a previously accepted complete bounded literal without exposing it."""

    if _bounded_literal_span(candidate, source) is None:
        raise ValueError("literal evidence requires a bounded match")
    return _ValidatorSpanMatchV1(
        segment=source,
        token_count=len(_normalized_tokens(_normalize(source))),
        left_token_boundary=True,
        right_token_boundary=True,
        source_ratio=1.0,
        candidate_ratio=len(source) / max(1, len(candidate)),
    )


def _ratio_ppm(value: float) -> int:
    return min(1_000_000, max(0, round(value * 1_000_000)))


def _validator_finding_evidence(
    *,
    finding_code: str,
    source_kind: str,
    match: _ValidatorSpanMatchV1,
) -> TaskwiseValidatorFindingEvidenceV1:
    return TaskwiseValidatorFindingEvidenceV1(
        finding_code=finding_code,
        source_kind=source_kind,
        token_count=match.token_count,
        char_count=len(match.segment),
        left_token_boundary=match.left_token_boundary,
        right_token_boundary=match.right_token_boundary,
        source_ratio_ppm=_ratio_ppm(match.source_ratio),
        candidate_ratio_ppm=_ratio_ppm(match.candidate_ratio),
        segment_sha256=_sha256_bytes(match.segment.encode("utf-8")),
    )


def _artifact_finding_evidence(
    *,
    finding_code: str,
    payload_sha256: str,
    utf8_byte_count: int,
    estimated_token_count: int,
) -> TaskwiseValidatorFindingEvidenceV1:
    """Bind non-literal findings to the whole candidate without recording content."""

    return TaskwiseValidatorFindingEvidenceV1(
        finding_code=finding_code,
        source_kind="artifact",
        token_count=estimated_token_count,
        char_count=utf8_byte_count,
        left_token_boundary=True,
        right_token_boundary=True,
        source_ratio_ppm=1_000_000,
        candidate_ratio_ppm=1_000_000,
        segment_sha256=payload_sha256,
    )


def _literal_token_character(character: str) -> bool:
    return character == "_" or character.isalnum()


def _contains_answer_map(text: str) -> bool:
    """Detect closed A-D mappings while ignoring chemical letter prefixes."""

    if type(text) is not str:
        raise TypeError("answer-map scan input must be a string")
    scan_text = _ANSWER_MAP_WRAPPER_RE.sub(" ", _security_scan_text(text))
    return _ANSWER_MAP_RE.search(scan_text) is not None


def _contains_path_or_benchmark_marker(text: str) -> bool:
    """Detect explicit benchmark/file references without treating chemistry as paths."""

    if type(text) is not str:
        raise TypeError("path-marker scan input must be a string")
    scan_text = _security_scan_text(text)
    if _PATH_OR_BENCHMARK_RE.search(scan_text) is not None:
        return True
    for match in _POSIX_ABSOLUTE_PATH_RE.finditer(scan_text):
        if match.start() > 0:
            preceding = scan_text[match.start() - 1]
            if not preceding.isspace() and preceding not in _POSIX_PATH_OPENING_BOUNDARY:
                continue
        segments = match.group(0)[1:].split("/")
        if any(
            not segment
            or (not segment[0].isalnum() and segment[0] not in "._~")
            or (not segment[-1].isalnum() and segment[-1] not in "._~")
            for segment in segments
        ):
            continue
        return True
    return False


def _normalize_rule(value: str) -> str:
    """Canonical duplicate key preserving internal chemistry punctuation."""

    canonical = " ".join(_security_scan_text(value).casefold().split())
    return canonical.rstrip(" \t.!?;:,。！？；：，\"'”’")


def _sequential_ngram_overlap(
    source: str,
    candidate: str,
    *,
    minimum_characters: int = _NGRAM_CONTIGUOUS_WIDTH,
    minimum_tokens: int = 1,
    long_token_characters: int = _NGRAM_CONTIGUOUS_WIDTH,
    minimum_source_ratio: float = 0.0,
    minimum_candidate_ratio: float = 0.0,
) -> bool:
    """Require a complete-token contiguous overlap under a deterministic policy."""

    return (
        _sequential_ngram_match(
            source,
            candidate,
            minimum_characters=minimum_characters,
            minimum_tokens=minimum_tokens,
            long_token_characters=long_token_characters,
            minimum_source_ratio=minimum_source_ratio,
            minimum_candidate_ratio=minimum_candidate_ratio,
        )
        is not None
    )


def _sequential_ngram_match(
    source: str,
    candidate: str,
    *,
    minimum_characters: int = _NGRAM_CONTIGUOUS_WIDTH,
    minimum_tokens: int = 1,
    long_token_characters: int = _NGRAM_CONTIGUOUS_WIDTH,
    minimum_source_ratio: float = 0.0,
    minimum_candidate_ratio: float = 0.0,
) -> _ValidatorSpanMatchV1 | None:
    """Return deterministic measurements for a complete-token contiguous overlap."""

    if type(source) is not str or type(candidate) is not str:
        raise TypeError("ngram scan inputs must be strings")
    if (
        isinstance(minimum_characters, bool)
        or not isinstance(minimum_characters, int)
        or minimum_characters < _NGRAM_CONTIGUOUS_WIDTH
        or isinstance(minimum_tokens, bool)
        or not isinstance(minimum_tokens, int)
        or minimum_tokens < 1
        or isinstance(long_token_characters, bool)
        or not isinstance(long_token_characters, int)
        or long_token_characters < minimum_characters
        or isinstance(minimum_source_ratio, bool)
        or not isinstance(minimum_source_ratio, (int, float))
        or not 0 <= minimum_source_ratio <= 1
        or isinstance(minimum_candidate_ratio, bool)
        or not isinstance(minimum_candidate_ratio, (int, float))
        or not 0 <= minimum_candidate_ratio <= 1
    ):
        raise ValueError("ngram policy is invalid")
    if len(source) < minimum_characters:
        return None
    tokens = _unicode_token_spans(source)
    required_span_characters = max(
        minimum_characters,
        math.ceil(len(source) * minimum_source_ratio),
        math.ceil(len(candidate) * minimum_candidate_ratio),
    )

    def ratios_allow(span: str) -> bool:
        return (
            len(span) / len(source) >= minimum_source_ratio
            and len(span) / max(1, len(candidate)) >= minimum_candidate_ratio
        )

    for start_index, start_token in enumerate(tokens):
        long_token = start_token.group(0)
        if (
            len(long_token) >= long_token_characters
            and ratios_allow(long_token)
            and _contains_bounded_literal(candidate, long_token)
        ):
            return _ValidatorSpanMatchV1(
                segment=long_token,
                token_count=1,
                left_token_boundary=True,
                right_token_boundary=True,
                source_ratio=len(long_token) / len(source),
                candidate_ratio=len(long_token) / max(1, len(candidate)),
            )
        required_end_index = start_index + minimum_tokens - 1
        for end_index in range(start_index, len(tokens)):
            if end_index < required_end_index:
                continue
            token_aligned_span = source[start_token.start() : tokens[end_index].end()]
            if len(token_aligned_span) < required_span_characters:
                continue
            if ratios_allow(token_aligned_span) and _contains_bounded_literal(
                candidate,
                token_aligned_span,
            ):
                return _ValidatorSpanMatchV1(
                    segment=token_aligned_span,
                    token_count=end_index - start_index + 1,
                    left_token_boundary=True,
                    right_token_boundary=True,
                    source_ratio=len(token_aligned_span) / len(source),
                    candidate_ratio=len(token_aligned_span) / max(1, len(candidate)),
                )
            break
    return None


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _synthetic_supervised_auxiliary_output(
    packet: SupervisedEvolutionPacketV2,
) -> SupervisedStructuredAuxiliaryOutputV2:
    """Deterministic no-model projection used only by explicit synthetic tests."""

    skill = (
        f"# Category Skill: {packet.category}\n\n"
        f"## Name\n- {packet.category.casefold().replace('_', '-')}-reasoning-v2\n\n"
        f"## Description\n- Reusable category reasoning workflow for {packet.category}.\n\n"
        f"## Category Scope\n- Apply only to {packet.category}.\n\n"
        "## When To Use\n- When solving this chemistry category.\n\n"
        "## Workflow\n1. Apply the category principle before comparing choices.\n\n"
        "## Validation Checks\n- Validate the conclusion against chemical constraints.\n\n"
        "## Failure Recovery\n- Reject unsupported shortcuts before finalizing.\n"
    )
    agent = (
        f"# Category Agent System: {packet.category}\n\n"
        "## Directives\n"
        "- When solving this category, use explicit chemistry constraints. "
        "Validate by checking the selected choice against those constraints.\n\n"
        "## Output Discipline\n- Return exactly the requested option format.\n"
    )
    return SupervisedStructuredAuxiliaryOutputV2(
        category=packet.category,
        skill_markdown=skill,
        agent_system_markdown=agent,
        skill_source_sha256=canonical_digest(
            {"synthetic": True, "target": "skill_bundle", "packet": packet.digest}
        ),
        agent_system_source_sha256=canonical_digest(
            {"synthetic": True, "target": "agent_system", "packet": packet.digest}
        ),
        skill_markdown_sha256=_sha256_bytes(skill.encode("utf-8")),
        agent_system_markdown_sha256=_sha256_bytes(agent.encode("utf-8")),
        skill_evidence_digests=(packet.digest,),
        agent_system_evidence_digests=(packet.digest,),
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _validator_input_digest(values: tuple[str, ...]) -> str:
    return _sha256_bytes(_canonical_json(list(values)).encode("utf-8"))


SupervisedCoreEvolutionBridgeV2 = TaskwiseCoreEvolutionBridgeV1
SupervisedCoreEvolutionErrorV2 = TaskwiseCoreEvolutionError
SupervisedCorePredecessorV2 = TaskwiseCorePredecessorV1
SupervisedCoreUpdateRequestV2 = TaskwiseCoreUpdateRequestV1
SupervisedCoreUpdateResultV2 = TaskwiseCoreUpdateResultV1


__all__ = [
    "BRIDGE_ID",
    "METHOD_ID",
    "PROTOCOL_ID",
    "SUPERVISED_REFLECTOR_PROJECTION_ID",
    "ReflectorBoundaryFactoryV1",
    "SupervisedAuxiliaryCoreArtifactV2",
    "SupervisedCoreEvolutionBridgeV2",
    "SupervisedCoreEvolutionErrorV2",
    "SupervisedCorePredecessorV2",
    "SupervisedCoreUpdateRequestV2",
    "SupervisedCoreUpdateResultV2",
    "TaskwiseArtifactLineageReceiptV1",
    "TaskwiseCoreEvolutionBridgeV1",
    "TaskwiseCoreEvolutionError",
    "TaskwiseCoreFailureReceiptV1",
    "TaskwiseCorePredecessorV1",
    "TaskwiseCoreUpdatePortAdapterV1",
    "TaskwiseCoreUpdateRequestV1",
    "TaskwiseCoreUpdateResultV1",
    "TaskwiseTextMemoryValidatorV1",
    "TaskwiseValidatorFindingEvidenceV1",
    "build_supervised_core_bridge_at_roots_v2",
    "build_taskwise_core_port_at_roots_v1",
]
