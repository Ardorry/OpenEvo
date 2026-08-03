"""Single-target, delayed-promotion Core path for Safe-Evolve V2.

The supervised Reflector produces a validated canonical projection.  This module
does not synthesize or edit that projection: it registers one run-private input
dataset, creates one plan-bound job against the verified executable registry,
executes it through Core's worker lifecycle, and verifies the resulting payload
while it is still unpromoted.  Promotion is a separate, idempotent operation
performed only after a later forward block passes the preregistered gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openevo.evolution.artifact_payloads import ArtifactPayloadService
from openevo.evolution.framework import (
    DescriptorKind,
    EvolutionExecutionProfile,
    EvolutionTargetSelection,
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
)
from openevo.evolution.planned_jobs import PlanBoundJobCreateRequest, PlannedInputBinding
from openevo.evolution.store import EvolutionStore
from openevo.evolution.worker import run_once

from openevo_chembench.chembench4k_dataset import normalize_benchmark_text
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    sha256_bytes,
    write_private_file,
)
from openevo_chembench.temperature_full_evolve_v1.core_evolution import _StoreWorkerClientV1
from openevo_chembench.temperature_safe_evolve_v2.artifacts import (
    CanonicalEntryV2,
    SafeArtifactStateV2,
    SelectedTarget,
    render_full_projection_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.config import PROTOCOL_ID

DATASET_MANIFEST_SCHEMA = "TemperatureSafeCoreDatasetManifestV2"
DATASET_RECORD_SCHEMA = "TemperatureSafeCoreDatasetRecordV2"
COORDINATOR_ID = "temperature_safe_single_target_core_v2"
VALIDATOR_ID = "temperature_safe_core_payload_validator_v2"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_CORE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,511}\Z", re.ASCII)
_UID = re.compile(r"\b[0-9a-f]{64}\b", re.ASCII)
_ANSWER_MAP = re.compile(
    r"\b(?:answer|option|choice|prediction)\s*(?:is|=|:|->|maps?\s+to)?\s*[ABCD]\b",
    re.IGNORECASE | re.ASCII,
)
_STANDALONE_CHOICE = re.compile(r"(?m)^\s*(?:[-*]\s*)?[ABCD][.)]?\s*$", re.ASCII)


class SafeCoreEvolutionError(RuntimeError):
    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True, repr=False)
class SafeCoreUpdateResultV2:
    state: SafeArtifactStateV2
    receipt: dict[str, object]

    def __repr__(self) -> str:
        return "SafeCoreUpdateResultV2(<private-artifact-content-redacted>)"


def create_safe_core_store_v2(
    *, db_path: Path, artifact_root: Path, registry: VerifiedExecutableRegistry
) -> EvolutionStore:
    """Create or reopen one owner-private fold-local Core store."""

    verified = require_verified_executable_registry(registry)
    if not db_path.is_absolute() or not artifact_root.is_absolute():
        raise TypeError("Core paths must be absolute")
    for directory in (db_path.parent, artifact_root):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        _require_private_directory(directory)
    store = EvolutionStore(
        db_path=db_path,
        artifact_root=artifact_root,
        executable_registry=verified,
    )
    store.initialize()
    if db_path.exists():
        db_path.chmod(0o600)
    return store


def read_safe_core_store_identity_v2(*, db_path: Path) -> str:
    """Read the exact fold-local Core store identity after initialization."""

    if not db_path.is_absolute() or not db_path.is_file():
        raise SafeCoreEvolutionError("SAFE_CORE_STORE_IDENTITY_DATABASE_MISSING")
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        row = connection.execute(
            "SELECT store_id, binding_state FROM store_identity WHERE singleton = 1"
        ).fetchone()
        extra = connection.execute("SELECT COUNT(*) FROM store_identity").fetchone()
        connection.close()
    except sqlite3.Error as exc:
        raise SafeCoreEvolutionError("SAFE_CORE_STORE_IDENTITY_UNAVAILABLE") from exc
    if (
        row is None
        or extra is None
        or int(extra[0]) != 1
        or re.fullmatch(r"store_[0-9a-f]{16}", str(row[0])) is None
        or row[1] != "bound"
    ):
        raise SafeCoreEvolutionError("SAFE_CORE_STORE_IDENTITY_INVALID")
    return str(row[0])


def execute_provisional_update_v2(
    *,
    store: EvolutionStore,
    registry: VerifiedExecutableRegistry,
    run_id: str,
    fold_id: str,
    batch_index: int,
    selected_target: SelectedTarget,
    predecessor: SafeArtifactStateV2,
    entries: tuple[CanonicalEntryV2, ...],
    source_packet_sha256: str,
    prior_evidence_sha256: str,
    reflector_receipt_sha256: str,
    forbidden_normalized_questions: tuple[str, ...],
    evidence_root: Path,
) -> SafeCoreUpdateResultV2:
    """Create and validate one unpromoted selected-target successor."""

    verified = require_verified_executable_registry(registry)
    _validate_update_inputs(
        store=store,
        run_id=run_id,
        fold_id=fold_id,
        batch_index=batch_index,
        selected_target=selected_target,
        predecessor=predecessor,
        entries=entries,
        source_packet_sha256=source_packet_sha256,
        prior_evidence_sha256=prior_evidence_sha256,
        reflector_receipt_sha256=reflector_receipt_sha256,
        evidence_root=evidence_root,
    )
    projection = render_full_projection_v2(
        selected_target=selected_target,
        entries=tuple(entry for entry in entries if entry.status != "retired"),
    )
    core_projection = projection or _empty_core_projection(selected_target)
    projection_bytes = projection.encode("utf-8")
    dataset = _ensure_dataset(
        store=store,
        run_id=run_id,
        fold_id=fold_id,
        batch_index=batch_index,
        selected_target=selected_target,
        projection=core_projection,
        source_packet_sha256=source_packet_sha256,
        prior_evidence_sha256=prior_evidence_sha256,
        reflector_receipt_sha256=reflector_receipt_sha256,
        predecessor_state_sha256=predecessor.digest,
        evidence_root=evidence_root,
    )
    request, request_receipt = _job_request(
        registry=verified,
        run_id=run_id,
        fold_id=fold_id,
        batch_index=batch_index,
        selected_target=selected_target,
        predecessor=predecessor,
        projection=core_projection,
        source_packet_sha256=source_packet_sha256,
        prior_evidence_sha256=prior_evidence_sha256,
        reflector_receipt_sha256=reflector_receipt_sha256,
        dataset=dataset,
    )
    try:
        created = store.create_plan_bound_job(request, snapshot=verified.snapshot)
    except Exception as exc:
        raise SafeCoreEvolutionError("SAFE_CORE_JOB_CREATE_FAILED") from exc
    if (
        type(created) is not JobCreateResponse
        or _CORE_ID.fullmatch(created.job_id) is None
        or created.state
        not in {JobState.PENDING, JobState.CLAIMED, JobState.RUNNING, JobState.SUCCEEDED}
    ):
        raise SafeCoreEvolutionError("SAFE_CORE_JOB_CREATE_INVALID")
    job_id = created.job_id
    _persist_exact(
        evidence_root / "job_request_receipt.json",
        canonical_json_bytes({**request_receipt, "job_id": job_id}),
    )
    before = store.get_internal_job_result(job_id)
    if before.get("state") == str(JobState.PENDING):
        claimed = run_once(
            _StoreWorkerClientV1(store),
            worker_id=f"temperature-safe-core-{fold_id.lower()}-b{batch_index}",
            capabilities=[request.job_type],
            artifact_root=store.files.root,
            lease_seconds=600,
            executable_registry=verified,
        )
        if not claimed:
            raise SafeCoreEvolutionError("SAFE_CORE_JOB_NOT_CLAIMABLE")
    elif before.get("state") != str(JobState.SUCCEEDED):
        raise SafeCoreEvolutionError("SAFE_CORE_JOB_NOT_RECOVERABLY_PENDING")
    result = store.get_internal_job_result(job_id)
    observation = _validate_job_output(
        store=store,
        request=request,
        request_receipt=request_receipt,
        job_id=job_id,
        selected_target=selected_target,
        dataset=dataset,
        projection=core_projection,
        forbidden_normalized_questions=forbidden_normalized_questions,
        result=result,
    )
    validation_receipt = {
        "schema_version": "TemperatureSafeCoreValidationReceiptV2",
        "status": "PASS_UNPROMOTED",
        "validator_id": VALIDATOR_ID,
        "run_id": run_id,
        "fold_id": fold_id,
        "batch_index": batch_index,
        "selected_target": selected_target,
        "predecessor_state_sha256": predecessor.digest,
        "source_packet_sha256": source_packet_sha256,
        "prior_evidence_sha256": prior_evidence_sha256,
        "reflector_receipt_sha256": reflector_receipt_sha256,
        "dataset_artifact_id": dataset["artifact_id"],
        "dataset_receipt_sha256": dataset["receipt_sha256"],
        "job_id": job_id,
        "artifact_id": observation["artifact_id"],
        "projection_sha256": hashlib.sha256(projection_bytes).hexdigest(),
        "projection_utf8_bytes": len(projection_bytes),
        "core_transport_projection_sha256": hashlib.sha256(
            core_projection.encode("utf-8")
        ).hexdigest(),
        "core_transport_projection_utf8_bytes": len(core_projection.encode("utf-8")),
        "core_payload_sha256": observation["payload_sha256"],
        "core_payload_utf8_bytes": observation["payload_utf8_bytes"],
        "payload_manifest_sha256": observation["payload_manifest_sha256"],
        "registry_sha256": verified.snapshot.registry_digest,
        "promoted": False,
        "selected_target_core_job_count": 1,
    }
    validation_sha = sha256_bytes(canonical_json_bytes(validation_receipt))
    state = SafeArtifactStateV2(
        run_id=run_id,
        fold_id=fold_id,
        selected_target=selected_target,
        state_kind="provisional",
        batch_index=batch_index,
        predecessor_state_sha256=predecessor.digest,
        prior_evidence_sha256=prior_evidence_sha256,
        source_packet_sha256=source_packet_sha256,
        entries=entries,
        projection=projection,
        projection_sha256=hashlib.sha256(projection_bytes).hexdigest(),
        projection_utf8_bytes=len(projection_bytes),
        core_artifact_id=str(observation["artifact_id"]),
        core_job_id=job_id,
        core_validation_receipt_sha256=validation_sha,
        core_payload_sha256=str(observation["payload_sha256"]),
        core_payload_utf8_bytes=int(observation["payload_utf8_bytes"]),
        promoted=False,
    )
    receipt = {
        **validation_receipt,
        "validation_receipt_sha256": validation_sha,
        "provisional_state_sha256": state.digest,
    }
    _persist_exact(evidence_root / "core_validation_receipt.json", canonical_json_bytes(receipt))
    _persist_exact(
        evidence_root / "provisional_state.json",
        canonical_json_bytes(state.model_dump(mode="json")),
    )
    return SafeCoreUpdateResultV2(state=state, receipt=receipt)


def _empty_core_projection(selected_target: SelectedTarget) -> str:
    if selected_target == "text_memory":
        return (
            "# Temperature Prediction Memory\n\n"
            "No runtime entries are active. The sparse retriever must inject nothing.\n"
        )
    return (
        "# Temperature Prediction Skill\n\n"
        "No runtime entries are active. The sparse retriever must inject nothing.\n"
    )


def promote_safe_state_v2(
    *, store: EvolutionStore, state: SafeArtifactStateV2, evidence_path: Path
) -> SafeArtifactStateV2:
    """Idempotently promote one forward-validated Core artifact and state."""

    if state.state_kind != "provisional" or state.promoted or state.core_artifact_id is None:
        raise SafeCoreEvolutionError("SAFE_CORE_PROMOTION_STATE_INVALID")
    artifact = store.get_artifact(state.core_artifact_id)
    if not artifact.promoted:
        artifact = store.update_artifact_promotion(state.core_artifact_id, promoted=True)
    if (
        type(artifact) is not ArtifactResponse
        or artifact.artifact_id != state.core_artifact_id
        or artifact.type is not ArtifactType(state.selected_target)
        or artifact.state is not ArtifactState.ACTIVE
        or not artifact.promoted
    ):
        raise SafeCoreEvolutionError("SAFE_CORE_PROMOTION_INVALID")
    entries = tuple(
        entry.model_copy(update={"status": "active"}) if entry.status == "provisional" else entry
        for entry in state.entries
    )
    promoted = SafeArtifactStateV2.model_validate(
        {
            **state.model_dump(mode="python", exclude={"state_kind", "entries", "promoted"}),
            "state_kind": "active",
            "entries": entries,
            "promoted": True,
        }
    )
    receipt = {
        "schema_version": "TemperatureSafeCorePromotionReceiptV2",
        "status": "PROMOTED_AFTER_FORWARD_GATE",
        "provisional_state_sha256": state.digest,
        "active_state_sha256": promoted.digest,
        "artifact_id": state.core_artifact_id,
        "artifact_payload_sha256": state.core_payload_sha256,
        "artifact_payload_utf8_bytes": state.core_payload_utf8_bytes,
    }
    _persist_exact(evidence_path, canonical_json_bytes(receipt))
    return promoted


def _validate_update_inputs(**values: Any) -> None:
    store = values["store"]
    predecessor = values["predecessor"]
    entries = values["entries"]
    if type(store) is not EvolutionStore or type(predecessor) is not SafeArtifactStateV2:
        raise TypeError("Core update requires exact store and predecessor")
    if (
        values["fold_id"] not in {"R0", "R1", "R2", "R3"}
        or values["batch_index"] not in {1, 2, 3, 4}
        or predecessor.fold_id != values["fold_id"]
        or predecessor.run_id != values["run_id"]
        or predecessor.selected_target != values["selected_target"]
        or predecessor.state_kind not in {"generation_zero", "active"}
        or type(entries) is not tuple
        or any(type(entry) is not CanonicalEntryV2 for entry in entries)
        or not values["evidence_root"].is_absolute()
    ):
        raise SafeCoreEvolutionError("SAFE_CORE_UPDATE_INPUT_INVALID")
    for key in (
        "source_packet_sha256",
        "prior_evidence_sha256",
        "reflector_receipt_sha256",
    ):
        if _SHA256.fullmatch(str(values[key])) is None:
            raise SafeCoreEvolutionError("SAFE_CORE_UPDATE_DIGEST_INVALID")


def _ensure_dataset(
    *,
    store: EvolutionStore,
    run_id: str,
    fold_id: str,
    batch_index: int,
    selected_target: SelectedTarget,
    projection: str,
    source_packet_sha256: str,
    prior_evidence_sha256: str,
    reflector_receipt_sha256: str,
    predecessor_state_sha256: str,
    evidence_root: Path,
) -> dict[str, object]:
    token = hashlib.sha256(run_id.encode()).hexdigest()[:16]
    directory = (
        store.files.root
        / "input_datasets"
        / PROTOCOL_ID
        / token
        / fold_id
        / f"batch_{batch_index}"
    )
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    manifest_path = directory / "manifest.json"
    records_path = directory / "records.jsonl"
    name = f"Temperature Safe-Evolve {fold_id} batch {batch_index} selected target dataset"
    task_id = f"temperature-safe-{fold_id.lower()}-batch-{batch_index}"
    session_id = f"temperature-safe-reflector-{fold_id.lower()}-batch-{batch_index}"
    record = {
        "schema_version": DATASET_RECORD_SCHEMA,
        "task_id": task_id,
        "session_id": session_id,
        "status": "accepted_selected_target_projection",
        "reward": None,
        "payload": {"summary": projection},
    }
    records_bytes = canonical_json_bytes(record)
    manifest = {
        "schema_version": DATASET_MANIFEST_SCHEMA,
        "name": name,
        "records_uri": records_path.as_uri(),
        "record_count": 1,
        "privacy": "run_private",
        "run_id": run_id,
        "fold_id": fold_id,
        "batch_index": batch_index,
        "selected_target": selected_target,
        "source_packet_sha256": source_packet_sha256,
        "prior_evidence_sha256": prior_evidence_sha256,
        "reflector_receipt_sha256": reflector_receipt_sha256,
        "predecessor_state_sha256": predecessor_state_sha256,
        "projection_sha256": hashlib.sha256(projection.encode()).hexdigest(),
        "records_sha256": hashlib.sha256(records_bytes).hexdigest(),
    }
    manifest_bytes = canonical_json_bytes(manifest)
    request = ArtifactRegisterRequest(
        type=ArtifactType.DATASET,
        name=name,
        uri=manifest_path.as_uri(),
        manifest={
            "schema_version": DATASET_MANIFEST_SCHEMA,
            "records_uri": records_path.as_uri(),
            "record_count": 1,
            "privacy": "run_private",
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "records_sha256": hashlib.sha256(records_bytes).hexdigest(),
        },
        lineage={
            "protocol_id": PROTOCOL_ID,
            "coordinator_id": COORDINATOR_ID,
            "run_id": run_id,
            "fold_id": fold_id,
            "batch_index": batch_index,
            "selected_target": selected_target,
            "source_packet_sha256": source_packet_sha256,
            "prior_evidence_sha256": prior_evidence_sha256,
            "reflector_receipt_sha256": reflector_receipt_sha256,
            "predecessor_state_sha256": predecessor_state_sha256,
        },
        compatibility={"agent_harness": ["codex"], "base_model": ["gpt-5.5"]},
        tags=[PROTOCOL_ID, "safe-single-target-dataset", fold_id, f"batch-{batch_index}"],
        promoted=False,
    )
    intent = {
        "schema_version": "TemperatureSafeCoreDatasetRegistrationIntentV2",
        "artifact_request_sha256": sha256_bytes(
            canonical_json_bytes(request.model_dump(mode="json"))
        ),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "records_sha256": hashlib.sha256(records_bytes).hexdigest(),
        "manifest_uri": manifest_path.as_uri(),
        "records_uri": records_path.as_uri(),
    }
    _persist_exact(
        evidence_root / "dataset_registration_intent.json", canonical_json_bytes(intent)
    )
    _persist_exact(records_path, records_bytes)
    _persist_exact(manifest_path, manifest_bytes)
    rows: list[Any]
    with store.connect() as connection:
        rows = connection.execute(
            "SELECT artifact_id, manifest_json, lineage_json, compatibility_json, tags_json, "
            "scores_json, type, name, uri, state, promoted, staging_job_id "
            "FROM artifacts WHERE name = ? OR uri = ? ORDER BY artifact_id LIMIT 3",
            (name, request.uri),
        ).fetchall()
    if len(rows) > 1:
        raise SafeCoreEvolutionError("SAFE_CORE_DATASET_REGISTRATION_AMBIGUOUS")
    if not rows:
        artifact = store.register_artifact(request)
    else:
        row = rows[0]
        expected_json = {
            "manifest_json": request.manifest,
            "lineage_json": request.lineage,
            "compatibility_json": request.compatibility,
            "tags_json": request.tags,
            "scores_json": request.scores,
        }
        if (
            row["type"] != str(ArtifactType.DATASET)
            or row["name"] != name
            or row["uri"] != request.uri
            or row["state"] != str(ArtifactState.ACTIVE)
            or row["promoted"] != 0
            or row["staging_job_id"] is not None
            or any(json.loads(row[key]) != expected for key, expected in expected_json.items())
        ):
            raise SafeCoreEvolutionError("SAFE_CORE_DATASET_REGISTRATION_DRIFT")
        artifact = store.get_artifact(str(row["artifact_id"]))
    if (
        artifact.type is not ArtifactType.DATASET
        or artifact.uri != request.uri
        or artifact.name != name
        or artifact.manifest != request.manifest
        or artifact.promoted
    ):
        raise SafeCoreEvolutionError("SAFE_CORE_DATASET_REGISTRATION_INVALID")
    receipt = {
        "schema_version": "TemperatureSafeCoreDatasetBindingV2",
        "artifact_id": artifact.artifact_id,
        "artifact_name": name,
        "manifest_uri": manifest_path.as_uri(),
        "records_uri": records_path.as_uri(),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "manifest_utf8_bytes": len(manifest_bytes),
        "records_sha256": hashlib.sha256(records_bytes).hexdigest(),
        "records_utf8_bytes": len(records_bytes),
        "task_id": task_id,
        "session_id": session_id,
    }
    receipt["receipt_sha256"] = sha256_bytes(canonical_json_bytes(receipt))
    _persist_exact(evidence_root / "dataset_binding.json", canonical_json_bytes(receipt))
    return receipt


def _job_request(
    *,
    registry: VerifiedExecutableRegistry,
    run_id: str,
    fold_id: str,
    batch_index: int,
    selected_target: SelectedTarget,
    predecessor: SafeArtifactStateV2,
    projection: str,
    source_packet_sha256: str,
    prior_evidence_sha256: str,
    reflector_receipt_sha256: str,
    dataset: dict[str, object],
) -> tuple[PlanBoundJobCreateRequest, dict[str, object]]:
    snapshot = registry.snapshot
    descriptor = snapshot.methods.get(selected_target)
    expected_bindings = (
        ("current_dataset",)
        if selected_target == "text_memory"
        else ("current_dataset", "prior_target_artifacts")
    )
    if (
        descriptor is None
        or selected_target not in registry.method_handles
        or descriptor.target_id != selected_target
        or descriptor.output_artifact_types != (selected_target,)
        or tuple(binding.binding_id for binding in descriptor.input_bindings) != expected_bindings
        or snapshot.identity_digest_for(DescriptorKind.METHOD, selected_target)
        != snapshot.identity_digests[f"method:{selected_target}"]
    ):
        raise SafeCoreEvolutionError("SAFE_CORE_REGISTERED_METHOD_INVALID")
    config = {} if selected_target == "text_memory" else {"skill_markdown": projection}
    selection = EvolutionTargetSelection(
        target_id=selected_target,
        enabled=True,
        method_id=selected_target,
        config=config,
    )
    identity = canonical_digest(
        {
            "protocol_id": PROTOCOL_ID,
            "coordinator_id": COORDINATOR_ID,
            "run_id": run_id,
            "fold_id": fold_id,
            "batch_index": batch_index,
            "selected_target": selected_target,
            "predecessor_state_sha256": predecessor.digest,
            "predecessor_artifact_id": predecessor.core_artifact_id,
            "source_packet_sha256": source_packet_sha256,
            "prior_evidence_sha256": prior_evidence_sha256,
            "reflector_receipt_sha256": reflector_receipt_sha256,
            "dataset_receipt_sha256": dataset["receipt_sha256"],
            "projection_sha256": hashlib.sha256(projection.encode()).hexdigest(),
            "projection_utf8_bytes": len(projection.encode("utf-8")),
            "registry_sha256": snapshot.registry_digest,
        }
    )
    plan = snapshot.compile_plan(
        plan_id=f"temperature-safe-{fold_id.lower()}-b{batch_index}-{selected_target}-{identity[:20]}",
        selections=(selection,),
        profile=EvolutionExecutionProfile(
            execution_mode="self_deployed", capture_mode="transcript", harness_id="codex"
        ),
    )
    lineage = {
        "schema_version": "TemperatureSafeCoreTargetLineageV2",
        "protocol_id": PROTOCOL_ID,
        "coordinator_id": COORDINATOR_ID,
        "run_id": run_id,
        "fold_id": fold_id,
        "batch_index": batch_index,
        "selected_target": selected_target,
        "predecessor_state_sha256": predecessor.digest,
        "predecessor_artifact_id": predecessor.core_artifact_id,
        "source_packet_sha256": source_packet_sha256,
        "prior_evidence_sha256": prior_evidence_sha256,
        "reflector_receipt_sha256": reflector_receipt_sha256,
        "dataset_receipt_sha256": dataset["receipt_sha256"],
        "projection_sha256": hashlib.sha256(projection.encode()).hexdigest(),
        "projection_utf8_bytes": len(projection.encode("utf-8")),
    }
    bindings = [
        PlannedInputBinding(
            binding_id="current_dataset", artifact_ids=(str(dataset["artifact_id"]),)
        )
    ]
    if selected_target == "skill_bundle":
        bindings.append(
            PlannedInputBinding(
                binding_id="prior_target_artifacts",
                artifact_ids=(
                    () if predecessor.core_artifact_id is None else (predecessor.core_artifact_id,)
                ),
            )
        )
    request = PlanBoundJobCreateRequest(
        plan=plan,
        target_id=selected_target,
        job_type=(
            f"chembench.temperature_safe_evolve_v2.{fold_id.lower()}."
            f"batch-{batch_index}.{selected_target}.{identity[:20]}"
        ),
        input_bindings=tuple(bindings),
        core_config={
            "name": f"Temperature Safe-Evolve {fold_id} batch {batch_index} {selected_target}",
            "promoted": False,
            "lineage": lineage,
            "compatibility": {
                "agent_harness": ["codex"],
                "base_model": ["gpt-5.5"],
                "task_tags": [PROTOCOL_ID],
            },
            "tags": [PROTOCOL_ID, COORDINATOR_ID, fold_id, f"batch-{batch_index}"],
        },
    )
    return request, {
        "schema_version": "TemperatureSafeCoreJobRequestReceiptV2",
        "request_sha256": sha256_bytes(canonical_json_bytes(request.model_dump(mode="json"))),
        "plan_id": plan.plan_id,
        "plan_sha256": canonical_digest(plan),
        "registry_sha256": plan.registry_snapshot_digest,
        "full_registry_sha256": snapshot.registry_digest,
        "method_identity_sha256": plan.selections[0].method_identity_digest,
        "lineage": lineage,
    }


def _validate_job_output(
    *,
    store: EvolutionStore,
    request: PlanBoundJobCreateRequest,
    request_receipt: dict[str, object],
    job_id: str,
    selected_target: SelectedTarget,
    dataset: dict[str, object],
    projection: str,
    forbidden_normalized_questions: tuple[str, ...],
    result: Mapping[str, object],
) -> dict[str, object]:
    if (
        result.get("job_id") != job_id
        or result.get("state") != str(JobState.SUCCEEDED)
        or result.get("error") is not None
        or type(result.get("artifact_ids")) is not list
        or len(result["artifact_ids"]) != 1
        or type(result.get("outputs")) is not list
        or len(result["outputs"]) != 1
    ):
        raise SafeCoreEvolutionError("SAFE_CORE_JOB_NOT_CLOSED_SUCCEEDED")
    output = result["outputs"][0]
    if not isinstance(output, Mapping):
        raise SafeCoreEvolutionError("SAFE_CORE_JOB_OUTPUT_INVALID")
    artifact_id = result["artifact_ids"][0]
    artifact = store.get_artifact(str(artifact_id))
    if (
        output.get("artifact_id") != artifact_id
        or output.get("type") != selected_target
        or output.get("promoted") is not False
        or output.get("payload_file_count") != 1
        or artifact.type is not ArtifactType(selected_target)
        or artifact.state is not ArtifactState.ACTIVE
        or artifact.promoted
    ):
        raise SafeCoreEvolutionError("SAFE_CORE_JOB_OUTPUT_INVALID")
    output_lineage = output.get("lineage")
    if not isinstance(output_lineage, Mapping):
        raise SafeCoreEvolutionError("SAFE_CORE_JOB_LINEAGE_INVALID")
    execution = output_lineage.get("openevo_execution")
    expected_execution = {
        "job_id": job_id,
        "plan_id": request.plan.plan_id,
        "plan_digest": request_receipt["plan_sha256"],
        "target_id": selected_target,
        "method_id": selected_target,
        "method_identity_digest": request_receipt["method_identity_sha256"],
        "registry_snapshot_digest": request_receipt["registry_sha256"],
        "declared_output_artifact_types": [selected_target],
    }
    if not isinstance(execution, Mapping) or any(
        execution.get(key) != value for key, value in expected_execution.items()
    ):
        raise SafeCoreEvolutionError("SAFE_CORE_JOB_LINEAGE_INVALID")
    expected_inputs = {
        binding.binding_id: list(binding.artifact_ids) for binding in request.input_bindings
    }
    actual_inputs = {
        str(binding.get("binding_id")): binding.get("artifact_ids")
        for binding in execution.get("input_bindings", [])
        if isinstance(binding, Mapping)
    }
    if actual_inputs != expected_inputs:
        raise SafeCoreEvolutionError("SAFE_CORE_JOB_LINEAGE_INVALID")
    content_path = "memory.md" if selected_target == "text_memory" else "SKILL.md"
    manifest_field = "content_path" if selected_target == "text_memory" else "entrypoint"
    if artifact.manifest.get(manifest_field) != content_path:
        raise SafeCoreEvolutionError("SAFE_CORE_JOB_CONTENT_PATH_INVALID")
    try:
        with ArtifactPayloadService(store.files.root) as payloads:
            snapshot = payloads.issue_snapshot(
                artifact_id=artifact.artifact_id,
                artifact_type=selected_target,
                name=artifact.name,
                uri=artifact.uri,
                manifest=artifact.manifest,
                scores=artifact.scores,
                rank_index=0,
            )
            if (
                len(snapshot.payload_entries) != 1
                or snapshot.payload_entries[0].relative_path != content_path
                or snapshot.payload_manifest_digest != output.get("payload_manifest_digest")
            ):
                raise ValueError("payload inventory mismatch")
            content = payloads.read_utf8_prefix(
                snapshot.payload_handle, content_path, max_chars=8192, max_bytes=8192
            )
    except Exception as exc:
        raise SafeCoreEvolutionError("SAFE_CORE_PAYLOAD_READ_INVALID") from exc
    encoded = content.encode("utf-8")
    if (
        not content.endswith("\n")
        or len(encoded) != output.get("payload_byte_size")
        or len(encoded) > 8192
        or "\x00" in content
        or _UID.search(content)
        or _ANSWER_MAP.search(content)
        or _STANDALONE_CHOICE.search(content)
    ):
        raise SafeCoreEvolutionError("SAFE_CORE_PAYLOAD_INVALID")
    if selected_target == "skill_bundle":
        if content != projection:
            raise SafeCoreEvolutionError("SAFE_CORE_PROJECTION_BINDING_INVALID")
    else:
        expected = "\n".join(
            [
                f"# Memory from {dataset['artifact_name']}",
                "",
                f"- job_id: {job_id}",
                f"- dataset_artifact_id: {dataset['artifact_id']}",
                f"- dataset_name: {dataset['artifact_name']}",
                "- record_count: 1",
                "",
                "## Records",
                (
                    f"- task={dataset['task_id']} session={dataset['session_id']} "
                    "status=accepted_selected_target_projection reward=None"
                ),
                f"  - summary: {projection.strip()}",
                "",
            ]
        )
        if content != expected:
            raise SafeCoreEvolutionError("SAFE_CORE_PROJECTION_BINDING_INVALID")
    normalized = normalize_benchmark_text(content)
    if any(
        len(question) >= 24 and question in normalized
        for question in forbidden_normalized_questions
    ):
        raise SafeCoreEvolutionError("SAFE_CORE_QUESTION_LEAKAGE")
    return {
        "artifact_id": artifact.artifact_id,
        "payload_sha256": hashlib.sha256(encoded).hexdigest(),
        "payload_utf8_bytes": len(encoded),
        "payload_manifest_sha256": snapshot.payload_manifest_digest,
    }


def _persist_exact(path: Path, payload: bytes) -> None:
    if path.exists():
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or path.read_bytes() != payload
        ):
            raise SafeCoreEvolutionError("SAFE_CORE_IMMUTABLE_EVIDENCE_DRIFT")
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    write_private_file(path, payload, replace=False)


def _require_private_directory(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise SafeCoreEvolutionError("SAFE_CORE_PRIVATE_DIRECTORY_INVALID")


__all__ = [
    "COORDINATOR_ID",
    "SafeCoreEvolutionError",
    "SafeCoreUpdateResultV2",
    "create_safe_core_store_v2",
    "execute_provisional_update_v2",
    "promote_safe_state_v2",
]
