"""OpenEvo Core lifecycle adapter for ChemBench4K frozen text memory.

This module is deliberately independent from the legacy ChemBench
``SafeEvolutionReflector``/``CandidateArtifact`` path.  It translates private
development trajectories into Core events and a Core dataset, creates one
immutable plan-bound job for the verified built-in text-memory method, executes
that job through the Core worker dispatcher, and resolves an approved artifact
through the verified Core target handler.

The adapter does not make model calls by itself.  Executing the default
``text_memory_expel_reflector`` method may make a Codex reflector call according
to the immutable job configuration supplied here; callers decide when to run
that worker.  Unit tests replace only the reflector transport, while retaining
the real verified method handle and all Core job/artifact/context contracts.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openevo import __version__ as openevo_version
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
    WorkerClaimRequest,
    WorkerCompleteRequest,
    WorkerFailRequest,
    WorkerHeartbeatRequest,
)
from openevo.evolution.planned_jobs import (
    PlanBoundJobCreateRequest,
    PlannedInputBinding,
)
from openevo.evolution.store import EvolutionStore
from openevo.evolution.time import utc_now_iso
from openevo.evolution.worker import run_once

from openevo_chembench.chembench4k_evaluation import (
    PrivateChemBench4KEvaluation,
)
from openevo_chembench.chembench4k_prompt import DevLeaveOneOutTask

if TYPE_CHECKING:
    from openevo_chembench.frozen_runtime_v2 import CoreResolvedTextMemoryV2
    from openevo_chembench.reflector_execution_boundary_v2 import (
        ReflectorExecutionBoundaryV2,
    )


PROTOCOL_ID = "chembench4k_frozen_generalization_v2"
DATASET_REPOSITORY = "AI4Chem/ChemBench4K"
DATASET_REVISION = "f8ad41a980170f4c5d0cc97e57722d06887c8f53"
METHOD_ID = "text_memory_expel_reflector"
METHOD_TARGET_ID = "text_memory"
METHOD_REGISTRY_SOURCE = "openevo.evolution.framework.builtins"
MODEL = "gpt-5.5"
EXPECTED_DEV_TRAJECTORIES = 45
DATASET_EVENT_TYPE = "openevo.session_completed"
DATASET_EVENT_SOURCE = "chembench4k.dev_loo.v2"
DATASET_PURPOSE = "chembench4k_dev_loo_text_memory_v2"
JOB_TYPE_PREFIX = "chembench4k.text_memory.v2"
MAX_MEMORY_BYTES = 64 * 1024
MAX_CORE_MANIFEST_BYTES = 1024 * 1024
_PRIVATE_REFLECTOR_INPUT_DIRECTORY = "private_reflector_inputs"

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_REVISION_RE = re.compile(r"[0-9a-f]{40}\Z", re.ASCII)
_TAXONOMY_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z", re.ASCII)


class CoreEvolutionV2Error(RuntimeError):
    """Fail-closed error without benchmark task or target content."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class CoreDevTrajectoryV2(_FrozenModel):
    """Private dev-only record projected into a Core dataset artifact.

    The prompt itself is intentionally represented only by a digest.  Raw
    completion and private evaluation labels remain in the private Core dataset,
    while text-memory output must pass a separate artifact validator before it
    can be promoted or resolved.
    """

    uid: str
    category: str = Field(min_length=1, max_length=128)
    source_split: Literal["dev"] = "dev"
    source_index: int = Field(ge=0)
    public_prompt_sha256: str
    raw_completion: str = Field(max_length=1_048_576)
    parsed_prediction: str | None
    target: Literal["A", "B", "C", "D"]
    correct: bool
    private_error_taxonomy: tuple[str, ...] = Field(default=(), max_length=32)
    dataset_revision: str = DATASET_REVISION
    dataset_sha256: str

    def __repr__(self) -> str:
        return "CoreDevTrajectoryV2(<private-dev-record>)"

    __str__ = __repr__

    @field_validator("uid", "public_prompt_sha256", "dataset_sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("digest must be a lowercase SHA-256")
        return value

    @field_validator("dataset_revision")
    @classmethod
    def _revision(cls, value: str) -> str:
        if _REVISION_RE.fullmatch(value) is None:
            raise ValueError("dataset revision must be a lowercase commit hash")
        if value != DATASET_REVISION:
            raise ValueError("dataset revision is not the frozen ChemBench4K revision")
        return value

    @field_validator("category")
    @classmethod
    def _category(cls, value: str) -> str:
        if value != value.strip() or any(ord(character) < 32 for character in value):
            raise ValueError("category must be normalized bounded text")
        return value

    @field_validator("parsed_prediction")
    @classmethod
    def _official_prediction(cls, value: str | None) -> str | None:
        if value not in {None, ""} and (len(value) != 1 or not value.isupper()):
            raise ValueError("official prediction must be empty or one uppercase character")
        return value

    @field_validator("private_error_taxonomy")
    @classmethod
    def _taxonomy(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("private error taxonomy must not contain duplicates")
        if any(_TAXONOMY_RE.fullmatch(value) is None for value in values):
            raise ValueError("private error taxonomy must contain closed-style codes")
        return values

    @model_validator(mode="after")
    def _correctness(self) -> CoreDevTrajectoryV2:
        if self.correct != (self.parsed_prediction == self.target):
            raise ValueError("correctness does not match parsed prediction and target")
        return self

    def private_event(self, *, policy_version: str) -> EventIngestRequest:
        """Build one private Core transcript event without raw question/options."""

        prompt_identity = (
            "ChemBench4K dev leave-one-out trajectory.\n"
            f"category={self.category}\n"
            f"public_prompt_sha256={self.public_prompt_sha256}"
        )
        trajectory = {
            "status": "COMPLETED",
            "metadata": {
                "builder": "chembench4k_dev_loo_v2",
                "capture_mode": "transcript",
                "token_level_metrics_available": False,
                "source_split": "dev",
            },
            "traces": [
                {
                    "prompt_ids": [],
                    "response_ids": [],
                    "loss_mask": [],
                    "prompt_messages": [{"role": "user", "content": prompt_identity}],
                    "response_messages": [{"role": "assistant", "content": self.raw_completion}],
                    "finish_reason": "transcript",
                    "response_logprobs": None,
                    "reward": 1.0 if self.correct else 0.0,
                    "metadata": {
                        "capture_mode": "transcript",
                        "token_level_metrics_available": False,
                        "verifier": {
                            # Core's registered reflector only projects the
                            # closed ``summary`` fields into its prompt.  The
                            # private evaluation object (including target)
                            # remains in the dataset record and is never
                            # rendered for the reflector.
                            "summary": {code: 1 for code in self.private_error_taxonomy},
                            "private_error_taxonomy": list(self.private_error_taxonomy),
                        },
                    },
                }
            ],
        }
        return EventIngestRequest(
            source=DATASET_EVENT_SOURCE,
            event_type=DATASET_EVENT_TYPE,
            source_event_id=f"chembench4k-dev-loo:{self.uid}",
            task_id=self.uid,
            session_id=f"chembench4k-dev-loo-{self.uid[:24]}",
            policy_version=policy_version,
            rollout_step=0,
            agent={"harness": "codex", "model_name": MODEL},
            base_model=MODEL,
            reward=1.0 if self.correct else 0.0,
            status="COMPLETED",
            payload={
                "session_result": {
                    "session_id": f"chembench4k-dev-loo-{self.uid[:24]}",
                    "task_id": self.uid,
                    "status": "COMPLETED",
                    "trajectory": trajectory,
                    "metadata": {
                        "dataset_repository": DATASET_REPOSITORY,
                        "dataset_revision": self.dataset_revision,
                        "dataset_sha256": self.dataset_sha256,
                        "category": self.category,
                        "source_split": "dev",
                        "source_index": self.source_index,
                        "public_prompt_sha256": self.public_prompt_sha256,
                    },
                },
                "private_evaluation": {
                    "parsed_prediction": self.parsed_prediction,
                    "target": self.target,
                    "correct": self.correct,
                    "private_error_taxonomy": list(self.private_error_taxonomy),
                },
            },
        )


def build_core_dev_trajectories_v2(
    loo_tasks: tuple[DevLeaveOneOutTask, ...],
    evaluations: tuple[PrivateChemBench4KEvaluation, ...],
) -> tuple[CoreDevTrajectoryV2, ...]:
    """Bind the 45 private LOO attempts to target-aware Core dev records."""

    if (
        not isinstance(loo_tasks, tuple)
        or len(loo_tasks) != EXPECTED_DEV_TRAJECTORIES
        or any(type(item) is not DevLeaveOneOutTask for item in loo_tasks)
    ):
        raise ValueError("loo_tasks must contain exactly 45 DevLeaveOneOutTask values")
    if (
        not isinstance(evaluations, tuple)
        or len(evaluations) != EXPECTED_DEV_TRAJECTORIES
        or any(type(item) is not PrivateChemBench4KEvaluation for item in evaluations)
    ):
        raise ValueError("evaluations must contain exactly 45 private ChemBench4K evaluations")
    evaluations_by_uid = {evaluation.task_uid: evaluation for evaluation in evaluations}
    if len(evaluations_by_uid) != EXPECTED_DEV_TRAJECTORIES:
        raise ValueError("private dev evaluations must have unique task UIDs")

    records: list[CoreDevTrajectoryV2] = []
    for loo_task in loo_tasks:
        task = loo_task.evaluation_task
        evaluation = evaluations_by_uid.get(task.uid)
        if (
            evaluation is None
            or evaluation.category != task.category
            or evaluation.target != task.target
        ):
            raise ValueError("private dev evaluation does not match its LOO task identity")
        taxonomy: list[str] = []
        if not evaluation.official.prediction:
            taxonomy.append("format_failure")
        elif not evaluation.correct:
            taxonomy.append("incorrect_selection")
        if evaluation.strict.prediction is None:
            taxonomy.append("strict_format_mismatch")
        records.append(
            CoreDevTrajectoryV2(
                uid=task.uid,
                category=task.category,
                source_index=task.source_index,
                public_prompt_sha256=_sha256_bytes(loo_task.prompt.text.encode("utf-8")),
                raw_completion=evaluation.raw_completion,
                parsed_prediction=evaluation.official.prediction,
                target=task.target,
                correct=evaluation.correct,
                private_error_taxonomy=tuple(taxonomy),
                dataset_revision=task.dataset_revision,
                dataset_sha256=task.dataset_sha256,
            )
        )
    if set(evaluations_by_uid) != {task.evaluation_task.uid for task in loo_tasks}:
        raise ValueError("private dev evaluations do not exactly cover the LOO tasks")
    return tuple(records)


class RegisteredMethodEvidenceV2(_FrozenModel):
    method_id: Literal["text_memory_expel_reflector"]
    method_version: str
    registry_source: Literal["openevo.evolution.framework.builtins"]
    registry_digest: str
    reachable_registry_digest: str
    method_descriptor_digest: str
    method_identity_digest: str
    input_schema: str
    output_schema: tuple[Literal["text_memory"], ...]

    @field_validator(
        "registry_digest",
        "reachable_registry_digest",
        "method_descriptor_digest",
        "method_identity_digest",
    )
    @classmethod
    def _digests(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("method evidence digest must be a lowercase SHA-256")
        return value

    @field_validator("input_schema")
    @classmethod
    def _canonical_input_schema(cls, value: str) -> str:
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("method input schema must be canonical JSON") from exc
        if not isinstance(payload, dict) or canonical_json(payload) != value:
            raise ValueError("method input schema must be a canonical JSON object")
        return value

    def input_schema_value(self) -> dict[str, Any]:
        return json.loads(self.input_schema)


class MaintainerFrameworkBundleV2(_FrozenModel):
    """Paths and identity for a locally built, externally reloadable Core lock."""

    schema_version: Literal["chembench4k_maintainer_framework_bundle_v2"] = (
        "chembench4k_maintainer_framework_bundle_v2"
    )
    wheel_path: str
    framework_lock_path: str
    distribution_version: str
    distribution_digest: str

    @field_validator("distribution_digest")
    @classmethod
    def _sha256(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("framework distribution digest must be a SHA-256")
        return value


class CoreEvolutionJobV2(_FrozenModel):
    schema_version: Literal["chembench4k_core_evolution_job_v2"] = (
        "chembench4k_core_evolution_job_v2"
    )
    protocol_id: Literal["chembench4k_frozen_generalization_v2"] = PROTOCOL_ID
    dataset_id: str
    dataset_artifact_id: str
    dataset_manifest_sha256: str
    dataset_combined_sha256: str
    dataset_records_digest: str
    dev_uid_set_sha256: str
    ordered_loo_uid_sha256: str
    reflector_input_digest: str
    record_count: Literal[45]
    configured_max_records: Literal[45]
    records_visible_to_reflector: Literal[45]
    method: RegisteredMethodEvidenceV2
    plan_id: str
    plan_digest: str
    job_id: str
    job_type: str
    execution_envelope_digest: str

    @field_validator(
        "dataset_manifest_sha256",
        "dataset_combined_sha256",
        "dataset_records_digest",
        "dev_uid_set_sha256",
        "ordered_loo_uid_sha256",
        "reflector_input_digest",
        "plan_digest",
        "execution_envelope_digest",
    )
    @classmethod
    def _digests(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("job digest must be a lowercase SHA-256")
        return value


class CoreEvolutionExecutionV2(_FrozenModel):
    schema_version: Literal["chembench4k_core_evolution_execution_v2"] = (
        "chembench4k_core_evolution_execution_v2"
    )
    prepared_job: CoreEvolutionJobV2
    core_artifact_id: str
    artifact_payload_sha256: str
    artifact_manifest_sha256_before_promotion: str
    reflector_execution_receipt_sha256: str | None = None
    reflector_event_stream_sha256: str | None = None

    @field_validator(
        "artifact_payload_sha256",
        "artifact_manifest_sha256_before_promotion",
        "reflector_execution_receipt_sha256",
        "reflector_event_stream_sha256",
    )
    @classmethod
    def _digests(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("execution digest must be a lowercase SHA-256")
        return value


class CoreArtifactValidationReceiptV2(_FrozenModel):
    """Closed decision returned by a trusted benchmark artifact validator."""

    schema_version: Literal["chembench4k_core_artifact_validation_v2"] = (
        "chembench4k_core_artifact_validation_v2"
    )
    validator_id: str = Field(min_length=1, max_length=128)
    artifact_id: str = Field(min_length=1, max_length=256)
    artifact_payload_sha256: str
    passed: bool
    finding_codes: tuple[str, ...] = Field(default=(), max_length=128)

    @field_validator("artifact_payload_sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("validation payload digest must be a lowercase SHA-256")
        return value

    @field_validator("finding_codes")
    @classmethod
    def _findings(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("validation findings must not contain duplicates")
        if any(_TAXONOMY_RE.fullmatch(value.lower()) is None for value in values):
            raise ValueError("validation findings must be closed-style codes")
        return values

    @model_validator(mode="after")
    def _decision(self) -> CoreArtifactValidationReceiptV2:
        if self.passed == bool(self.finding_codes):
            raise ValueError("validation decision and findings are inconsistent")
        return self

    @property
    def digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json"))


class TextMemoryArtifactValidatorV2(Protocol):
    """Validator boundary invoked after Core publication and before promotion."""

    def validate(
        self,
        *,
        artifact: ArtifactResponse,
        payload: bytes,
        payload_sha256: str,
        lineage: dict[str, Any],
    ) -> CoreArtifactValidationReceiptV2: ...


class FrozenCoreTextMemoryV2(_FrozenModel):
    schema_version: Literal["chembench4k_frozen_core_text_memory_v2"] = (
        "chembench4k_frozen_core_text_memory_v2"
    )
    execution: CoreEvolutionExecutionV2
    validation_receipt: CoreArtifactValidationReceiptV2
    validation_receipt_sha256: str
    core_artifact_manifest_sha256: str
    core_context_id: str
    context_resolution_digest: str
    resolved_memory: str
    resolved_memory_sha256: str

    @field_validator(
        "validation_receipt_sha256",
        "core_artifact_manifest_sha256",
        "context_resolution_digest",
        "resolved_memory_sha256",
    )
    @classmethod
    def _digests(cls, value: str) -> str:
        if _SHA256_RE.fullmatch(value) is None:
            raise ValueError("frozen memory digest must be a lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def _resolved_digest(self) -> FrozenCoreTextMemoryV2:
        if _sha256_bytes(self.resolved_memory.encode("utf-8")) != self.resolved_memory_sha256:
            raise ValueError("resolved memory digest does not match the resolved text")
        return self

    def audit_payload(self) -> dict[str, Any]:
        """Return content-free identity evidence for the benchmark receipt."""

        return {
            "schema_version": self.schema_version,
            "protocol_id": PROTOCOL_ID,
            "dataset_id": self.execution.prepared_job.dataset_id,
            "dataset_artifact_id": self.execution.prepared_job.dataset_artifact_id,
            "dataset_combined_sha256": (self.execution.prepared_job.dataset_combined_sha256),
            "plan_id": self.execution.prepared_job.plan_id,
            "plan_digest": self.execution.prepared_job.plan_digest,
            "evolution_job_id": self.execution.prepared_job.job_id,
            "method_id": self.execution.prepared_job.method.method_id,
            "method_descriptor_digest": (
                self.execution.prepared_job.method.method_descriptor_digest
            ),
            "core_artifact_id": self.execution.core_artifact_id,
            "core_artifact_manifest_sha256": self.core_artifact_manifest_sha256,
            "artifact_payload_sha256": self.execution.artifact_payload_sha256,
            "reflector_execution_receipt_sha256": (
                self.execution.reflector_execution_receipt_sha256
            ),
            "reflector_event_stream_sha256": (self.execution.reflector_event_stream_sha256),
            "validation_receipt_sha256": self.validation_receipt_sha256,
            "core_context_id": self.core_context_id,
            "context_resolution_digest": self.context_resolution_digest,
            "resolved_memory_sha256": self.resolved_memory_sha256,
        }


class _StoreWorkerClient:
    """Transport-neutral in-process adapter for Core's real worker protocol."""

    def __init__(self, store: EvolutionStore) -> None:
        self._store = store
        self.completed: dict[str, Any] | None = None
        self.failed: dict[str, Any] | None = None

    def claim(
        self,
        worker_id: str,
        capabilities: list[str],
        *,
        lease_seconds: int | None = None,
        method_capabilities: list[str] | None = None,
        method_identity_capabilities: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        request = WorkerClaimRequest(
            worker_id=worker_id,
            capabilities=capabilities,
            method_capabilities=method_capabilities,
            method_identity_capabilities=method_identity_capabilities,
            lease_seconds=lease_seconds or 600,
        )
        claimed = self._store.claim_job(request).job
        return None if claimed is None else claimed.model_dump(mode="json")

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
        self.completed = dict(
            self._store.complete_job(
                job_id,
                WorkerCompleteRequest(
                    lease_id=lease_id,
                    artifacts=artifacts,
                    report=report or {},
                ),
            )
        )
        return self.completed

    def fail(
        self,
        job_id: str,
        lease_id: str,
        error: str,
        *,
        retryable: bool = True,
    ) -> dict[str, Any]:
        self.failed = dict(
            self._store.fail_job(
                job_id,
                WorkerFailRequest(
                    lease_id=lease_id,
                    error=error,
                    retryable=retryable,
                ),
            )
        )
        return self.failed


class OpenEvoTextMemoryLifecycleV2:
    """Standalone-maintainer adapter over the real OpenEvo Core lifecycle."""

    def __init__(
        self,
        *,
        db_path: str | Path,
        artifact_root: str | Path,
        executable_registry: VerifiedExecutableRegistry,
    ) -> None:
        self._registry = require_verified_executable_registry(executable_registry)
        self._require_registered_method()
        self._store = EvolutionStore(
            db_path=db_path,
            artifact_root=artifact_root,
            executable_registry=self._registry,
        )
        self._store.initialize()

    @classmethod
    def from_framework_lock(
        cls,
        *,
        db_path: str | Path,
        artifact_root: str | Path,
        framework_lock: str | Path,
    ) -> OpenEvoTextMemoryLifecycleV2:
        """Load the actual locked Core distribution and verified method handles."""

        return cls(
            db_path=db_path,
            artifact_root=artifact_root,
            executable_registry=load_verified_framework_registry(framework_lock),
        )

    @property
    def method_evidence(self) -> RegisteredMethodEvidenceV2:
        descriptor = self._registry.snapshot.methods[METHOD_ID]
        identity = self._registry.snapshot.identity_for(
            DescriptorKind.METHOD,
            METHOD_ID,
        )
        return RegisteredMethodEvidenceV2(
            method_id=METHOD_ID,
            method_version=identity.implementation.distribution_version,
            registry_source=METHOD_REGISTRY_SOURCE,
            registry_digest=self._registry.snapshot.registry_digest,
            reachable_registry_digest=self._registry.snapshot.plan_snapshot_digest(
                (
                    EvolutionTargetSelection(
                        target_id=METHOD_TARGET_ID,
                        enabled=True,
                        method_id=METHOD_ID,
                        config={
                            "max_records": EXPECTED_DEV_TRAJECTORIES,
                            "reflector_llm": {
                                "provider": "codex_cli",
                                "model": MODEL,
                            },
                        },
                    ),
                ),
                self._execution_profile(),
            ),
            method_descriptor_digest=canonical_digest(descriptor),
            method_identity_digest=self._registry.snapshot.identity_digest_for(
                DescriptorKind.METHOD,
                METHOD_ID,
            ),
            input_schema=canonical_json(
                {
                    "bindings": [
                        binding.model_dump(mode="json") for binding in descriptor.input_bindings
                    ],
                    "config_schema": descriptor.config_schema,
                }
            ),
            output_schema=("text_memory",),
        )

    def prepare_dev_job(
        self,
        records: tuple[CoreDevTrajectoryV2, ...],
        *,
        configured_max_records: int,
        plan_id: str | None = None,
        reflector_timeout_seconds: float = 900.0,
    ) -> CoreEvolutionJobV2:
        """Ingest exactly 45 dev LOO records and create an immutable Core job."""

        if (
            isinstance(configured_max_records, bool)
            or configured_max_records != EXPECTED_DEV_TRAJECTORIES
        ):
            raise ValueError("configured max_records must explicitly equal 45")
        (
            records_digest,
            uid_set_digest,
            ordered_uid_digest,
            dataset_sha256,
        ) = self._validate_records(records)
        identity_suffix = _sha256_bytes(
            (PROTOCOL_ID + DATASET_REVISION + dataset_sha256 + records_digest).encode("utf-8")
        )
        effective_plan_id = plan_id or f"chembench4k.frozen.v2.{identity_suffix[:32]}"
        policy_version = f"{PROTOCOL_ID}.{identity_suffix[:32]}"

        self._ingest_ordered_private_events(
            records,
            policy_version=policy_version,
        )
        dataset = self._store.create_dataset(
            DatasetCreateRequest(
                name="ChemBench4K dev LOO trajectories v2",
                purpose=DATASET_PURPOSE,
                query={
                    "event_types": [DATASET_EVENT_TYPE],
                    "status": ["COMPLETED"],
                    "policy_version": policy_version,
                },
                limits={
                    "max_events": EXPECTED_DEV_TRAJECTORIES,
                    "max_traces": EXPECTED_DEV_TRAJECTORIES,
                },
            )
        )
        if (
            dataset.event_count != EXPECTED_DEV_TRAJECTORIES
            or dataset.trace_count != EXPECTED_DEV_TRAJECTORIES
        ):
            raise CoreEvolutionV2Error(
                "Core dataset does not contain exactly 45 dev LOO trajectories"
            )
        dataset_artifact = self._store.get_artifact(dataset.artifact_id)
        dataset_manifest_path = _safe_core_file_path(
            dataset_artifact.uri,
            allowed_root=self._store.files.root,
        )
        dataset_manifest_sha256 = _sha256_bytes(
            _read_bounded_regular_file(
                dataset_manifest_path,
                maximum=MAX_CORE_MANIFEST_BYTES,
            )
        )

        selection = EvolutionTargetSelection(
            target_id=METHOD_TARGET_ID,
            enabled=True,
            method_id=METHOD_ID,
            config={
                "max_records": configured_max_records,
                "reflector_llm": {
                    "provider": "codex_cli",
                    "model": MODEL,
                    "timeout_seconds": reflector_timeout_seconds,
                },
            },
        )
        plan = self._registry.snapshot.compile_plan(
            plan_id=effective_plan_id,
            selections=(selection,),
            profile=self._execution_profile(),
        )
        job_type = f"{JOB_TYPE_PREFIX}.{identity_suffix[:24]}"
        request = PlanBoundJobCreateRequest(
            plan=plan,
            target_id=METHOD_TARGET_ID,
            job_type=job_type,
            input_bindings=(
                PlannedInputBinding(
                    binding_id="dataset_inputs",
                    artifact_ids=(dataset.artifact_id,),
                ),
                PlannedInputBinding(
                    binding_id="prior_target_artifacts",
                    artifact_ids=(),
                ),
            ),
            core_config={
                "name": "ChemBench4K frozen text memory v2",
                "policy_version": policy_version,
                "promoted": False,
                "lineage": {
                    "protocol_id": PROTOCOL_ID,
                    "dataset_repository": DATASET_REPOSITORY,
                    "dataset_revision": DATASET_REVISION,
                    "dataset_combined_sha256": dataset_sha256,
                    "dev_record_count": EXPECTED_DEV_TRAJECTORIES,
                    "dev_uid_set_sha256": uid_set_digest,
                    "ordered_loo_uid_sha256": ordered_uid_digest,
                    "reflector_input_digest": records_digest,
                    "configured_max_records": configured_max_records,
                    "records_visible_to_reflector": EXPECTED_DEV_TRAJECTORIES,
                },
                "compatibility": {
                    "agent_harness": ["codex"],
                    "auth_mode": ["subscription"],
                    "base_model": [MODEL],
                    "task_tags": [PROTOCOL_ID],
                },
                "tags": [
                    PROTOCOL_ID,
                    "chembench4k",
                    "dev-only",
                    "frozen-candidate",
                ],
                "forbidden_literals": {
                    "dev_uid": [record.uid for record in records],
                    "dev_uid_prefix": [record.uid[:24] for record in records],
                    "dev_session_id": [
                        f"chembench4k-dev-loo-{record.uid[:24]}" for record in records
                    ],
                    "public_prompt_sha256": [record.public_prompt_sha256 for record in records],
                },
            },
        )
        created = self._store.create_plan_bound_job(
            request,
            snapshot=self._registry.snapshot,
        )
        with self._store.connect() as connection:
            job_row = connection.execute(
                "SELECT execution_envelope_digest FROM jobs WHERE job_id = ?",
                (created.job_id,),
            ).fetchone()
        if (
            job_row is None
            or not isinstance(job_row["execution_envelope_digest"], str)
            or _SHA256_RE.fullmatch(job_row["execution_envelope_digest"]) is None
        ):
            raise CoreEvolutionV2Error("Core plan-bound job lacks an execution envelope digest")
        self._write_private_reflector_input(
            job_id=created.job_id,
            records=records,
            expected_digest=records_digest,
        )
        return CoreEvolutionJobV2(
            dataset_id=dataset.dataset_id,
            dataset_artifact_id=dataset.artifact_id,
            dataset_manifest_sha256=dataset_manifest_sha256,
            dataset_combined_sha256=dataset_sha256,
            dataset_records_digest=records_digest,
            dev_uid_set_sha256=uid_set_digest,
            ordered_loo_uid_sha256=ordered_uid_digest,
            reflector_input_digest=records_digest,
            record_count=EXPECTED_DEV_TRAJECTORIES,
            configured_max_records=configured_max_records,
            records_visible_to_reflector=EXPECTED_DEV_TRAJECTORIES,
            method=self.method_evidence,
            plan_id=plan.plan_id,
            plan_digest=canonical_digest(plan),
            job_id=created.job_id,
            job_type=job_type,
            execution_envelope_digest=job_row["execution_envelope_digest"],
        )

    def private_reflector_input_path(self, prepared: CoreEvolutionJobV2) -> Path:
        """Return the trusted dev-only input bound to one prepared Core job."""

        self._require_prepared_identity(prepared)
        path = (
            self._store.files.root
            / _PRIVATE_REFLECTOR_INPUT_DIRECTORY
            / prepared.job_id
            / "dev_loo_records.jsonl"
        )
        records, digest = _read_private_reflector_records(path)
        if len(records) != EXPECTED_DEV_TRAJECTORIES or digest != (
            prepared.reflector_input_digest
        ):
            raise CoreEvolutionV2Error(
                "private reflector input does not match the immutable Core job"
            )
        return path

    def execute_registered_method(
        self,
        prepared: CoreEvolutionJobV2,
        *,
        reflector_boundary: ReflectorExecutionBoundaryV2 | None = None,
        test_only_allow_unisolated_reflector: bool = False,
        worker_id: str = "chembench4k-text-memory-worker-v2",
        lease_seconds: int = 1800,
    ) -> CoreEvolutionExecutionV2:
        """Dispatch the plan-bound job through Core's verified method handle."""

        self._require_prepared_identity(prepared)
        if reflector_boundary is None and not test_only_allow_unisolated_reflector:
            raise CoreEvolutionV2Error(
                "registered reflector execution requires the OS-isolated wrapper"
            )
        if reflector_boundary is not None and test_only_allow_unisolated_reflector:
            raise ValueError("reflector execution mode is ambiguous")
        client = _StoreWorkerClient(self._store)
        boundary_receipt_sha256: str | None = None
        event_stream_sha256: str | None = None
        if reflector_boundary is None:
            claimed = run_once(
                client,
                worker_id=worker_id,
                capabilities=[prepared.job_type],
                artifact_root=self._store.files.root,
                lease_seconds=lease_seconds,
                executable_registry=self._registry,
            )
        else:
            from openevo_chembench.reflector_execution_boundary_v2 import (
                ReflectorBoundaryStatusV2,
            )

            with reflector_boundary.activate() as activation:
                claimed = run_once(
                    client,
                    worker_id=worker_id,
                    capabilities=[prepared.job_type],
                    artifact_root=self._store.files.root,
                    lease_seconds=lease_seconds,
                    executable_registry=self._registry,
                )
            receipt = activation.load_receipt_for_audit()
            if receipt.status is ReflectorBoundaryStatusV2.SECURITY_TOOL_USE_VIOLATION:
                raise CoreEvolutionV2Error("REFLECTOR_SECURITY_TOOL_USE_VIOLATION")
            if (
                receipt.status is not ReflectorBoundaryStatusV2.COMPLETED
                or not receipt.cleanup_complete
                or receipt.event_counts
                or receipt.retry_allowed
                or receipt.resume_allowed
                or receipt.replacement_completion_allowed
            ):
                raise CoreEvolutionV2Error(
                    "isolated reflector execution did not produce a safe terminal receipt"
                )
            boundary_receipt_sha256 = receipt.digest
            event_stream_sha256 = receipt.event_stream_sha256
        if not claimed or client.completed is None:
            raise CoreEvolutionV2Error(
                "registered Core text-memory method did not complete successfully"
            )
        if client.completed.get("job_id") != prepared.job_id:
            raise CoreEvolutionV2Error("Core worker completed an unexpected job")
        artifact_ids = client.completed.get("artifact_ids")
        if not isinstance(artifact_ids, list) or len(artifact_ids) != 1:
            raise CoreEvolutionV2Error("Core text-memory method must publish exactly one artifact")
        artifact = self._store.get_artifact(str(artifact_ids[0]))
        self._require_candidate_artifact(artifact, prepared=prepared)
        payload_path = _safe_core_file_path(
            artifact.uri,
            allowed_root=self._store.files.root,
        )
        payload = _read_bounded_regular_file(payload_path, maximum=MAX_MEMORY_BYTES)
        manifest_path = self._store.files.artifact_manifest_path(
            str(ArtifactType.TEXT_MEMORY),
            artifact.artifact_id,
        )
        return CoreEvolutionExecutionV2(
            prepared_job=prepared,
            core_artifact_id=artifact.artifact_id,
            artifact_payload_sha256=_sha256_bytes(payload),
            artifact_manifest_sha256_before_promotion=_sha256_bytes(
                _read_bounded_regular_file(
                    manifest_path,
                    maximum=MAX_CORE_MANIFEST_BYTES,
                )
            ),
            reflector_execution_receipt_sha256=boundary_receipt_sha256,
            reflector_event_stream_sha256=event_stream_sha256,
        )

    def validate_promote_and_resolve(
        self,
        execution: CoreEvolutionExecutionV2,
        *,
        validator: TextMemoryArtifactValidatorV2,
    ) -> FrozenCoreTextMemoryV2:
        """Validate, promote, and resolve one immutable text-memory candidate."""

        self._require_prepared_identity(execution.prepared_job)
        artifact = self._store.get_artifact(execution.core_artifact_id)
        self._require_candidate_artifact(
            artifact,
            prepared=execution.prepared_job,
        )
        if artifact.promoted:
            raise CoreEvolutionV2Error("candidate was promoted before benchmark validation")
        payload_path = _safe_core_file_path(
            artifact.uri,
            allowed_root=self._store.files.root,
        )
        payload = _read_bounded_regular_file(payload_path, maximum=MAX_MEMORY_BYTES)
        payload_sha256 = _sha256_bytes(payload)
        if payload_sha256 != execution.artifact_payload_sha256:
            raise CoreEvolutionV2Error("candidate payload changed after Core publication")

        receipt = validator.validate(
            artifact=artifact,
            payload=payload,
            payload_sha256=payload_sha256,
            lineage=self._artifact_lineage(artifact.artifact_id),
        )
        if type(receipt) is not CoreArtifactValidationReceiptV2:
            raise CoreEvolutionV2Error("artifact validator returned an untrusted receipt type")
        if (
            receipt.artifact_id != artifact.artifact_id
            or receipt.artifact_payload_sha256 != payload_sha256
        ):
            raise CoreEvolutionV2Error("artifact validation receipt is not bound to the candidate")
        if not receipt.passed or receipt.finding_codes:
            raise CoreEvolutionV2Error("text-memory candidate failed validation")

        promoted = self._store.update_artifact_promotion(
            artifact.artifact_id,
            promoted=True,
        )
        if not promoted.promoted:
            raise CoreEvolutionV2Error("Core artifact promotion did not persist")
        manifest_path = self._store.files.artifact_manifest_path(
            str(ArtifactType.TEXT_MEMORY),
            artifact.artifact_id,
        )
        artifact_manifest_sha256 = _sha256_bytes(
            _read_bounded_regular_file(
                manifest_path,
                maximum=MAX_CORE_MANIFEST_BYTES,
            )
        )

        materialized = self._store.resolve_materialized_context(
            self._context_request(artifact.artifact_id)
        )
        resolved_memory = _resolved_text_memory(
            materialized,
            expected_artifact_id=artifact.artifact_id,
        )
        return FrozenCoreTextMemoryV2(
            execution=execution,
            validation_receipt=receipt,
            validation_receipt_sha256=receipt.digest,
            core_artifact_manifest_sha256=artifact_manifest_sha256,
            core_context_id=materialized.context_id,
            context_resolution_digest=canonical_digest(materialized),
            resolved_memory=resolved_memory,
            resolved_memory_sha256=_sha256_bytes(resolved_memory.encode("utf-8")),
        )

    def verify_frozen_record(
        self,
        frozen: FrozenCoreTextMemoryV2,
        *,
        validator: TextMemoryArtifactValidatorV2,
    ) -> dict[str, Any]:
        """Recompute frozen evidence from the current Core store and registry.

        This method accepts no verification booleans.  It reopens the persisted
        job, active promoted artifact, managed payload, artifact manifest,
        materialized context manifest, and every materialized blob needed to
        prove the resolved memory binding.
        """

        if type(frozen) is not FrozenCoreTextMemoryV2:
            raise TypeError("frozen must be exact FrozenCoreTextMemoryV2")
        execution = frozen.execution
        prepared = execution.prepared_job
        self._require_prepared_identity(prepared)
        with self._store.connect() as connection:
            job_row = connection.execute(
                """
                SELECT state, method, plan_id, method_identity_digest,
                       execution_envelope_digest
                FROM jobs WHERE job_id = ?
                """,
                (prepared.job_id,),
            ).fetchone()
            artifact_row = connection.execute(
                """
                SELECT type, state, promoted, manifest_path
                FROM artifacts WHERE artifact_id = ?
                """,
                (execution.core_artifact_id,),
            ).fetchone()
            context_row = connection.execute(
                """
                SELECT cm.context_id, cm.registry_digest, cm.request_digest,
                       cm.manifest_json, c.response_json,
                       c.selected_artifact_ids_json
                FROM context_materializations AS cm
                JOIN contexts AS c USING (context_id)
                WHERE cm.context_id = ?
                """,
                (frozen.core_context_id,),
            ).fetchone()
        if (
            job_row is None
            or job_row["state"] != "succeeded"
            or job_row["method"] != METHOD_ID
            or job_row["plan_id"] != prepared.plan_id
            or job_row["method_identity_digest"] != prepared.method.method_identity_digest
            or job_row["execution_envelope_digest"] != prepared.execution_envelope_digest
        ):
            raise CoreEvolutionV2Error(
                "persisted Core evolution job is not a completed immutable job"
            )
        if (
            artifact_row is None
            or artifact_row["type"] != "text_memory"
            or artifact_row["state"] != "active"
            or artifact_row["promoted"] != 1
        ):
            raise CoreEvolutionV2Error(
                "persisted Core artifact is not an active promoted text memory"
            )
        artifact = self._store.get_artifact(execution.core_artifact_id)
        self._require_candidate_artifact(artifact, prepared=prepared)
        payload_path = _safe_core_file_path(
            artifact.uri,
            allowed_root=self._store.files.root,
        )
        payload = _read_bounded_regular_file(
            payload_path,
            maximum=MAX_MEMORY_BYTES,
        )
        payload_sha256 = _sha256_bytes(payload)
        if payload_sha256 != execution.artifact_payload_sha256:
            raise CoreEvolutionV2Error(
                "persisted Core artifact payload digest does not match frozen evidence"
            )
        manifest_path = self._store.files.artifact_manifest_path(
            str(ArtifactType.TEXT_MEMORY),
            execution.core_artifact_id,
        )
        recorded_manifest_path = Path(str(artifact_row["manifest_path"]))
        if Path(os.path.abspath(recorded_manifest_path)) != Path(os.path.abspath(manifest_path)):
            raise CoreEvolutionV2Error(
                "persisted Core artifact manifest path does not match its managed identity"
            )
        manifest_sha256 = _sha256_bytes(
            _read_bounded_regular_file(
                manifest_path,
                maximum=MAX_CORE_MANIFEST_BYTES,
            )
        )
        if manifest_sha256 != frozen.core_artifact_manifest_sha256:
            raise CoreEvolutionV2Error(
                "persisted Core artifact manifest digest does not match frozen evidence"
            )
        receipt = frozen.validation_receipt
        # Re-run the same validator over an immutable view of the candidate at
        # its pre-promotion state.  Promotion is the only permitted state
        # transition after the original receipt; payload, manifest, lineage,
        # and identity all come from the authoritative current Core record.
        validation_view = artifact.model_copy(update={"promoted": False})
        recomputed_receipt = validator.validate(
            artifact=validation_view,
            payload=payload,
            payload_sha256=payload_sha256,
            lineage=self._artifact_lineage(execution.core_artifact_id),
        )
        if type(recomputed_receipt) is not CoreArtifactValidationReceiptV2:
            raise CoreEvolutionV2Error("artifact validator returned an untrusted receipt type")
        if (
            receipt.artifact_id != execution.core_artifact_id
            or receipt.artifact_payload_sha256 != payload_sha256
            or not receipt.passed
            or receipt.finding_codes
            or receipt.digest != frozen.validation_receipt_sha256
            or recomputed_receipt != receipt
        ):
            raise CoreEvolutionV2Error(
                "artifact validation receipt is not valid for persisted Core state"
            )

        if context_row is None:
            raise CoreEvolutionV2Error("persisted Core materialized context is unavailable")
        try:
            materialized = MaterializedContext.model_validate_json(
                str(context_row["manifest_json"])
            )
            response = MaterializedContext.model_validate_json(str(context_row["response_json"]))
            selected_ids = json.loads(str(context_row["selected_artifact_ids_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CoreEvolutionV2Error("persisted Core materialized context is invalid") from exc
        if (
            canonical_json(materialized) != context_row["manifest_json"]
            or response != materialized
            or materialized.context_id != frozen.core_context_id
            or materialized.registry_digest != self._registry.snapshot.registry_digest
            or context_row["registry_digest"] != materialized.registry_digest
            or context_row["request_digest"] != materialized.request_digest
            or selected_ids != [execution.core_artifact_id]
            or materialized.selection.artifact_ids != (execution.core_artifact_id,)
            or canonical_digest(materialized) != frozen.context_resolution_digest
        ):
            raise CoreEvolutionV2Error(
                "persisted Core context identity does not match frozen evidence"
            )
        resolved_memory = _resolved_text_memory(
            materialized,
            expected_artifact_id=execution.core_artifact_id,
        )
        resolved_memory_sha256 = _sha256_bytes(resolved_memory.encode("utf-8"))
        if (
            resolved_memory != frozen.resolved_memory
            or resolved_memory_sha256 != frozen.resolved_memory_sha256
        ):
            raise CoreEvolutionV2Error(
                "persisted Core resolved memory does not match frozen evidence"
            )

        matching_blob_count = 0
        for blob in materialized.blobs:
            with self._store.open_materialized_blob(
                materialized.context_id,
                blob.blob_id,
            ) as lease:
                blob_payload = lease.stream.read()
            if len(blob_payload) != blob.size_bytes or _sha256_bytes(blob_payload) != blob.sha256:
                raise CoreEvolutionV2Error(
                    "Core materialized blob does not match its authority record"
                )
            if blob_payload == resolved_memory.encode("utf-8"):
                matching_blob_count += 1
        if matching_blob_count != 1:
            raise CoreEvolutionV2Error(
                "Core materialized context does not contain exactly one resolved memory blob"
            )

        return {
            **frozen.audit_payload(),
            "schema_version": "chembench4k_frozen_core_verification_v2",
            "job_state": "succeeded",
            "artifact_state": "active",
            "artifact_promoted": True,
            "artifact_type": "text_memory",
            "registry_digest": self._registry.snapshot.registry_digest,
            "materialized_blob_count": len(materialized.blobs),
            "verified_from_core_state": True,
        }

    def issue_runtime_memory(
        self,
        frozen: FrozenCoreTextMemoryV2,
        *,
        validator: TextMemoryArtifactValidatorV2,
    ) -> CoreResolvedTextMemoryV2:
        """Issue the runner DTO only after re-verifying current Core state."""

        self.verify_frozen_record(frozen, validator=validator)
        from openevo_chembench.frozen_runtime_v2 import (
            _issue_core_resolved_text_memory_v2,
        )

        return _issue_core_resolved_text_memory_v2(
            core_artifact_id=frozen.execution.core_artifact_id,
            artifact_payload_sha256=(frozen.execution.artifact_payload_sha256),
            context_resolution_digest=frozen.context_resolution_digest,
            resolved_memory_sha256=frozen.resolved_memory_sha256,
            markdown=frozen.resolved_memory,
        )

    def _require_registered_method(self) -> None:
        snapshot = self._registry.snapshot
        if METHOD_ID not in snapshot.methods:
            raise CoreEvolutionV2Error(
                "verified Core registry lacks the ChemBench4K text-memory method"
            )
        if METHOD_ID not in self._registry.method_handles:
            raise CoreEvolutionV2Error(
                "verified Core registry lacks the text-memory executable handle"
            )
        descriptor = snapshot.methods[METHOD_ID]
        if (
            descriptor.target_id != METHOD_TARGET_ID
            or descriptor.output_artifact_types != ("text_memory",)
            or descriptor.invocation_abi.value != "legacy_worker_job_v1"
        ):
            raise CoreEvolutionV2Error(
                "registered text-memory method descriptor has an incompatible contract"
            )
        if descriptor.implementation_ref.entry_point != (
            "openevo.evolution.methods:text_memory_expel_reflector"
        ):
            raise CoreEvolutionV2Error(
                "registered text-memory method entry point is not the protected built-in"
            )

    def _ingest_ordered_private_events(
        self,
        records: tuple[CoreDevTrajectoryV2, ...],
        *,
        policy_version: str,
    ) -> None:
        """Use distinct Core timestamps so dataset order exactly matches LOO order."""

        previous_ingested_at: str | None = None
        for record in records:
            if previous_ingested_at is not None:
                _wait_for_core_ingest_tick(previous_ingested_at)
            event = record.private_event(policy_version=policy_version)
            self._store.ingest_event(event)
            with self._store.connect() as connection:
                row = connection.execute(
                    """
                    SELECT ingested_at
                    FROM events
                    WHERE source = ? AND event_type = ? AND source_event_id = ?
                    """,
                    (event.source, event.event_type, event.source_event_id),
                ).fetchone()
            if (
                row is None
                or not isinstance(row["ingested_at"], str)
                or (
                    previous_ingested_at is not None and row["ingested_at"] <= previous_ingested_at
                )
            ):
                raise CoreEvolutionV2Error(
                    "Core event timestamps do not preserve deterministic LOO order"
                )
            previous_ingested_at = row["ingested_at"]

    def _write_private_reflector_input(
        self,
        *,
        job_id: str,
        records: tuple[CoreDevTrajectoryV2, ...],
        expected_digest: str,
    ) -> Path:
        payloads = [record.model_dump(mode="json") for record in records]
        if canonical_digest(payloads) != expected_digest:
            raise CoreEvolutionV2Error("private reflector input digest drifted")
        directory = self._store.files.root / _PRIVATE_REFLECTOR_INPUT_DIRECTORY / job_id
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        path = directory / "dev_loo_records.jsonl"
        encoded = "".join(f"{canonical_json(payload)}\n" for payload in payloads).encode("utf-8")
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        records_from_disk, digest = _read_private_reflector_records(path)
        if len(records_from_disk) != EXPECTED_DEV_TRAJECTORIES or digest != expected_digest:
            raise CoreEvolutionV2Error("private reflector input write verification failed")
        return path

    def _validate_records(
        self,
        records: tuple[CoreDevTrajectoryV2, ...],
    ) -> tuple[str, str, str, str]:
        if not isinstance(records, tuple) or len(records) != EXPECTED_DEV_TRAJECTORIES:
            raise ValueError("Core evolution requires exactly 45 dev LOO trajectories")
        if any(type(record) is not CoreDevTrajectoryV2 for record in records):
            raise TypeError("Core evolution records must be exact CoreDevTrajectoryV2")
        uids = tuple(record.uid for record in records)
        if len(uids) != len(set(uids)):
            raise ValueError("Core evolution dev UIDs must be unique")
        dataset_hashes = {record.dataset_sha256 for record in records}
        if len(dataset_hashes) != 1:
            raise ValueError("Core evolution records must bind one dataset snapshot")
        records_digest = canonical_digest([record.model_dump(mode="json") for record in records])
        uid_set_digest = canonical_digest(sorted(uids))
        ordered_uid_digest = _sha256_bytes("\n".join(uids).encode("ascii"))
        return (
            records_digest,
            uid_set_digest,
            ordered_uid_digest,
            next(iter(dataset_hashes)),
        )

    def _require_prepared_identity(self, prepared: CoreEvolutionJobV2) -> None:
        if type(prepared) is not CoreEvolutionJobV2:
            raise TypeError("prepared job must be exact CoreEvolutionJobV2")
        evidence = self.method_evidence
        if prepared.method != evidence:
            raise CoreEvolutionV2Error(
                "prepared job method identity does not match the verified registry"
            )
        with self._store.connect() as connection:
            row = connection.execute(
                """
                SELECT state, plan_id, method, job_type, method_identity_digest,
                       execution_envelope_digest
                FROM jobs WHERE job_id = ?
                """,
                (prepared.job_id,),
            ).fetchone()
        if row is None:
            raise CoreEvolutionV2Error("prepared Core evolution job is unavailable")
        if (
            row["plan_id"] != prepared.plan_id
            or row["method"] != METHOD_ID
            or row["job_type"] != prepared.job_type
            or row["method_identity_digest"] != evidence.method_identity_digest
            or row["execution_envelope_digest"] != prepared.execution_envelope_digest
        ):
            raise CoreEvolutionV2Error("prepared Core evolution job identity drifted")

    def _require_candidate_artifact(
        self,
        artifact: ArtifactResponse,
        *,
        prepared: CoreEvolutionJobV2,
    ) -> None:
        if (
            artifact.type is not ArtifactType.TEXT_MEMORY
            or artifact.state is not ArtifactState.ACTIVE
        ):
            raise CoreEvolutionV2Error("Core worker output is not an active text-memory artifact")
        lineage = self._artifact_lineage(artifact.artifact_id)
        if (
            lineage.get("protocol_id") != PROTOCOL_ID
            or lineage.get("dataset_repository") != DATASET_REPOSITORY
            or lineage.get("dataset_revision") != DATASET_REVISION
            or lineage.get("dataset_combined_sha256") != prepared.dataset_combined_sha256
            or lineage.get("dev_record_count") != EXPECTED_DEV_TRAJECTORIES
            or lineage.get("dev_uid_set_sha256") != prepared.dev_uid_set_sha256
            or lineage.get("ordered_loo_uid_sha256") != prepared.ordered_loo_uid_sha256
            or lineage.get("reflector_input_digest") != prepared.reflector_input_digest
            or lineage.get("configured_max_records") != prepared.configured_max_records
            or lineage.get("records_visible_to_reflector") != prepared.records_visible_to_reflector
        ):
            raise CoreEvolutionV2Error(
                "Core artifact does not carry exact dev-only benchmark provenance"
            )
        manifest = artifact.manifest
        if (
            not isinstance(manifest, dict)
            or manifest.get("record_count") != EXPECTED_DEV_TRAJECTORIES
            or manifest.get("reflected_record_count") != EXPECTED_DEV_TRAJECTORIES
            or not isinstance(manifest.get("success_count"), int)
            or not isinstance(manifest.get("failure_count"), int)
            or manifest["success_count"] + manifest["failure_count"] != EXPECTED_DEV_TRAJECTORIES
        ):
            raise CoreEvolutionV2Error(
                "Core artifact does not prove all 45 dev records reached the reflector"
            )
        execution = lineage.get("openevo_execution")
        if not isinstance(execution, dict):
            raise CoreEvolutionV2Error("Core worker output lacks store-owned execution lineage")
        if (
            execution.get("job_id") != prepared.job_id
            or execution.get("plan_id") != prepared.plan_id
            or execution.get("method_id") != METHOD_ID
            or execution.get("method_identity_digest") != prepared.method.method_identity_digest
        ):
            raise CoreEvolutionV2Error("Core artifact lineage does not match the immutable job")
        lineage_text = canonical_json(lineage)
        if any(
            marker in lineage_text
            for marker in (
                '"source_split":"test"',
                '"test_uid"',
                '"test_uids"',
            )
        ):
            raise CoreEvolutionV2Error("Core artifact lineage contains test provenance")

    def _artifact_lineage(self, artifact_id: str) -> dict[str, Any]:
        with self._store.connect() as connection:
            row = connection.execute(
                "SELECT lineage_json FROM artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise CoreEvolutionV2Error("Core artifact lineage is unavailable")
        try:
            lineage = json.loads(str(row["lineage_json"]))
        except json.JSONDecodeError as exc:
            raise CoreEvolutionV2Error("Core artifact lineage is not valid JSON") from exc
        if not isinstance(lineage, dict):
            raise CoreEvolutionV2Error("Core artifact lineage is not an object")
        return lineage

    @staticmethod
    def _execution_profile() -> EvolutionExecutionProfile:
        return EvolutionExecutionProfile(
            execution_mode="subscription",
            capture_mode="transcript",
            harness_id="codex",
        )

    @classmethod
    def _context_request(
        cls,
        artifact_id: str,
    ) -> ContextProjectionResolveRequest:
        return ContextProjectionResolveRequest(
            task_id="chembench4k-frozen-test-context-v2",
            instruction="Resolve the frozen text memory for a later test item.",
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
            execution_profile=cls._execution_profile(),
            destination_roots=RuntimeDestinationRoots(
                target_data="/openevo/session/evolution",
                harness_skills="/openevo/session/evolution/skills",
                harness_instruction="/workspace/repository",
            ),
            target_limits={
                METHOD_TARGET_ID: TargetConsumptionLimits(
                    max_artifacts=1,
                    max_text_chars=MAX_MEMORY_BYTES,
                    max_text_bytes=MAX_MEMORY_BYTES,
                    max_payload_bytes=MAX_MEMORY_BYTES,
                    max_adapters=0,
                )
            },
        )


def _resolved_text_memory(
    context: MaterializedContext,
    *,
    expected_artifact_id: str,
) -> str:
    if context.selection.artifact_ids != (expected_artifact_id,):
        raise CoreEvolutionV2Error(
            "Core context did not select exactly the approved text-memory artifact"
        )
    matches = tuple(
        projection
        for projection in context.projections
        if projection.target_id == METHOD_TARGET_ID
    )
    if len(matches) != 1:
        raise CoreEvolutionV2Error("Core context did not produce one text-memory projection")
    projection = matches[0]
    if projection.artifact_ids != (expected_artifact_id,) or len(projection.instructions) != 1:
        raise CoreEvolutionV2Error(
            "Core text-memory projection has unexpected artifact provenance"
        )
    text = projection.instructions[0].text
    if not text:
        raise CoreEvolutionV2Error("Core resolved text memory is empty")
    return text


def _safe_core_file_path(uri: str, *, allowed_root: Path) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise CoreEvolutionV2Error("Core artifact does not use a local file URI")
    path = Path(unquote(parsed.path))
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(allowed_root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise CoreEvolutionV2Error(
            "Core artifact payload is outside the managed artifact root"
        ) from exc
    return resolved


def _read_bounded_regular_file(path: Path, *, maximum: int) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise CoreEvolutionV2Error("Core artifact payload is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_size > maximum
    ):
        raise CoreEvolutionV2Error(
            "Core artifact payload violates the bounded regular-file policy"
        )
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise CoreEvolutionV2Error("Core artifact payload is unreadable") from exc
    if len(payload) != metadata.st_size:
        raise CoreEvolutionV2Error("Core artifact payload changed while reading")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CoreEvolutionV2Error("Core artifact payload is not valid UTF-8") from exc
    return payload


def _wait_for_core_ingest_tick(previous_timestamp: str) -> None:
    deadline = time.monotonic() + 2.5
    while utc_now_iso() <= previous_timestamp:
        if time.monotonic() >= deadline:
            raise CoreEvolutionV2Error(
                "Core clock did not advance for deterministic dataset ordering"
            )
        time.sleep(0.01)


def _read_private_reflector_records(
    path: Path,
) -> tuple[list[dict[str, Any]], str]:
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_size > 64 * 1024 * 1024
        ):
            raise OSError
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise CoreEvolutionV2Error("private reflector input is unavailable") from exc
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CoreEvolutionV2Error("private reflector input is invalid") from exc
        if (
            not isinstance(value, dict)
            or canonical_json(value) != line
            or value.get("source_split") != "dev"
        ):
            raise CoreEvolutionV2Error("private reflector input is invalid")
        records.append(value)
    if (
        len(records) != EXPECTED_DEV_TRAJECTORIES
        or len({record.get("uid") for record in records}) != EXPECTED_DEV_TRAJECTORIES
    ):
        raise CoreEvolutionV2Error("private reflector input is not exactly 45 unique records")
    return records, canonical_digest(records)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise CoreEvolutionV2Error("Core manifest is unreadable") from exc
    return digest.hexdigest()


def build_maintainer_framework_bundle_v2(
    *,
    repository_root: str | Path,
    output_directory: str | Path,
    python_executable: str | Path | None = None,
) -> MaintainerFrameworkBundleV2:
    """Build a pinned Core wheel and sibling framework lock without network.

    The resulting lock is meant to be reloaded from an environment where this
    wheel is the one and only non-editable ``openevo`` installation.  The Core
    verifier intentionally rejects the current source/editable environment, so
    this helper does not claim that merely building the bundle verifies the
    running interpreter.
    """

    repository = Path(repository_root).resolve(strict=True)
    if not repository.is_dir() or not (repository / "pyproject.toml").is_file():
        raise CoreEvolutionV2Error("OpenEvo repository root is invalid")
    output = Path(output_directory).resolve()
    if output.exists():
        raise CoreEvolutionV2Error("framework bundle output directory already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Keep the virtual-environment launcher path intact.  Resolving its symlink
    # to /usr/bin/python would silently drop the build backend installed in the
    # selected environment and can produce an ``UNKNOWN`` wheel.
    interpreter = Path(
        os.path.abspath(
            os.fspath(python_executable if python_executable is not None else sys.executable)
        )
    )
    if not interpreter.is_file():
        raise CoreEvolutionV2Error("framework bundle Python executable is invalid")

    with tempfile.TemporaryDirectory(
        prefix=".chembench4k-framework-v2-",
        dir=output.parent,
    ) as temp_value:
        temp = Path(temp_value)
        completed = subprocess.run(
            (
                os.fspath(interpreter),
                "-m",
                "pip",
                "--disable-pip-version-check",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--wheel-dir",
                os.fspath(temp),
                os.fspath(repository),
            ),
            cwd=repository,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=600,
            env={
                **os.environ,
                "PIP_NO_INDEX": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            },
        )
        if completed.returncode != 0:
            raise CoreEvolutionV2Error("offline OpenEvo wheel build failed")
        wheels = tuple(sorted(temp.glob("openevo-*.whl")))
        if len(wheels) != 1:
            raise CoreEvolutionV2Error(
                "offline OpenEvo wheel build did not produce exactly one wheel"
            )
        wheel = wheels[0]
        digest = _sha256_file(wheel)
        lock = {
            "schema_version": "1",
            "distribution": "openevo",
            "distribution_version": openevo_version,
            "distribution_digest": digest,
            "wheel_filename": wheel.name,
        }
        staging = temp / "bundle"
        staging.mkdir(mode=0o700)
        final_wheel = staging / wheel.name
        final_wheel.write_bytes(wheel.read_bytes())
        final_wheel.chmod(0o600)
        lock_path = staging / "framework-lock.json"
        lock_path.write_text(
            canonical_json(lock) + "\n",
            encoding="utf-8",
        )
        lock_path.chmod(0o600)
        if _sha256_file(final_wheel) != digest:
            raise CoreEvolutionV2Error("framework wheel changed while publishing the bundle")
        staging.rename(output)
        final_wheel = output / wheel.name
        lock_path = output / "framework-lock.json"
    return MaintainerFrameworkBundleV2(
        wheel_path=os.fspath(final_wheel),
        framework_lock_path=os.fspath(lock_path),
        distribution_version=openevo_version,
        distribution_digest=digest,
    )


__all__ = [
    "CoreArtifactValidationReceiptV2",
    "CoreDevTrajectoryV2",
    "CoreEvolutionExecutionV2",
    "CoreEvolutionJobV2",
    "CoreEvolutionV2Error",
    "DATASET_REPOSITORY",
    "DATASET_REVISION",
    "EXPECTED_DEV_TRAJECTORIES",
    "FrozenCoreTextMemoryV2",
    "MaintainerFrameworkBundleV2",
    "METHOD_ID",
    "MODEL",
    "OpenEvoTextMemoryLifecycleV2",
    "PROTOCOL_ID",
    "RegisteredMethodEvidenceV2",
    "TextMemoryArtifactValidatorV2",
    "build_maintainer_framework_bundle_v2",
    "build_core_dev_trajectories_v2",
]
