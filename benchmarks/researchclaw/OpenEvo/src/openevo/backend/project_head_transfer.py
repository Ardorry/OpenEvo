"""Pure builders for append-only project-head authority transfer."""

from __future__ import annotations

import hashlib
import json

from openevo.backend.contracts.v2 import models as m2


def build_cross_project_restored_head(
    *,
    predecessor: m2.ProjectHeadRefV2,
    source: m2.ProjectHeadRefV2,
    restore_transition_id: str,
) -> m2.ProjectHeadRefV2:
    """Copy source evolution authority onto an unused destination project.

    The destination keeps its own immutable workspace and effective execution
    snapshot.  Evolution/runtime identities are re-bound to the destination,
    while their hashes retain the exact source authority in the composition.
    """

    if source.project_id == predecessor.project_id:
        raise ValueError("cross-project restore requires independent projects")
    evolution_payload = {
        "artifact_count": source.evolution_revision.artifact_count,
        "destination_project_id": predecessor.project_id,
        "restore_transition_id": restore_transition_id,
        "source_evolution_revision": source.evolution_revision.model_dump(
            mode="json"
        ),
        "source_project_head_id": source.project_head_id,
        "source_project_head_manifest_sha256": source.manifest_sha256,
    }
    evolution_sha256 = hashlib.sha256(
        json.dumps(
            evolution_payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    evolution = m2.EvolutionRevisionRefV2(
        evolution_revision_id=f"evolution-revision-{evolution_sha256}",
        project_id=predecessor.project_id,
        manifest_sha256=evolution_sha256,
        artifact_count=source.evolution_revision.artifact_count,
    )
    runtime_payload = {
        "destination_project_id": predecessor.project_id,
        "evolution_revision_id": evolution.evolution_revision_id,
        "evolution_revision_manifest_sha256": evolution.manifest_sha256,
        "registry_sha256": source.registry_sha256,
        "restore_transition_id": restore_transition_id,
        "source_project_head_id": source.project_head_id,
        "source_runtime_context_snapshot": (
            source.runtime_context_snapshot.model_dump(mode="json")
        ),
    }
    runtime_sha256 = hashlib.sha256(
        json.dumps(
            runtime_payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    runtime = m2.RuntimeContextSnapshotRefV2(
        runtime_context_snapshot_id=f"runtime-context-{runtime_sha256}",
        project_id=predecessor.project_id,
        evolution_revision_id=evolution.evolution_revision_id,
        evolution_revision_manifest_sha256=evolution.manifest_sha256,
        registry_sha256=source.registry_sha256,
        runtime_contract_sha256=(
            source.runtime_context_snapshot.runtime_contract_sha256
        ),
        manifest_sha256=runtime_sha256,
    )
    composition = {
        "effective_execution_snapshot": (
            predecessor.effective_execution_snapshot.model_dump(mode="json")
        ),
        "evolution_revision": evolution.model_dump(mode="json"),
        "generation": predecessor.generation + 1,
        "predecessor_project_head_id": predecessor.project_head_id,
        "project_id": predecessor.project_id,
        "registry_sha256": source.registry_sha256,
        "restore_source_project_head_id": source.project_head_id,
        "restore_source_project_id": source.project_id,
        "runtime_context_snapshot": runtime.model_dump(mode="json"),
        "successor_transition_id": restore_transition_id,
        "transition_outcome": "cross_project_historical_restore",
        "workspace_snapshot": predecessor.workspace_snapshot.model_dump(mode="json"),
    }
    manifest_sha256 = hashlib.sha256(
        json.dumps(
            composition,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return m2.ProjectHeadRefV2(
        project_head_id=f"project-head-{manifest_sha256}",
        project_id=predecessor.project_id,
        generation=predecessor.generation + 1,
        predecessor_project_head_id=predecessor.project_head_id,
        workspace_snapshot=predecessor.workspace_snapshot,
        evolution_revision=evolution,
        runtime_context_snapshot=runtime,
        effective_execution_snapshot=predecessor.effective_execution_snapshot,
        registry_sha256=source.registry_sha256,
        manifest_sha256=manifest_sha256,
    )


__all__ = ["build_cross_project_restored_head"]
