from __future__ import annotations

from pathlib import Path

import pytest

from openevo.backend.contracts.v2.models import (
    EffectiveExecutionSnapshotRefV2,
    EvolutionRevisionRefV2,
    ProjectHeadRefV2,
    RuntimeContextSnapshotRefV2,
    WorkspaceSnapshotRefV2,
)
from openevo.backend.project_freeze_control import (
    FrozenProjectArtifactV1,
    FrozenProjectForkRequestV1,
    ProjectFreezeRequestV1,
)
from openevo.backend.run_control import CoreTaskControlError
from openevo.backend.runtime_context_binding_v2 import (
    runtime_context_binding_for_head,
)
from openevo.backend.science_execution import (
    ScienceSuccessorMethodPlanV2,
    ScienceSuccessorPlanV2,
)
from openevo.evolution.revisions import AtomicFrozenProjectForkManifestV1
from tests.backend.test_science_successor_v2 import (
    _admit,
    _authority,
    _owner,
    _TripleArtifactFreezePreparer,
)


def _destination_head() -> ProjectHeadRefV2:
    project_id = "project-official-task"
    evolution = EvolutionRevisionRefV2(
        evolution_revision_id="official-evolution-0",
        project_id=project_id,
        manifest_sha256="a" * 64,
        artifact_count=0,
    )
    runtime = RuntimeContextSnapshotRefV2(
        runtime_context_snapshot_id="official-runtime-0",
        project_id=project_id,
        evolution_revision_id=evolution.evolution_revision_id,
        evolution_revision_manifest_sha256=evolution.manifest_sha256,
        registry_sha256="a" * 64,
        runtime_contract_sha256="b" * 64,
        manifest_sha256="c" * 64,
    )
    return ProjectHeadRefV2(
        project_head_id="official-project-head-0",
        project_id=project_id,
        generation=0,
        predecessor_project_head_id=None,
        workspace_snapshot=WorkspaceSnapshotRefV2(
            workspace_snapshot_id="official-workspace-0",
            project_id=project_id,
            manifest_sha256="d" * 64,
            entry_count=4,
            byte_size=2048,
        ),
        evolution_revision=evolution,
        runtime_context_snapshot=runtime,
        effective_execution_snapshot=EffectiveExecutionSnapshotRefV2(
            effective_execution_snapshot_id="official-execution-0",
            project_id=project_id,
            execution_mode="codex_subscription_transcript",
            capture_mode="transcript",
            token_level_metrics_available=False,
            producer_id="subscription-snapshot-issuer-v1",
            snapshot_sha256="e" * 64,
        ),
        registry_sha256="a" * 64,
        manifest_sha256="f" * 64,
    )


def test_frozen_project_fork_is_append_only_durable_and_runtime_bindable(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path, _TripleArtifactFreezePreparer())
    _, task = _admit(owner)
    plan = ScienceSuccessorPlanV2(
        project_id=task.project_id,
        task_id=task.task_id,
        task_admission_id=task.admission.task_admission_id,
        admission_sha256=task.admission.admission_sha256,
        accepted_attempt_id=task.attempts[0].attempt_id,
        predecessor_project_head_id=(
            task.admission.predecessor_project_head.project_head_id
        ),
        normalized_evolution_intent_sha256=(
            task.admission.normalized_evolution_intent_sha256
        ),
        enabled_methods=tuple(
            ScienceSuccessorMethodPlanV2(
                target_id=target,
                method_id=f"openevo.test.{target}.v1",
                output_artifact_type=target,
            )
            for target in _TripleArtifactFreezePreparer._targets
        ),
    )
    transition = owner.run_successor_transition(
        task.task_id,
        accepted_attempt_id=task.attempts[0].attempt_id,
        plan=plan,
    )
    assert transition.state == "committed"
    source_head = owner.active_project_head(task.project_id)
    artifacts = tuple(
        FrozenProjectArtifactV1(
            artifact_type=target,
            registry_id=f"artifact-{target}-1",
            sha256=(str(index + 1) * 64),
        )
        for index, target in enumerate(_TripleArtifactFreezePreparer._targets)
    )
    frozen = owner.freeze_project(
        ProjectFreezeRequestV1(
            project_id=task.project_id,
            expected_project_head_id=source_head.project_head_id,
            expected_project_head_manifest_sha256=source_head.manifest_sha256,
            composite_id="composite-final",
            composite_sha256="4" * 64,
            artifacts=artifacts,
            protocol_sha256="5" * 64,
            core_identity_sha256="6" * 64,
            adapter_identity_sha256="7" * 64,
            runtime_digest="sha256:" + "8" * 64,
            codex_cli_version="0.144.1",
            model="gpt-5.5",
            reasoning_effort="high",
            training_manifest_sha256="9" * 64,
            budget_policy_sha256="a" * 64,
            tool_policy_sha256="b" * 64,
            idempotency_key="freeze-source",
        ),
        verified_artifacts=artifacts,
    )
    destination = _authority(_destination_head())
    owner.publish_project_admission_authority(destination)
    request = FrozenProjectForkRequestV1(
        source_project_id=task.project_id,
        source_freeze_receipt_id=frozen.freeze_receipt_id,
        source_freeze_authority_sha256=frozen.authority_sha256,
        expected_source_project_head_id=source_head.project_head_id,
        expected_source_project_head_manifest_sha256=source_head.manifest_sha256,
        destination_project_id=destination.project_id,
        expected_destination_project_head_id=(
            destination.active_project_head.project_head_id
        ),
        expected_destination_project_head_manifest_sha256=(
            destination.active_project_head.manifest_sha256
        ),
        expected_destination_project_config_sha256=(
            destination.project_config_sha256
        ),
        idempotency_key="official-task-seed-1",
    )
    authority = owner.fork_frozen_project(request)
    assert type(authority.commit.manifest) is AtomicFrozenProjectForkManifestV1
    assert authority.source_mutated is False
    assert authority.seeded_destination_project_head.workspace_snapshot == (
        destination.workspace_snapshot
    )
    assert authority.artifacts == frozen.artifacts
    assert owner.fork_frozen_project(request) == authority
    assert owner.frozen_project_fork(authority.fork_receipt_id) == authority
    assert runtime_context_binding_for_head(
        project_head=authority.seeded_destination_project_head,
        service_generation_sha256="c" * 64,
        framework_lock_sha256="d" * 64,
        successor_commit=authority.commit,
    ).source == "materialized_inherited"
    source_after = owner.project_freeze(task.project_id)
    assert source_after == frozen
    owner.close()

    recovered = _owner(tmp_path, _TripleArtifactFreezePreparer())
    try:
        assert recovered.frozen_project_fork(authority.fork_receipt_id) == authority
        assert recovered.active_project_head(destination.project_id) == (
            authority.seeded_destination_project_head
        )
        with pytest.raises(CoreTaskControlError):
            recovered.fork_frozen_project(
                request.model_copy(update={"source_freeze_authority_sha256": "0" * 64})
            )
    finally:
        recovered.close()
