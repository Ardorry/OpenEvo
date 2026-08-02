"""Fail-closed three-target Core coordinator for Temperature full-evolve v1.

The coordinator performs no model inference and starts no service.  It creates
three ordinary plan-bound Core jobs against a verified executable registry:

* ``text_memory`` consumes a run-private one-record dataset whose
  ``payload.summary`` is the already validated memory projection;
* ``skill_bundle`` consumes the validated skill projection through the formal
  manual materializer;
* ``agent_system`` consumes the validated agent-system projection through the
  formal manual materializer.

The fixed framing added by Core's ``text_memory`` implementation is accepted,
but the actual Core-owned payload bytes are reread through the verified payload
scanner and checked for exact projection binding, role separation, leakage and
the combined 8 KiB hard limit.  No successor state is published until all
three initially unpromoted outputs pass, are promoted, and close one formal
multi-target materialization.  Canonical private checkpoints retain both the
committed head and any partially-created next batch; recovery replays the same
plan requests (which Core stores idempotently) and revalidates the authoritative
job, artifact, promotion, materialized-context and payload records before
restoring the coordinator.  Runtime consumers receive only the issuer-sealed
three-target context after that authoritative replay.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol

from openevo.evolution.artifact_payloads import ArtifactPayloadService
from openevo.evolution.context_materialization import MaterializedContext
from openevo.evolution.context_projection import ContextProjectionResolveRequest
from openevo.evolution.framework import (
    DescriptorKind,
    EvolutionExecutionProfile,
    EvolutionTargetSelection,
    RuntimeDestinationRoots,
    TargetConsumptionLimits,
    canonical_digest,
)
from openevo.evolution.framework.builtins import (
    VerifiedExecutableRegistry,
    require_verified_executable_registry,
)
from openevo.evolution.models import (
    ArtifactRegisterRequest,
    ArtifactResponse,
    ArtifactState,
    ArtifactType,
    JobCreateResponse,
    JobState,
    WorkerClaimRequest,
    WorkerCompleteRequest,
    WorkerFailRequest,
    WorkerHeartbeatRequest,
)
from openevo.evolution.planned_jobs import (
    PlanBoundJobCreateRequest,
    PlannedInputBinding,
)
from openevo.evolution.worker import run_once
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openevo_chembench.chembench4k_dataset import normalize_benchmark_text
from openevo_chembench.frozen_runtime_v2 import _issue_core_resolved_text_memory_v2
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    sha256_bytes,
    write_private_file,
)
from openevo_chembench.supervised_transfer_v2.artifacts import (
    CoreResolvedSupervisedContextV2,
    _issue_core_resolved_auxiliary_v2,
)
from openevo_chembench.temperature_full_evolve_v1.artifacts import (
    HARD_TOTAL_BYTES,
    TARGET_MAX_BYTES,
    TARGET_ORDER,
    TARGET_TOTAL_BYTES,
    ProjectedArtifactSetV1,
    ProjectedArtifactV1,
)
from openevo_chembench.temperature_full_evolve_v1.evidence import RuleEvidenceIndexV1

PROTOCOL_ID = "chembench_temperature_full_evolve_v1"
CORE_COORDINATOR_ID = "temperature_three_target_planned_job_coordinator_v1"
TARGET_VALIDATOR_ID = "temperature_core_payload_validator_v1"
TEXT_MEMORY_DATASET_SCHEMA = "TemperatureProjectedMemoryDatasetManifestV1"
TEXT_MEMORY_RECORD_SCHEMA = "TemperatureProjectedMemoryDatasetRecordV1"
TARGET_METHODS: Mapping[str, str] = MappingProxyType(
    {
        "text_memory": "text_memory",
        "skill_bundle": "skill_bundle",
        "agent_system": "agent_system",
    }
)

_MAX_BATCH_INDEX = 4
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_CORE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,511}\Z", re.ASCII)
_UID = re.compile(r"\b[0-9a-f]{64}\b", re.ASCII)
_ANSWER_MAP = re.compile(
    r"\b(?:answer|option|choice|prediction)\s*(?:is|=|:|->|maps?\s+to)?\s*[ABCD]\b",
    re.IGNORECASE | re.ASCII,
)
_STANDALONE_CHOICE = re.compile(r"(?m)^\s*(?:[-*]\s*)?[ABCD][.)]?\s*$", re.ASCII)
_RULE_LINE_PREFIX = re.compile(r"^rule-[0-9a-f]{24}:\s*", re.ASCII)
_CHECKPOINT_STATE_SCHEMA = "TemperatureCoreEvolutionStateCheckpointV1"
_CHECKPOINT_COORDINATOR_SCHEMA = "TemperatureCoreCoordinatorCheckpointV1"
_CONTEXT_TARGET_ORDER = ("agent_system", "text_memory", "skill_bundle")
_CONTEXT_INSTRUCTION = "Resolve the frozen Temperature full-evolve v1 runtime context."
_CONTEXT_DESTINATIONS = RuntimeDestinationRoots(
    target_data="/openevo/session/evolution",
    harness_skills="/openevo/session/evolution/skills",
    harness_instruction="/workspace/repository",
)


class TemperatureCoreEvolutionError(RuntimeError):
    """Closed coordinator failure without private payload text."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


def _require_sha256(value: object, finding_code: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        raise TemperatureCoreEvolutionError(finding_code)
    return value


def _require_core_id(value: object, finding_code: str) -> str:
    if type(value) is not str or _CORE_ID.fullmatch(value) is None:
        raise TemperatureCoreEvolutionError(finding_code)
    return value


def _forbidden_questions_sha256(values: tuple[str, ...]) -> str:
    if not isinstance(values, tuple) or any(
        type(value) is not str or not value.strip() for value in values
    ):
        raise TypeError("forbidden_questions must be a non-empty-text tuple")
    return sha256_bytes(canonical_json_bytes(list(values)))


class CoreTextMemoryDatasetBindingV1(_FrozenModel):
    """Digest binding for the registered run-private projected-memory dataset."""

    schema_version: Literal["TemperatureCoreTextMemoryDatasetBindingV1"] = (
        "TemperatureCoreTextMemoryDatasetBindingV1"
    )
    batch_index: int = Field(ge=1, le=_MAX_BATCH_INDEX)
    artifact_id: str
    artifact_name: str = Field(min_length=1, max_length=512)
    manifest_uri: str = Field(min_length=1, max_length=4096)
    records_uri: str = Field(min_length=1, max_length=4096)
    manifest_sha256: str
    manifest_utf8_bytes: int = Field(ge=1, le=64 * 1024)
    records_sha256: str
    records_utf8_bytes: int = Field(ge=1, le=64 * 1024)
    source_packet_sha256: str
    reflector_receipt_sha256: str
    evidence_sha256: str
    projected_artifact_set_sha256: str
    projected_memory_sha256: str
    projected_memory_utf8_bytes: int = Field(ge=1, le=8192)

    @field_validator("artifact_id")
    @classmethod
    def _artifact_id(cls, value: str) -> str:
        if _CORE_ID.fullmatch(value) is None:
            raise ValueError("dataset artifact ID is invalid")
        return value

    @field_validator(
        "manifest_sha256",
        "records_sha256",
        "source_packet_sha256",
        "reflector_receipt_sha256",
        "evidence_sha256",
        "projected_artifact_set_sha256",
        "projected_memory_sha256",
    )
    @classmethod
    def _digests(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("dataset binding digest is invalid")
        return value

    @model_validator(mode="after")
    def _file_uris(self) -> CoreTextMemoryDatasetBindingV1:
        if (
            not self.manifest_uri.startswith("file:///")
            or not self.records_uri.startswith("file:///")
            or self.manifest_uri == self.records_uri
        ):
            raise ValueError("dataset binding requires distinct absolute file URIs")
        return self

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.model_dump(mode="json")))

    def __repr__(self) -> str:
        return (
            "CoreTextMemoryDatasetBindingV1(<run-private-paths-redacted>; "
            f"batch_index={self.batch_index})"
        )

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class RenderedCoreTextMemoryDatasetV1:
    """Exact private dataset bytes to register as the text-memory job input."""

    batch_index: int
    artifact_name: str
    manifest_path: Path
    records_path: Path
    manifest_bytes: bytes
    records_bytes: bytes
    source_packet_sha256: str
    reflector_receipt_sha256: str
    evidence_sha256: str
    projected_artifact_set_sha256: str
    projected_memory_sha256: str
    projected_memory_utf8_bytes: int

    def __repr__(self) -> str:
        return (
            "RenderedCoreTextMemoryDatasetV1(<private-bytes-and-paths>; "
            f"batch_index={self.batch_index})"
        )

    def write_private(self) -> None:
        """Idempotently write owner-only dataset files before registration."""

        for path, payload in (
            (self.records_path, self.records_bytes),
            (self.manifest_path, self.manifest_bytes),
        ):
            if path.exists():
                try:
                    existing = path.read_bytes()
                except OSError as exc:
                    raise TemperatureCoreEvolutionError(
                        "CORE_TEXT_MEMORY_DATASET_WRITE_FAILED"
                    ) from exc
                if existing != payload:
                    raise TemperatureCoreEvolutionError(
                        "CORE_TEXT_MEMORY_DATASET_EXISTING_BYTES_MISMATCH"
                    )
                continue
            write_private_file(path, payload, replace=False)

    def artifact_request(self) -> ArtifactRegisterRequest:
        return ArtifactRegisterRequest(
            type=ArtifactType.DATASET,
            name=self.artifact_name,
            uri=self.manifest_path.as_uri(),
            manifest={
                "schema_version": TEXT_MEMORY_DATASET_SCHEMA,
                "records_uri": self.records_path.as_uri(),
                "record_count": 1,
                "privacy": "run_private",
                "manifest_sha256": sha256_bytes(self.manifest_bytes),
                "records_sha256": sha256_bytes(self.records_bytes),
            },
            lineage={
                "protocol_id": PROTOCOL_ID,
                "batch_index": self.batch_index,
                "source_packet_sha256": self.source_packet_sha256,
                "reflector_receipt_sha256": self.reflector_receipt_sha256,
                "evidence_sha256": self.evidence_sha256,
                "projected_artifact_set_sha256": self.projected_artifact_set_sha256,
                "projected_memory_sha256": self.projected_memory_sha256,
            },
            tags=[PROTOCOL_ID, "projected-memory-dataset", f"batch-{self.batch_index}"],
            promoted=False,
        )

    def bind(self, artifact: ArtifactResponse) -> CoreTextMemoryDatasetBindingV1:
        if (
            type(artifact) is not ArtifactResponse
            or artifact.type is not ArtifactType.DATASET
            or artifact.name != self.artifact_name
            or artifact.uri != self.manifest_path.as_uri()
            or artifact.promoted
        ):
            raise TemperatureCoreEvolutionError(
                "CORE_TEXT_MEMORY_DATASET_REGISTRATION_INVALID"
            )
        return CoreTextMemoryDatasetBindingV1(
            batch_index=self.batch_index,
            artifact_id=artifact.artifact_id,
            artifact_name=self.artifact_name,
            manifest_uri=self.manifest_path.as_uri(),
            records_uri=self.records_path.as_uri(),
            manifest_sha256=sha256_bytes(self.manifest_bytes),
            manifest_utf8_bytes=len(self.manifest_bytes),
            records_sha256=sha256_bytes(self.records_bytes),
            records_utf8_bytes=len(self.records_bytes),
            source_packet_sha256=self.source_packet_sha256,
            reflector_receipt_sha256=self.reflector_receipt_sha256,
            evidence_sha256=self.evidence_sha256,
            projected_artifact_set_sha256=self.projected_artifact_set_sha256,
            projected_memory_sha256=self.projected_memory_sha256,
            projected_memory_utf8_bytes=self.projected_memory_utf8_bytes,
        )


def render_core_text_memory_dataset_v1(
    *,
    batch_index: int,
    projected_memory: ProjectedArtifactV1,
    dataset_directory: Path,
    source_packet_sha256: str,
    reflector_receipt_sha256: str,
    evidence_sha256: str,
    projected_artifact_set_sha256: str,
) -> RenderedCoreTextMemoryDatasetV1:
    """Render the one-record dataset consumed by Core's ``text_memory`` method."""

    if (
        isinstance(batch_index, bool)
        or not isinstance(batch_index, int)
        or not 1 <= batch_index <= _MAX_BATCH_INDEX
        or type(projected_memory) is not ProjectedArtifactV1
        or projected_memory.target_id != "text_memory"
        or not isinstance(dataset_directory, Path)
        or not dataset_directory.is_absolute()
    ):
        raise TypeError("projected-memory dataset arguments are invalid")
    for digest, code in (
        (source_packet_sha256, "CORE_SOURCE_PACKET_DIGEST_INVALID"),
        (reflector_receipt_sha256, "CORE_REFLECTOR_RECEIPT_DIGEST_INVALID"),
        (evidence_sha256, "CORE_EVIDENCE_DIGEST_INVALID"),
        (projected_artifact_set_sha256, "CORE_PROJECTED_SET_DIGEST_INVALID"),
    ):
        _require_sha256(digest, code)
    records_path = dataset_directory / "records.jsonl"
    manifest_path = dataset_directory / "manifest.json"
    artifact_name = f"Temperature projected memory batch {batch_index}"
    record = {
        "schema_version": TEXT_MEMORY_RECORD_SCHEMA,
        "task_id": f"temperature-projected-memory-batch-{batch_index}",
        "session_id": f"temperature-reflector-batch-{batch_index}",
        "status": "accepted_projected_memory",
        "reward": None,
        "payload": {"summary": projected_memory.content},
    }
    records_bytes = canonical_json_bytes(record)
    manifest = {
        "schema_version": TEXT_MEMORY_DATASET_SCHEMA,
        "name": artifact_name,
        "records_uri": records_path.as_uri(),
        "record_count": 1,
        "privacy": "run_private",
        "batch_index": batch_index,
        "source_packet_sha256": source_packet_sha256,
        "reflector_receipt_sha256": reflector_receipt_sha256,
        "evidence_sha256": evidence_sha256,
        "projected_artifact_set_sha256": projected_artifact_set_sha256,
        "projected_memory_sha256": projected_memory.sha256,
        "projected_memory_utf8_bytes": projected_memory.utf8_byte_count,
        "records_sha256": sha256_bytes(records_bytes),
    }
    return RenderedCoreTextMemoryDatasetV1(
        batch_index=batch_index,
        artifact_name=artifact_name,
        manifest_path=manifest_path,
        records_path=records_path,
        manifest_bytes=canonical_json_bytes(manifest),
        records_bytes=records_bytes,
        source_packet_sha256=source_packet_sha256,
        reflector_receipt_sha256=reflector_receipt_sha256,
        evidence_sha256=evidence_sha256,
        projected_artifact_set_sha256=projected_artifact_set_sha256,
        projected_memory_sha256=projected_memory.sha256,
        projected_memory_utf8_bytes=projected_memory.utf8_byte_count,
    )


