from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from openevo.backend.contracts.v2.models import (
    EffectiveExecutionSnapshotRefV2,
    EvolutionRevisionRefV2,
    ProjectHeadRefV2,
    RuntimeContextSnapshotRefV2,
    ScienceProjectConfigV2,
    TaskSubmitRequestV2,
    TaskV2,
    TransitionChangedEventV2,
    WorkspaceSnapshotRefV2,
    project_config_sha256_for,
)
from openevo.backend.run_control import CoreTaskControlError
from openevo.backend.project_freeze_control import (
    FrozenProjectArtifactV1,
    ProjectFreezeRequestV1,
)
from openevo.backend.runtime_context_binding_v2 import (
    runtime_context_binding_for_head,
)
from openevo.backend.science_execution import (
    AcceptedWorkspaceResultV2,
    ScienceMethodOutputV2,
    ScienceSuccessorMethodPlanV2,
    ScienceSuccessorPlanV2,
    ScienceSuccessorPreparationContextV2,
    SealedTranscriptDatasetV2,
    SuccessorMaterializationV2,
    ValidatedScienceOutputsV2,
)
from openevo.backend.science_successor import (
    ScienceSuccessorCleanupContextV2,
    ScienceSuccessorCleanupReceiptV2,
)
import openevo.backend.science_run_owner as task_owner_module
import openevo.backend.science_run_store as task_store_module
from openevo.backend.science_run_owner import (
    CoreScienceTaskOwnerV2,
    ScienceSuccessorPreparerV2,
    _as_inherited_successor_contribution,
    _successor_transition_failure_is_retryable,
)
from openevo.backend.science_run_store import (
    ScienceProjectAdmissionAuthorityV2,
    ScienceProjectReadinessBlockerV2,
    ScienceTaskPreconditionFailedV2,
    ScienceTaskStoreV2Error,
)
from openevo.evolution.revisions import (
    AtomicHistoricalRestoreManifestV2,
    AtomicSuccessorCommitV2,
    SuccessorArtifactContributionV2,
    atomic_successor_manifest_sha256,
)
from openevo.evolution.framework.handlers import (
    PayloadManifestEntry,
    payload_tree_digest,
)
from openevo.evolution.models import ArtifactContentAdmissionReceipt
from openevo.evolution.runtime_injection import build_runtime_injection_plan
from openevo.experiments.clients import EvolutionHttpStatusError


def _project_config() -> ScienceProjectConfigV2:
    return ScienceProjectConfigV2.model_validate(
        {
            "task": {
                "title": "Successor task",
                "objective": "Produce a verified successor.",
            },
            "workspace": {
                "kind": "scratch",
                "display_name": "Successor workspace",
            },
            "execution": {
                "mode": "codex_subscription_transcript",
                "capture_mode": "transcript",
                "token_level_metrics_available": False,
                "harness_id": "codex",
                "codex_model": "gpt-5.5",
                "reasoning_effort": "high",
                "token_limit": 32768,
                "task_network_allow_internet": False,
            },
            "evolution": {"targets": {}},
        }
    )


def test_successor_http_failure_retryability_uses_closed_status_classification() -> None:
    assert (
        _successor_transition_failure_is_retryable(EvolutionHttpStatusError(status_code=422))
        is False
    )