class CoreProjectedArtifactV1(_FrozenModel):
    target_id: Literal["text_memory", "skill_bundle", "agent_system"]
    content: str
    sha256: str
    utf8_bytes: int = Field(ge=1, le=8192)

    @field_validator("sha256")
    @classmethod
    def _sha(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("projected artifact digest is invalid")
        return value

    @model_validator(mode="after")
    def _content(self) -> CoreProjectedArtifactV1:
        encoded = self.content.encode("utf-8")
        if (
            not self.content.endswith("\n")
            or len(encoded) != self.utf8_bytes
            or sha256_bytes(encoded) != self.sha256
        ):
            raise ValueError("projected artifact content binding is invalid")
        return self

    @classmethod
    def from_projection(cls, value: ProjectedArtifactV1) -> CoreProjectedArtifactV1:
        return cls(
            target_id=value.target_id,
            content=value.content,
            sha256=value.sha256,
            utf8_bytes=value.utf8_byte_count,
        )

    def __repr__(self) -> str:
        return f"CoreProjectedArtifactV1(target_id={self.target_id!r}, <private-content>)"

    __str__ = __repr__


class CoreTargetJobSpecV1(_FrozenModel):
    batch_index: int = Field(ge=1, le=_MAX_BATCH_INDEX)
    target_id: Literal["text_memory", "skill_bundle", "agent_system"]
    method_id: str
    request: PlanBoundJobCreateRequest
    plan_digest: str
    registry_snapshot_digest: str
    full_registry_digest: str
    method_identity_digest: str
    predecessor_state_sha256: str
    predecessor_artifact_id: str | None
    source_packet_sha256: str
    reflector_receipt_sha256: str
    evidence_sha256: str
    projected_artifact_set_sha256: str
    dataset_binding_sha256: str
    projected_artifact: CoreProjectedArtifactV1
    projection_validation_receipt_sha256: str

    @field_validator("method_id")
    @classmethod
    def _method(cls, value: str) -> str:
        if _CORE_ID.fullmatch(value) is None:
            raise ValueError("method ID is invalid")
        return value

    @field_validator("predecessor_artifact_id")
    @classmethod
    def _prior(cls, value: str | None) -> str | None:
        if value is not None and _CORE_ID.fullmatch(value) is None:
            raise ValueError("predecessor artifact ID is invalid")
        return value

    @field_validator(
        "plan_digest",
        "registry_snapshot_digest",
        "full_registry_digest",
        "method_identity_digest",
        "predecessor_state_sha256",
        "source_packet_sha256",
        "reflector_receipt_sha256",
        "evidence_sha256",
        "projected_artifact_set_sha256",
        "dataset_binding_sha256",
        "projection_validation_receipt_sha256",
    )
    @classmethod
    def _digests(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("target job spec digest is invalid")
        return value

    @model_validator(mode="after")
    def _closed(self) -> CoreTargetJobSpecV1:
        selection = self.request.selection()
        if (
            self.method_id != TARGET_METHODS[self.target_id]
            or self.projected_artifact.target_id != self.target_id
            or self.request.target_id != self.target_id
            or selection.method_id != self.method_id
            or canonical_digest(self.request.plan) != self.plan_digest
            or selection.method_identity_digest != self.method_identity_digest
            or self.request.plan.registry_snapshot_digest
            != self.registry_snapshot_digest
        ):
            raise ValueError("target job spec closure is invalid")
        return self

    def __repr__(self) -> str:
        return (
            "CoreTargetJobSpecV1(<request-and-content-redacted>; "
            f"target_id={self.target_id!r}, batch_index={self.batch_index})"
        )

    __str__ = __repr__


class CoreTargetPlannedJobV1(_FrozenModel):
    spec: CoreTargetJobSpecV1
    job_id: str
    expected_core_payload_sha256: str
    expected_core_payload_utf8_bytes: int = Field(ge=1, le=HARD_TOTAL_BYTES)

    @field_validator("job_id")
    @classmethod
    def _job_id(cls, value: str) -> str:
        if _CORE_ID.fullmatch(value) is None:
            raise ValueError("planned job ID is invalid")
        return value

    @field_validator("expected_core_payload_sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("expected Core payload digest is invalid")
        return value

    @property
    def batch_index(self) -> int:
        return self.spec.batch_index

    @property
    def target_id(self) -> str:
        return self.spec.target_id

    @property
    def method_id(self) -> str:
        return self.spec.method_id

    @property
    def request(self) -> PlanBoundJobCreateRequest:
        return self.spec.request

    def __repr__(self) -> str:
        return (
            "CoreTargetPlannedJobV1(<request-content-redacted>; "
            f"target_id={self.target_id!r}, batch_index={self.batch_index})"
        )

    __str__ = __repr__


class CoreMaterializedTargetBindingV1(_FrozenModel):
    """Private binding of one promoted artifact to its injected Core blobs."""

    schema_version: Literal["TemperatureCoreMaterializedTargetBindingV1"] = (
        "TemperatureCoreMaterializedTargetBindingV1"
    )
    target_id: Literal["text_memory", "skill_bundle", "agent_system"]
    artifact_id: str
    artifact_payload_sha256: str
    artifact_payload_utf8_bytes: int = Field(ge=1, le=HARD_TOTAL_BYTES)
    resolved_content: str
    resolved_content_sha256: str
    resolved_content_utf8_bytes: int = Field(ge=1, le=HARD_TOTAL_BYTES)
    blob_ids: tuple[str, ...]
    blob_inventory_sha256: str

    @field_validator("artifact_id")
    @classmethod
    def _artifact_id(cls, value: str) -> str:
        if _CORE_ID.fullmatch(value) is None:
            raise ValueError("materialized target artifact ID is invalid")
        return value

    @field_validator(
        "artifact_payload_sha256",
        "resolved_content_sha256",
        "blob_inventory_sha256",
    )
    @classmethod
    def _digests(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("materialized target digest is invalid")
        return value

    @field_validator("blob_ids")
    @classmethod
    def _blob_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if (
            not values
            or len(values) != len(set(values))
            or any(_CORE_ID.fullmatch(value) is None for value in values)
        ):
            raise ValueError("materialized target blob IDs are invalid")
        return values

    @model_validator(mode="after")
    def _content_binding(self) -> CoreMaterializedTargetBindingV1:
        encoded = self.resolved_content.encode("utf-8")
        expected_blob_count = 2 if self.target_id == "agent_system" else 1
        if (
            len(encoded) != self.resolved_content_utf8_bytes
            or sha256_bytes(encoded) != self.resolved_content_sha256
            or self.artifact_payload_sha256 != self.resolved_content_sha256
            or self.artifact_payload_utf8_bytes != self.resolved_content_utf8_bytes
            or len(self.blob_ids) != expected_blob_count
        ):
            raise ValueError("materialized target content binding is invalid")
        return self

    def __repr__(self) -> str:
        return (
            "CoreMaterializedTargetBindingV1(<identities-and-content-redacted>; "
            f"target_id={self.target_id!r})"
        )

    __str__ = __repr__


class CoreMaterializedContextBindingV1(_FrozenModel):
    """Private authoritative binding for the one three-target materialization."""

    schema_version: Literal["TemperatureCoreMaterializedContextBindingV1"] = (
        "TemperatureCoreMaterializedContextBindingV1"
    )
    context_id: str
    request_sha256: str
    registry_sha256: str
    materialization_sha256: str
    selected_artifact_ids: tuple[str, str, str]
    selected_artifact_ids_sha256: str
    projection_sha256: str
    instruction_sha256: str
    instruction_utf8_bytes: int = Field(ge=1, le=HARD_TOTAL_BYTES * 2)
    environment_sha256: str
    blob_inventory_sha256: str
    target_bindings: tuple[CoreMaterializedTargetBindingV1, ...]

    @field_validator("context_id")
    @classmethod
    def _context_id(cls, value: str) -> str:
        if _CORE_ID.fullmatch(value) is None:
            raise ValueError("materialized context ID is invalid")
        return value

    @field_validator(
        "request_sha256",
        "registry_sha256",
        "materialization_sha256",
        "selected_artifact_ids_sha256",
        "projection_sha256",
        "instruction_sha256",
        "environment_sha256",
        "blob_inventory_sha256",
    )
    @classmethod
    def _digests(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("materialized context digest is invalid")
        return value

    @model_validator(mode="after")
    def _closed(self) -> CoreMaterializedContextBindingV1:
        if (
            len(set(self.selected_artifact_ids)) != len(TARGET_ORDER)
            or sha256_bytes(canonical_json_bytes(list(self.selected_artifact_ids)))
            != self.selected_artifact_ids_sha256
            or tuple(item.target_id for item in self.target_bindings)
            != tuple(TARGET_ORDER)
            or set(self.selected_artifact_ids)
            != {item.artifact_id for item in self.target_bindings}
        ):
            raise ValueError("materialized context closure is invalid")
        return self

    def target(self, target_id: str) -> CoreMaterializedTargetBindingV1:
        return next(item for item in self.target_bindings if item.target_id == target_id)

    def __repr__(self) -> str:
        return (
            "CoreMaterializedContextBindingV1(<context-content-redacted>; "
            f"context_id={self.context_id!r})"
        )

    __str__ = __repr__


class PreparedCoreBatchV1(_FrozenModel):
    schema_version: Literal["TemperaturePreparedCoreBatchV1"] = (
        "TemperaturePreparedCoreBatchV1"
    )
    batch_index: int = Field(ge=1, le=_MAX_BATCH_INDEX)
    predecessor_state_sha256: str
    source_packet_sha256: str
    reflector_receipt_sha256: str
    evidence_sha256: str
    projected_artifact_set_sha256: str
    projected_total_utf8_bytes: int = Field(ge=1, le=HARD_TOTAL_BYTES)
    forbidden_questions_sha256: str
    full_registry_digest: str
    request_sha256: str
    text_memory_dataset: CoreTextMemoryDatasetBindingV1
    projected_artifacts: tuple[CoreProjectedArtifactV1, ...]
    job_specs: tuple[CoreTargetJobSpecV1, ...]
    jobs: tuple[CoreTargetPlannedJobV1, ...]
    validated_receipts: tuple[CoreTargetCompletionReceiptV1, ...] = ()
    promoted_artifact_ids: tuple[str, ...] = ()
    materialized_context: CoreMaterializedContextBindingV1 | None = None

    @field_validator(
        "predecessor_state_sha256",
        "source_packet_sha256",
        "reflector_receipt_sha256",
        "evidence_sha256",
        "projected_artifact_set_sha256",
        "forbidden_questions_sha256",
        "full_registry_digest",
        "request_sha256",
    )
    @classmethod
    def _digests(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("prepared batch digest is invalid")
        return value

    @model_validator(mode="after")
    def _closed(self) -> PreparedCoreBatchV1:
        targets = tuple(TARGET_ORDER)
        if (
            self.text_memory_dataset.batch_index != self.batch_index
            or tuple(item.target_id for item in self.projected_artifacts) != targets
            or tuple(item.target_id for item in self.job_specs) != targets
            or tuple(item.target_id for item in self.jobs)
            != targets[: len(self.jobs)]
            or len(self.jobs) > len(targets)
            or sum(item.utf8_bytes for item in self.projected_artifacts)
            != self.projected_total_utf8_bytes
        ):
            raise ValueError("prepared batch target closure is invalid")
        for spec, projected in zip(self.job_specs, self.projected_artifacts, strict=True):
            if (
                spec.projected_artifact != projected
                or spec.batch_index != self.batch_index
                or spec.predecessor_state_sha256 != self.predecessor_state_sha256
                or spec.source_packet_sha256 != self.source_packet_sha256
                or spec.reflector_receipt_sha256 != self.reflector_receipt_sha256
                or spec.evidence_sha256 != self.evidence_sha256
                or spec.projected_artifact_set_sha256
                != self.projected_artifact_set_sha256
                or spec.full_registry_digest != self.full_registry_digest
                or spec.dataset_binding_sha256 != self.text_memory_dataset.digest
            ):
                raise ValueError("prepared batch job spec closure is invalid")
        if any(job.spec != self.job_specs[index] for index, job in enumerate(self.jobs)):
            raise ValueError("prepared batch created-job prefix is invalid")
        if self.validated_receipts:
            if (
                not self.complete
                or tuple(receipt.target_id for receipt in self.validated_receipts)
                != targets
                or any(
                    receipt.planned_job != self.jobs[index]
                    for index, receipt in enumerate(self.validated_receipts)
                )
                or len({receipt.artifact_id for receipt in self.validated_receipts})
                != len(targets)
            ):
                raise ValueError("prepared batch completion receipts are invalid")
        elif self.promoted_artifact_ids or self.materialized_context is not None:
            raise ValueError("prepared batch has progress before output validation")
        receipt_ids = tuple(receipt.artifact_id for receipt in self.validated_receipts)
        if (
            self.promoted_artifact_ids != receipt_ids[: len(self.promoted_artifact_ids)]
            or tuple(
                receipt.artifact_id
                for receipt in self.validated_receipts
                if receipt.promoted
            )
            != self.promoted_artifact_ids
        ):
            raise ValueError("prepared batch promotion prefix is invalid")
        if self.materialized_context is not None and (
            len(self.promoted_artifact_ids) != len(targets)
            or set(self.materialized_context.selected_artifact_ids) != set(receipt_ids)
            or any(
                self.materialized_context.target(receipt.target_id).artifact_id
                != receipt.artifact_id
                for receipt in self.validated_receipts
            )
        ):
            raise ValueError("prepared batch materialized context is invalid")
        return self

    @property
    def complete(self) -> bool:
        return len(self.jobs) == len(TARGET_ORDER)

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.model_dump(mode="json")))

    def __repr__(self) -> str:
        return (
            "PreparedCoreBatchV1(<private-job-contracts>; "
            f"batch_index={self.batch_index}, created_jobs={len(self.jobs)})"
        )

    __str__ = __repr__


class CoreTargetCompletionReceiptV1(_FrozenModel):
    schema_version: Literal["TemperatureCoreTargetCompletionReceiptV1"] = (
        "TemperatureCoreTargetCompletionReceiptV1"
    )
    status: Literal[
        "SUCCEEDED_VALIDATED_UNPROMOTED",
        "SUCCEEDED_VALIDATED_PROMOTED",
    ] = "SUCCEEDED_VALIDATED_UNPROMOTED"
    planned_job: CoreTargetPlannedJobV1
    artifact_id: str
    artifact_content: str
    core_payload_sha256: str
    core_payload_utf8_bytes: int = Field(ge=1, le=HARD_TOTAL_BYTES)
    payload_manifest_sha256: str
    actual_payload_validation_sha256: str
    projected_to_core_binding_sha256: str
    promoted: bool = False

    @field_validator("artifact_id")
    @classmethod
    def _artifact_id(cls, value: str) -> str:
        if _CORE_ID.fullmatch(value) is None:
            raise ValueError("completion artifact ID is invalid")
        return value

    @field_validator(
        "core_payload_sha256",
        "payload_manifest_sha256",
        "actual_payload_validation_sha256",
        "projected_to_core_binding_sha256",
    )
    @classmethod
    def _digests(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("completion receipt digest is invalid")
        return value

    @model_validator(mode="after")
    def _payload(self) -> CoreTargetCompletionReceiptV1:
        encoded = self.artifact_content.encode("utf-8")
        if (
            len(encoded) != self.core_payload_utf8_bytes
            or sha256_bytes(encoded) != self.core_payload_sha256
            or self.planned_job.expected_core_payload_sha256 != self.core_payload_sha256
            or self.planned_job.expected_core_payload_utf8_bytes
            != self.core_payload_utf8_bytes
            or self.status
            != (
                "SUCCEEDED_VALIDATED_PROMOTED"
                if self.promoted
                else "SUCCEEDED_VALIDATED_UNPROMOTED"
            )
        ):
            raise ValueError("completion payload closure is invalid")
        return self

    @property
    def target_id(self) -> str:
        return self.planned_job.target_id

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.model_dump(mode="json")))

    def __repr__(self) -> str:
        return (
            "CoreTargetCompletionReceiptV1(<identities-and-content-redacted>; "
            f"target_id={self.target_id!r}, batch_index={self.planned_job.batch_index})"
        )

    __str__ = __repr__


PreparedCoreBatchV1.model_rebuild()


class CoreEvolutionStateV1(_FrozenModel):
    """Generation zero or one fully validated, three-target successor state."""

    schema_version: Literal["TemperatureCoreEvolutionStateV1"] = (
        "TemperatureCoreEvolutionStateV1"
    )
    protocol_id: Literal["chembench_temperature_full_evolve_v1"] = PROTOCOL_ID
    coordinator_id: Literal["temperature_three_target_planned_job_coordinator_v1"] = (
        CORE_COORDINATOR_ID
    )
    batch_index: int = Field(ge=0, le=_MAX_BATCH_INDEX)
    full_registry_digest: str
    predecessor_state_sha256: str | None
    committed_batch: PreparedCoreBatchV1 | None
    target_receipts: tuple[CoreTargetCompletionReceiptV1, ...]

    @field_validator("full_registry_digest", "predecessor_state_sha256")
    @classmethod
    def _digests(cls, value: str | None) -> str | None:
        if value is not None and _SHA256.fullmatch(value) is None:
            raise ValueError("state digest is invalid")
        return value

    @model_validator(mode="after")
    def _closed(self) -> CoreEvolutionStateV1:
        if self.batch_index == 0:
            if self.predecessor_state_sha256 is not None or self.committed_batch is not None:
                raise ValueError("generation-zero state contains successor data")
            if self.target_receipts:
                raise ValueError("generation-zero state contains target receipts")
            return self
        batch = self.committed_batch
        if (
            batch is None
            or not batch.complete
            or self.predecessor_state_sha256 is None
            or batch.batch_index != self.batch_index
            or batch.predecessor_state_sha256 != self.predecessor_state_sha256
            or batch.full_registry_digest != self.full_registry_digest
            or tuple(receipt.target_id for receipt in self.target_receipts)
            != tuple(TARGET_ORDER)
            or any(
                receipt.planned_job != batch.jobs[index]
                for index, receipt in enumerate(self.target_receipts)
            )
            or batch.validated_receipts != self.target_receipts
            or tuple(receipt.artifact_id for receipt in self.target_receipts)
            != batch.promoted_artifact_ids
            or any(not receipt.promoted for receipt in self.target_receipts)
            or batch.materialized_context is None
            or set(batch.materialized_context.selected_artifact_ids)
            != {receipt.artifact_id for receipt in self.target_receipts}
            or len({receipt.artifact_id for receipt in self.target_receipts})
            != len(TARGET_ORDER)
            or self.total_artifact_utf8_bytes > HARD_TOTAL_BYTES
        ):
            raise ValueError("successor state closure is invalid")
        return self

    @classmethod
    def generation_zero(cls, *, full_registry_digest: str) -> CoreEvolutionStateV1:
        return cls(
            batch_index=0,
            full_registry_digest=full_registry_digest,
            predecessor_state_sha256=None,
            committed_batch=None,
            target_receipts=(),
        )

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.model_dump(mode="json")))

    @property
    def total_artifact_utf8_bytes(self) -> int:
        return sum(receipt.core_payload_utf8_bytes for receipt in self.target_receipts)

    def artifact_id_for(self, target_id: str) -> str | None:
        return next(
            (
                receipt.artifact_id
                for receipt in self.target_receipts
                if receipt.target_id == target_id
            ),
            None,
        )

    def content_for(self, target_id: str) -> str:
        return next(
            (
                receipt.artifact_content
                for receipt in self.target_receipts
                if receipt.target_id == target_id
            ),
            "",
        )

    @property
    def materialized_context(self) -> CoreMaterializedContextBindingV1 | None:
        return None if self.committed_batch is None else self.committed_batch.materialized_context

    def to_private_checkpoint_bytes(self) -> bytes:
        body = self.model_dump(mode="json")
        return canonical_json_bytes(
            {
                "schema_version": _CHECKPOINT_STATE_SCHEMA,
                "state": body,
                "state_sha256": self.digest,
            }
        )

    @classmethod
    def from_private_checkpoint_bytes(cls, payload: bytes) -> CoreEvolutionStateV1:
        raw = _load_canonical_checkpoint(payload, _CHECKPOINT_STATE_SCHEMA)
        if set(raw) != {"schema_version", "state", "state_sha256"}:
            raise TemperatureCoreEvolutionError("CORE_STATE_CHECKPOINT_SCHEMA_INVALID")
        try:
            state = cls.model_validate(raw["state"])
        except (TypeError, ValueError) as exc:
            raise TemperatureCoreEvolutionError("CORE_STATE_CHECKPOINT_INVALID") from exc
        if raw["state_sha256"] != state.digest:
            raise TemperatureCoreEvolutionError("CORE_STATE_CHECKPOINT_DIGEST_MISMATCH")
        return state

    def to_public_receipt(self) -> dict[str, object]:
        batch = self.committed_batch
        return {
            "schema_version": "TemperatureCoreEvolutionStateReceiptV1",
            "protocol_id": self.protocol_id,
            "coordinator_id": self.coordinator_id,
            "batch_index": self.batch_index,
            "full_registry_digest": self.full_registry_digest,
            "predecessor_state_sha256": self.predecessor_state_sha256,
            "source_packet_sha256": None if batch is None else batch.source_packet_sha256,
            "reflector_receipt_sha256": (
                None if batch is None else batch.reflector_receipt_sha256
            ),
            "evidence_sha256": None if batch is None else batch.evidence_sha256,
            "projected_artifact_set_sha256": (
                None if batch is None else batch.projected_artifact_set_sha256
            ),
            "target_artifacts": [
                {
                    "target_id": receipt.target_id,
                    "method_id": receipt.planned_job.method_id,
                    "projected_artifact_sha256": (
                        receipt.planned_job.spec.projected_artifact.sha256
                    ),
                    "core_payload_sha256": receipt.core_payload_sha256,
                    "core_payload_utf8_bytes": receipt.core_payload_utf8_bytes,
                    "payload_manifest_sha256": receipt.payload_manifest_sha256,
                    "projected_to_core_binding_sha256": (
                        receipt.projected_to_core_binding_sha256
                    ),
                    "promoted": receipt.promoted,
                    "completion_receipt_sha256": receipt.digest,
                }
                for receipt in self.target_receipts
            ],
            "total_artifact_utf8_bytes": self.total_artifact_utf8_bytes,
            "hard_total_utf8_bytes": HARD_TOTAL_BYTES,
            "atomic_three_target_commit": self.batch_index > 0,
            "materialized_context": (
                None
                if batch is None or batch.materialized_context is None
                else {
                    "context_id": batch.materialized_context.context_id,
                    "request_sha256": batch.materialized_context.request_sha256,
                    "registry_sha256": batch.materialized_context.registry_sha256,
                    "materialization_sha256": (
                        batch.materialized_context.materialization_sha256
                    ),
                    "selected_artifact_ids_sha256": (
                        batch.materialized_context.selected_artifact_ids_sha256
                    ),
                    "selected_artifact_count": len(
                        batch.materialized_context.selected_artifact_ids
                    ),
                    "projection_sha256": batch.materialized_context.projection_sha256,
                    "instruction_sha256": batch.materialized_context.instruction_sha256,
                    "instruction_utf8_bytes": (
                        batch.materialized_context.instruction_utf8_bytes
                    ),
                    "environment_sha256": (
                        batch.materialized_context.environment_sha256
                    ),
                    "blob_inventory_sha256": (
                        batch.materialized_context.blob_inventory_sha256
                    ),
                    "target_context": [
                        {
                            "target_id": target.target_id,
                            "resolved_content_sha256": target.resolved_content_sha256,
                            "resolved_content_utf8_bytes": (
                                target.resolved_content_utf8_bytes
                            ),
                            "blob_inventory_sha256": target.blob_inventory_sha256,
                            "blob_count": len(target.blob_ids),
                        }
                        for target in batch.materialized_context.target_bindings
                    ],
                }
            ),
            "state_sha256": self.digest,
        }

    def __repr__(self) -> str:
        return f"CoreEvolutionStateV1(<private-state>; batch_index={self.batch_index})"

    __str__ = __repr__


class PlannedJobStorePort(Protocol):
    def create_plan_bound_job(
        self,
        request: PlanBoundJobCreateRequest,
        *,
        snapshot: Any,
    ) -> JobCreateResponse: ...

    def get_artifact(self, artifact_id: str) -> ArtifactResponse: ...

    def get_internal_job_result(self, job_id: str) -> Mapping[str, object]: ...

    def update_artifact_promotion(
        self,
        artifact_id: str,
        *,
        promoted: bool,
    ) -> ArtifactResponse: ...

    def resolve_materialized_context(
        self,
        request: ContextProjectionResolveRequest,
    ) -> MaterializedContext: ...

    def open_materialized_blob(self, context_id: str, blob_id: str) -> Any: ...

    def connect(self) -> Any: ...


class CoreBatchExecutionReceiptV1(_FrozenModel):
    """Content-free receipt for one exact three-job worker pass."""

    schema_version: Literal["TemperatureCoreBatchExecutionReceiptV1"] = (
        "TemperatureCoreBatchExecutionReceiptV1"
    )
    batch_index: int = Field(ge=1, le=_MAX_BATCH_INDEX)
    prepared_batch_sha256: str
    job_ids: tuple[str, str, str]
    newly_executed_job_count: int = Field(ge=0, le=3)
    already_succeeded_job_count: int = Field(ge=0, le=3)
    final_status: Literal["THREE_CORE_JOBS_SUCCEEDED"] = "THREE_CORE_JOBS_SUCCEEDED"

    @field_validator("prepared_batch_sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _SHA256.fullmatch(value) is None:
            raise ValueError("execution receipt digest is invalid")
        return value

    @field_validator("job_ids")
    @classmethod
    def _jobs(cls, values: tuple[str, str, str]) -> tuple[str, str, str]:
        if len(set(values)) != 3 or any(_CORE_ID.fullmatch(value) is None for value in values):
            raise ValueError("execution receipt job IDs are invalid")
        return values

    @model_validator(mode="after")
    def _counts(self) -> CoreBatchExecutionReceiptV1:
        if self.newly_executed_job_count + self.already_succeeded_job_count != 3:
            raise ValueError("execution receipt counts do not close three jobs")
        return self

    @property
    def digest(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.model_dump(mode="json")))

    def __repr__(self) -> str:
        return (
            "CoreBatchExecutionReceiptV1(<job-identities-redacted>; "
            f"batch_index={self.batch_index})"
        )

    __str__ = __repr__


class _StoreWorkerClientV1:
    """Transport-neutral adapter from a run-private Store to Core ``run_once``."""

    def __init__(self, store: Any) -> None:
        required = ("claim_job", "heartbeat_job", "complete_job", "fail_job")
        if any(not callable(getattr(store, name, None)) for name in required):
            raise TypeError("worker store does not expose the formal worker lifecycle")
        self._store = store

    def claim(
        self,
        worker_id: str,
        capabilities: list[str],
        *,
        lease_seconds: int | None = None,
        method_capabilities: list[str] | None = None,
        method_identity_capabilities: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        response = self._store.claim_job(
            WorkerClaimRequest(
                worker_id=worker_id,
                capabilities=capabilities,
                lease_seconds=lease_seconds or 600,
                method_capabilities=method_capabilities,
                method_identity_capabilities=method_identity_capabilities,
            )
        )
        return None if response.job is None else response.job.model_dump(mode="json")

    def heartbeat(
        self,
        job_id: str,
        lease_id: str,
        *,
        progress: float | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        return dict(
            self._store.heartbeat_job(
                job_id,
                WorkerHeartbeatRequest(
                    lease_id=lease_id,
                    progress=progress,
                    message=message,
                ),
            )
        )

    def complete(
        self,
        job_id: str,
        lease_id: str,
        artifacts: list[dict[str, Any]],
        *,
        report: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return dict(
            self._store.complete_job(
                job_id,
                WorkerCompleteRequest(
                    lease_id=lease_id,
                    artifacts=artifacts,
                    report=report or {},
                ),
            )
        )

    def fail(
        self,
        job_id: str,
        lease_id: str,
        error: str,
        *,
        retryable: bool = True,
    ) -> dict[str, Any]:
        return dict(
            self._store.fail_job(
                job_id,
                WorkerFailRequest(
                    lease_id=lease_id,
                    error=error,
                    retryable=retryable,
                ),
            )
        )


class TemperatureCoreEvolutionCoordinatorV1:
    """Create, observe, validate and atomically advance three Core targets."""

    def __init__(
        self,
        *,
        executable_registry: VerifiedExecutableRegistry,
        planned_job_port: PlannedJobStorePort,
        core_artifact_root: Path,
        initial_state: CoreEvolutionStateV1 | None = None,
        initial_prepared: PreparedCoreBatchV1 | None = None,
    ) -> None:
        self._registry = require_verified_executable_registry(executable_registry)
        for method_name in (
            "create_plan_bound_job",
            "get_artifact",
            "get_internal_job_result",
            "update_artifact_promotion",
            "resolve_materialized_context",
            "open_materialized_blob",
            "connect",
        ):
            if not callable(getattr(planned_job_port, method_name, None)):
                raise TypeError(f"planned_job_port must expose {method_name}")
        if not isinstance(core_artifact_root, Path) or not core_artifact_root.is_absolute():
            raise TypeError("core_artifact_root must be an absolute pathlib.Path")
        self._port = planned_job_port
        self._core_artifact_root = core_artifact_root
        self._forbidden_questions: tuple[str, ...] | None = None
        self._validate_registered_methods()
        genesis = CoreEvolutionStateV1.generation_zero(
            full_registry_digest=self._registry.snapshot.registry_digest
        )
        self._head = genesis if initial_state is None else initial_state
        self._prepared = initial_prepared
        if type(self._head) is not CoreEvolutionStateV1:
            raise TypeError("initial_state must be exact CoreEvolutionStateV1")
        if self._prepared is not None and type(self._prepared) is not PreparedCoreBatchV1:
            raise TypeError("initial_prepared must be exact PreparedCoreBatchV1")
        if self._head.full_registry_digest != self._registry.snapshot.registry_digest:
            raise TemperatureCoreEvolutionError("CORE_STATE_REGISTRY_MISMATCH")
        if self._prepared is not None and (
            self._prepared.batch_index != self._head.batch_index + 1
            or self._prepared.predecessor_state_sha256 != self._head.digest
            or self._prepared.full_registry_digest != self._head.full_registry_digest
        ):
            raise TemperatureCoreEvolutionError("CORE_PREPARED_CHECKPOINT_INVALID")

    @property
    def head(self) -> CoreEvolutionStateV1:
        return self._head

    @property
    def prepared(self) -> PreparedCoreBatchV1 | None:
        return self._prepared

    def prepare_batch(
        self,
        *,
        batch_index: int,
        predecessor: CoreEvolutionStateV1,
        source_packet_sha256: str,
        reflector_receipt_sha256: str,
        evidence: RuleEvidenceIndexV1,
        projected_artifacts: ProjectedArtifactSetV1,
        text_memory_dataset: CoreTextMemoryDatasetBindingV1,
        forbidden_questions: tuple[str, ...],
    ) -> PreparedCoreBatchV1:
        self._validate_prepare_inputs(
            batch_index=batch_index,
            predecessor=predecessor,
            source_packet_sha256=source_packet_sha256,
            reflector_receipt_sha256=reflector_receipt_sha256,
            evidence=evidence,
            projected_artifacts=projected_artifacts,
            text_memory_dataset=text_memory_dataset,
        )
        forbidden_digest = _forbidden_questions_sha256(forbidden_questions)
        self._verify_text_memory_dataset(
            text_memory_dataset,
            projected_memory=projected_artifacts.memory,
        )
        projected = tuple(
            CoreProjectedArtifactV1.from_projection(item)
            for item in projected_artifacts.artifacts
        )
        specs = tuple(
            self._build_job_spec(
                batch_index=batch_index,
                predecessor=predecessor,
                source_packet_sha256=source_packet_sha256,
                reflector_receipt_sha256=reflector_receipt_sha256,
                evidence_sha256=evidence.digest,
                projected_artifact_set_sha256=projected_artifacts.digest,
                dataset=text_memory_dataset,
                projected=item,
            )
            for item in projected
        )
        request_identity = {
            "batch_index": batch_index,
            "predecessor_state_sha256": predecessor.digest,
            "source_packet_sha256": source_packet_sha256,
            "reflector_receipt_sha256": reflector_receipt_sha256,
            "evidence_sha256": evidence.digest,
            "projected_artifact_set_sha256": projected_artifacts.digest,
            "text_memory_dataset_binding_sha256": text_memory_dataset.digest,
            "forbidden_questions_sha256": forbidden_digest,
            "full_registry_digest": self._registry.snapshot.registry_digest,
            "job_request_sha256": [
                sha256_bytes(canonical_json_bytes(spec.request.model_dump(mode="json")))
                for spec in specs
            ],
        }
        request_sha256 = sha256_bytes(canonical_json_bytes(request_identity))
        if self._prepared is None:
            self._prepared = PreparedCoreBatchV1(
                batch_index=batch_index,
                predecessor_state_sha256=predecessor.digest,
                source_packet_sha256=source_packet_sha256,
                reflector_receipt_sha256=reflector_receipt_sha256,
                evidence_sha256=evidence.digest,
                projected_artifact_set_sha256=projected_artifacts.digest,
                projected_total_utf8_bytes=projected_artifacts.total_utf8_bytes,
                forbidden_questions_sha256=forbidden_digest,
                full_registry_digest=self._registry.snapshot.registry_digest,
                request_sha256=request_sha256,
                text_memory_dataset=text_memory_dataset,
                projected_artifacts=projected,
                job_specs=specs,
                jobs=(),
            )
        elif (
            self._prepared.request_sha256 != request_sha256
            or self._prepared.job_specs != specs
            or self._prepared.text_memory_dataset != text_memory_dataset
        ):
            raise TemperatureCoreEvolutionError("CORE_DIFFERENT_BATCH_ALREADY_PREPARED")

        # Reissue every already-created request as an authoritative idempotency
        # check, then continue any missing suffix.  The prefix is persisted in
        # memory immediately after each successful side effect, so a failure on
        # job 2/3 does not erase job 1/2 from the private checkpoint.
        for index, spec in enumerate(specs):
            existing = self._prepared.jobs[index] if index < len(self._prepared.jobs) else None
            try:
                created = self._port.create_plan_bound_job(
                    spec.request,
                    snapshot=self._registry.snapshot,
                )
            except Exception as exc:
                raise TemperatureCoreEvolutionError("CORE_PLANNED_JOB_CREATE_FAILED") from exc
            if type(created) is not JobCreateResponse or created.state not in {
                JobState.PENDING,
                JobState.CLAIMED,
                JobState.RUNNING,
                JobState.SUCCEEDED,
            }:
                raise TemperatureCoreEvolutionError("CORE_PLANNED_JOB_CREATE_INVALID")
            _require_core_id(created.job_id, "CORE_PLANNED_JOB_ID_INVALID")
            planned = self._created_job(spec, created.job_id, text_memory_dataset)
            if existing is not None and existing != planned:
                raise TemperatureCoreEvolutionError("CORE_PLANNED_JOB_IDEMPOTENCY_MISMATCH")
            if existing is None:
                self._prepared = PreparedCoreBatchV1.model_validate(
                    {
                        **self._prepared.model_dump(mode="python", exclude={"jobs"}),
                        "jobs": (*self._prepared.jobs, planned),
                    }
                )
            if (
                planned.expected_core_payload_utf8_bytes
                > TARGET_MAX_BYTES[planned.target_id]
            ):
                raise TemperatureCoreEvolutionError(
                    "CORE_EXPECTED_TARGET_PAYLOAD_TOO_LARGE"
                )
        return self._prepared

    def execute_prepared_jobs(
        self,
        *,
        prepared: PreparedCoreBatchV1,
        worker_id_prefix: str = "temperature-full-evolve-core-worker",
        lease_seconds: int = 600,
    ) -> CoreBatchExecutionReceiptV1:
        """Run exactly the prepared three jobs through Core's verified worker path.

        A restart skips jobs already closed as succeeded and executes only the
        still-pending suffix.  Claimed/running or failed jobs are never silently
        replaced; those states fail closed for the orchestration layer to recover
        through the existing lease/checkpoint policy.
        """

        if (
            type(prepared) is not PreparedCoreBatchV1
            or self._prepared is None
            or prepared.digest != self._prepared.digest
            or not prepared.complete
            or type(worker_id_prefix) is not str
            or not worker_id_prefix.strip()
            or isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 30 <= lease_seconds <= 86_400
        ):
            raise TemperatureCoreEvolutionError("CORE_EXECUTION_PREPARED_BATCH_INVALID")
        client = _StoreWorkerClientV1(self._port)
        newly_executed = 0
        already_succeeded = 0
        for job in prepared.jobs:
            before = self._port.get_internal_job_result(job.job_id)
            state = before.get("state") if isinstance(before, Mapping) else None
            if state == str(JobState.SUCCEEDED):
                already_succeeded += 1
                continue
            if state != str(JobState.PENDING):
                raise TemperatureCoreEvolutionError("CORE_JOB_NOT_RECOVERABLY_PENDING")
            claimed = run_once(
                client,
                worker_id=f"{worker_id_prefix}-{prepared.batch_index}-{job.target_id}",
                capabilities=[job.request.job_type],
                artifact_root=self._core_artifact_root,
                lease_seconds=lease_seconds,
                executable_registry=self._registry,
            )
            if not claimed:
                raise TemperatureCoreEvolutionError("CORE_JOB_NOT_CLAIMABLE")
            after = self._port.get_internal_job_result(job.job_id)
            if (
                not isinstance(after, Mapping)
                or after.get("state") != str(JobState.SUCCEEDED)
                or after.get("error") is not None
            ):
                raise TemperatureCoreEvolutionError("CORE_JOB_EXECUTION_FAILED")
            newly_executed += 1
        return CoreBatchExecutionReceiptV1(
            batch_index=prepared.batch_index,
            prepared_batch_sha256=prepared.digest,
            job_ids=tuple(job.job_id for job in prepared.jobs),
            newly_executed_job_count=newly_executed,
            already_succeeded_job_count=already_succeeded,
        )

    def commit_completed_batch(
        self,
        *,
        prepared: PreparedCoreBatchV1,
        forbidden_questions: tuple[str, ...],
    ) -> CoreEvolutionStateV1:
        if type(prepared) is not PreparedCoreBatchV1:
            raise TypeError("prepared must be exact PreparedCoreBatchV1")
        if (
            self._prepared is None
            or prepared.request_sha256 != self._prepared.request_sha256
            or prepared.jobs != self._prepared.jobs
            or prepared.predecessor_state_sha256 != self._head.digest
            or not prepared.complete
            or _forbidden_questions_sha256(forbidden_questions)
            != prepared.forbidden_questions_sha256
        ):
            raise TemperatureCoreEvolutionError("CORE_PREPARED_BATCH_IDENTITY_INVALID")
        self._forbidden_questions = forbidden_questions
        working = self._prepared
        observed = self._observe_and_validate_outputs(
            working,
            forbidden_questions=forbidden_questions,
        )
        if working.validated_receipts and not _same_receipts_except_promotion(
            working.validated_receipts,
            observed,
        ):
            raise TemperatureCoreEvolutionError("CORE_VALIDATED_OUTPUT_IDENTITY_DRIFT")
        promoted_ids = _promoted_prefix(observed)
        if working.promoted_artifact_ids != promoted_ids[: len(working.promoted_artifact_ids)]:
            raise TemperatureCoreEvolutionError("CORE_PROMOTION_CHECKPOINT_DRIFT")
        self._prepared = _prepared_with_progress(
            working,
            receipts=observed,
            promoted_artifact_ids=promoted_ids,
            materialized_context=working.materialized_context,
        )

        # Promotion is intentionally sequential and checkpoint-visible.  An
        # interrupted second/third update leaves a strict prefix that recovery
        # reopens from Store; no artifact or job is rolled back or copied.
        while len(self._prepared.promoted_artifact_ids) < len(TARGET_ORDER):
            index = len(self._prepared.promoted_artifact_ids)
            receipt = self._prepared.validated_receipts[index]
            try:
                promoted = self._port.update_artifact_promotion(
                    receipt.artifact_id,
                    promoted=True,
                )
            except Exception as exc:
                raise TemperatureCoreEvolutionError("CORE_ARTIFACT_PROMOTION_FAILED") from exc
            if (
                type(promoted) is not ArtifactResponse
                or promoted.artifact_id != receipt.artifact_id
                or promoted.type is not ArtifactType(receipt.target_id)
                or promoted.state is not ArtifactState.ACTIVE
                or not promoted.promoted
            ):
                raise TemperatureCoreEvolutionError("CORE_ARTIFACT_PROMOTION_INVALID")
            refreshed = self._observe_and_validate_outputs(
                self._prepared,
                forbidden_questions=forbidden_questions,
            )
            authoritative_prefix = _promoted_prefix(refreshed)
            if len(authoritative_prefix) != index + 1:
                raise TemperatureCoreEvolutionError("CORE_ARTIFACT_PROMOTION_ORDER_INVALID")
            self._prepared = _prepared_with_progress(
                self._prepared,
                receipts=refreshed,
                promoted_artifact_ids=authoritative_prefix,
                materialized_context=None,
            )

        context_request = self._context_request(self._prepared)
        if self._prepared.materialized_context is None:
            materialized = self._find_or_resolve_materialized_context(
                context_request,
                self._prepared.validated_receipts,
            )
            binding = self._validate_materialized_context(
                materialized.context_id,
                request=context_request,
                receipts=self._prepared.validated_receipts,
            )
            self._prepared = _prepared_with_progress(
                self._prepared,
                receipts=self._prepared.validated_receipts,
                promoted_artifact_ids=self._prepared.promoted_artifact_ids,
                materialized_context=binding,
            )
        else:
            rebound = self._validate_materialized_context(
                self._prepared.materialized_context.context_id,
                request=context_request,
                receipts=self._prepared.validated_receipts,
            )
            if rebound != self._prepared.materialized_context:
                raise TemperatureCoreEvolutionError("CORE_MATERIALIZED_CONTEXT_DRIFT")

        committed = self._prepared
        successor = CoreEvolutionStateV1(
            batch_index=committed.batch_index,
            full_registry_digest=committed.full_registry_digest,
            predecessor_state_sha256=committed.predecessor_state_sha256,
            committed_batch=committed,
            target_receipts=committed.validated_receipts,
        )
        self._head = successor
        self._prepared = None
        return successor

    def issue_runtime_context(self) -> CoreResolvedSupervisedContextV2 | None:
        """Issue only an issuer-sealed context after authoritative Store replay."""

        if self._head.batch_index == 0:
            return None
        receipts, binding = self._verify_committed_head_authoritative()
        by_target = {receipt.target_id: receipt for receipt in receipts}
        return CoreResolvedSupervisedContextV2(
            memory=_issue_core_resolved_text_memory_v2(
                core_artifact_id=by_target["text_memory"].artifact_id,
                artifact_payload_sha256=by_target["text_memory"].core_payload_sha256,
                context_resolution_digest=binding.materialization_sha256,
                resolved_memory_sha256=binding.target(
                    "text_memory"
                ).resolved_content_sha256,
                markdown=binding.target("text_memory").resolved_content,
            ),
            skill=_issue_core_resolved_auxiliary_v2(
                target_id="skill_bundle",
                core_artifact_id=by_target["skill_bundle"].artifact_id,
                artifact_payload_sha256=by_target["skill_bundle"].core_payload_sha256,
                context_resolution_digest=binding.materialization_sha256,
                resolved_content_sha256=binding.target(
                    "skill_bundle"
                ).resolved_content_sha256,
                markdown=binding.target("skill_bundle").resolved_content,
            ),
            agent_system=_issue_core_resolved_auxiliary_v2(
                target_id="agent_system",
                core_artifact_id=by_target["agent_system"].artifact_id,
                artifact_payload_sha256=by_target["agent_system"].core_payload_sha256,
                context_resolution_digest=binding.materialization_sha256,
                resolved_content_sha256=binding.target(
                    "agent_system"
                ).resolved_content_sha256,
                markdown=binding.target("agent_system").resolved_content,
            ),
        )

    def to_private_checkpoint_bytes(self) -> bytes:
        body = {
            "head": self._head.model_dump(mode="json"),
            "prepared": (
                None if self._prepared is None else self._prepared.model_dump(mode="json")
            ),
        }
        return canonical_json_bytes(
            {
                "schema_version": _CHECKPOINT_COORDINATOR_SCHEMA,
                "checkpoint": body,
                "checkpoint_sha256": sha256_bytes(canonical_json_bytes(body)),
            }
        )

    @classmethod
    def recover_from_private_checkpoint_bytes(
        cls,
        payload: bytes,
        *,
        executable_registry: VerifiedExecutableRegistry,
        planned_job_port: PlannedJobStorePort,
        core_artifact_root: Path,
        forbidden_questions: tuple[str, ...],
    ) -> TemperatureCoreEvolutionCoordinatorV1:
        raw = _load_canonical_checkpoint(payload, _CHECKPOINT_COORDINATOR_SCHEMA)
        if set(raw) != {"schema_version", "checkpoint", "checkpoint_sha256"}:
            raise TemperatureCoreEvolutionError("CORE_COORDINATOR_CHECKPOINT_SCHEMA_INVALID")
        body = raw["checkpoint"]
        if type(body) is not dict or set(body) != {"head", "prepared"}:
            raise TemperatureCoreEvolutionError("CORE_COORDINATOR_CHECKPOINT_SCHEMA_INVALID")
        if raw["checkpoint_sha256"] != sha256_bytes(canonical_json_bytes(body)):
            raise TemperatureCoreEvolutionError("CORE_COORDINATOR_CHECKPOINT_DIGEST_MISMATCH")
        try:
            head = CoreEvolutionStateV1.model_validate(body["head"])
            prepared = (
                None
                if body["prepared"] is None
                else PreparedCoreBatchV1.model_validate(body["prepared"])
            )
        except (TypeError, ValueError) as exc:
            raise TemperatureCoreEvolutionError("CORE_COORDINATOR_CHECKPOINT_INVALID") from exc
        coordinator = cls(
            executable_registry=executable_registry,
            planned_job_port=planned_job_port,
            core_artifact_root=core_artifact_root,
            initial_state=head,
            initial_prepared=prepared,
        )
        coordinator._forbidden_questions = forbidden_questions
        coordinator._recover_authoritative_state(forbidden_questions=forbidden_questions)
        return coordinator

    def _recover_authoritative_state(self, *, forbidden_questions: tuple[str, ...]) -> None:
        forbidden_digest = _forbidden_questions_sha256(forbidden_questions)
        if self._head.batch_index:
            committed = self._head.committed_batch
            if committed is None or committed.forbidden_questions_sha256 != forbidden_digest:
                raise TemperatureCoreEvolutionError("CORE_RECOVERY_FORBIDDEN_SET_MISMATCH")
            self._verify_text_memory_dataset(
                committed.text_memory_dataset,
                projected_memory=_projected_for(committed, "text_memory"),
            )
            self._revalidate_created_jobs(committed)
            observed = self._observe_and_validate_outputs(
                committed,
                forbidden_questions=forbidden_questions,
            )
            if observed != self._head.target_receipts or not all(
                receipt.promoted for receipt in observed
            ):
                raise TemperatureCoreEvolutionError("CORE_RECOVERY_OUTPUT_MISMATCH")
            self._verify_committed_head_authoritative()
        if self._prepared is not None:
            if self._prepared.forbidden_questions_sha256 != forbidden_digest:
                raise TemperatureCoreEvolutionError("CORE_RECOVERY_FORBIDDEN_SET_MISMATCH")
            self._verify_text_memory_dataset(
                self._prepared.text_memory_dataset,
                projected_memory=_projected_for(self._prepared, "text_memory"),
            )
            self._revalidate_created_jobs(self._prepared)
            if self._prepared.complete:
                states = []
                for job in self._prepared.jobs:
                    try:
                        result = self._port.get_internal_job_result(job.job_id)
                    except Exception as exc:
                        raise TemperatureCoreEvolutionError(
                            "CORE_RECOVERY_JOB_OBSERVATION_FAILED"
                        ) from exc
                    state = result.get("state") if isinstance(result, Mapping) else None
                    if state not in {str(JobState.PENDING), str(JobState.SUCCEEDED)}:
                        raise TemperatureCoreEvolutionError(
                            "CORE_RECOVERY_JOB_STATE_NOT_RESUMABLE"
                        )
                    states.append(state)
                if not all(state == str(JobState.SUCCEEDED) for state in states):
                    if (
                        self._prepared.validated_receipts
                        or self._prepared.promoted_artifact_ids
                        or self._prepared.materialized_context is not None
                    ):
                        raise TemperatureCoreEvolutionError(
                            "CORE_RECOVERY_PROGRESS_BEFORE_JOBS_SUCCEEDED"
                        )
                    return
                observed = self._observe_and_validate_outputs(
                    self._prepared,
                    forbidden_questions=forbidden_questions,
                )
                if self._prepared.validated_receipts and not _same_receipts_except_promotion(
                    self._prepared.validated_receipts,
                    observed,
                ):
                    raise TemperatureCoreEvolutionError(
                        "CORE_RECOVERY_PREPARED_OUTPUT_MISMATCH"
                    )
                promoted_ids = _promoted_prefix(observed)
                if self._prepared.promoted_artifact_ids != promoted_ids[
                    : len(self._prepared.promoted_artifact_ids)
                ]:
                    raise TemperatureCoreEvolutionError(
                        "CORE_RECOVERY_PROMOTION_CHECKPOINT_DRIFT"
                    )
                binding = self._prepared.materialized_context
                if binding is not None:
                    request = self._context_request(self._prepared)
                    rebound = self._validate_materialized_context(
                        binding.context_id,
                        request=request,
                        receipts=observed,
                    )
                    if rebound != binding:
                        raise TemperatureCoreEvolutionError(
                            "CORE_RECOVERY_MATERIALIZED_CONTEXT_DRIFT"
                        )
                elif len(promoted_ids) == len(TARGET_ORDER):
                    promoted_prepared = _prepared_with_progress(
                        self._prepared,
                        receipts=observed,
                        promoted_artifact_ids=promoted_ids,
                        materialized_context=None,
                    )
                    request = self._context_request(promoted_prepared)
                    existing = self._find_existing_materialized_context(
                        request,
                        observed,
                    )
                    if existing is not None:
                        binding = self._validate_materialized_context(
                            existing.context_id,
                            request=request,
                            receipts=observed,
                        )
                self._prepared = _prepared_with_progress(
                    self._prepared,
                    receipts=observed,
                    promoted_artifact_ids=promoted_ids,
                    materialized_context=binding,
                )

    def _verify_committed_head_authoritative(
        self,
    ) -> tuple[
        tuple[CoreTargetCompletionReceiptV1, ...],
        CoreMaterializedContextBindingV1,
    ]:
        committed = self._head.committed_batch
        if committed is None or committed.materialized_context is None:
            raise TemperatureCoreEvolutionError("CORE_COMMITTED_CONTEXT_MISSING")
        if self._forbidden_questions is None:
            raise TemperatureCoreEvolutionError("CORE_FORBIDDEN_SET_REQUIRED_FOR_ISSUANCE")
        observed = self._observe_and_validate_outputs(
            committed,
            forbidden_questions=self._forbidden_questions,
        )
        if observed != self._head.target_receipts or not all(
            receipt.promoted for receipt in observed
        ):
            raise TemperatureCoreEvolutionError("CORE_COMMITTED_OUTPUT_DRIFT")
        request = self._context_request(committed)
        rebound = self._validate_materialized_context(
            committed.materialized_context.context_id,
            request=request,
            receipts=observed,
        )
        if rebound != committed.materialized_context:
            raise TemperatureCoreEvolutionError("CORE_COMMITTED_CONTEXT_DRIFT")
        return observed, rebound

    def _revalidate_created_jobs(self, prepared: PreparedCoreBatchV1) -> None:
        for job in prepared.jobs:
            try:
                created = self._port.create_plan_bound_job(
                    job.request,
                    snapshot=self._registry.snapshot,
                )
            except Exception as exc:
                raise TemperatureCoreEvolutionError("CORE_RECOVERY_JOB_REVALIDATION_FAILED") from exc
            if type(created) is not JobCreateResponse or created.job_id != job.job_id:
                raise TemperatureCoreEvolutionError("CORE_RECOVERY_JOB_ID_MISMATCH")

    def _context_request(
        self,
        prepared: PreparedCoreBatchV1,
    ) -> ContextProjectionResolveRequest:
        if not prepared.validated_receipts or len(prepared.promoted_artifact_ids) != len(
            TARGET_ORDER
        ):
            raise TemperatureCoreEvolutionError("CORE_CONTEXT_INPUTS_NOT_PROMOTED")
        by_target = {
            receipt.target_id: receipt.artifact_id
            for receipt in prepared.validated_receipts
        }
        requested_ids = tuple(by_target[target] for target in TARGET_ORDER)
        return ContextProjectionResolveRequest(
            task_id=(
                f"temperature-full-evolve-v1-batch-{prepared.batch_index}-"
                f"{prepared.request_sha256[:24]}"
            ),
            instruction=_CONTEXT_INSTRUCTION,
            agent={"harness": "codex"},
            base_model="gpt-5.5",
            policy_version=PROTOCOL_ID,
            metadata={
                "task_tags": [PROTOCOL_ID, "temperature-full-evolve-v1"],
                "evolution": {"context_artifact_ids": list(requested_ids)},
            },
            execution_profile=EvolutionExecutionProfile(
                execution_mode="self_deployed",
                capture_mode="transcript",
                harness_id="codex",
            ),
            destination_roots=_CONTEXT_DESTINATIONS,
            target_limits={
                target: TargetConsumptionLimits(
                    max_artifacts=1,
                    max_text_chars=TARGET_MAX_BYTES[target],
                    max_text_bytes=TARGET_MAX_BYTES[target],
                    max_payload_bytes=(
                        TARGET_MAX_BYTES[target] * 2
                        if target == "agent_system"
                        else TARGET_MAX_BYTES[target]
                    ),
                    max_adapters=0,
                )
                for target in TARGET_ORDER
            },
        )

    def _find_or_resolve_materialized_context(
        self,
        request: ContextProjectionResolveRequest,
        receipts: tuple[CoreTargetCompletionReceiptV1, ...],
    ) -> MaterializedContext:
        existing = self._find_existing_materialized_context(request, receipts)
        if existing is not None:
            return existing
        try:
            created = self._port.resolve_materialized_context(request)
        except Exception as exc:
            # A crash/error after Core's atomic persistence may leave a fully
            # committed context.  Rediscover exactly this request before
            # classifying the operation as failed.
            try:
                recovered = self._find_existing_materialized_context(request, receipts)
            except Exception as recovery_exc:
                raise TemperatureCoreEvolutionError(
                    "CORE_CONTEXT_RESOLUTION_RECOVERY_FAILED"
                ) from recovery_exc
            if recovered is None:
                raise TemperatureCoreEvolutionError("CORE_CONTEXT_RESOLUTION_FAILED") from exc
            return recovered
        if type(created) is not MaterializedContext:
            raise TemperatureCoreEvolutionError("CORE_CONTEXT_RESOLUTION_INVALID")
        return created

    def _find_existing_materialized_context(
        self,
        request: ContextProjectionResolveRequest,
        receipts: tuple[CoreTargetCompletionReceiptV1, ...],
    ) -> MaterializedContext | None:
        request_sha256 = canonical_digest(request)
        try:
            with self._port.connect() as connection:
                rows = connection.execute(
                    "SELECT cm.context_id, cm.manifest_json, c.request_json, "
                    "c.response_json, c.selected_artifact_ids_json "
                    "FROM context_materializations AS cm "
                    "JOIN contexts AS c USING(context_id) "
                    "WHERE cm.request_digest = ? ORDER BY cm.context_id",
                    (request_sha256,),
                ).fetchall()
        except Exception as exc:
            raise TemperatureCoreEvolutionError("CORE_CONTEXT_DISCOVERY_FAILED") from exc
        if not rows:
            return None
        candidates: list[MaterializedContext] = []
        for row in rows:
            try:
                persisted_request = ContextProjectionResolveRequest.model_validate_json(
                    str(row["request_json"])
                )
                materialized = MaterializedContext.model_validate_json(
                    str(row["manifest_json"])
                )
                response = MaterializedContext.model_validate_json(str(row["response_json"]))
                selected = json.loads(str(row["selected_artifact_ids_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise TemperatureCoreEvolutionError("CORE_CONTEXT_DISCOVERY_TAMPERED") from exc
            if persisted_request != request:
                raise TemperatureCoreEvolutionError("CORE_CONTEXT_REQUEST_DIGEST_COLLISION")
            if (
                response != materialized
                or materialized.context_id != row["context_id"]
                or selected != list(materialized.selection.artifact_ids)
            ):
                raise TemperatureCoreEvolutionError("CORE_CONTEXT_DISCOVERY_TAMPERED")
            # This also opens every DB-authorized blob.  A matching but damaged
            # context is never ignored in favor of creating a replacement.
            self._validate_materialized_context(
                materialized.context_id,
                request=request,
                receipts=receipts,
            )
            candidates.append(materialized)
        if len(candidates) != 1:
            raise TemperatureCoreEvolutionError("CORE_CONTEXT_REQUEST_NOT_UNIQUE")
        return candidates[0]

    def _validate_materialized_context(
        self,
        context_id: str,
        *,
        request: ContextProjectionResolveRequest,
        receipts: tuple[CoreTargetCompletionReceiptV1, ...],
    ) -> CoreMaterializedContextBindingV1:
        if tuple(receipt.target_id for receipt in receipts) != tuple(TARGET_ORDER) or any(
            not receipt.promoted for receipt in receipts
        ):
            raise TemperatureCoreEvolutionError("CORE_CONTEXT_RECEIPTS_INVALID")
        try:
            with self._port.connect() as connection:
                row = connection.execute(
                    "SELECT cm.manifest_json, cm.registry_digest, cm.request_digest, "
                    "c.request_json, c.response_json, c.selected_artifact_ids_json "
                    "FROM context_materializations AS cm "
                    "JOIN contexts AS c USING(context_id) WHERE cm.context_id = ?",
                    (context_id,),
                ).fetchone()
        except Exception as exc:
            raise TemperatureCoreEvolutionError("CORE_CONTEXT_AUTHORITATIVE_READ_FAILED") from exc
        if row is None:
            raise TemperatureCoreEvolutionError("CORE_CONTEXT_NOT_FOUND")
        try:
            persisted_request = ContextProjectionResolveRequest.model_validate_json(
                str(row["request_json"])
            )
            materialized = MaterializedContext.model_validate_json(str(row["manifest_json"]))
            response = MaterializedContext.model_validate_json(str(row["response_json"]))
            selected_ids = json.loads(str(row["selected_artifact_ids_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TemperatureCoreEvolutionError("CORE_CONTEXT_AUTHORITATIVE_ROW_INVALID") from exc

        by_target = {receipt.target_id: receipt for receipt in receipts}
        expected_selected = tuple(
            by_target[target].artifact_id for target in TARGET_ORDER
        )
        if (
            persisted_request != request
            or response != materialized
            or materialized.context_id != context_id
            or materialized.request_digest != canonical_digest(request)
            or row["request_digest"] != materialized.request_digest
            or materialized.registry_digest != self._registry.snapshot.registry_digest
            or row["registry_digest"] != materialized.registry_digest
            or selected_ids != list(materialized.selection.artifact_ids)
            or len(selected_ids) != len(expected_selected)
            or set(selected_ids) != set(expected_selected)
            or materialized.selection.skipped_artifacts
            or materialized.base_model != "gpt-5.5"
            or tuple(projection.target_id for projection in materialized.projections)
            != _CONTEXT_TARGET_ORDER
            or any(
                projection.artifact_ids != (by_target[projection.target_id].artifact_id,)
                for projection in materialized.projections
            )
            or materialized.adapter_merge_spec.adapters
            or materialized.adapter_merge_spec.merge_mode != "reference_only"
        ):
            raise TemperatureCoreEvolutionError("CORE_CONTEXT_IDENTITY_INVALID")

        expected_environment = {
            "OPENEVO_AGENT_SYSTEM_FILE": "/openevo/session/evolution/agent_system.md",
            "OPENEVO_AGENT_SYSTEM_TARGET": "/workspace/repository/AGENTS.md",
            "OPENEVO_AGENT_SYSTEM_TARGETS": '["/workspace/repository/AGENTS.md"]',
            "OPENEVO_AGENTS_MD": "/workspace/repository/AGENTS.md",
            "OPENEVO_MEMORY_FILE": "/openevo/session/evolution/memory.md",
            "OPENEVO_SKILLS_DIR": "/openevo/session/evolution/skills",
        }
        actual_environment = {item.name: item.value for item in materialized.environment}
        expected_instruction = (
            "Use the following evolved agent system instructions for this task:\n"
            f"{by_target['agent_system'].artifact_content.strip()}\n\n"
            "Use the following long-term memory for this task:\n"
            f"{by_target['text_memory'].artifact_content.strip()}"
        )
        if (
            actual_environment != expected_environment
            or len(actual_environment) != len(materialized.environment)
            or materialized.instruction != expected_instruction
        ):
            raise TemperatureCoreEvolutionError("CORE_CONTEXT_INJECTION_INVALID")

        expected_destinations = {
            "text_memory": {("target_data", "memory.md")},
            "skill_bundle": {
                (
                    "harness_skills",
                    f"{by_target['skill_bundle'].artifact_id}/SKILL.md",
                )
            },
            "agent_system": {
                ("target_data", "agent_system.md"),
                ("harness_instruction", "AGENTS.md"),
            },
        }
        blobs_by_target: dict[str, list[object]] = {target: [] for target in TARGET_ORDER}
        for blob in materialized.blobs:
            if blob.target_id not in blobs_by_target:
                raise TemperatureCoreEvolutionError("CORE_CONTEXT_BLOB_TARGET_INVALID")
            blobs_by_target[blob.target_id].append(blob)
        target_bindings: list[CoreMaterializedTargetBindingV1] = []
        for target_id in TARGET_ORDER:
            receipt = by_target[target_id]
            blobs = blobs_by_target[target_id]
            destinations = {
                (blob.destination_scope.value, blob.destination_relative_path)
                for blob in blobs
            }
            if destinations != expected_destinations[target_id]:
                raise TemperatureCoreEvolutionError("CORE_CONTEXT_BLOB_DESTINATION_INVALID")
            encoded = receipt.artifact_content.encode("utf-8")
            for blob in blobs:
                if (
                    blob.source_artifact_ids != (receipt.artifact_id,)
                    or blob.size_bytes != len(encoded)
                    or blob.sha256 != receipt.core_payload_sha256
                ):
                    raise TemperatureCoreEvolutionError("CORE_CONTEXT_BLOB_BINDING_INVALID")
                try:
                    with self._port.open_materialized_blob(
                        materialized.context_id,
                        blob.blob_id,
                    ) as lease:
                        actual = lease.stream.read(HARD_TOTAL_BYTES + 1)
                        if lease.stream.read(1):
                            raise ValueError("materialized blob exceeds bounded read")
                except Exception as exc:
                    raise TemperatureCoreEvolutionError("CORE_CONTEXT_BLOB_READ_FAILED") from exc
                if actual != encoded:
                    raise TemperatureCoreEvolutionError("CORE_CONTEXT_BLOB_BYTES_MISMATCH")
            blob_payload = [blob.model_dump(mode="json") for blob in blobs]
            target_bindings.append(
                CoreMaterializedTargetBindingV1(
                    target_id=target_id,
                    artifact_id=receipt.artifact_id,
                    artifact_payload_sha256=receipt.core_payload_sha256,
                    artifact_payload_utf8_bytes=receipt.core_payload_utf8_bytes,
                    resolved_content=receipt.artifact_content,
                    resolved_content_sha256=receipt.core_payload_sha256,
                    resolved_content_utf8_bytes=receipt.core_payload_utf8_bytes,
                    blob_ids=tuple(blob.blob_id for blob in blobs),
                    blob_inventory_sha256=sha256_bytes(
                        canonical_json_bytes(blob_payload)
                    ),
                )
            )
        if sum(len(values) for values in blobs_by_target.values()) != len(
            materialized.blobs
        ):
            raise TemperatureCoreEvolutionError("CORE_CONTEXT_BLOB_SET_INVALID")
        instruction_bytes = materialized.instruction.encode("utf-8")
        return CoreMaterializedContextBindingV1(
            context_id=materialized.context_id,
            request_sha256=materialized.request_digest,
            registry_sha256=materialized.registry_digest,
            materialization_sha256=canonical_digest(materialized),
            selected_artifact_ids=materialized.selection.artifact_ids,
            selected_artifact_ids_sha256=sha256_bytes(
                canonical_json_bytes(list(materialized.selection.artifact_ids))
            ),
            projection_sha256=sha256_bytes(
                canonical_json_bytes(
                    [item.model_dump(mode="json") for item in materialized.projections]
                )
            ),
            instruction_sha256=sha256_bytes(instruction_bytes),
            instruction_utf8_bytes=len(instruction_bytes),
            environment_sha256=sha256_bytes(
                canonical_json_bytes(
                    [item.model_dump(mode="json") for item in materialized.environment]
                )
            ),
            blob_inventory_sha256=sha256_bytes(
                canonical_json_bytes(
                    [item.model_dump(mode="json") for item in materialized.blobs]
                )
            ),
            target_bindings=tuple(target_bindings),
        )

    def _validate_prepare_inputs(
        self,
        *,
        batch_index: int,
        predecessor: CoreEvolutionStateV1,
        source_packet_sha256: str,
        reflector_receipt_sha256: str,
        evidence: RuleEvidenceIndexV1,
        projected_artifacts: ProjectedArtifactSetV1,
        text_memory_dataset: CoreTextMemoryDatasetBindingV1,
    ) -> None:
        if type(predecessor) is not CoreEvolutionStateV1:
            raise TypeError("predecessor must be exact CoreEvolutionStateV1")
        if type(evidence) is not RuleEvidenceIndexV1:
            raise TypeError("evidence must be exact RuleEvidenceIndexV1")
        if type(projected_artifacts) is not ProjectedArtifactSetV1:
            raise TypeError("projected_artifacts must be exact ProjectedArtifactSetV1")
        if type(text_memory_dataset) is not CoreTextMemoryDatasetBindingV1:
            raise TypeError("text_memory_dataset must be exact binding DTO")
        if (
            isinstance(batch_index, bool)
            or not isinstance(batch_index, int)
            or not 1 <= batch_index <= _MAX_BATCH_INDEX
            or predecessor != self._head
            or predecessor.batch_index != batch_index - 1
        ):
            raise TemperatureCoreEvolutionError("CORE_BATCH_PREDECESSOR_INVALID")
        _require_sha256(source_packet_sha256, "CORE_SOURCE_PACKET_DIGEST_INVALID")
        _require_sha256(reflector_receipt_sha256, "CORE_REFLECTOR_RECEIPT_DIGEST_INVALID")
        if (
            evidence.batch_index != batch_index
            or projected_artifacts.batch_index != batch_index
            or projected_artifacts.evidence_sha256 != evidence.digest
            or text_memory_dataset.batch_index != batch_index
            or text_memory_dataset.source_packet_sha256 != source_packet_sha256
            or text_memory_dataset.reflector_receipt_sha256 != reflector_receipt_sha256
            or text_memory_dataset.evidence_sha256 != evidence.digest
            or text_memory_dataset.projected_artifact_set_sha256
            != projected_artifacts.digest
            or text_memory_dataset.projected_memory_sha256
            != projected_artifacts.memory.sha256
            or text_memory_dataset.projected_memory_utf8_bytes
            != projected_artifacts.memory.utf8_byte_count
        ):
            raise TemperatureCoreEvolutionError("CORE_BATCH_EVIDENCE_BINDING_INVALID")

    def _build_job_spec(
        self,
        *,
        batch_index: int,
        predecessor: CoreEvolutionStateV1,
        source_packet_sha256: str,
        reflector_receipt_sha256: str,
        evidence_sha256: str,
        projected_artifact_set_sha256: str,
        dataset: CoreTextMemoryDatasetBindingV1,
        projected: CoreProjectedArtifactV1,
    ) -> CoreTargetJobSpecV1:
        target_id = projected.target_id
        method_id = TARGET_METHODS[target_id]
        predecessor_artifact_id = predecessor.artifact_id_for(target_id)
        projection_validation_receipt_sha256 = sha256_bytes(
            canonical_json_bytes(
                {
                    "schema_version": "TemperatureProjectionValidationReceiptV1",
                    "validator_id": "temperature_projected_artifact_validator_v1",
                    "status": "PASS",
                    "batch_index": batch_index,
                    "target_id": target_id,
                    "source_packet_sha256": source_packet_sha256,
                    "evidence_sha256": evidence_sha256,
                    "projected_artifact_set_sha256": projected_artifact_set_sha256,
                    "projected_artifact_sha256": projected.sha256,
                    "projected_artifact_utf8_bytes": projected.utf8_bytes,
                }
            )
        )
        if target_id == "text_memory":
            selection_config: dict[str, object] = {}
        elif target_id == "skill_bundle":
            selection_config = {"skill_markdown": projected.content}
        else:
            selection_config = {
                "agent_system_markdown": projected.content,
                "target_path": "AGENTS.md",
            }
        selection = EvolutionTargetSelection(
            target_id=target_id,
            enabled=True,
            method_id=method_id,
            config=selection_config,
        )
        identity = canonical_digest(
            {
                "protocol_id": PROTOCOL_ID,
                "coordinator_id": CORE_COORDINATOR_ID,
                "batch_index": batch_index,
                "target_id": target_id,
                "method_id": method_id,
                "predecessor_state_sha256": predecessor.digest,
                "predecessor_artifact_id": predecessor_artifact_id,
                "source_packet_sha256": source_packet_sha256,
                "reflector_receipt_sha256": reflector_receipt_sha256,
                "evidence_sha256": evidence_sha256,
                "projected_artifact_set_sha256": projected_artifact_set_sha256,
                "dataset_binding_sha256": dataset.digest,
                "projected_artifact_sha256": projected.sha256,
                "projected_artifact_utf8_bytes": projected.utf8_bytes,
                "projection_validation_receipt_sha256": (
                    projection_validation_receipt_sha256
                ),
                "full_registry_digest": self._registry.snapshot.registry_digest,
            }
        )
        plan = self._registry.snapshot.compile_plan(
            plan_id=f"temperature-batch-{batch_index}-{target_id}-{identity[:24]}",
            selections=(selection,),
            profile=EvolutionExecutionProfile(
                execution_mode="self_deployed",
                capture_mode="transcript",
                harness_id="codex",
            ),
        )
        descriptor = self._registry.snapshot.methods[method_id]
        binding_ids = tuple(binding.binding_id for binding in descriptor.input_bindings)
        expected_bindings = (
            ("current_dataset",)
            if target_id == "text_memory"
            else ("current_dataset", "prior_target_artifacts")
        )
        if binding_ids != expected_bindings:
            raise TemperatureCoreEvolutionError("CORE_REGISTERED_METHOD_BINDINGS_INVALID")
        lineage = {
            "schema_version": "TemperatureCoreTargetLineageV1",
            "protocol_id": PROTOCOL_ID,
            "coordinator_id": CORE_COORDINATOR_ID,
            "batch_index": batch_index,
            "target_id": target_id,
            "method_id": method_id,
            "predecessor_state_sha256": predecessor.digest,
            "predecessor_artifact_id": predecessor_artifact_id,
            "source_packet_sha256": source_packet_sha256,
            "reflector_receipt_sha256": reflector_receipt_sha256,
            "evidence_sha256": evidence_sha256,
            "projected_artifact_set_sha256": projected_artifact_set_sha256,
            "dataset_binding_sha256": dataset.digest,
            "projected_artifact_sha256": projected.sha256,
            "projected_artifact_utf8_bytes": projected.utf8_bytes,
            "projection_validation_receipt_sha256": projection_validation_receipt_sha256,
        }
        core_config: dict[str, object] = {
            "name": f"Temperature batch {batch_index} {target_id}",
            "promoted": False,
            "lineage": lineage,
            "compatibility": {
                "agent_harness": ["codex"],
                "base_model": ["gpt-5.5"],
                "task_tags": [PROTOCOL_ID, "temperature-full-evolve-v1"],
            },
            "tags": [PROTOCOL_ID, CORE_COORDINATOR_ID, f"batch-{batch_index}"],
        }
        bindings = [
            PlannedInputBinding(
                binding_id="current_dataset",
                artifact_ids=(dataset.artifact_id,),
            )
        ]
        if target_id != "text_memory":
            bindings.append(
                PlannedInputBinding(
                    binding_id="prior_target_artifacts",
                    artifact_ids=(
                        ()
                        if predecessor_artifact_id is None
                        else (predecessor_artifact_id,)
                    ),
                )
            )
        request = PlanBoundJobCreateRequest(
            plan=plan,
            target_id=target_id,
            job_type=(
                "chembench.temperature_full_evolve_v1."
                f"batch-{batch_index}.{target_id}.{identity[:24]}"
            ),
            input_bindings=tuple(bindings),
            core_config=core_config,
        )
        return CoreTargetJobSpecV1(
            batch_index=batch_index,
            target_id=target_id,
            method_id=method_id,
            request=request,
            plan_digest=canonical_digest(plan),
            registry_snapshot_digest=plan.registry_snapshot_digest,
            full_registry_digest=self._registry.snapshot.registry_digest,
            method_identity_digest=plan.selections[0].method_identity_digest,
            predecessor_state_sha256=predecessor.digest,
            predecessor_artifact_id=predecessor_artifact_id,
            source_packet_sha256=source_packet_sha256,
            reflector_receipt_sha256=reflector_receipt_sha256,
            evidence_sha256=evidence_sha256,
            projected_artifact_set_sha256=projected_artifact_set_sha256,
            dataset_binding_sha256=dataset.digest,
            projected_artifact=projected,
            projection_validation_receipt_sha256=(
                projection_validation_receipt_sha256
            ),
        )

    def _created_job(
        self,
        spec: CoreTargetJobSpecV1,
        job_id: str,
        dataset: CoreTextMemoryDatasetBindingV1,
    ) -> CoreTargetPlannedJobV1:
        if spec.target_id == "text_memory":
            content = _expected_core_text_memory(spec, job_id, dataset)
        else:
            content = spec.projected_artifact.content
        encoded = content.encode("utf-8")
        if len(encoded) > HARD_TOTAL_BYTES:
            raise TemperatureCoreEvolutionError("CORE_EXPECTED_TARGET_PAYLOAD_TOO_LARGE")
        return CoreTargetPlannedJobV1(
            spec=spec,
            job_id=job_id,
            expected_core_payload_sha256=sha256_bytes(encoded),
            expected_core_payload_utf8_bytes=len(encoded),
        )

    def _verify_text_memory_dataset(
        self,
        binding: CoreTextMemoryDatasetBindingV1,
        *,
        projected_memory: CoreProjectedArtifactV1 | ProjectedArtifactV1,
    ) -> None:
        try:
            artifact = self._port.get_artifact(binding.artifact_id)
        except Exception as exc:
            raise TemperatureCoreEvolutionError("CORE_TEXT_MEMORY_DATASET_NOT_FOUND") from exc
        if (
            type(artifact) is not ArtifactResponse
            or artifact.type is not ArtifactType.DATASET
            or artifact.state is not ArtifactState.ACTIVE
            or artifact.promoted
            or artifact.name != binding.artifact_name
            or artifact.uri != binding.manifest_uri
        ):
            raise TemperatureCoreEvolutionError("CORE_TEXT_MEMORY_DATASET_RECORD_INVALID")
        expected_records = _expected_dataset_record_bytes(binding, projected_memory.content)
        expected_manifest = _expected_dataset_manifest_bytes(binding)
        if (
            len(expected_records) != binding.records_utf8_bytes
            or sha256_bytes(expected_records) != binding.records_sha256
            or len(expected_manifest) != binding.manifest_utf8_bytes
            or sha256_bytes(expected_manifest) != binding.manifest_sha256
        ):
            raise TemperatureCoreEvolutionError("CORE_TEXT_MEMORY_DATASET_BINDING_INVALID")
        actual_manifest = self._read_verified_uri_file(
            artifact_id=f"{binding.artifact_id}:manifest",
            artifact_type="dataset",
            name=binding.artifact_name,
            uri=binding.manifest_uri,
            relative_path="manifest.json",
            max_bytes=64 * 1024,
        )
        actual_records = self._read_verified_uri_file(
            artifact_id=f"{binding.artifact_id}:records",
            artifact_type="dataset",
            name=binding.artifact_name,
            uri=binding.records_uri,
            relative_path="records.jsonl",
            max_bytes=64 * 1024,
        )
        if actual_manifest != expected_manifest or actual_records != expected_records:
            raise TemperatureCoreEvolutionError("CORE_TEXT_MEMORY_DATASET_BYTES_MISMATCH")

    def _read_verified_uri_file(
        self,
        *,
        artifact_id: str,
        artifact_type: str,
        name: str,
        uri: str,
        relative_path: str,
        max_bytes: int,
    ) -> bytes:
        try:
            with ArtifactPayloadService(self._core_artifact_root) as payloads:
                snapshot = payloads.issue_snapshot(
                    artifact_id=artifact_id,
                    artifact_type=artifact_type,
                    name=name,
                    uri=uri,
                    manifest={"content_path": relative_path},
                    scores={},
                    rank_index=0,
                )
                if (
                    len(snapshot.payload_entries) != 1
                    or snapshot.payload_entries[0].relative_path != relative_path
                    or snapshot.payload_entries[0].size_bytes > max_bytes
                ):
                    raise ValueError("verified file inventory is invalid")
                text = payloads.read_utf8_prefix(
                    snapshot.payload_handle,
                    relative_path,
                    max_chars=max_bytes,
                    max_bytes=max_bytes,
                )
        except Exception as exc:
            raise TemperatureCoreEvolutionError("CORE_VERIFIED_PAYLOAD_READ_FAILED") from exc
        encoded = text.encode("utf-8")
        if len(encoded) != snapshot.payload_entries[0].size_bytes:
            raise TemperatureCoreEvolutionError("CORE_VERIFIED_PAYLOAD_TRUNCATED")
        return encoded

    def _observe_and_validate_outputs(
        self,
        prepared: PreparedCoreBatchV1,
        *,
        forbidden_questions: tuple[str, ...],
    ) -> tuple[CoreTargetCompletionReceiptV1, ...]:
        self._revalidate_created_jobs(prepared)
        observations: list[tuple[CoreTargetPlannedJobV1, str, str, str]] = []
        promotion_by_artifact: dict[str, bool] = {}
        # Tuple entries are planned job, artifact ID, payload manifest digest,
        # and exact UTF-8 content.
        for rank, job in enumerate(prepared.jobs):
            try:
                result = self._port.get_internal_job_result(job.job_id)
            except Exception as exc:
                raise TemperatureCoreEvolutionError("CORE_JOB_OBSERVATION_FAILED") from exc
            if (
                not isinstance(result, Mapping)
                or result.get("job_id") != job.job_id
                or result.get("state") != str(JobState.SUCCEEDED)
                or result.get("error") is not None
                or type(result.get("artifact_ids")) is not list
                or len(result["artifact_ids"]) != 1
                or type(result.get("outputs")) is not list
                or len(result["outputs"]) != 1
            ):
                raise TemperatureCoreEvolutionError("CORE_JOB_NOT_CLOSED_SUCCEEDED")
            output = result["outputs"][0]
            if not isinstance(output, Mapping):
                raise TemperatureCoreEvolutionError("CORE_JOB_OUTPUT_RECORD_INVALID")
            artifact_id = result["artifact_ids"][0]
            if type(artifact_id) is not str or output.get("artifact_id") != artifact_id:
                raise TemperatureCoreEvolutionError("CORE_JOB_OUTPUT_IDENTITY_INVALID")
            try:
                artifact = self._port.get_artifact(artifact_id)
            except Exception as exc:
                raise TemperatureCoreEvolutionError("CORE_JOB_OUTPUT_ARTIFACT_MISSING") from exc
            expected_type = ArtifactType(job.target_id)
            if (
                type(artifact) is not ArtifactResponse
                or artifact.type is not expected_type
                or artifact.state is not ArtifactState.ACTIVE
                or output.get("type") != job.target_id
                or output.get("name") != artifact.name
                or output.get("manifest") != artifact.manifest
                or output.get("promoted") is not artifact.promoted
                or output.get("payload_file_count") != 1
                or output.get("payload_byte_size")
                != job.expected_core_payload_utf8_bytes
            ):
                raise TemperatureCoreEvolutionError("CORE_JOB_OUTPUT_RECORD_INVALID")
            _validate_execution_lineage(output.get("lineage"), job)
            content_path = _core_content_path(job.target_id, artifact.manifest)
            try:
                with ArtifactPayloadService(self._core_artifact_root) as payloads:
                    snapshot = payloads.issue_snapshot(
                        artifact_id=artifact.artifact_id,
                        artifact_type=job.target_id,
                        name=artifact.name,
                        uri=artifact.uri,
                        manifest=artifact.manifest,
                        scores=artifact.scores,
                        rank_index=rank,
                    )
                    if (
                        len(snapshot.payload_entries) != 1
                        or snapshot.payload_entries[0].relative_path != content_path
                        or snapshot.payload_entries[0].size_bytes
                        != job.expected_core_payload_utf8_bytes
                        or snapshot.payload_manifest_digest
                        != output.get("payload_manifest_digest")
                    ):
                        raise ValueError("Core payload inventory mismatch")
                    content = payloads.read_utf8_prefix(
                        snapshot.payload_handle,
                        content_path,
                        max_chars=HARD_TOTAL_BYTES,
                        max_bytes=HARD_TOTAL_BYTES,
                    )
            except Exception as exc:
                raise TemperatureCoreEvolutionError("CORE_JOB_OUTPUT_PAYLOAD_INVALID") from exc
            encoded = content.encode("utf-8")
            if (
                len(encoded) != job.expected_core_payload_utf8_bytes
                or sha256_bytes(encoded) != job.expected_core_payload_sha256
            ):
                raise TemperatureCoreEvolutionError("CORE_JOB_OUTPUT_PAYLOAD_MISMATCH")
            observations.append(
                (
                    job,
                    artifact_id,
                    str(output["payload_manifest_digest"]),
                    content,
                )
            )
            promotion_by_artifact[artifact_id] = artifact.promoted

        validation_sha256 = _validate_actual_payload_set(
            observations,
            forbidden_questions=forbidden_questions,
        )
        receipts = []
        for job, artifact_id, payload_manifest_sha256, content in observations:
            payload_sha256 = sha256_bytes(content.encode("utf-8"))
            relation = (
                "core_text_memory_fixed_framing_v1"
                if job.target_id == "text_memory"
                else "byte_identical_manual_materializer_v1"
            )
            binding_sha256 = sha256_bytes(
                canonical_json_bytes(
                    {
                        "schema_version": "TemperatureProjectedToCorePayloadBindingV1",
                        "target_id": job.target_id,
                        "relation": relation,
                        "projected_artifact_sha256": (
                            job.spec.projected_artifact.sha256
                        ),
                        "projected_artifact_utf8_bytes": (
                            job.spec.projected_artifact.utf8_bytes
                        ),
                        "core_payload_sha256": payload_sha256,
                        "core_payload_utf8_bytes": len(content.encode("utf-8")),
                        "actual_payload_validation_sha256": validation_sha256,
                    }
                )
            )
            receipts.append(
                CoreTargetCompletionReceiptV1(
                    status=(
                        "SUCCEEDED_VALIDATED_PROMOTED"
                        if promotion_by_artifact[artifact_id]
                        else "SUCCEEDED_VALIDATED_UNPROMOTED"
                    ),
                    planned_job=job,
                    artifact_id=artifact_id,
                    artifact_content=content,
                    core_payload_sha256=payload_sha256,
                    core_payload_utf8_bytes=len(content.encode("utf-8")),
                    payload_manifest_sha256=payload_manifest_sha256,
                    actual_payload_validation_sha256=validation_sha256,
                    projected_to_core_binding_sha256=binding_sha256,
                    promoted=promotion_by_artifact[artifact_id],
                )
            )
        return tuple(receipts)

    def _validate_registered_methods(self) -> None:
        snapshot = self._registry.snapshot
        expected_bindings = {
            "text_memory": ("current_dataset",),
            "skill_bundle": ("current_dataset", "prior_target_artifacts"),
            "agent_system": ("current_dataset", "prior_target_artifacts"),
        }
        for target_id, method_id in TARGET_METHODS.items():
            descriptor = snapshot.methods.get(method_id)
            if descriptor is None or method_id not in self._registry.method_handles:
                raise TemperatureCoreEvolutionError("CORE_REGISTERED_METHOD_MISSING")
            if (
                descriptor.target_id != target_id
                or descriptor.output_artifact_types != (target_id,)
                or tuple(binding.binding_id for binding in descriptor.input_bindings)
                != expected_bindings[target_id]
                or snapshot.identity_digest_for(DescriptorKind.METHOD, method_id)
                != snapshot.identity_digests[f"method:{method_id}"]
            ):
                raise TemperatureCoreEvolutionError("CORE_REGISTERED_METHOD_INVALID")


def _promoted_prefix(
    receipts: tuple[CoreTargetCompletionReceiptV1, ...],
) -> tuple[str, ...]:
    promoted: list[str] = []
    encountered_unpromoted = False
    for receipt in receipts:
        if receipt.promoted:
            if encountered_unpromoted:
                raise TemperatureCoreEvolutionError("CORE_PROMOTION_NOT_CANONICAL_PREFIX")
            promoted.append(receipt.artifact_id)
        else:
            encountered_unpromoted = True
    return tuple(promoted)


def _same_receipts_except_promotion(
    left: tuple[CoreTargetCompletionReceiptV1, ...],
    right: tuple[CoreTargetCompletionReceiptV1, ...],
) -> bool:
    def immutable_payload(receipt: CoreTargetCompletionReceiptV1) -> dict[str, object]:
        return receipt.model_dump(mode="json", exclude={"status", "promoted"})

    return len(left) == len(right) and all(
        immutable_payload(first) == immutable_payload(second)
        for first, second in zip(left, right, strict=True)
    )


def _prepared_with_progress(
    prepared: PreparedCoreBatchV1,
    *,
    receipts: tuple[CoreTargetCompletionReceiptV1, ...],
    promoted_artifact_ids: tuple[str, ...],
    materialized_context: CoreMaterializedContextBindingV1 | None,
) -> PreparedCoreBatchV1:
    return PreparedCoreBatchV1.model_validate(
        {
            **prepared.model_dump(
                mode="python",
                exclude={
                    "validated_receipts",
                    "promoted_artifact_ids",
                    "materialized_context",
                },
            ),
            "validated_receipts": receipts,
            "promoted_artifact_ids": promoted_artifact_ids,
            "materialized_context": materialized_context,
        }
    )


def _expected_dataset_record_bytes(
    binding: CoreTextMemoryDatasetBindingV1,
    projected_content: str,
) -> bytes:
    return canonical_json_bytes(
        {
            "schema_version": TEXT_MEMORY_RECORD_SCHEMA,
            "task_id": f"temperature-projected-memory-batch-{binding.batch_index}",
            "session_id": f"temperature-reflector-batch-{binding.batch_index}",
            "status": "accepted_projected_memory",
            "reward": None,
            "payload": {"summary": projected_content},
        }
    )


def _expected_dataset_manifest_bytes(binding: CoreTextMemoryDatasetBindingV1) -> bytes:
    return canonical_json_bytes(
        {
            "schema_version": TEXT_MEMORY_DATASET_SCHEMA,
            "name": binding.artifact_name,
            "records_uri": binding.records_uri,
            "record_count": 1,
            "privacy": "run_private",
            "batch_index": binding.batch_index,
            "source_packet_sha256": binding.source_packet_sha256,
            "reflector_receipt_sha256": binding.reflector_receipt_sha256,
            "evidence_sha256": binding.evidence_sha256,
            "projected_artifact_set_sha256": binding.projected_artifact_set_sha256,
            "projected_memory_sha256": binding.projected_memory_sha256,
            "projected_memory_utf8_bytes": binding.projected_memory_utf8_bytes,
            "records_sha256": binding.records_sha256,
        }
    )


def _expected_core_text_memory(
    spec: CoreTargetJobSpecV1,
    job_id: str,
    dataset: CoreTextMemoryDatasetBindingV1,
) -> str:
    summary = spec.projected_artifact.content.strip()
    return "\n".join(
        [
            f"# Memory from {dataset.artifact_name}",
            "",
            f"- job_id: {job_id}",
            f"- dataset_artifact_id: {dataset.artifact_id}",
            f"- dataset_name: {dataset.artifact_name}",
            "- record_count: 1",
            "",
            "## Records",
            (
                f"- task=temperature-projected-memory-batch-{dataset.batch_index} "
                f"session=temperature-reflector-batch-{dataset.batch_index} "
                "status=accepted_projected_memory reward=None"
            ),
            f"  - summary: {summary}",
            "",
        ]
    )


def _projected_for(
    prepared: PreparedCoreBatchV1,
    target_id: str,
) -> CoreProjectedArtifactV1:
    return next(item for item in prepared.projected_artifacts if item.target_id == target_id)


def _core_content_path(target_id: str, manifest: Mapping[str, object]) -> str:
    if target_id == "skill_bundle":
        value = manifest.get("entrypoint")
        expected = "SKILL.md"
    elif target_id == "agent_system":
        value = manifest.get("content_path")
        expected = "AGENTS.md"
    else:
        value = manifest.get("content_path")
        expected = "memory.md"
    if value != expected:
        raise TemperatureCoreEvolutionError("CORE_JOB_OUTPUT_CONTENT_PATH_INVALID")
    return expected


def _validate_execution_lineage(
    lineage: object,
    job: CoreTargetPlannedJobV1,
) -> None:
    if not isinstance(lineage, Mapping):
        raise TemperatureCoreEvolutionError("CORE_JOB_OUTPUT_LINEAGE_INVALID")
    execution = lineage.get("openevo_execution")
    if not isinstance(execution, Mapping):
        raise TemperatureCoreEvolutionError("CORE_JOB_OUTPUT_LINEAGE_INVALID")
    expected = {
        "job_id": job.job_id,
        "plan_id": job.request.plan.plan_id,
        "plan_digest": job.spec.plan_digest,
        "target_id": job.target_id,
        "method_id": job.method_id,
        "method_identity_digest": job.spec.method_identity_digest,
        "registry_snapshot_digest": job.spec.registry_snapshot_digest,
        "declared_output_artifact_types": [job.target_id],
    }
    if any(execution.get(key) != value for key, value in expected.items()):
        raise TemperatureCoreEvolutionError("CORE_JOB_OUTPUT_LINEAGE_INVALID")
    input_bindings = execution.get("input_bindings")
    if not isinstance(input_bindings, list):
        raise TemperatureCoreEvolutionError("CORE_JOB_OUTPUT_LINEAGE_INVALID")
    expected_ids = {
        binding.binding_id: list(binding.artifact_ids)
        for binding in job.request.input_bindings
    }
    actual_ids: dict[str, object] = {}
    for binding in input_bindings:
        if not isinstance(binding, Mapping) or type(binding.get("binding_id")) is not str:
            raise TemperatureCoreEvolutionError("CORE_JOB_OUTPUT_LINEAGE_INVALID")
        actual_ids[str(binding["binding_id"])] = binding.get("artifact_ids")
    if actual_ids != expected_ids:
        raise TemperatureCoreEvolutionError("CORE_JOB_OUTPUT_LINEAGE_INVALID")


def _validate_actual_payload_set(
    observations: list[tuple[CoreTargetPlannedJobV1, str, str, str]],
    *,
    forbidden_questions: tuple[str, ...],
) -> str:
    if tuple(job.target_id for job, *_ in observations) != tuple(TARGET_ORDER):
        raise TemperatureCoreEvolutionError("CORE_ACTUAL_PAYLOAD_TARGET_SET_INVALID")
    total = sum(len(content.encode("utf-8")) for *_, content in observations)
    if total > TARGET_TOTAL_BYTES or total > HARD_TOTAL_BYTES:
        raise TemperatureCoreEvolutionError("CORE_ACTUAL_PAYLOAD_HARD_BUDGET_EXCEEDED")
    normalized_questions = tuple(
        value
        for question in forbidden_questions
        if len(value := normalize_benchmark_text(question)) >= 24
    )
    line_owners: dict[str, str] = {}
    role_markers = {
        "text_memory": "# Temperature Prediction Memory",
        "skill_bundle": "# Temperature Prediction Skill",
        "agent_system": "# Temperature Prediction Agent System",
    }
    payload_summary = []
    for job, _artifact_id, payload_manifest, content in observations:
        encoded = content.encode("utf-8")
        if len(encoded) > TARGET_MAX_BYTES[job.target_id] or len(encoded) > HARD_TOTAL_BYTES:
            raise TemperatureCoreEvolutionError(
                "CORE_ACTUAL_PAYLOAD_TARGET_BUDGET_EXCEEDED"
            )
        if (
            not content.endswith("\n")
            or "\x00" in content
            or _UID.search(content)
            or _ANSWER_MAP.search(content)
            or _STANDALONE_CHOICE.search(content)
        ):
            raise TemperatureCoreEvolutionError("CORE_ACTUAL_PAYLOAD_LEAKAGE")
        normalized = normalize_benchmark_text(content)
        if any(question in normalized for question in normalized_questions):
            raise TemperatureCoreEvolutionError("CORE_ACTUAL_PAYLOAD_QUESTION_LEAKAGE")
        semantic = job.spec.projected_artifact.content
        if role_markers[job.target_id] not in semantic:
            raise TemperatureCoreEvolutionError("CORE_ACTUAL_PAYLOAD_ROLE_INVALID")
        if job.target_id != "text_memory" and content != semantic:
            raise TemperatureCoreEvolutionError("CORE_MANUAL_PAYLOAD_PROJECTION_MISMATCH")
        for line in semantic.splitlines():
            candidate = line.removeprefix("- ")
            candidate = _RULE_LINE_PREFIX.sub("", candidate)
            candidate = normalize_benchmark_text(candidate)
            if len(candidate) < 24 or candidate.startswith("no validated"):
                continue
            owner = line_owners.setdefault(candidate, job.target_id)
            if owner != job.target_id:
                raise TemperatureCoreEvolutionError("CORE_ACTUAL_PAYLOAD_CROSS_TARGET_DUPLICATION")
        payload_summary.append(
            {
                "target_id": job.target_id,
                "projected_artifact_sha256": job.spec.projected_artifact.sha256,
                "core_payload_sha256": sha256_bytes(encoded),
                "core_payload_utf8_bytes": len(encoded),
                "payload_manifest_sha256": payload_manifest,
            }
        )
    return sha256_bytes(
        canonical_json_bytes(
            {
                "schema_version": "TemperatureActualCorePayloadSetValidationReceiptV1",
                "validator_id": TARGET_VALIDATOR_ID,
                "status": "PASS",
                "forbidden_questions_sha256": _forbidden_questions_sha256(
                    forbidden_questions
                ),
                "hard_total_utf8_bytes": HARD_TOTAL_BYTES,
                "target_total_utf8_bytes": TARGET_TOTAL_BYTES,
                "actual_total_utf8_bytes": total,
                "payloads": payload_summary,
            }
        )
    )


def _load_canonical_checkpoint(payload: bytes, schema: str) -> dict[str, object]:
    if type(payload) is not bytes or not payload or len(payload) > 2 * 1024 * 1024:
        raise TemperatureCoreEvolutionError("CORE_CHECKPOINT_BYTES_INVALID")
    try:
        decoded = payload.decode("utf-8")
        raw = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TemperatureCoreEvolutionError("CORE_CHECKPOINT_JSON_INVALID") from exc
    if type(raw) is not dict or raw.get("schema_version") != schema:
        raise TemperatureCoreEvolutionError("CORE_CHECKPOINT_SCHEMA_INVALID")
    try:
        canonical = canonical_json_bytes(raw)
    except (TypeError, ValueError) as exc:
        raise TemperatureCoreEvolutionError("CORE_CHECKPOINT_CANONICALIZATION_FAILED") from exc
    if canonical != payload:
        raise TemperatureCoreEvolutionError("CORE_CHECKPOINT_NOT_CANONICAL")
    return raw


__all__ = [
    "CORE_COORDINATOR_ID",
    "PROTOCOL_ID",
    "TARGET_METHODS",
    "TARGET_VALIDATOR_ID",
    "TEXT_MEMORY_DATASET_SCHEMA",
    "TEXT_MEMORY_RECORD_SCHEMA",
    "CoreBatchExecutionReceiptV1",
    "CoreEvolutionStateV1",
    "CoreMaterializedContextBindingV1",
    "CoreMaterializedTargetBindingV1",
    "CoreProjectedArtifactV1",
    "CoreTargetCompletionReceiptV1",
    "CoreTargetJobSpecV1",
    "CoreTargetPlannedJobV1",
    "CoreTextMemoryDatasetBindingV1",
    "PlannedJobStorePort",
    "PreparedCoreBatchV1",
    "RenderedCoreTextMemoryDatasetV1",
    "TemperatureCoreEvolutionCoordinatorV1",
    "TemperatureCoreEvolutionError",
    "render_core_text_memory_dataset_v1",
]