def test_inherited_successor_contribution_does_not_replay_admission_evidence() -> None:
    proposal_id = "artifact-native-update-1"
    content_payload = {
        "schema_version": "openevo.artifact_content_admission.v1",
        "basis_sha256": "1" * 64,
        "proposal_artifact_ids": [proposal_id],
        "source_artifact_ids": ["dataset-native-update-1"],
        "source_payload_sha256": "2" * 64,
        "scanned_file_count": 1,
        "scanned_byte_count": 32,
        "finding_count": 0,
        "finding_categories": [],
        "passed": True,
    }
    content_sha256 = hashlib.sha256(
        json.dumps(
            content_payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    produced = SuccessorArtifactContributionV2(
        target_id="agent_system",
        artifact_id=proposal_id,
        artifact_type="agent_system",
        owner_successor_transition_id="successor-native-update-1",
        origin="produced",
        evolution_job_id="job-native-update-1",
        admission_action="update",
        admission_decision_id="decision-native-update-1",
        admission_decision_sha256="3" * 64,
        content_admission=ArtifactContentAdmissionReceipt.model_validate(
            {**content_payload, "content_sha256": content_sha256}
        ),
        proposal_artifact_ids=(proposal_id,),
    )

    inherited = _as_inherited_successor_contribution(produced)

    assert inherited == SuccessorArtifactContributionV2(
        target_id=produced.target_id,
        artifact_id=produced.artifact_id,
        artifact_type=produced.artifact_type,
        owner_successor_transition_id=produced.owner_successor_transition_id,
        origin="inherited",
    )
    assert (
        _successor_transition_failure_is_retryable(EvolutionHttpStatusError(status_code=409))
        is False
    )
    assert (
        _successor_transition_failure_is_retryable(EvolutionHttpStatusError(status_code=429))
        is True
    )
    assert (
        _successor_transition_failure_is_retryable(EvolutionHttpStatusError(status_code=503))
        is True
    )


def test_attempt_result_schema_migration_rolls_back_as_one_unit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "migration"
    root.mkdir(mode=0o700)
    database = root / "science-tasks-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE attempt_executions (
                attempt_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                state TEXT NOT NULL,
                receipt_sha256 TEXT,
                execution_json BLOB NOT NULL,
                resource_version INTEGER NOT NULL CHECK (resource_version >= 1)
            ) STRICT;
            """
        )

    original_migration = task_store_module._migrate_v2_attempt_execution_results

    def _interrupt_after_first_alter(connection: sqlite3.Connection) -> None:
        connection.execute("ALTER TABLE attempt_executions ADD COLUMN session_result_sha256 TEXT")
        raise SystemExit("simulated migration interruption")

    monkeypatch.setattr(
        task_store_module,
        "_migrate_v2_attempt_execution_results",
        _interrupt_after_first_alter,
    )
    with pytest.raises(SystemExit, match="simulated migration interruption"):
        task_store_module.ScienceTaskStoreV2(root)

    with sqlite3.connect(database) as connection:
        columns_after_crash = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(attempt_executions)").fetchall()
        }
    assert "session_result_sha256" not in columns_after_crash
    assert "session_result_json" not in columns_after_crash

    monkeypatch.setattr(
        task_store_module,
        "_migrate_v2_attempt_execution_results",
        original_migration,
    )
    recovered = task_store_module.ScienceTaskStoreV2(root)
    recovered.close()
    with sqlite3.connect(database) as connection:
        columns_after_recovery = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(attempt_executions)").fetchall()
        }
    assert {
        "session_result_sha256",
        "session_result_json",
    }.issubset(columns_after_recovery)


class _Clock:
    def __init__(self) -> None:
        self._next = datetime(2026, 7, 23, 1, tzinfo=timezone.utc)
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            value = self._next
            self._next += timedelta(microseconds=1)
            return value


def _workspace(project_id: str, seed: str) -> WorkspaceSnapshotRefV2:
    return WorkspaceSnapshotRefV2(
        workspace_snapshot_id=f"workspace-{seed}",
        project_id=project_id,
        manifest_sha256=seed * 64,
        entry_count=4,
        byte_size=2048,
    )


def _head(
    project_id: str = "project-1",
    *,
    registry_sha256: str = "a" * 64,
    runtime_contract_sha256: str = "b" * 64,
) -> ProjectHeadRefV2:
    evolution = EvolutionRevisionRefV2(
        evolution_revision_id="evolution-0",
        project_id=project_id,
        manifest_sha256="2" * 64,
        artifact_count=0,
    )
    context = RuntimeContextSnapshotRefV2(
        runtime_context_snapshot_id="runtime-context-0",
        project_id=project_id,
        evolution_revision_id=evolution.evolution_revision_id,
        evolution_revision_manifest_sha256=evolution.manifest_sha256,
        registry_sha256=registry_sha256,
        runtime_contract_sha256=runtime_contract_sha256,
        manifest_sha256="3" * 64,
    )
    execution = EffectiveExecutionSnapshotRefV2(
        effective_execution_snapshot_id="execution-0",
        project_id=project_id,
        execution_mode="codex_subscription_transcript",
        capture_mode="transcript",
        token_level_metrics_available=False,
        producer_id="subscription-snapshot-issuer-v1",
        snapshot_sha256="4" * 64,
    )
    return ProjectHeadRefV2(
        project_head_id="project-head-0",
        project_id=project_id,
        generation=0,
        predecessor_project_head_id=None,
        workspace_snapshot=_workspace(project_id, "1"),
        evolution_revision=evolution,
        runtime_context_snapshot=context,
        effective_execution_snapshot=execution,
        registry_sha256=context.registry_sha256,
        manifest_sha256="5" * 64,
    )


def _authority(head: ProjectHeadRefV2 | None = None) -> ScienceProjectAdmissionAuthorityV2:
    head = head or _head()
    return ScienceProjectAdmissionAuthorityV2(
        project_id=head.project_id,
        active_project_head=head,
        project_config_sha256=project_config_sha256_for(_project_config()),
        workspace_snapshot=head.workspace_snapshot,
        normalized_evolution_intent_sha256="8" * 64,
    )


def _request(authority: ScienceProjectAdmissionAuthorityV2) -> TaskSubmitRequestV2:
    return TaskSubmitRequestV2(
        project_id=authority.project_id,
        expected_project_admission_etag=authority.project_etag,
        expected_project_head_id=authority.active_project_head.project_head_id,
        expected_project_head_manifest_sha256=(authority.active_project_head.manifest_sha256),
        expected_project_config_sha256=authority.project_config_sha256,
    )


def _plan(task: TaskV2) -> ScienceSuccessorPlanV2:
    return ScienceSuccessorPlanV2(
        project_id=task.project_id,
        task_id=task.task_id,
        task_admission_id=task.admission.task_admission_id,
        admission_sha256=task.admission.admission_sha256,
        accepted_attempt_id=task.attempts[-1].attempt_id,
        predecessor_project_head_id=(task.admission.predecessor_project_head.project_head_id),
        normalized_evolution_intent_sha256=(task.admission.normalized_evolution_intent_sha256),
        enabled_methods=(
            ScienceSuccessorMethodPlanV2(
                target_id="text_memory",
                method_id="openevo.text-memory.reflect.v1",
                output_artifact_type="text_memory",
            ),
        ),
    )


class _Preparer(ScienceSuccessorPreparerV2):
    def __init__(
        self,
        *,
        fail_phase: str | None = None,
        block_methods: tuple[threading.Event, threading.Event] | None = None,
        crash: bool = False,
        workspace_seed: str = "f",
    ) -> None:
        self.fail_phase = fail_phase
        self.block_methods = block_methods
        self.crash = crash
        self.workspace_seed = workspace_seed
        self.calls: list[str] = []

    def _enter(self, phase: str) -> None:
        self.calls.append(phase)
        if phase == "running_methods" and self.block_methods is not None:
            entered, release = self.block_methods
            entered.set()
            assert release.wait(timeout=5)
        if self.crash and phase == "running_methods":
            raise SystemExit("simulated process interruption")
        if self.fail_phase == phase:
            raise RuntimeError(f"injected {phase} failure with secret detail")

    def seal_dataset(
        self,
        context: ScienceSuccessorPreparationContextV2,
    ) -> SealedTranscriptDatasetV2:
        self._enter("sealing_dataset")
        return SealedTranscriptDatasetV2(
            dataset_id="dataset-1",
            artifact_id="artifact-dataset-1",
            manifest_sha256="b" * 64,
            record_count=12,
            task_id=context.task.task_id,
            task_admission_id=context.task.admission.task_admission_id,
            accepted_attempt_id=context.accepted_attempt.attempt_id,
            capture_mode="transcript",
            token_level_metrics_available=False,
            sealed=True,
        )

    def run_methods(
        self,
        context: ScienceSuccessorPreparationContextV2,
        dataset: SealedTranscriptDatasetV2,
    ) -> tuple[ScienceMethodOutputV2, ...]:
        self._enter("running_methods")
        assert dataset.accepted_attempt_id == context.accepted_attempt.attempt_id
        return (
            ScienceMethodOutputV2(
                target_id="text_memory",
                method_id="openevo.text-memory.reflect.v1",
                artifact_id="artifact-memory-1",
                artifact_type="text_memory",
                manifest_sha256="c" * 64,
                byte_size=1024,
                execution_boundary="outside_inference",
            ),
        )

    def reconcile_completed_methods(
        self,
        context: ScienceSuccessorPreparationContextV2,
        dataset: SealedTranscriptDatasetV2,
    ) -> tuple[ScienceMethodOutputV2, ...]:
        self._enter("reconciling_methods")
        assert dataset.accepted_attempt_id == context.accepted_attempt.attempt_id
        return (
            ScienceMethodOutputV2(
                target_id="text_memory",
                method_id="openevo.text-memory.reflect.v1",
                artifact_id="artifact-memory-1",
                artifact_type="text_memory",
                manifest_sha256="c" * 64,
                byte_size=1024,
                execution_boundary="outside_inference",
            ),
        )

    def resolve_training_feedback(
        self,
        context: ScienceSuccessorPreparationContextV2,
        dataset: SealedTranscriptDatasetV2,
    ) -> SealedTranscriptDatasetV2:
        self._enter("resolving_training_feedback")
        assert dataset.task_id == context.task.task_id
        return dataset

    def recover_dataset(
        self,
        context: ScienceSuccessorPreparationContextV2,
        *,
        dataset_id: str,
        manifest_sha256: str,
    ) -> SealedTranscriptDatasetV2:
        self._enter("recovering_dataset")
        assert dataset_id == "dataset-1"
        assert manifest_sha256 == "b" * 64
        return SealedTranscriptDatasetV2(
            dataset_id=dataset_id,
            artifact_id="artifact-dataset-1",
            manifest_sha256=manifest_sha256,
            record_count=12,
            task_id=context.task.task_id,
            task_admission_id=context.task.admission.task_admission_id,
            accepted_attempt_id=context.accepted_attempt.attempt_id,
            capture_mode="transcript",
            token_level_metrics_available=False,
            sealed=True,
        )

    def validate_outputs(
        self,
        context: ScienceSuccessorPreparationContextV2,
        dataset: SealedTranscriptDatasetV2,
        outputs: tuple[ScienceMethodOutputV2, ...],
    ) -> ValidatedScienceOutputsV2:
        self._enter("validating")
        return ValidatedScienceOutputsV2(
            project_id=context.task.project_id,
            successor_transition_id=context.transition.transition.successor_transition_id,
            predecessor_project_head_id=(
                context.task.admission.predecessor_project_head.project_head_id
            ),
            dataset=dataset,
            outputs=outputs,
            composition=tuple(
                SuccessorArtifactContributionV2(
                    target_id=item.target_id,
                    artifact_id=item.artifact_id,
                    artifact_type=item.artifact_type,
                    owner_successor_transition_id=(
                        context.transition.transition.successor_transition_id
                    ),
                    origin="produced",
                )
                for item in outputs
            ),
            evolution_revision=EvolutionRevisionRefV2(
                evolution_revision_id="evolution-1",
                project_id=context.task.project_id,
                manifest_sha256="d" * 64,
                artifact_count=1,
            ),
        )

    def materialize_context(
        self,
        context: ScienceSuccessorPreparationContextV2,
        validated: ValidatedScienceOutputsV2,
    ) -> SuccessorMaterializationV2:
        self._enter("materializing")
        evolution = validated.evolution_revision
        return SuccessorMaterializationV2(
            project_id=context.task.project_id,
            successor_transition_id=context.transition.transition.successor_transition_id,
            predecessor_project_head_id=(
                context.task.admission.predecessor_project_head.project_head_id
            ),
            materialized_context_id="ctx-successor-1",
            materialized_context_manifest_sha256="e" * 64,
            runtime_context_snapshot=RuntimeContextSnapshotRefV2(
                runtime_context_snapshot_id="runtime-context-1",
                project_id=context.task.project_id,
                evolution_revision_id=evolution.evolution_revision_id,
                evolution_revision_manifest_sha256=evolution.manifest_sha256,
                registry_sha256=context.task.admission.registry_sha256,
                runtime_contract_sha256=(
                    context.task.admission.predecessor_project_head.runtime_context_snapshot.runtime_contract_sha256
                ),
                manifest_sha256="1" * 64,
            ),
        )

    def capture_workspace_result(
        self,
        context: ScienceSuccessorPreparationContextV2,
    ) -> AcceptedWorkspaceResultV2:
        self._enter("workspace")
        return AcceptedWorkspaceResultV2(
            project_id=context.task.project_id,
            task_id=context.task.task_id,
            accepted_attempt_id=context.accepted_attempt.attempt_id,
            workspace_snapshot=_workspace(
                context.task.project_id,
                self.workspace_seed,
            ),
        )

    def discard_transition_outputs(
        self,
        context: ScienceSuccessorCleanupContextV2,
    ) -> ScienceSuccessorCleanupReceiptV2:
        self._enter("discarding_outputs")
        return ScienceSuccessorCleanupReceiptV2(
            successor_transition_id=(context.transition.transition.successor_transition_id),
        )


class _InheritanceChainPreparer(_Preparer):
    def __init__(self) -> None:
        super().__init__()
        self.inherited_commit: AtomicSuccessorCommitV2 | None = None

    def run_methods(
        self,
        context: ScienceSuccessorPreparationContextV2,
        dataset: SealedTranscriptDatasetV2,
    ) -> tuple[ScienceMethodOutputV2, ...]:
        if context.plan.enabled_methods:
            return super().run_methods(context, dataset)
        self._enter("running_methods")
        return ()

    def validate_outputs(
        self,
        context: ScienceSuccessorPreparationContextV2,
        dataset: SealedTranscriptDatasetV2,
        outputs: tuple[ScienceMethodOutputV2, ...],
    ) -> ValidatedScienceOutputsV2:
        if context.plan.enabled_methods:
            return super().validate_outputs(
                context,
                dataset,
                outputs,
            )
        self._enter("validating")
        inherited = self.inherited_commit
        assert inherited is not None
        predecessor = context.task.admission.predecessor_project_head
        return ValidatedScienceOutputsV2(
            project_id=context.task.project_id,
            successor_transition_id=(context.transition.transition.successor_transition_id),
            predecessor_project_head_id=(predecessor.project_head_id),
            dataset=dataset,
            outputs=(),
            composition=tuple(
                item.model_copy(update={"origin": "inherited"})
                for item in inherited.manifest.artifacts
            ),
            evolution_revision=predecessor.evolution_revision,
        )

    def materialize_context(
        self,
        context: ScienceSuccessorPreparationContextV2,
        validated: ValidatedScienceOutputsV2,
    ) -> SuccessorMaterializationV2:
        if context.plan.enabled_methods:
            return super().materialize_context(
                context,
                validated,
            )
        self._enter("materializing")
        inherited = self.inherited_commit
        assert inherited is not None
        manifest = inherited.manifest
        predecessor = context.task.admission.predecessor_project_head
        if manifest.runtime_context_source == "materialized_new":
            runtime_source = "materialized_inherited"
            source_transition_id = manifest.successor_transition_id
            source_predecessor_id = manifest.predecessor_project_head_id
        else:
            runtime_source = manifest.runtime_context_source
            source_transition_id = manifest.materialized_source_successor_transition_id
            source_predecessor_id = manifest.materialized_source_predecessor_project_head_id
        return SuccessorMaterializationV2(
            project_id=context.task.project_id,
            successor_transition_id=(context.transition.transition.successor_transition_id),
            predecessor_project_head_id=(predecessor.project_head_id),
            runtime_context_source=runtime_source,
            materialized_source_successor_transition_id=(source_transition_id),
            materialized_source_predecessor_project_head_id=(source_predecessor_id),
            materialized_context_id=(manifest.materialized_context_id),
            materialized_context_manifest_sha256=(manifest.materialized_context_manifest_sha256),
            runtime_context_snapshot=(predecessor.runtime_context_snapshot),
        )


class _LegacyMigrationChainPreparer(_Preparer):
    def __init__(self, target_id: str | None, *, ordinal: int) -> None:
        super().__init__(workspace_seed="f" if ordinal == 1 else "e")
        self.target_id = target_id
        self.ordinal = ordinal

    @staticmethod
    def _method_id(target_id: str) -> str:
        return {
            "text_memory": "openevo.text-memory.reflect.v1",
            "skill_bundle": "openevo.skill-bundle.manual.v1",
        }[target_id]

    @staticmethod
    def _digest(label: str) -> str:
        return hashlib.sha256(label.encode("utf-8")).hexdigest()

    def seal_dataset(
        self,
        context: ScienceSuccessorPreparationContextV2,
    ) -> SealedTranscriptDatasetV2:
        self._enter("sealing_dataset")
        task_id = context.task.task_id
        return SealedTranscriptDatasetV2(
            dataset_id=f"dataset-{task_id}",
            artifact_id=f"artifact-dataset-{task_id}",
            manifest_sha256=self._digest(f"dataset:{task_id}"),
            record_count=12,
            task_id=task_id,
            task_admission_id=context.task.admission.task_admission_id,
            accepted_attempt_id=context.accepted_attempt.attempt_id,
            capture_mode="transcript",
            token_level_metrics_available=False,
            sealed=True,
        )

    def run_methods(
        self,
        context: ScienceSuccessorPreparationContextV2,
        dataset: SealedTranscriptDatasetV2,
    ) -> tuple[ScienceMethodOutputV2, ...]:
        self._enter("running_methods")
        target_id = self.target_id
        if target_id is None:
            return ()
        task_id = context.task.task_id
        return (
            ScienceMethodOutputV2(
                target_id=target_id,
                method_id=self._method_id(target_id),
                artifact_id=f"artifact-{target_id}-{task_id}",
                artifact_type=target_id,
                manifest_sha256=self._digest(f"artifact:{target_id}:{task_id}"),
                byte_size=1024,
                execution_boundary="outside_inference",
            ),
        )

    def validate_outputs(
        self,
        context: ScienceSuccessorPreparationContextV2,
        dataset: SealedTranscriptDatasetV2,
        outputs: tuple[ScienceMethodOutputV2, ...],
    ) -> ValidatedScienceOutputsV2:
        self._enter("validating")
        task_id = context.task.task_id
        target_id = self.target_id or "empty"
        transition_id = context.transition.transition.successor_transition_id
        return ValidatedScienceOutputsV2(
            project_id=context.task.project_id,
            successor_transition_id=transition_id,
            predecessor_project_head_id=(
                context.task.admission.predecessor_project_head.project_head_id
            ),
            dataset=dataset,
            outputs=outputs,
            composition=tuple(
                SuccessorArtifactContributionV2(
                    target_id=output.target_id,
                    artifact_id=output.artifact_id,
                    artifact_type=output.artifact_type,
                    owner_successor_transition_id=transition_id,
                    origin="produced",
                )
                for output in outputs
            ),
            evolution_revision=EvolutionRevisionRefV2(
                evolution_revision_id=(f"evolution-{target_id}-{task_id}"),
                project_id=context.task.project_id,
                manifest_sha256=self._digest(f"evolution:{target_id}:{task_id}"),
                artifact_count=len(outputs),
            ),
        )

    def materialize_context(
        self,
        context: ScienceSuccessorPreparationContextV2,
        validated: ValidatedScienceOutputsV2,
    ) -> SuccessorMaterializationV2:
        self._enter("materializing")
        task_id = context.task.task_id
        evolution = validated.evolution_revision
        return SuccessorMaterializationV2(
            project_id=context.task.project_id,
            successor_transition_id=(context.transition.transition.successor_transition_id),
            predecessor_project_head_id=(
                context.task.admission.predecessor_project_head.project_head_id
            ),
            materialized_context_id=f"ctx-{task_id}",
            materialized_context_manifest_sha256=self._digest(f"context:{task_id}"),
            runtime_context_snapshot=RuntimeContextSnapshotRefV2(
                runtime_context_snapshot_id=f"runtime-{task_id}",
                project_id=context.task.project_id,
                evolution_revision_id=evolution.evolution_revision_id,
                evolution_revision_manifest_sha256=(evolution.manifest_sha256),
                registry_sha256=context.task.admission.registry_sha256,
                runtime_contract_sha256=(
                    context.task.admission.predecessor_project_head.runtime_context_snapshot.runtime_contract_sha256
                ),
                manifest_sha256=self._digest(f"runtime:{task_id}"),
            ),
        )


def _owner(tmp_path: Path, preparer: _Preparer) -> CoreScienceTaskOwnerV2:
    return CoreScienceTaskOwnerV2(
        state_root=tmp_path,
        clock=_Clock(),
        successor_preparer=preparer,
    )


def _admit(owner: CoreScienceTaskOwnerV2) -> tuple[ScienceProjectAdmissionAuthorityV2, TaskV2]:
    authority = _authority()
    owner.publish_project_admission_authority(authority)
    task = owner.invoke(
        "submitCoreTaskV2",
        {"request": _request(authority), "idempotency_key": "submit-1"},
    )
    assert isinstance(task, TaskV2)
    return authority, task


def _legacy_chain_plan(
    task: TaskV2,
    target_id: str | None,
) -> ScienceSuccessorPlanV2:
    enabled_methods = ()
    if target_id is not None:
        enabled_methods = (
            ScienceSuccessorMethodPlanV2(
                target_id=target_id,
                method_id=(_LegacyMigrationChainPreparer._method_id(target_id)),
                output_artifact_type=target_id,
            ),
        )
    return ScienceSuccessorPlanV2(
        project_id=task.project_id,
        task_id=task.task_id,
        task_admission_id=task.admission.task_admission_id,
        admission_sha256=task.admission.admission_sha256,
        accepted_attempt_id=task.attempts[-1].attempt_id,
        predecessor_project_head_id=(task.admission.predecessor_project_head.project_head_id),
        normalized_evolution_intent_sha256=(task.admission.normalized_evolution_intent_sha256),
        enabled_methods=enabled_methods,
    )


def test_completed_attempt_commits_one_complete_adjacent_successor(
    tmp_path: Path,
) -> None:
    preparer = _Preparer()
    owner = _owner(tmp_path, preparer)
    predecessor, task = _admit(owner)
    try:
        transition = owner.run_successor_transition(
            task.task_id,
            accepted_attempt_id=task.attempts[0].attempt_id,
            plan=_plan(task),
        )

        assert transition.state == "committed"
        successor = transition.transition.successor_project_head
        assert successor is not None
        assert successor.generation == 1
        assert successor.predecessor_project_head_id == (
            predecessor.active_project_head.project_head_id
        )
        assert successor.workspace_snapshot.workspace_snapshot_id == "workspace-f"
        assert successor.evolution_revision.evolution_revision_id == "evolution-1"
        assert (
            successor.runtime_context_snapshot.runtime_context_snapshot_id == "runtime-context-1"
        )
        assert (
            successor.effective_execution_snapshot
            == predecessor.active_project_head.effective_execution_snapshot
        )
        assert preparer.calls == [
            "sealing_dataset",
            "resolving_training_feedback",
            "running_methods",
            "validating",
            "materializing",
            "workspace",
        ]
        assert owner.active_project_head(task.project_id) == successor
        assert owner.list_project_heads(task.project_id) == [
            predecessor.active_project_head,
            successor,
        ]
        commit = owner.successor_commit(transition.transition.successor_transition_id)
        assert commit is not None
        assert commit.manifest_sha256 == atomic_successor_manifest_sha256(commit.manifest)
        assert commit.manifest.dataset_id == "dataset-1"
        assert commit.manifest.method_artifact_ids == ("artifact-memory-1",)
        assert commit.manifest.materialized_context_id == "ctx-successor-1"
        completed = owner.invoke("getCoreTaskV2", {"task_id": task.task_id})
        assert completed.state == "completed"
        assert completed.authoritative_attempt_id == task.attempts[0].attempt_id
        assert completed.successor_transition == transition.transition
    finally:
        owner.close()


class _TripleArtifactFreezePreparer(_Preparer):
    _targets = ("agent_system", "skill_bundle", "text_memory")

    def run_methods(
        self,
        context: ScienceSuccessorPreparationContextV2,
        dataset: SealedTranscriptDatasetV2,
    ) -> tuple[ScienceMethodOutputV2, ...]:
        self._enter("running_methods")
        return tuple(
            ScienceMethodOutputV2(
                target_id=target,
                method_id=f"openevo.test.{target}.v1",
                artifact_id=f"artifact-{target}-1",
                artifact_type=target,
                manifest_sha256=hashlib.sha256(target.encode()).hexdigest(),
                byte_size=128,
                execution_boundary="outside_inference",
            )
            for target in self._targets
        )

    def validate_outputs(
        self,
        context: ScienceSuccessorPreparationContextV2,
        dataset: SealedTranscriptDatasetV2,
        outputs: tuple[ScienceMethodOutputV2, ...],
    ) -> ValidatedScienceOutputsV2:
        self._enter("validating")
        return ValidatedScienceOutputsV2(
            project_id=context.task.project_id,
            successor_transition_id=(
                context.transition.transition.successor_transition_id
            ),
            predecessor_project_head_id=(
                context.task.admission.predecessor_project_head.project_head_id
            ),
            dataset=dataset,
            outputs=outputs,
            composition=tuple(
                SuccessorArtifactContributionV2(
                    target_id=item.target_id,
                    artifact_id=item.artifact_id,
                    artifact_type=item.artifact_type,
                    owner_successor_transition_id=(
                        context.transition.transition.successor_transition_id
                    ),
                    origin="produced",
                )
                for item in outputs
            ),
            evolution_revision=EvolutionRevisionRefV2(
                evolution_revision_id="evolution-freeze-1",
                project_id=context.task.project_id,
                manifest_sha256="d" * 64,
                artifact_count=3,
            ),
        )


def test_project_freeze_is_durable_idempotent_and_blocks_mutations(
    tmp_path: Path,
) -> None:
    preparer = _TripleArtifactFreezePreparer()
    owner = _owner(tmp_path, preparer)
    authority, task = _admit(owner)
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
    head = owner.active_project_head(task.project_id)
    artifacts = tuple(
        FrozenProjectArtifactV1(
            artifact_type=target,
            registry_id=f"artifact-{target}-1",
            sha256=hashlib.sha256(f"payload:{target}".encode()).hexdigest(),
        )
        for target in _TripleArtifactFreezePreparer._targets
    )
    request = ProjectFreezeRequestV1(
        project_id=task.project_id,
        expected_project_head_id=head.project_head_id,
        expected_project_head_manifest_sha256=head.manifest_sha256,
        composite_id="composite-final",
        composite_sha256="1" * 64,
        artifacts=artifacts,
        protocol_sha256="2" * 64,
        core_identity_sha256="3" * 64,
        adapter_identity_sha256="4" * 64,
        runtime_digest="sha256:" + "5" * 64,
        codex_cli_version="0.144.1",
        model="gpt-5.5",
        reasoning_effort="high",
        training_manifest_sha256="6" * 64,
        budget_policy_sha256="7" * 64,
        tool_policy_sha256="8" * 64,
        idempotency_key="freeze-final",
    )
    frozen = owner.freeze_project(request, verified_artifacts=artifacts)
    assert frozen.core_authoritative is True
    assert frozen.frozen is True
    assert frozen.evolution_enabled is False
    assert owner.freeze_project(request, verified_artifacts=artifacts) == frozen
    assert owner.project_freeze(task.project_id) == frozen
    frozen_authority = owner.project_admission_authority(task.project_id)
    with pytest.raises(CoreTaskControlError, match="frozen project"):
        owner.invoke(
            "submitCoreTaskV2",
            {
                "request": _request(frozen_authority),
                "idempotency_key": "submit-after-freeze",
            },
        )
    owner.close()

    recovered = _owner(tmp_path, _TripleArtifactFreezePreparer())
    try:
        assert recovered.project_freeze(task.project_id) == frozen
        with pytest.raises(CoreTaskControlError, match="frozen project"):
            recovered.restore_historical_project_head(
                project_id=task.project_id,
                expected_project_head_id=head.project_head_id,
                source_project_head_id=(
                    authority.active_project_head.project_head_id
                ),
                restore_request_id="restore-after-freeze",
            )
    finally:
        recovered.close()


def test_project_freeze_accepts_historical_restore_of_native_artifacts(
    tmp_path: Path,
) -> None:
    """Best-of-three restores a prior head before the final project freeze."""

    owner = _owner(tmp_path, _TripleArtifactFreezePreparer())
    _authority, task = _admit(owner)
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
    evolved = transition.transition.successor_project_head
    assert evolved is not None
    restored, restore_commit = owner.restore_historical_project_head(
        project_id=task.project_id,
        expected_project_head_id=evolved.project_head_id,
        source_project_head_id=evolved.project_head_id,
        restore_request_id="restore-best-before-freeze",
    )
    assert type(restore_commit.manifest) is AtomicHistoricalRestoreManifestV2
    artifacts = tuple(
        FrozenProjectArtifactV1(
            artifact_type=target,
            registry_id=f"artifact-{target}-1",
            sha256=hashlib.sha256(f"payload:{target}".encode()).hexdigest(),
        )
        for target in _TripleArtifactFreezePreparer._targets
    )
    request = ProjectFreezeRequestV1(
        project_id=task.project_id,
        expected_project_head_id=restored.project_head_id,
        expected_project_head_manifest_sha256=restored.manifest_sha256,
        composite_id="composite-restored-best",
        composite_sha256="1" * 64,
        artifacts=artifacts,
        protocol_sha256="2" * 64,
        core_identity_sha256="3" * 64,
        adapter_identity_sha256="4" * 64,
        runtime_digest="sha256:" + "5" * 64,
        codex_cli_version="0.144.1",
        model="gpt-5.5",
        reasoning_effort="high",
        training_manifest_sha256="6" * 64,
        budget_policy_sha256="7" * 64,
        tool_policy_sha256="8" * 64,
        idempotency_key="freeze-restored-best",
    )

    frozen = owner.freeze_project(request, verified_artifacts=artifacts)

    assert frozen.project_head_id == restored.project_head_id
    assert frozen.artifacts == artifacts
    assert owner.project_freeze(task.project_id) == frozen


def test_historical_restore_publishes_linear_durable_runtime_authority(
    tmp_path: Path,
) -> None:
    preparer = _Preparer()
    owner = _owner(tmp_path, preparer)
    predecessor, task = _admit(owner)
    transition = owner.run_successor_transition(
        task.task_id,
        accepted_attempt_id=task.attempts[0].attempt_id,
        plan=_plan(task),
    )
    evolved = transition.transition.successor_project_head
    assert evolved is not None

    restored, receipt = owner.restore_historical_project_head(
        project_id=task.project_id,
        expected_project_head_id=evolved.project_head_id,
        source_project_head_id=evolved.project_head_id,
        restore_request_id="restore-best-1",
    )
    assert restored.generation == evolved.generation + 1
    assert restored.predecessor_project_head_id == evolved.project_head_id
    assert restored.workspace_snapshot == evolved.workspace_snapshot
    assert restored.evolution_revision == evolved.evolution_revision
    assert restored.runtime_context_snapshot == evolved.runtime_context_snapshot
    assert type(receipt.manifest) is AtomicHistoricalRestoreManifestV2
    assert receipt.manifest.source_project_head_id == evolved.project_head_id
    assert owner.active_project_head(task.project_id) == restored
    assert runtime_context_binding_for_head(
        project_head=restored,
        service_generation_sha256="a" * 64,
        framework_lock_sha256="b" * 64,
        successor_commit=receipt,
    ).source == "materialized_inherited"

    replay_head, replay_receipt = owner.restore_historical_project_head(
        project_id=task.project_id,
        expected_project_head_id=evolved.project_head_id,
        source_project_head_id=evolved.project_head_id,
        restore_request_id="restore-best-1",
    )
    assert replay_head == restored
    assert replay_receipt == receipt
    restore_id = receipt.manifest.successor_transition_id

    rolled_back, rollback_receipt = owner.restore_historical_project_head(
        project_id=task.project_id,
        expected_project_head_id=restored.project_head_id,
        source_project_head_id=predecessor.active_project_head.project_head_id,
        restore_request_id="restore-genesis-2",
    )
    assert rolled_back.generation == restored.generation + 1
    assert rolled_back.evolution_revision.artifact_count == 0
    assert runtime_context_binding_for_head(
        project_head=rolled_back,
        service_generation_sha256="a" * 64,
        framework_lock_sha256="b" * 64,
        successor_commit=rollback_receipt,
    ).source == "empty_inherited"
    owner.close()

    restarted = _owner(tmp_path, _Preparer())
    try:
        assert restarted.active_project_head(task.project_id) == rolled_back
        assert restarted.historical_restore_commit(restore_id) == receipt
        assert restarted._ledger.prior_dataset_artifact_ids_for_head(
            rolled_back.project_head_id
        ) == ("artifact-dataset-1",)
    finally:
        restarted.close()


def test_cross_project_restore_rebinds_source_artifacts_to_fresh_workspace(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path, _Preparer())
    _source_authority, task = _admit(owner)
    transition = owner.run_successor_transition(
        task.task_id,
        accepted_attempt_id=task.attempts[0].attempt_id,
        plan=_plan(task),
    )
    source = transition.transition.successor_project_head
    assert source is not None

    destination_evolution = EvolutionRevisionRefV2(
        evolution_revision_id="destination-evolution-0",
        project_id="project-2",
        manifest_sha256="6" * 64,
        artifact_count=0,
    )
    destination_runtime = RuntimeContextSnapshotRefV2(
        runtime_context_snapshot_id="destination-runtime-0",
        project_id="project-2",
        evolution_revision_id=destination_evolution.evolution_revision_id,
        evolution_revision_manifest_sha256=destination_evolution.manifest_sha256,
        registry_sha256=source.registry_sha256,
        runtime_contract_sha256="b" * 64,
        manifest_sha256="7" * 64,
    )
    destination_execution = EffectiveExecutionSnapshotRefV2(
        effective_execution_snapshot_id="destination-execution-0",
        project_id="project-2",
        execution_mode="codex_subscription_transcript",
        capture_mode="transcript",
        token_level_metrics_available=False,
        producer_id="subscription-snapshot-issuer-v1",
        snapshot_sha256="8" * 64,
    )
    destination = ProjectHeadRefV2(
        project_head_id="destination-project-head-0",
        project_id="project-2",
        generation=0,
        predecessor_project_head_id=None,
        workspace_snapshot=_workspace("project-2", "9"),
        evolution_revision=destination_evolution,
        runtime_context_snapshot=destination_runtime,
        effective_execution_snapshot=destination_execution,
        registry_sha256=source.registry_sha256,
        manifest_sha256="0" * 64,
    )
    owner.publish_project_admission_authority(_authority(destination))

    transferred, receipt = owner.restore_historical_project_head(
        project_id="project-2",
        expected_project_head_id=destination.project_head_id,
        source_project_head_id=source.project_head_id,
        restore_request_id="cross-task-restore-1",
        cross_project=True,
    )
    assert transferred.project_id == "project-2"
    assert transferred.workspace_snapshot == destination.workspace_snapshot
    assert (
        transferred.effective_execution_snapshot
        == destination.effective_execution_snapshot
    )
    assert transferred.evolution_revision.project_id == "project-2"
    assert transferred.runtime_context_snapshot.project_id == "project-2"
    assert (
        transferred.evolution_revision.artifact_count
        == source.evolution_revision.artifact_count
    )
    assert receipt.manifest.source_project_head_id == source.project_head_id
    assert owner.active_project_head(task.project_id) == source
    assert owner.active_project_head("project-2") == transferred
    assert owner._ledger.prior_dataset_artifact_ids_for_head(
        transferred.project_head_id
    ) == ("artifact-dataset-1",)

    replay_head, replay_receipt = owner.restore_historical_project_head(
        project_id="project-2",
        expected_project_head_id=destination.project_head_id,
        source_project_head_id=source.project_head_id,
        restore_request_id="cross-task-restore-1",
        cross_project=True,
    )
    assert replay_head == transferred
    assert replay_receipt == receipt
    with pytest.raises(
        CoreTaskControlError,
        match="immutable Task ownership precondition changed",
    ):
        owner.restore_historical_project_head(
            project_id="project-2",
            expected_project_head_id=transferred.project_head_id,
            source_project_head_id=source.project_head_id,
            restore_request_id="cross-task-restore-without-mode",
        )
    owner._successor_preparer = _LegacyMigrationChainPreparer(
        "text_memory",
        ordinal=2,
    )
    destination_authority = owner.project_admission_authority("project-2")
    destination_task = owner.invoke(
        "submitCoreTaskV2",
        {
            "request": _request(destination_authority),
            "idempotency_key": "submit-after-cross-task-restore",
        },
    )
    destination_transition = owner.run_successor_transition(
        destination_task.task_id,
        accepted_attempt_id=destination_task.attempts[0].attempt_id,
        plan=_legacy_chain_plan(destination_task, "text_memory"),
    )
    destination_successor = destination_transition.transition.successor_project_head
    assert destination_successor is not None
    assert destination_successor.predecessor_project_head_id == transferred.project_head_id
    owner.close()
    restarted = _owner(tmp_path, _Preparer())
    try:
        assert restarted.active_project_head("project-2") == destination_successor
        assert restarted.active_project_head(task.project_id) == source
        assert restarted._ledger.prior_dataset_artifact_ids_for_head(
            transferred.project_head_id
        ) == ("artifact-dataset-1",)
    finally:
        restarted.close()


def test_historical_restore_receipt_tamper_fails_store_restart(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path, _Preparer())
    _predecessor, task = _admit(owner)
    transition = owner.run_successor_transition(
        task.task_id,
        accepted_attempt_id=task.attempts[0].attempt_id,
        plan=_plan(task),
    )
    evolved = transition.transition.successor_project_head
    assert evolved is not None
    owner.restore_historical_project_head(
        project_id=task.project_id,
        expected_project_head_id=evolved.project_head_id,
        source_project_head_id=evolved.project_head_id,
        restore_request_id="restore-tamper-1",
    )
    owner.close()

    database = tmp_path / "science-tasks-v2" / "science-tasks-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE historical_restore_commits SET manifest_sha256 = ?",
            ("6" * 64,),
        )
        connection.commit()
    with pytest.raises(
        ScienceTaskStoreV2Error,
        match="historical restore receipt row",
    ):
        _owner(tmp_path, _Preparer())


@pytest.mark.parametrize(
    "phase",
    [
        "sealing_dataset",
        "running_methods",
        "validating",
        "materializing",
        "workspace",
    ],
)
def test_preparation_failure_keeps_predecessor_active_and_next_task_not_ready(
    tmp_path: Path,
    phase: str,
) -> None:
    preparer = _Preparer(fail_phase=phase)
    owner = _owner(tmp_path, preparer)
    predecessor, task = _admit(owner)
    try:
        with pytest.raises(CoreTaskControlError) as failed:
            owner.run_successor_transition(
                task.task_id,
                accepted_attempt_id=task.attempts[0].attempt_id,
                plan=_plan(task),
            )
        assert failed.value.code == "successor_transition_failed"
        assert "secret detail" not in str(failed.value)

        transition = owner.get_successor_transition_for_task(task.task_id)
        assert transition.state == "failed"
        assert transition.error is not None
        assert transition.error.retryable is True
        assert transition.error.repair_action == "retry"
        assert transition.transition.successor_project_head is None
        final_event = owner.list_task_events(task.task_id)[-1]
        assert final_event.event_type == "transition_changed"
        assert final_event.state == "failed"
        assert owner.active_project_head(task.project_id) == predecessor.active_project_head
        assert owner.list_project_heads(task.project_id) == [predecessor.active_project_head]
        assert owner.successor_commit(transition.transition.successor_transition_id) is None
        with pytest.raises(CoreTaskControlError) as not_ready:
            owner.invoke(
                "submitCoreTaskV2",
                {"request": _request(predecessor), "idempotency_key": "next-task"},
            )
        assert not_ready.value.code == "project_not_ready"
        assert owner.ownership_counts() == (1, 1, 1)
    finally:
        owner.close()


def test_failed_successor_recovery_context_reads_task_from_admission(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path, _Preparer(fail_phase="workspace"))
    predecessor, task = _admit(owner)
    plan = _plan(task)
    try:
        with pytest.raises(CoreTaskControlError):
            owner.run_successor_transition(
                task.task_id,
                accepted_attempt_id=task.attempts[0].attempt_id,
                plan=plan,
            )
        transition = owner.get_successor_transition_for_task(task.task_id)
        attempts = owner.successor_transition_attempts(
            transition.transition.successor_transition_id
        )
        source = SimpleNamespace(
            successor_transition_id=transition.transition.successor_transition_id,
            project_id=task.project_id,
            task_id=task.task_id,
            accepted_attempt_id=task.attempts[0].attempt_id,
            predecessor_project_head_id=(
                predecessor.active_project_head.project_head_id
            ),
            transition_attempt_count=len(attempts),
            transition_attempt_capacity=len(attempts),
            active_project_head_id=predecessor.active_project_head.project_head_id,
            active_project_head_sha256=predecessor.active_project_head.manifest_sha256,
        )

        context = owner.recovery_successor_context(source, plan)

        assert context.task.task_id == task.task_id
        assert context.transition.transition.task_admission is not None
        assert context.transition.transition.task_admission.task_id == task.task_id
    finally:
        owner.close()


def test_failed_materialization_retries_the_same_transition_from_sealed_dataset(
    tmp_path: Path,
) -> None:
    preparer = _Preparer(fail_phase="materializing")
    owner = _owner(tmp_path, preparer)
    predecessor, task = _admit(owner)
    try:
        with pytest.raises(CoreTaskControlError):
            owner.run_successor_transition(
                task.task_id,
                accepted_attempt_id=task.attempts[0].attempt_id,
                plan=_plan(task),
            )
        failed = owner.get_successor_transition_for_task(task.task_id)
        transition_id = failed.transition.successor_transition_id
        assert failed.state == "failed"
        assert failed.progress_completed == 4

        preparer.fail_phase = None
        committed = owner.retry_successor_transition(
            transition_id,
            expected_project_head_id=(predecessor.active_project_head.project_head_id),
            retry_request_id="retry-materialization-1",
        )

        assert committed.state == "committed"
        assert committed.transition.successor_transition_id == transition_id
        assert committed.transition.successor_project_head is not None
        assert committed.transition.successor_project_head.generation == 1
        attempts = owner.successor_transition_attempts(transition_id)
        assert [
            (attempt.ordinal, attempt.state, attempt.retry_request_id) for attempt in attempts
        ] == [
            (1, "failed", "initial"),
            (2, "committed", "retry-materialization-1"),
        ]
        dataset_event = next(
            event
            for event in owner.list_task_events(task.task_id)
            if event.event_type == "dataset_sealed"
        )
        commit = owner.successor_commit(transition_id)
        assert commit is not None
        assert attempts[0].dataset_id == dataset_event.dataset_id
        assert attempts[0].dataset_sha256 == dataset_event.dataset_sha256
        assert attempts[0].commit_manifest_sha256 is None
        assert attempts[1].dataset_id == dataset_event.dataset_id
        assert attempts[1].dataset_sha256 == dataset_event.dataset_sha256
        assert attempts[1].commit_manifest_sha256 == (commit.manifest_sha256)
        assert preparer.calls == [
            "sealing_dataset",
            "resolving_training_feedback",
            "running_methods",
            "validating",
            "materializing",
            "recovering_dataset",
            "resolving_training_feedback",
            "running_methods",
            "validating",
            "materializing",
            "workspace",
        ]
        assert [
            event.state
            for event in owner.list_task_events(task.task_id)
            if isinstance(event, TransitionChangedEventV2)
        ] == [
            "pending",
            "sealing_dataset",
            "running_methods",
            "validating",
            "materializing",
            "failed",
            "running_methods",
            "validating",
            "materializing",
            "committing",
            "committed",
        ]
    finally:
        owner.close()


def test_feedback_hook_waits_after_dataset_seal_and_retries_idempotently(
    tmp_path: Path,
) -> None:
    preparer = _Preparer(fail_phase="resolving_training_feedback")
    owner = _owner(tmp_path, preparer)
    predecessor, task = _admit(owner)
    try:
        with pytest.raises(CoreTaskControlError):
            owner.run_successor_transition(
                task.task_id,
                accepted_attempt_id=task.attempts[0].attempt_id,
                plan=_plan(task),
            )
        failed = owner.get_successor_transition_for_task(task.task_id)
        dataset_events = [
            event
            for event in owner.list_task_events(task.task_id)
            if event.event_type == "dataset_sealed"
        ]
        assert len(dataset_events) == 1
        assert failed.error is not None and failed.error.retryable is True

        preparer.fail_phase = None
        committed = owner.retry_successor_transition(
            failed.transition.successor_transition_id,
            expected_project_head_id=(predecessor.active_project_head.project_head_id),
            retry_request_id="retry-after-feedback-attachment",
        )
        assert committed.state == "committed"
        assert [
            call
            for call in preparer.calls
            if call in {"sealing_dataset", "recovering_dataset"}
        ] == ["sealing_dataset", "recovering_dataset"]
        assert len(
            [
                event
                for event in owner.list_task_events(task.task_id)
                if event.event_type == "dataset_sealed"
            ]
        ) == 1
        transition_id = failed.transition.successor_transition_id
    finally:
        owner.close()

    restarted = _owner(tmp_path, _Preparer())
    try:
        recovered = restarted.get_successor_transition(transition_id)
        assert recovered == committed
        assert restarted.active_project_head(task.project_id).generation == 1
    finally:
        restarted.close()


def test_nonretryable_successor_failure_rejects_new_retry_without_an_attempt(
    tmp_path: Path,
) -> None:
    class _NonRetryablePreparer(_Preparer):
        def _enter(self, phase: str) -> None:
            self.calls.append(phase)
            if phase == "materializing":
                raise ValueError("deterministic materialization contract failure")

    preparer = _NonRetryablePreparer()
    owner = _owner(tmp_path, preparer)
    predecessor, task = _admit(owner)
    try:
        with pytest.raises(CoreTaskControlError):
            owner.run_successor_transition(
                task.task_id,
                accepted_attempt_id=task.attempts[0].attempt_id,
                plan=_plan(task),
            )
        failed = owner.get_successor_transition_for_task(task.task_id)
        assert failed.error is not None
        assert failed.error.retryable is False
        transition_id = failed.transition.successor_transition_id
        attempts_before = owner.successor_transition_attempts(transition_id)

        with pytest.raises(CoreTaskControlError) as rejected:
            owner.retry_successor_transition(
                transition_id,
                expected_project_head_id=(predecessor.active_project_head.project_head_id),
                retry_request_id="retry-nonretryable",
            )

        assert rejected.value.http_status == 409
        assert owner.successor_transition_attempts(transition_id) == attempts_before
        assert owner.get_successor_transition(transition_id) == failed
    finally:
        owner.close()


def _terminal_successor_authority_sha256(
    owner: CoreScienceTaskOwnerV2,
    successor_transition_id: str,
) -> str:
    payload = {
        "transition": owner.get_successor_transition(
            successor_transition_id
        ).model_dump(mode="json"),
        "attempts": [
            attempt.model_dump(mode="json")
            for attempt in owner.successor_transition_attempts(
                successor_transition_id
            )
        ],
        "commit": None,
        "artifacts": [],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class _CompletedMethodsReconciliationPreparer(_Preparer):
    def __init__(self) -> None:
        super().__init__()
        self.reject_materialization = True

    def _enter(self, phase: str) -> None:
        self.calls.append(phase)
        if phase == "materializing" and self.reject_materialization:
            raise ValueError("historical post-method materialization failure")


def test_completed_methods_reconciliation_is_no_job_and_idempotent(
    tmp_path: Path,
) -> None:
    preparer = _CompletedMethodsReconciliationPreparer()
    owner = _owner(tmp_path, preparer)
    predecessor, task = _admit(owner)
    try:
        with pytest.raises(CoreTaskControlError):
            owner.run_successor_transition(
                task.task_id,
                accepted_attempt_id=task.attempts[0].attempt_id,
                plan=_plan(task),
            )
        failed = owner.get_successor_transition_for_task(task.task_id)
        transition_id = failed.transition.successor_transition_id
        source_attempt = owner.successor_transition_attempts(transition_id)[-1]
        terminal_sha256 = _terminal_successor_authority_sha256(
            owner,
            transition_id,
        )
        calls_before_reconciliation = tuple(preparer.calls)

        with pytest.raises(CoreTaskControlError) as drifted:
            owner.reconcile_completed_successor_methods(
                transition_id,
                expected_project_head_id=(
                    predecessor.active_project_head.project_head_id
                ),
                expected_terminal_attempt_id=(
                    source_attempt.transition_attempt_id
                ),
                expected_terminal_authority_sha256="0" * 64,
                reconciliation_request_id="reconcile-completed-methods-1",
            )
        assert drifted.value.http_status == 412
        assert owner.successor_transition_attempts(transition_id) == [
            source_attempt,
        ]
        assert preparer.calls.count("running_methods") == 1

        preparer.reject_materialization = False
        committed = owner.reconcile_completed_successor_methods(
            transition_id,
            expected_project_head_id=(
                predecessor.active_project_head.project_head_id
            ),
            expected_terminal_attempt_id=source_attempt.transition_attempt_id,
            expected_terminal_authority_sha256=terminal_sha256,
            reconciliation_request_id="reconcile-completed-methods-1",
        )
        calls_after_commit = tuple(preparer.calls)
        attempts_after_commit = owner.successor_transition_attempts(transition_id)

        assert committed.state == "committed"
        assert preparer.calls.count("running_methods") == 1
        assert preparer.calls.count("reconciling_methods") == 3
        assert calls_before_reconciliation == (
            "sealing_dataset",
            "resolving_training_feedback",
            "running_methods",
            "validating",
            "materializing",
        )
        assert [
            (
                attempt.ordinal,
                attempt.state,
                attempt.reconciliation_only,
                attempt.reconciliation_source_attempt_id,
                attempt.reconciliation_source_authority_sha256,
            )
            for attempt in attempts_after_commit
        ] == [
            (1, "failed", False, None, None),
            (
                2,
                "committed",
                True,
                source_attempt.transition_attempt_id,
                terminal_sha256,
            ),
        ]

        replay = owner.reconcile_completed_successor_methods(
            transition_id,
            expected_project_head_id=(
                predecessor.active_project_head.project_head_id
            ),
            expected_terminal_attempt_id=source_attempt.transition_attempt_id,
            expected_terminal_authority_sha256=terminal_sha256,
            reconciliation_request_id="reconcile-completed-methods-1",
        )
        assert replay == committed
        assert tuple(preparer.calls) == calls_after_commit
        assert owner.successor_transition_attempts(transition_id) == (
            attempts_after_commit
        )
    finally:
        owner.close()


def test_completed_methods_reconciliation_resumes_after_owner_exit(
    tmp_path: Path,
) -> None:
    preparer = _CompletedMethodsReconciliationPreparer()
    owner = _owner(tmp_path, preparer)
    predecessor, task = _admit(owner)
    with pytest.raises(CoreTaskControlError):
        owner.run_successor_transition(
            task.task_id,
            accepted_attempt_id=task.attempts[0].attempt_id,
            plan=_plan(task),
        )
    failed = owner.get_successor_transition_for_task(task.task_id)
    transition_id = failed.transition.successor_transition_id
    source_attempt = owner.successor_transition_attempts(transition_id)[-1]
    terminal_sha256 = _terminal_successor_authority_sha256(owner, transition_id)
    owner._ledger.begin_completed_methods_reconciliation(
        transition_id,
        expected_project_head_id=(
            predecessor.active_project_head.project_head_id
        ),
        expected_terminal_attempt_id=source_attempt.transition_attempt_id,
        expected_terminal_authority_sha256=terminal_sha256,
        reconciliation_request_id="reconcile-after-owner-exit-1",
        now=datetime(2026, 7, 23, 8, tzinfo=timezone.utc),
    )
    owner.close()

    restarted_preparer = _CompletedMethodsReconciliationPreparer()
    restarted_preparer.reject_materialization = False
    restarted = _owner(tmp_path, restarted_preparer)
    try:
        committed = restarted.reconcile_completed_successor_methods(
            transition_id,
            expected_project_head_id=(
                predecessor.active_project_head.project_head_id
            ),
            expected_terminal_attempt_id=source_attempt.transition_attempt_id,
            expected_terminal_authority_sha256=terminal_sha256,
            reconciliation_request_id="reconcile-after-owner-exit-1",
        )
        assert committed.state == "committed"
        assert restarted_preparer.calls.count("running_methods") == 0
        assert restarted_preparer.calls.count("reconciling_methods") == 1
        assert len(restarted.successor_transition_attempts(transition_id)) == 2
    finally:
        restarted.close()


def test_stale_successor_attempt_cannot_mutate_a_new_retry(
    tmp_path: Path,
) -> None:
    preparer = _Preparer(fail_phase="materializing")
    owner = _owner(tmp_path, preparer)
    predecessor, task = _admit(owner)
    try:
        with pytest.raises(CoreTaskControlError):
            owner.run_successor_transition(
                task.task_id,
                accepted_attempt_id=task.attempts[0].attempt_id,
                plan=_plan(task),
            )
        failed = owner.get_successor_transition_for_task(task.task_id)
        assert failed.error is not None
        transition_id = failed.transition.successor_transition_id
        stale_attempt = owner.successor_transition_attempts(transition_id)[-1]
        ledger = owner._ledger
        resumed = ledger.retry_successor_transition(
            transition_id,
            expected_project_head_id=(predecessor.active_project_head.project_head_id),
            retry_request_id="retry-fenced",
            now=datetime(2026, 7, 23, 8, tzinfo=timezone.utc),
        )
        assert resumed.state == "running_methods"
        current_attempt = owner.successor_transition_attempts(transition_id)[-1]

        with pytest.raises(ScienceTaskPreconditionFailedV2):
            ledger.advance_successor_transition(
                transition_id,
                expected_transition_attempt_id=(stale_attempt.transition_attempt_id),
                state="validating",
                now=datetime(2026, 7, 23, 8, 0, 1, tzinfo=timezone.utc),
            )
        with pytest.raises(ScienceTaskPreconditionFailedV2):
            ledger.fail_successor_transition(
                transition_id,
                expected_transition_attempt_id=(stale_attempt.transition_attempt_id),
                error=failed.error,
                now=datetime(2026, 7, 23, 8, 0, 2, tzinfo=timezone.utc),
            )
        assert owner.get_successor_transition_for_task(task.task_id).state == "running_methods"

        fenced_failure = ledger.fail_successor_transition(
            transition_id,
            expected_transition_attempt_id=(current_attempt.transition_attempt_id),
            error=failed.error,
            now=datetime(2026, 7, 23, 8, 0, 3, tzinfo=timezone.utc),
        )
        assert fenced_failure.state == "failed"
    finally:
        owner.close()


def test_failed_dataset_seal_retries_from_pending_without_recovery_evidence(
    tmp_path: Path,
) -> None:
    preparer = _Preparer(fail_phase="sealing_dataset")
    owner = _owner(tmp_path, preparer)
    predecessor, task = _admit(owner)
    try:
        with pytest.raises(CoreTaskControlError):
            owner.run_successor_transition(
                task.task_id,
                accepted_attempt_id=task.attempts[0].attempt_id,
                plan=_plan(task),
            )
        failed = owner.get_successor_transition_for_task(task.task_id)
        assert failed.state == "failed"
        assert failed.progress_completed == 1

        preparer.fail_phase = None
        committed = owner.retry_successor_transition(
            failed.transition.successor_transition_id,
            expected_project_head_id=(predecessor.active_project_head.project_head_id),
            retry_request_id="retry-dataset-seal-1",
        )

        assert committed.state == "committed"
        assert preparer.calls == [
            "sealing_dataset",
            "sealing_dataset",
            "resolving_training_feedback",
            "running_methods",
            "validating",
            "materializing",
            "workspace",
        ]
        assert [
            event.state
            for event in owner.list_task_events(task.task_id)
            if isinstance(event, TransitionChangedEventV2)
        ] == [
            "pending",
            "sealing_dataset",
            "failed",
            "pending",
            "sealing_dataset",
            "running_methods",
            "validating",
            "materializing",
            "committing",
            "committed",
        ]
    finally:
        owner.close()


def test_production_worker_executes_a_durable_retry_asynchronously(
    tmp_path: Path,
) -> None:
    first = _owner(tmp_path, _Preparer(fail_phase="materializing"))
    predecessor, task = _admit(first)
    with pytest.raises(CoreTaskControlError):
        first.run_successor_transition(
            task.task_id,
            accepted_attempt_id=task.attempts[0].attempt_id,
            plan=_plan(task),
        )
    transition_id = first.get_successor_transition_for_task(
        task.task_id
    ).transition.successor_transition_id
    first.close()

    class _UnexpectedAttemptExecutor:
        def execute(self, **_kwargs: object) -> object:
            raise AssertionError("retry recovery must not execute another Attempt")

    preparer = _Preparer()
    restarted = CoreScienceTaskOwnerV2(
        state_root=tmp_path,
        clock=_Clock(),
        successor_preparer=preparer,
        attempt_executor_factory=lambda _ledger: _UnexpectedAttemptExecutor(),
    )
    try:
        accepted = restarted.retry_successor_transition(
            transition_id,
            expected_project_head_id=(predecessor.active_project_head.project_head_id),
            retry_request_id="retry-production-worker-1",
        )
        assert accepted.state == "running_methods"

        committed = None
        for _ in range(200):
            observed = restarted.get_successor_transition(transition_id)
            if observed.state == "committed":
                committed = observed
                break
            threading.Event().wait(0.01)

        assert committed is not None
        assert preparer.calls == [
            "recovering_dataset",
            "resolving_training_feedback",
            "running_methods",
            "validating",
            "materializing",
            "workspace",
        ]
    finally:
        restarted.close()


def test_retry_request_identity_does_not_create_a_second_attempt_after_failure(
    tmp_path: Path,
) -> None:
    preparer = _Preparer(fail_phase="materializing")
    owner = _owner(tmp_path, preparer)
    predecessor, task = _admit(owner)
    try:
        with pytest.raises(CoreTaskControlError):
            owner.run_successor_transition(
                task.task_id,
                accepted_attempt_id=task.attempts[0].attempt_id,
                plan=_plan(task),
            )
        transition_id = owner.get_successor_transition_for_task(
            task.task_id
        ).transition.successor_transition_id
        with pytest.raises(CoreTaskControlError):
            owner.retry_successor_transition(
                transition_id,
                expected_project_head_id=(predecessor.active_project_head.project_head_id),
                retry_request_id="retry-same-action",
            )
        replayed = owner.retry_successor_transition(
            transition_id,
            expected_project_head_id=(predecessor.active_project_head.project_head_id),
            retry_request_id="retry-same-action",
            allow_in_progress_recovery=True,
        )
        assert replayed.state == "failed"
        attempts = owner.successor_transition_attempts(transition_id)
        assert [
            (attempt.ordinal, attempt.state, attempt.retry_request_id) for attempt in attempts
        ] == [
            (1, "failed", "initial"),
            (2, "failed", "retry-same-action"),
        ]
    finally:
        owner.close()


def test_failed_transition_abandon_atomically_advances_accepted_workspace(
    tmp_path: Path,
) -> None:
    preparer = _Preparer(fail_phase="materializing", workspace_seed="9")
    owner = _owner(tmp_path, preparer)
    predecessor, task = _admit(owner)
    try:
        with pytest.raises(CoreTaskControlError):
            owner.run_successor_transition(
                task.task_id,
                accepted_attempt_id=task.attempts[0].attempt_id,
                plan=_plan(task),
            )
        failed = owner.get_successor_transition_for_task(task.task_id)

        cancelled = owner.abandon_successor_transition(
            failed.transition.successor_transition_id,
            expected_project_head_id=(predecessor.active_project_head.project_head_id),
            abandon_request_id="abandon-workspace-result",
        )

        assert cancelled.state == "cancelled"
        assert cancelled.error is None
        assert preparer.calls[-1] == "discarding_outputs"
        successor = cancelled.transition.successor_project_head
        assert successor is not None
        assert successor.generation == predecessor.active_project_head.generation + 1
        assert successor.predecessor_project_head_id == (
            predecessor.active_project_head.project_head_id
        )
        assert successor.workspace_snapshot == _workspace(task.project_id, "9")
        assert successor.evolution_revision == predecessor.active_project_head.evolution_revision
        assert (
            successor.runtime_context_snapshot
            == predecessor.active_project_head.runtime_context_snapshot
        )
        assert (
            successor.effective_execution_snapshot
            == predecessor.active_project_head.effective_execution_snapshot
        )
        assert owner.active_project_head(task.project_id) == successor
        completed = owner.invoke("getCoreTaskV2", {"task_id": task.task_id})
        assert completed.state == "completed"
        commit = owner.successor_commit(cancelled.transition.successor_transition_id)
        assert commit is not None
        assert commit.manifest.atomic_evolution_abandon_contract_version == "2"
        assert not hasattr(commit.manifest, "dataset_id")
        binding = runtime_context_binding_for_head(
            project_head=successor,
            service_generation_sha256="a" * 64,
            framework_lock_sha256="b" * 64,
            successor_commit=commit,
        )
        assert binding.source == "empty_inherited"
        assert binding.materialized_context_id is None
        assert binding.selected_artifact_ids == ()
        authority = owner.project_admission_authority(task.project_id)
        assert authority.blockers == ()
        next_task = owner.invoke(
            "submitCoreTaskV2",
            {"request": _request(authority), "idempotency_key": "after-abandon"},
        )
        assert next_task.admission.predecessor_project_head == successor
    finally:
        owner.close()

    restarted = _owner(tmp_path, _Preparer())
    try:
        assert (
            restarted.get_successor_transition(cancelled.transition.successor_transition_id)
            == cancelled
        )
        assert (
            restarted.invoke(
                "getCoreTaskV2",
                {"task_id": task.task_id},
            ).state
            == "completed"
        )
        assert (
            restarted.invoke(
                "getCoreTaskV2",
                {"task_id": next_task.task_id},
            ).state
            == "admitted"
        )
        assert restarted.project_admission_authority(task.project_id).blockers == ()
        assert restarted.active_project_head(task.project_id) == successor
    finally:
        restarted.close()


def test_cancelled_abandon_replay_retries_interrupted_output_cleanup(
    tmp_path: Path,
) -> None:
    class _InterruptedCleanupPreparer(_Preparer):
        def __init__(self) -> None:
            super().__init__(
                fail_phase="materializing",
                workspace_seed="9",
            )
            self.discard_attempts = 0

        def discard_transition_outputs(
            self,
            context: ScienceSuccessorCleanupContextV2,
        ) -> ScienceSuccessorCleanupReceiptV2:
            self._enter("discarding_outputs")
            self.discard_attempts += 1
            if self.discard_attempts == 1:
                raise SystemExit("simulated interruption after abandon commit")
            return ScienceSuccessorCleanupReceiptV2(
                successor_transition_id=(context.transition.transition.successor_transition_id),
            )

    preparer = _InterruptedCleanupPreparer()
    owner = _owner(tmp_path, preparer)
    predecessor, task = _admit(owner)
    try:
        with pytest.raises(CoreTaskControlError):
            owner.run_successor_transition(
                task.task_id,
                accepted_attempt_id=task.attempts[0].attempt_id,
                plan=_plan(task),
            )
        failed = owner.get_successor_transition_for_task(task.task_id)
        with pytest.raises(
            SystemExit,
            match="simulated interruption",
        ):
            owner.abandon_successor_transition(
                failed.transition.successor_transition_id,
                expected_project_head_id=(predecessor.active_project_head.project_head_id),
                abandon_request_id="abandon-cleanup-crash",
            )
        committed = owner.get_successor_transition(failed.transition.successor_transition_id)
        assert committed.state == "cancelled"
        assert preparer.discard_attempts == 1
    finally:
        owner.close()

    restarted = _owner(tmp_path, preparer)
    try:
        assert preparer.discard_attempts == 2
        replayed = restarted.abandon_successor_transition(
            failed.transition.successor_transition_id,
            expected_project_head_id=(predecessor.active_project_head.project_head_id),
            abandon_request_id="abandon-cleanup-crash",
            allow_cancelled_recovery=True,
        )
        assert replayed == committed
        assert preparer.discard_attempts == 2
    finally:
        restarted.close()


def test_abandon_cleanup_failure_blocks_admission_until_in_process_replay(
    tmp_path: Path,
) -> None:
    class _RetryableCleanupPreparer(_Preparer):
        def __init__(self) -> None:
            super().__init__(
                fail_phase="materializing",
                workspace_seed="9",
            )
            self.discard_attempts = 0

        def discard_transition_outputs(
            self,
            context: ScienceSuccessorCleanupContextV2,
        ) -> ScienceSuccessorCleanupReceiptV2:
            self._enter("discarding_outputs")
            self.discard_attempts += 1
            if self.discard_attempts == 1:
                raise RuntimeError("transport failed before durable discard")
            return ScienceSuccessorCleanupReceiptV2(
                successor_transition_id=(context.transition.transition.successor_transition_id),
            )

    preparer = _RetryableCleanupPreparer()
    owner = _owner(tmp_path, preparer)
    predecessor, task = _admit(owner)
    try:
        with pytest.raises(CoreTaskControlError):
            owner.run_successor_transition(
                task.task_id,
                accepted_attempt_id=(task.attempts[0].attempt_id),
                plan=_plan(task),
            )
        failed = owner.get_successor_transition_for_task(task.task_id)
        transition_id = failed.transition.successor_transition_id

        with pytest.raises(CoreTaskControlError) as interrupted:
            owner.abandon_successor_transition(
                transition_id,
                expected_project_head_id=(predecessor.active_project_head.project_head_id),
                abandon_request_id="abandon-cleanup-retry",
            )
        assert interrupted.value.code == "task_owner_unavailable"
        assert interrupted.value.retryable is True
        cancelled = owner.get_successor_transition(transition_id)
        assert cancelled.state == "cancelled"
        assert owner._ledger.successor_cleanup_receipt(transition_id) is None
        assert owner._ledger.pending_successor_cleanup_ids() == [transition_id]
        blocked = owner.project_admission_authority(task.project_id)
        assert blocked.blockers == (ScienceProjectReadinessBlockerV2.SUCCESSOR_TRANSITION,)
        with pytest.raises(CoreTaskControlError) as not_ready:
            owner.invoke(
                "submitCoreTaskV2",
                {
                    "request": _request(blocked),
                    "idempotency_key": ("blocked-before-durable-cleanup"),
                },
            )
        assert not_ready.value.code == "project_not_ready"

        unblocked = ScienceProjectAdmissionAuthorityV2(
            project_id=blocked.project_id,
            active_project_head=blocked.active_project_head,
            project_config_sha256=(blocked.project_config_sha256),
            workspace_snapshot=blocked.workspace_snapshot,
            normalized_evolution_intent_sha256=(blocked.normalized_evolution_intent_sha256),
            blockers=(),
        )
        with pytest.raises(CoreTaskControlError) as bypass:
            owner.publish_project_admission_authority(
                unblocked,
                expected_project_head_id=(blocked.active_project_head.project_head_id),
            )
        assert bypass.value.code == "task_precondition_failed"
        assert owner.project_admission_authority(task.project_id) == blocked
        assert owner._ledger.pending_successor_cleanup_ids() == [transition_id]

        replayed = owner.abandon_successor_transition(
            transition_id,
            expected_project_head_id=(predecessor.active_project_head.project_head_id),
            abandon_request_id="abandon-cleanup-retry",
            allow_cancelled_recovery=True,
        )
        assert replayed == cancelled
        assert preparer.discard_attempts == 2
        assert owner._ledger.successor_cleanup_receipt(transition_id) is not None
        assert owner._ledger.pending_successor_cleanup_ids() == []
        ready = owner.project_admission_authority(task.project_id)
        assert ready.blockers == ()
        next_task = owner.invoke(
            "submitCoreTaskV2",
            {
                "request": _request(ready),
                "idempotency_key": ("after-durable-cleanup"),
            },
        )
        assert next_task.state == "admitted"
    finally:
        owner.close()


def test_restart_rejects_deleted_successor_abandon_receipt(
    tmp_path: Path,
) -> None:
    preparer = _Preparer(
        fail_phase="materializing",
        workspace_seed="9",
    )
    owner = _owner(tmp_path, preparer)
    predecessor, task = _admit(owner)
    try:
        with pytest.raises(CoreTaskControlError):
            owner.run_successor_transition(
                task.task_id,
                accepted_attempt_id=task.attempts[0].attempt_id,
                plan=_plan(task),
            )
        failed = owner.get_successor_transition_for_task(task.task_id)
        cancelled = owner.abandon_successor_transition(
            failed.transition.successor_transition_id,
            expected_project_head_id=(predecessor.active_project_head.project_head_id),
            abandon_request_id="abandon-before-receipt-delete",
        )
        assert cancelled.state == "cancelled"
    finally:
        owner.close()

    database = tmp_path / "science-tasks-v2" / "science-tasks-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM successor_abandon_receipts WHERE successor_transition_id = ?",
            (failed.transition.successor_transition_id,),
        )
        connection.commit()

    with pytest.raises(
        ScienceTaskStoreV2Error,
        match="abandon authority receipt",
    ):
        _owner(tmp_path, _Preparer())


def test_restart_rejects_cross_bound_successor_abandon_receipt(
    tmp_path: Path,
) -> None:
    preparer = _Preparer(
        fail_phase="materializing",
        workspace_seed="9",
    )
    owner = _owner(tmp_path, preparer)
    predecessor, first_task = _admit(owner)
    try:
        with pytest.raises(CoreTaskControlError):
            owner.run_successor_transition(
                first_task.task_id,
                accepted_attempt_id=(first_task.attempts[0].attempt_id),
                plan=_plan(first_task),
            )
        first_failed = owner.get_successor_transition_for_task(first_task.task_id)
        owner.abandon_successor_transition(
            first_failed.transition.successor_transition_id,
            expected_project_head_id=(predecessor.active_project_head.project_head_id),
            abandon_request_id="abandon-cross-bind-1",
        )
        next_authority = owner.project_admission_authority(first_task.project_id)
        second_task = owner.invoke(
            "submitCoreTaskV2",
            {
                "request": _request(next_authority),
                "idempotency_key": ("submit-abandon-cross-bind-2"),
            },
        )
        with pytest.raises(CoreTaskControlError):
            owner.run_successor_transition(
                second_task.task_id,
                accepted_attempt_id=(second_task.attempts[0].attempt_id),
                plan=_plan(second_task),
            )
        second_failed = owner.get_successor_transition_for_task(second_task.task_id)
    finally:
        owner.close()

    database = tmp_path / "science-tasks-v2" / "science-tasks-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        second_attempt_id = connection.execute(
            "SELECT transition_attempt_id FROM "
            "successor_transition_attempts WHERE "
            "successor_transition_id = ? "
            "ORDER BY ordinal DESC LIMIT 1",
            (second_failed.transition.successor_transition_id,),
        ).fetchone()[0]
        connection.execute(
            "UPDATE successor_abandon_receipts SET "
            "transition_attempt_id = ? "
            "WHERE successor_transition_id = ?",
            (
                second_attempt_id,
                first_failed.transition.successor_transition_id,
            ),
        )
        connection.commit()

    with pytest.raises(
        ScienceTaskStoreV2Error,
        match="abandon receipt has no exact cancelled authority",
    ):
        _owner(tmp_path, _Preparer())


def test_legacy_cancelled_successor_migrates_to_unattributed_exemption(
    tmp_path: Path,
) -> None:
    preparer = _Preparer(
        fail_phase="materializing",
        workspace_seed="9",
    )
    owner = _owner(tmp_path, preparer)
    predecessor, task = _admit(owner)
    try:
        with pytest.raises(CoreTaskControlError):
            owner.run_successor_transition(
                task.task_id,
                accepted_attempt_id=task.attempts[0].attempt_id,
                plan=_plan(task),
            )
        failed = owner.get_successor_transition_for_task(task.task_id)
        cancelled = owner.abandon_successor_transition(
            failed.transition.successor_transition_id,
            expected_project_head_id=(predecessor.active_project_head.project_head_id),
            abandon_request_id=("abandon-before-legacy-downgrade"),
        )
        assert cancelled.state == "cancelled"
    finally:
        owner.close()

    database = tmp_path / "science-tasks-v2" / "science-tasks-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        for table in (
            "task_close_receipts",
            "legacy_task_close_exemptions",
            "successor_abandon_receipts",
            "legacy_successor_abandon_exemptions",
            "action_receipt_migrations",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.commit()

    migrated = _owner(tmp_path, _Preparer())
    try:
        with sqlite3.connect(database) as connection:
            exemption = connection.execute(
                "SELECT expected_project_head_id, "
                "transition_attempt_id FROM "
                "legacy_successor_abandon_exemptions "
                "WHERE successor_transition_id = ?",
                (failed.transition.successor_transition_id,),
            ).fetchone()
            expected_attempt_id = connection.execute(
                "SELECT transition_attempt_id FROM "
                "successor_transition_attempts WHERE "
                "successor_transition_id = ? "
                "ORDER BY ordinal DESC LIMIT 1",
                (failed.transition.successor_transition_id,),
            ).fetchone()[0]
        assert exemption == (
            predecessor.active_project_head.project_head_id,
            expected_attempt_id,
        )
        with pytest.raises(CoreTaskControlError) as replay:
            migrated.abandon_successor_transition(
                failed.transition.successor_transition_id,
                expected_project_head_id=(predecessor.active_project_head.project_head_id),
                abandon_request_id=("abandon-before-legacy-downgrade"),
                allow_cancelled_recovery=True,
            )
        assert replay.value.code == "task_terminal"
    finally:
        migrated.close()


def test_restart_rejects_pending_cleanup_without_readiness_blocker(
    tmp_path: Path,
) -> None:
    class _InterruptedCleanupPreparer(_Preparer):
        def discard_transition_outputs(
            self,
            context: ScienceSuccessorCleanupContextV2,
        ) -> ScienceSuccessorCleanupReceiptV2:
            self._enter("discarding_outputs")
            raise SystemExit("simulated cleanup interruption")

    preparer = _InterruptedCleanupPreparer(
        fail_phase="materializing",
        workspace_seed="9",
    )
    owner = _owner(tmp_path, preparer)
    predecessor, task = _admit(owner)
    try:
        with pytest.raises(CoreTaskControlError):
            owner.run_successor_transition(
                task.task_id,
                accepted_attempt_id=(task.attempts[0].attempt_id),
                plan=_plan(task),
            )
        failed = owner.get_successor_transition_for_task(task.task_id)
        with pytest.raises(
            SystemExit,
            match="cleanup interruption",
        ):
            owner.abandon_successor_transition(
                failed.transition.successor_transition_id,
                expected_project_head_id=(predecessor.active_project_head.project_head_id),
                abandon_request_id="abandon-blocker-crash",
            )
        blocked = owner.project_admission_authority(task.project_id)
        assert blocked.blockers == (ScienceProjectReadinessBlockerV2.SUCCESSOR_TRANSITION,)
    finally:
        owner.close()

    corrupted = ScienceProjectAdmissionAuthorityV2(
        project_id=blocked.project_id,
        active_project_head=blocked.active_project_head,
        project_config_sha256=(blocked.project_config_sha256),
        workspace_snapshot=blocked.workspace_snapshot,
        normalized_evolution_intent_sha256=(blocked.normalized_evolution_intent_sha256),
        blockers=(),
    )
    database = tmp_path / "science-tasks-v2" / "science-tasks-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE project_admission_authorities SET authority_json = ? WHERE project_id = ?",
            (
                task_store_module._v2_authority_bytes(corrupted),
                corrupted.project_id,
            ),
        )
        connection.commit()

    with pytest.raises(
        ScienceTaskStoreV2Error,
        match="pending v2 successor cleanup.*readiness blocker",
    ):
        task_store_module.ScienceTaskStoreV2(tmp_path / "science-tasks-v2")


def test_abandon_without_a_sealed_dataset_never_fabricates_one(
    tmp_path: Path,
) -> None:
    preparer = _Preparer(fail_phase="sealing_dataset", workspace_seed="9")
    owner = _owner(tmp_path, preparer)
    predecessor, task = _admit(owner)
    try:
        with pytest.raises(CoreTaskControlError):
            owner.run_successor_transition(
                task.task_id,
                accepted_attempt_id=task.attempts[0].attempt_id,
                plan=_plan(task),
            )
        failed = owner.get_successor_transition_for_task(task.task_id)
        cancelled = owner.abandon_successor_transition(
            failed.transition.successor_transition_id,
            expected_project_head_id=(predecessor.active_project_head.project_head_id),
            abandon_request_id="abandon-before-dataset",
        )
        successor = cancelled.transition.successor_project_head
        assert successor is not None
        assert successor.workspace_snapshot == _workspace(task.project_id, "9")
        commit = owner.successor_commit(cancelled.transition.successor_transition_id)
        assert commit is not None
        assert commit.manifest.atomic_evolution_abandon_contract_version == "2"
        assert not hasattr(commit.manifest, "dataset_id")
        assert not any(
            event.event_type == "dataset_sealed" for event in owner.list_task_events(task.task_id)
        )
        assert preparer.calls == [
            "sealing_dataset",
            "workspace",
            "discarding_outputs",
        ]
    finally:
        owner.close()


def test_abandon_inherits_the_exact_materialized_runtime_authority(
    tmp_path: Path,
) -> None:
    preparer = _Preparer()
    owner = _owner(tmp_path, preparer)
    _genesis, first_task = _admit(owner)
    try:
        first_transition = owner.run_successor_transition(
            first_task.task_id,
            accepted_attempt_id=first_task.attempts[0].attempt_id,
            plan=_plan(first_task),
        )
        first_head = first_transition.transition.successor_project_head
        assert first_head is not None
        first_commit = owner.successor_commit(first_transition.transition.successor_transition_id)
        assert first_commit is not None

        preparer.fail_phase = "materializing"
        preparer.workspace_seed = "9"
        authority = owner.project_admission_authority(first_task.project_id)
        second_task = owner.invoke(
            "submitCoreTaskV2",
            {
                "request": _request(authority),
                "idempotency_key": "submit-before-inherited-abandon",
            },
        )
        with pytest.raises(CoreTaskControlError):
            owner.run_successor_transition(
                second_task.task_id,
                accepted_attempt_id=second_task.attempts[0].attempt_id,
                plan=_plan(second_task),
            )
        failed = owner.get_successor_transition_for_task(second_task.task_id)
        cancelled = owner.abandon_successor_transition(
            failed.transition.successor_transition_id,
            expected_project_head_id=first_head.project_head_id,
            abandon_request_id="abandon-inherit-artifacts",
        )
        inherited_head = cancelled.transition.successor_project_head
        assert inherited_head is not None
        assert inherited_head.workspace_snapshot == _workspace(
            second_task.project_id,
            "9",
        )
        assert inherited_head.evolution_revision == first_head.evolution_revision
        assert inherited_head.runtime_context_snapshot == first_head.runtime_context_snapshot

        inherited_commit = owner.successor_commit(cancelled.transition.successor_transition_id)
        assert inherited_commit is not None
        assert (
            inherited_commit.manifest.method_artifact_ids
            == first_commit.manifest.method_artifact_ids
        )
        assert tuple(
            (
                item.target_id,
                item.artifact_id,
                item.artifact_type,
                item.owner_successor_transition_id,
            )
            for item in inherited_commit.manifest.artifacts
        ) == tuple(
            (
                item.target_id,
                item.artifact_id,
                item.artifact_type,
                item.owner_successor_transition_id,
            )
            for item in first_commit.manifest.artifacts
        )
        assert {item.origin for item in inherited_commit.manifest.artifacts} == {"inherited"}
        binding = runtime_context_binding_for_head(
            project_head=inherited_head,
            service_generation_sha256="a" * 64,
            framework_lock_sha256="b" * 64,
            successor_commit=inherited_commit,
        )
        assert binding.source == "materialized_inherited"
        assert binding.successor_transition_id == (first_commit.manifest.successor_transition_id)
        assert binding.source_predecessor_project_head_id == (
            first_commit.manifest.predecessor_project_head_id
        )
        assert binding.materialized_context_id == (first_commit.manifest.materialized_context_id)
        assert binding.materialized_context_manifest_sha256 == (
            first_commit.manifest.materialized_context_manifest_sha256
        )
        assert binding.selected_artifact_ids == (first_commit.manifest.method_artifact_ids)
    finally:
        owner.close()


def test_abandon_after_no_evolution_preserves_original_runtime_source(
    tmp_path: Path,
) -> None:
    preparer = _InheritanceChainPreparer()
    owner = _owner(tmp_path, preparer)
    _genesis, first_task = _admit(owner)
    first = owner.run_successor_transition(
        first_task.task_id,
        accepted_attempt_id=first_task.attempts[0].attempt_id,
        plan=_plan(first_task),
    )
    first_head = first.transition.successor_project_head
    assert first_head is not None
    first_commit = owner.successor_commit(first.transition.successor_transition_id)
    assert first_commit is not None

    preparer.inherited_commit = first_commit
    authority = owner.project_admission_authority(first_task.project_id)
    second_task = owner.invoke(
        "submitCoreTaskV2",
        {
            "request": _request(authority),
            "idempotency_key": "no-evolution-middle-task",
        },
    )
    no_evolution_plan = ScienceSuccessorPlanV2.model_validate(
        {
            **_plan(second_task).model_dump(mode="python"),
            "enabled_methods": (),
        }
    )
    second = owner.run_successor_transition(
        second_task.task_id,
        accepted_attempt_id=second_task.attempts[0].attempt_id,
        plan=no_evolution_plan,
    )
    second_head = second.transition.successor_project_head
    assert second_head is not None
    second_commit = owner.successor_commit(second.transition.successor_transition_id)
    assert second_commit is not None
    assert second_commit.manifest.runtime_context_source == "materialized_inherited"
    assert (
        second_commit.manifest.materialized_source_successor_transition_id
        == first.transition.successor_transition_id
    )

    preparer.inherited_commit = second_commit
    preparer.fail_phase = "materializing"
    preparer.workspace_seed = "9"
    third_authority = owner.project_admission_authority(first_task.project_id)
    third_task = owner.invoke(
        "submitCoreTaskV2",
        {
            "request": _request(third_authority),
            "idempotency_key": "abandon-after-no-evolution",
        },
    )
    with pytest.raises(CoreTaskControlError):
        owner.run_successor_transition(
            third_task.task_id,
            accepted_attempt_id=third_task.attempts[0].attempt_id,
            plan=_plan(third_task),
        )
    failed = owner.get_successor_transition_for_task(third_task.task_id)
    cancelled = owner.abandon_successor_transition(
        failed.transition.successor_transition_id,
        expected_project_head_id=second_head.project_head_id,
        abandon_request_id="abandon-inherit-context",
    )
    abandoned_commit = owner.successor_commit(cancelled.transition.successor_transition_id)
    assert abandoned_commit is not None
    assert (
        abandoned_commit.manifest.runtime_context_source
        == second_commit.manifest.runtime_context_source
    )
    assert (
        abandoned_commit.manifest.materialized_source_successor_transition_id
        == first.transition.successor_transition_id
    )
    assert (
        abandoned_commit.manifest.materialized_source_predecessor_project_head_id
        == first_commit.manifest.predecessor_project_head_id
    )
    assert (
        abandoned_commit.manifest.materialized_context_id
        == first_commit.manifest.materialized_context_id
    )
    assert tuple(
        (
            item.target_id,
            item.artifact_id,
            item.artifact_type,
            item.owner_successor_transition_id,
        )
        for item in abandoned_commit.manifest.artifacts
    ) == tuple(
        (
            item.target_id,
            item.artifact_id,
            item.artifact_type,
            item.owner_successor_transition_id,
        )
        for item in first_commit.manifest.artifacts
    )
    final_head = cancelled.transition.successor_project_head
    assert final_head is not None
    owner.close()

    restarted = _owner(tmp_path, _Preparer())
    try:
        assert restarted.active_project_head(first_task.project_id) == final_head
        assert (
            restarted.successor_commit(cancelled.transition.successor_transition_id)
            == abandoned_commit
        )
    finally:
        restarted.close()


def test_restart_fails_closed_on_rewritten_cancelled_transition_progress(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path, _Preparer(fail_phase="materializing"))
    predecessor, task = _admit(owner)
    with pytest.raises(CoreTaskControlError):
        owner.run_successor_transition(
            task.task_id,
            accepted_attempt_id=task.attempts[0].attempt_id,
            plan=_plan(task),
        )
    failed = owner.get_successor_transition_for_task(task.task_id)
    cancelled = owner.abandon_successor_transition(
        failed.transition.successor_transition_id,
        expected_project_head_id=predecessor.active_project_head.project_head_id,
        abandon_request_id="abandon-corrupt-transition",
    )
    owner.close()

    database = tmp_path / "science-tasks-v2" / "science-tasks-v2.sqlite3"
    rewritten = cancelled.model_copy(
        update={"progress_completed": cancelled.progress_completed - 1}
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE successor_transitions SET transition_json = ? "
            "WHERE successor_transition_id = ?",
            (
                task_store_module._v2_model_bytes(rewritten),
                cancelled.transition.successor_transition_id,
            ),
        )
        connection.commit()

    with pytest.raises(ScienceTaskStoreV2Error, match="event history"):
        _owner(tmp_path, _Preparer())


def test_db_commit_failure_exposes_no_partial_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparer = _Preparer()
    owner = _owner(tmp_path, preparer)
    predecessor, task = _admit(owner)

    def fail_commit(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected commit failure")

    monkeypatch.setattr(task_store_module, "_before_v2_successor_commit", fail_commit)
    try:
        with pytest.raises(CoreTaskControlError):
            owner.run_successor_transition(
                task.task_id,
                accepted_attempt_id=task.attempts[0].attempt_id,
                plan=_plan(task),
            )
        transition = owner.get_successor_transition_for_task(task.task_id)
        assert transition.state == "failed"
        assert owner.active_project_head(task.project_id) == predecessor.active_project_head
        assert owner.list_project_heads(task.project_id) == [predecessor.active_project_head]
        assert owner.successor_commit(transition.transition.successor_transition_id) is None
    finally:
        owner.close()


def test_next_task_is_not_created_while_successor_methods_are_running(
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    preparer = _Preparer(block_methods=(entered, release))
    owner = _owner(tmp_path, preparer)
    predecessor, task = _admit(owner)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                owner.run_successor_transition,
                task.task_id,
                accepted_attempt_id=task.attempts[0].attempt_id,
                plan=_plan(task),
            )
            assert entered.wait(timeout=5)
            with pytest.raises(CoreTaskControlError) as not_ready:
                owner.invoke(
                    "submitCoreTaskV2",
                    {
                        "request": _request(predecessor),
                        "idempotency_key": "concurrent-next",
                    },
                )
            assert not_ready.value.code == "project_not_ready"
            assert owner.ownership_counts() == (1, 1, 1)
            release.set()
            assert future.result(timeout=5).state == "committed"
    finally:
        release.set()
        owner.close()


def test_interrupted_transition_recovers_failed_without_advancing_head(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path, _Preparer(crash=True))
    predecessor, task = _admit(owner)
    with pytest.raises(SystemExit):
        owner.run_successor_transition(
            task.task_id,
            accepted_attempt_id=task.attempts[0].attempt_id,
            plan=_plan(task),
        )
    transition_id = owner.get_successor_transition_for_task(
        task.task_id
    ).transition.successor_transition_id
    owner.close()

    restarted = _owner(tmp_path, _Preparer())
    try:
        recovered = restarted.get_successor_transition(transition_id)
        assert recovered.state == "failed"
        assert recovered.error is not None
        assert recovered.error.code == "successor_transition_interrupted"
        assert recovered.error.retryable is True
        assert recovered.error.repair_action == "retry"
        assert restarted.active_project_head(task.project_id) == (predecessor.active_project_head)
        assert restarted.list_project_heads(task.project_id) == [predecessor.active_project_head]
    finally:
        restarted.close()


def test_restart_fails_closed_on_tampered_atomic_successor_receipt(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path, _Preparer())
    _predecessor, task = _admit(owner)
    owner.run_successor_transition(
        task.task_id,
        accepted_attempt_id=task.attempts[0].attempt_id,
        plan=_plan(task),
    )
    owner.close()

    database = tmp_path / "science-tasks-v2" / "science-tasks-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT successor_transition_id, commit_json FROM successor_commits"
        ).fetchone()
        assert row is not None
        corrupted = bytes(row[1]).replace(b'"dataset_id":"dataset-1"', b'"dataset_id":"dataset-2"')
        assert corrupted != bytes(row[1])
        connection.execute(
            "UPDATE successor_commits SET commit_json = ? WHERE successor_transition_id = ?",
            (corrupted, row[0]),
        )
        connection.commit()

    with pytest.raises(ScienceTaskStoreV2Error, match="persisted v2 document"):
        _owner(tmp_path, _Preparer())


def test_commit_closure_rejects_successor_with_forged_inherited_context(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path, _Preparer())
    predecessor, task = _admit(owner)
    committed = owner.run_successor_transition(
        task.task_id,
        accepted_attempt_id=task.attempts[0].attempt_id,
        plan=_plan(task),
    )
    receipt = owner.successor_commit(committed.transition.successor_transition_id)
    assert receipt is not None
    try:
        manifest = receipt.manifest.model_copy(
            update={
                "runtime_context_source": ("materialized_inherited"),
                "materialized_source_successor_transition_id": (
                    receipt.manifest.successor_transition_id
                ),
                "materialized_source_predecessor_project_head_id": (
                    predecessor.active_project_head.project_head_id
                ),
            }
        )
        forged = AtomicSuccessorCommitV2(
            manifest_sha256=atomic_successor_manifest_sha256(manifest),
            manifest=manifest,
        )
        database = tmp_path / "science-tasks-v2" / "science-tasks-v2.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.row_factory = sqlite3.Row
            with pytest.raises(
                ScienceTaskPreconditionFailedV2,
                match="materialized|evolution|successor",
            ):
                task_store_module._validate_v2_successor_commit_closure(
                    connection=connection,
                    task=owner.invoke(
                        "getCoreTaskV2",
                        {"task_id": task.task_id},
                    ),
                    transition=committed,
                    successor=(committed.transition.successor_project_head),
                    commit=forged,
                )
    finally:
        owner.close()


def test_commit_closure_rejects_enabled_successor_without_typed_composition(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path, _Preparer())
    _predecessor, task = _admit(owner)
    committed = owner.run_successor_transition(
        task.task_id,
        accepted_attempt_id=task.attempts[0].attempt_id,
        plan=_plan(task),
    )
    receipt = owner.successor_commit(committed.transition.successor_transition_id)
    assert receipt is not None
    try:
        manifest = receipt.manifest.model_copy(update={"artifacts": ()})
        forged = AtomicSuccessorCommitV2(
            manifest_sha256=atomic_successor_manifest_sha256(manifest),
            manifest=manifest,
        )
        database = tmp_path / "science-tasks-v2" / "science-tasks-v2.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.row_factory = sqlite3.Row
            with pytest.raises(
                ScienceTaskPreconditionFailedV2,
                match="artifact|composition|plan",
            ):
                task_store_module._validate_v2_successor_commit_closure(
                    connection=connection,
                    task=owner.invoke(
                        "getCoreTaskV2",
                        {"task_id": task.task_id},
                    ),
                    transition=committed,
                    successor=(committed.transition.successor_project_head),
                    commit=forged,
                )
    finally:
        owner.close()


def _downgrade_successor_chain_to_legacy_schema(
    database: Path,
) -> dict[str, str]:
    legacy_manifest_sha256_by_transition: dict[str, str] = {}
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT successor_transition_id, commit_json "
            "FROM successor_commits ORDER BY successor_transition_id"
        ).fetchall()
        for row in rows:
            transition_id = str(row["successor_transition_id"])
            commit = task_store_module._v2_model_from_bytes(
                AtomicSuccessorCommitV2,
                bytes(row["commit_json"]),
            )
            legacy_manifest = commit.manifest.model_copy(update={"artifacts": ()})
            legacy_commit = AtomicSuccessorCommitV2(
                manifest_sha256=atomic_successor_manifest_sha256(legacy_manifest),
                manifest=legacy_manifest,
            )
            attempt_row = connection.execute(
                "SELECT transition_attempt_id, attempt_json "
                "FROM successor_transition_attempts "
                "WHERE successor_transition_id = ? "
                "ORDER BY ordinal DESC LIMIT 1",
                (transition_id,),
            ).fetchone()
            assert attempt_row is not None
            attempt = task_store_module._v2_model_from_bytes(
                task_store_module.ScienceSuccessorTransitionAttemptV2,
                bytes(attempt_row["attempt_json"]),
            )
            legacy_attempt = attempt.model_copy(
                update={"commit_manifest_sha256": (legacy_commit.manifest_sha256)}
            )
            connection.execute(
                "UPDATE successor_commits SET manifest_sha256 = ?, "
                "commit_json = ? WHERE successor_transition_id = ?",
                (
                    legacy_commit.manifest_sha256,
                    task_store_module._v2_model_bytes(legacy_commit),
                    transition_id,
                ),
            )
            connection.execute(
                "UPDATE successor_transition_attempts SET "
                "attempt_json = ? WHERE transition_attempt_id = ?",
                (
                    task_store_module._v2_model_bytes(legacy_attempt),
                    attempt.transition_attempt_id,
                ),
            )
            legacy_manifest_sha256_by_transition[transition_id] = legacy_commit.manifest_sha256

        connection.execute("DROP INDEX IF EXISTS successor_commits_project_head_idx")
        connection.execute("ALTER TABLE successor_commits DROP COLUMN successor_project_head_id")
        connection.execute(
            "ALTER TABLE successor_commits DROP COLUMN composition_semantics_version"
        )
        connection.execute("ALTER TABLE successor_commits DROP COLUMN legacy_manifest_sha256")
        connection.commit()
    return legacy_manifest_sha256_by_transition


def _assert_legacy_successor_chain_migration(
    tmp_path: Path,
    *,
    second_target_id: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparer = _LegacyMigrationChainPreparer(
        "text_memory",
        ordinal=1,
    )
    owner = _owner(tmp_path, preparer)
    try:
        _authority, first_task = _admit(owner)
        first_transition = owner.run_successor_transition(
            first_task.task_id,
            accepted_attempt_id=first_task.attempts[0].attempt_id,
            plan=_legacy_chain_plan(
                first_task,
                "text_memory",
            ),
        )
        second_authority = owner.project_admission_authority(first_task.project_id)
        second_task = owner.invoke(
            "submitCoreTaskV2",
            {
                "request": _request(second_authority),
                "idempotency_key": "legacy-chain-submit-2",
            },
        )
        preparer.target_id = second_target_id
        preparer.ordinal = 2
        preparer.workspace_seed = "e"
        # Reproduce the pre-composition writer boundary: it allowed a
        # successor to replace the entire artifact set and materialized a
        # distinct empty context for a no-target plan.
        with monkeypatch.context() as legacy_writer:
            legacy_writer.setattr(
                task_owner_module,
                "_validate_successor_materialization_receipt",
                lambda value, **_kwargs: value,
            )
            legacy_writer.setattr(
                task_store_module,
                "_validate_v2_successor_commit_closure",
                lambda **_kwargs: None,
            )
            second_transition = owner.run_successor_transition(
                second_task.task_id,
                accepted_attempt_id=(second_task.attempts[0].attempt_id),
                plan=_legacy_chain_plan(
                    second_task,
                    second_target_id,
                ),
            )
        heads_before_migration = tuple(owner.list_project_heads(first_task.project_id))
    finally:
        owner.close()

    transition_ids = (
        first_transition.transition.successor_transition_id,
        second_transition.transition.successor_transition_id,
    )
    database = tmp_path / "science-tasks-v2" / "science-tasks-v2.sqlite3"
    legacy_sha256_by_transition = _downgrade_successor_chain_to_legacy_schema(database)

    restarted = _owner(tmp_path, _Preparer())
    try:
        heads_after_migration = tuple(restarted.list_project_heads(first_task.project_id))
        commits_after_migration = {
            transition_id: restarted.successor_commit(transition_id)
            for transition_id in transition_ids
        }
    finally:
        restarted.close()

    assert heads_after_migration == heads_before_migration
    heads_by_generation = {head.generation: head for head in heads_after_migration}
    assert tuple(sorted(heads_by_generation)) == (0, 1, 2)
    assert (
        heads_by_generation[1].predecessor_project_head_id
        == heads_by_generation[0].project_head_id
    )
    assert (
        heads_by_generation[2].predecessor_project_head_id
        == heads_by_generation[1].project_head_id
    )

    first_commit = commits_after_migration[transition_ids[0]]
    second_commit = commits_after_migration[transition_ids[1]]
    assert first_commit is not None
    assert second_commit is not None
    assert (
        first_commit.manifest.successor_project_head_id == heads_by_generation[1].project_head_id
    )
    assert (
        second_commit.manifest.successor_project_head_id == heads_by_generation[2].project_head_id
    )
    assert [
        (
            artifact.target_id,
            artifact.artifact_type,
            artifact.origin,
            artifact.owner_successor_transition_id,
        )
        for artifact in first_commit.manifest.artifacts
    ] == [
        (
            "text_memory",
            "text_memory",
            "produced",
            transition_ids[0],
        )
    ]
    if second_target_id is None:
        assert second_commit.manifest.method_artifact_ids == ()
        assert second_commit.manifest.artifacts == ()
        assert heads_by_generation[2].evolution_revision.artifact_count == 0
    else:
        assert [
            (
                artifact.target_id,
                artifact.artifact_type,
                artifact.origin,
                artifact.owner_successor_transition_id,
            )
            for artifact in second_commit.manifest.artifacts
        ] == [
            (
                second_target_id,
                second_target_id,
                "produced",
                transition_ids[1],
            )
        ]
        assert all(
            artifact.target_id != "text_memory" for artifact in second_commit.manifest.artifacts
        )
        assert heads_by_generation[2].evolution_revision.artifact_count == 1

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        provenance_rows = connection.execute(
            "SELECT successor_transition_id, "
            "successor_project_head_id, "
            "composition_semantics_version, "
            "legacy_manifest_sha256, manifest_sha256 "
            "FROM successor_commits"
        ).fetchall()
        attempt_rows = connection.execute(
            "SELECT successor_transition_id, attempt_json FROM successor_transition_attempts"
        ).fetchall()
    provenance_by_transition = {
        str(row["successor_transition_id"]): row for row in provenance_rows
    }
    attempt_by_transition = {
        str(row["successor_transition_id"]): (
            task_store_module._v2_model_from_bytes(
                task_store_module.ScienceSuccessorTransitionAttemptV2,
                bytes(row["attempt_json"]),
            )
        )
        for row in attempt_rows
    }
    for transition_id, commit in commits_after_migration.items():
        assert commit is not None
        provenance = provenance_by_transition[transition_id]
        assert provenance["successor_project_head_id"] == (
            commit.manifest.successor_project_head_id
        )
        assert provenance["composition_semantics_version"] == 1
        assert provenance["legacy_manifest_sha256"] == (legacy_sha256_by_transition[transition_id])
        assert provenance["manifest_sha256"] == (atomic_successor_manifest_sha256(commit.manifest))
        assert (
            attempt_by_transition[transition_id].commit_manifest_sha256
            == legacy_sha256_by_transition[transition_id]
        )
    assert legacy_sha256_by_transition[transition_ids[0]] != first_commit.manifest_sha256
    if second_target_id is not None:
        assert legacy_sha256_by_transition[transition_ids[1]] != second_commit.manifest_sha256

    restarted_again = _owner(tmp_path, _Preparer())
    try:
        assert (
            tuple(restarted_again.list_project_heads(first_task.project_id))
            == heads_after_migration
        )
        assert {
            transition_id: restarted_again.successor_commit(transition_id)
            for transition_id in transition_ids
        } == commits_after_migration
    finally:
        restarted_again.close()


def test_restart_migrates_legacy_partial_target_chain_without_inheritance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_legacy_successor_chain_migration(
        tmp_path,
        second_target_id="skill_bundle",
        monkeypatch=monkeypatch,
    )


def test_restart_migrates_legacy_nonempty_to_empty_chain_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_legacy_successor_chain_migration(
        tmp_path,
        second_target_id=None,
        monkeypatch=monkeypatch,
    )


def test_restart_migrates_legacy_successor_to_typed_composition(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path, _Preparer())
    _predecessor, task = _admit(owner)
    committed = owner.run_successor_transition(
        task.task_id,
        accepted_attempt_id=task.attempts[0].attempt_id,
        plan=_plan(task),
    )
    receipt = owner.successor_commit(committed.transition.successor_transition_id)
    assert receipt is not None
    original_artifacts = receipt.manifest.artifacts
    legacy_manifest = receipt.manifest.model_copy(update={"artifacts": ()})
    legacy_commit = AtomicSuccessorCommitV2(
        manifest_sha256=atomic_successor_manifest_sha256(legacy_manifest),
        manifest=legacy_manifest,
    )
    owner.close()

    database = tmp_path / "science-tasks-v2" / "science-tasks-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "ALTER TABLE successor_commits DROP COLUMN composition_semantics_version"
        )
        connection.execute("ALTER TABLE successor_commits DROP COLUMN legacy_manifest_sha256")
        attempt_row = connection.execute(
            "SELECT transition_attempt_id, attempt_json "
            "FROM successor_transition_attempts "
            "WHERE successor_transition_id = ? ORDER BY ordinal DESC "
            "LIMIT 1",
            (committed.transition.successor_transition_id,),
        ).fetchone()
        assert attempt_row is not None
        attempt = task_store_module._v2_model_from_bytes(
            task_store_module.ScienceSuccessorTransitionAttemptV2,
            bytes(attempt_row["attempt_json"]),
        )
        legacy_attempt = attempt.model_copy(
            update={"commit_manifest_sha256": (legacy_commit.manifest_sha256)}
        )
        connection.execute(
            "UPDATE successor_commits SET manifest_sha256 = ?, "
            "commit_json = ? WHERE successor_transition_id = ?",
            (
                legacy_commit.manifest_sha256,
                task_store_module._v2_model_bytes(legacy_commit),
                committed.transition.successor_transition_id,
            ),
        )
        connection.execute(
            "UPDATE successor_transition_attempts SET attempt_json = ? "
            "WHERE transition_attempt_id = ?",
            (
                task_store_module._v2_model_bytes(legacy_attempt),
                attempt.transition_attempt_id,
            ),
        )
        connection.commit()

    restarted = _owner(tmp_path, _Preparer())
    try:
        migrated = restarted.successor_commit(committed.transition.successor_transition_id)
        assert migrated is not None
        assert migrated.manifest.artifacts == original_artifacts
        assert migrated.manifest_sha256 != legacy_commit.manifest_sha256
        with sqlite3.connect(database) as connection:
            connection.row_factory = sqlite3.Row
            provenance = connection.execute(
                "SELECT composition_semantics_version, "
                "legacy_manifest_sha256 FROM successor_commits "
                "WHERE successor_transition_id = ?",
                (committed.transition.successor_transition_id,),
            ).fetchone()
            migrated_attempt_row = connection.execute(
                "SELECT attempt_json FROM successor_transition_attempts "
                "WHERE successor_transition_id = ? "
                "ORDER BY ordinal DESC LIMIT 1",
                (committed.transition.successor_transition_id,),
            ).fetchone()
        assert tuple(provenance) == (
            1,
            legacy_commit.manifest_sha256,
        )
        assert migrated_attempt_row is not None
        migrated_attempt = task_store_module._v2_model_from_bytes(
            task_store_module.ScienceSuccessorTransitionAttemptV2,
            bytes(migrated_attempt_row["attempt_json"]),
        )
        assert migrated_attempt.commit_manifest_sha256 == legacy_commit.manifest_sha256
    finally:
        restarted.close()


def test_restart_rejects_relabeling_current_commit_as_legacy(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path, _Preparer())
    _predecessor, task = _admit(owner)
    committed = owner.run_successor_transition(
        task.task_id,
        accepted_attempt_id=task.attempts[0].attempt_id,
        plan=_plan(task),
    )
    receipt = owner.successor_commit(committed.transition.successor_transition_id)
    assert receipt is not None
    legacy_manifest = receipt.manifest.model_copy(update={"artifacts": ()})
    legacy_manifest_sha256 = atomic_successor_manifest_sha256(legacy_manifest)
    assert legacy_manifest_sha256 != receipt.manifest_sha256
    owner.close()

    database = tmp_path / "science-tasks-v2" / "science-tasks-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE successor_commits SET "
            "composition_semantics_version = 1, "
            "legacy_manifest_sha256 = ? "
            "WHERE successor_transition_id = ?",
            (
                legacy_manifest_sha256,
                committed.transition.successor_transition_id,
            ),
        )
        connection.commit()

    with pytest.raises(
        ScienceTaskStoreV2Error,
        match="legacy.*receipt|provenance",
    ):
        _owner(tmp_path, _Preparer())


def test_project_head_commit_lookup_deserializes_only_the_exact_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparer = _Preparer()
    owner = _owner(tmp_path, preparer)
    _genesis, first_task = _admit(owner)
    first = owner.run_successor_transition(
        first_task.task_id,
        accepted_attempt_id=first_task.attempts[0].attempt_id,
        plan=_plan(first_task),
    )
    first_head = first.transition.successor_project_head
    assert first_head is not None
    preparer.fail_phase = "materializing"
    authority = owner.project_admission_authority(first_task.project_id)
    second_task = owner.invoke(
        "submitCoreTaskV2",
        {
            "request": _request(authority),
            "idempotency_key": "commit-index-second-task",
        },
    )
    with pytest.raises(CoreTaskControlError):
        owner.run_successor_transition(
            second_task.task_id,
            accepted_attempt_id=second_task.attempts[0].attempt_id,
            plan=_plan(second_task),
        )
    failed = owner.get_successor_transition_for_task(second_task.task_id)
    owner.abandon_successor_transition(
        failed.transition.successor_transition_id,
        expected_project_head_id=first_head.project_head_id,
        abandon_request_id="abandon-recovery-budget",
    )
    owner.close()

    original = task_store_module._v2_model_from_bytes
    decoded_commits = 0

    def _counted(model_type, payload):
        nonlocal decoded_commits
        if model_type is AtomicSuccessorCommitV2:
            decoded_commits += 1
        return original(model_type, payload)

    monkeypatch.setattr(
        task_store_module,
        "_v2_model_from_bytes",
        _counted,
    )
    database = tmp_path / "science-tasks-v2" / "science-tasks-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        found = task_store_module._load_v2_successor_commit_for_project_head(
            connection,
            first_head.project_head_id,
        )
    assert found is not None
    assert found.manifest.successor_project_head_id == first_head.project_head_id
    assert decoded_commits == 1


def test_restart_fails_closed_on_rewritten_successor_event_history(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path, _Preparer())
    _predecessor, task = _admit(owner)
    owner.run_successor_transition(
        task.task_id,
        accepted_attempt_id=task.attempts[0].attempt_id,
        plan=_plan(task),
    )
    owner.close()

    database = tmp_path / "science-tasks-v2" / "science-tasks-v2.sqlite3"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT sequence, event_json FROM events "
            "WHERE task_id = ? AND event_type = 'transition_changed' "
            "ORDER BY sequence LIMIT 1",
            (task.task_id,),
        ).fetchone()
        assert row is not None
        event = TransitionChangedEventV2.model_validate_json(bytes(row[1]))
        rewritten = event.model_copy(update={"state": "committing", "progress_completed": 5})
        rewritten = rewritten.model_copy(
            update={"event_id": task_store_module._v2_event_id(rewritten)}
        )
        connection.execute(
            "UPDATE events SET event_id = ?, event_json = ? WHERE sequence = ?",
            (
                rewritten.event_id,
                task_store_module._v2_model_bytes(rewritten),
                row[0],
            ),
        )
        connection.commit()

    with pytest.raises(ScienceTaskStoreV2Error, match="event history"):
        _owner(tmp_path, _Preparer())


def test_committed_successor_recovers_as_the_exact_active_head(tmp_path: Path) -> None:
    owner = _owner(tmp_path, _Preparer())
    _predecessor, task = _admit(owner)
    committed = owner.run_successor_transition(
        task.task_id,
        accepted_attempt_id=task.attempts[0].attempt_id,
        plan=_plan(task),
    )
    transition_id = committed.transition.successor_transition_id
    successor = committed.transition.successor_project_head
    receipt = owner.successor_commit(transition_id)
    owner.close()

    restarted = _owner(tmp_path, _Preparer())
    try:
        assert restarted.get_successor_transition(transition_id) == committed
        assert restarted.successor_commit(transition_id) == receipt
        assert restarted.active_project_head(task.project_id) == successor
        assert restarted.invoke("getCoreTaskV2", {"task_id": task.task_id}).state == ("completed")
    finally:
        restarted.close()


def test_committed_context_is_pinned_only_by_the_second_task(
    tmp_path: Path,
) -> None:
    owner = _owner(tmp_path, _Preparer())
    predecessor, first = _admit(owner)
    try:
        committed = owner.run_successor_transition(
            first.task_id,
            accepted_attempt_id=first.attempts[0].attempt_id,
            plan=_plan(first),
        )
        successor = committed.transition.successor_project_head
        assert successor is not None
        assert first.admission.predecessor_project_head == predecessor.active_project_head
        assert first.admission.predecessor_project_head.runtime_context_snapshot != (
            successor.runtime_context_snapshot
        )

        next_authority = owner.project_admission_authority(first.project_id)
        second = owner.invoke(
            "submitCoreTaskV2",
            {"request": _request(next_authority), "idempotency_key": "submit-2"},
        )
        assert second.admission.predecessor_project_head == successor
        assert second.admission.predecessor_project_head.runtime_context_snapshot == (
            successor.runtime_context_snapshot
        )
        assert second.admission.predecessor_project_head.runtime_context_snapshot != (
            first.admission.predecessor_project_head.runtime_context_snapshot
        )
        receipt = owner.successor_commit(committed.transition.successor_transition_id)
        assert receipt is not None
        memory = b"Memory accepted only after the producing task completed."
        entry = PayloadManifestEntry(
            relative_path="memory.md",
            media_type="text/markdown",
            size_bytes=len(memory),
            sha256=hashlib.sha256(memory).hexdigest(),
        )
        artifact_id = receipt.manifest.method_artifact_ids[0]
        runtime_context = {
            "context_id": receipt.manifest.materialized_context_id,
            "memory": {
                "artifact_ids": [artifact_id],
                "rendered_text": memory.decode(),
                "items": [
                    {
                        "artifact_id": artifact_id,
                        "rendered_text": memory.decode(),
                    }
                ],
            },
            "agent_system": {
                "artifact_ids": [],
                "rendered_text": "",
                "target_path": "AGENTS.md",
                "targets": [],
            },
            "skills": [],
            "adapter_merge_spec": {
                "base_model": None,
                "merge_mode": "reference_only",
                "adapters": [],
            },
            "selection": {
                "artifact_ids": [artifact_id],
                "artifacts": [
                    {
                        "artifact_id": artifact_id,
                        "artifact_type": "text_memory",
                        "content_sha256": payload_tree_digest((entry,)),
                        "payload_entries": [entry.model_dump(mode="json")],
                    }
                ],
                "reasons": ["committed successor context"],
            },
        }
        injection = build_runtime_injection_plan(
            context=runtime_context,
            revision_id=successor.evolution_revision.evolution_revision_id,
            instruction="second task",
            expected_artifact_ids=(artifact_id,),
        )
        assert memory.decode() in injection.effective_instruction
        assert injection.authority["context_id"] == (receipt.manifest.materialized_context_id)
        assert injection.authority["revision_id"] == (
            second.admission.predecessor_project_head.evolution_revision.evolution_revision_id
        )
        assert injection.authority["revision_id"] != (
            first.admission.predecessor_project_head.evolution_revision.evolution_revision_id
        )
        assert owner.ownership_counts() == (2, 2, 2)
    finally:
        owner.close()
