from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from openevo.backend.contracts.v2.models import (
    EffectiveExecutionSnapshotRefV2,
    EvolutionRevisionRefV2,
    ProjectHeadRefV2,
    RuntimeContextSnapshotRefV2,
    WorkspaceArchiveDeclarationV2,
    WorkspaceSnapshotRefV2,
)
from openevo.backend.runtime_context_binding_v2 import RuntimeContextBindingV2
from openevo.backend.workspace_handoff_v2 import WorkspaceHandoffBindingV2
from openevo.backend.workspace_handoff_v2 import WorkspaceResultReceiptV2
from openevo.harness.factory import create_harness
from openevo.harness.presets.codex import CodexHarness
from openevo.rollout.models import SessionResult, TaskRequest
from openevo.rollout.run_owner import NativeTaskRunOwner
from openevo.trajectory.models import Trace, Trajectory

from openevo_researchclawbench.candidate_runner import (
    build_native_task_request,
    candidate_source_audit,
    execute_native_candidate,
)
from openevo_researchclawbench.config import ExperimentConfig, MANAGED_RUNTIME_IMAGE
from openevo_researchclawbench.hashing import UnsafePathError
from openevo_researchclawbench.workspace import WorkspaceReceipt


PROJECT_ROOT = Path(__file__).resolve().parents[4]
PROTOCOL = PROJECT_ROOT / "experiments/sequential_task_reflector_evolution_v0/protocol/protocol.yaml"


def _workspace(tmp_path: Path) -> WorkspaceReceipt:
    run = tmp_path / "run"
    candidate = tmp_path / "candidate"
    run.mkdir()
    candidate.mkdir()
    for name in ("data", "related_work", "code", "outputs", "artifacts", "skills"):
        (candidate / name).mkdir()
    (candidate / "report" / "images").mkdir(parents=True)
    (candidate / "INSTRUCTIONS.md").write_text("official task", encoding="utf-8")
    for name in ("data", "related_work", "code", "outputs", "report"):
        (run / name).mkdir(parents=True, exist_ok=True)
    instructions = run / "INSTRUCTIONS.md"
    instructions.write_text("official task", encoding="utf-8")
    return WorkspaceReceipt(
        task_id="Life_005",
        run_id="Life_005_a0",
        workspace=run,
        candidate_workspace=candidate,
        instructions=instructions,
        data=run / "data",
        related_work=run / "related_work",
        code=run / "code",
        outputs=run / "outputs",
        report=run / "report",
    )


def _authorities():
    project = "project-rcb"
    registry = "a" * 64
    revision = EvolutionRevisionRefV2(
        evolution_revision_id="evolution-genesis",
        project_id=project,
        manifest_sha256="b" * 64,
        artifact_count=0,
    )
    workspace = WorkspaceSnapshotRefV2(
        workspace_snapshot_id="workspace-genesis",
        project_id=project,
        manifest_sha256="c" * 64,
        entry_count=0,
        byte_size=0,
    )
    runtime = RuntimeContextSnapshotRefV2(
        runtime_context_snapshot_id="runtime-genesis",
        project_id=project,
        evolution_revision_id=revision.evolution_revision_id,
        evolution_revision_manifest_sha256=revision.manifest_sha256,
        registry_sha256=registry,
        runtime_contract_sha256="d" * 64,
        manifest_sha256="e" * 64,
    )
    execution = EffectiveExecutionSnapshotRefV2(
        effective_execution_snapshot_id="execution-genesis",
        project_id=project,
        execution_mode="codex_subscription_transcript",
        capture_mode="transcript",
        token_level_metrics_available=False,
        producer_id="core",
        snapshot_sha256="f" * 64,
    )
    head = ProjectHeadRefV2(
        project_head_id="head-genesis",
        project_id=project,
        generation=0,
        predecessor_project_head_id=None,
        workspace_snapshot=workspace,
        evolution_revision=revision,
        runtime_context_snapshot=runtime,
        effective_execution_snapshot=execution,
        registry_sha256=registry,
        manifest_sha256="1" * 64,
    )
    handoff = WorkspaceHandoffBindingV2(
        handoff_id="workspace-handoff-test",
        task_id="rollout-Life_005_a0",
        attempt_id="Life_005_a0",
        task_admission_id="admission-test",
        admission_sha256="2" * 64,
        project_id=project,
        input_workspace_snapshot=workspace,
        input_archive=WorkspaceArchiveDeclarationV2(
            format="openevo_deterministic_tar_v1",
            media_type="application/vnd.openevo.workspace-tar",
            content_sha256="3" * 64,
            byte_size=1024,
            entry_count=0,
            extracted_byte_size=0,
        ),
        service_generation_sha256="4" * 64,
        registry_sha256=registry,
        framework_lock_sha256="5" * 64,
        created_at="2026-07-28T00:00:00.000000Z",
    )
    context = RuntimeContextBindingV2(
        source="empty_genesis",
        project_head=head,
        service_generation_sha256=handoff.service_generation_sha256,
        framework_lock_sha256=handoff.framework_lock_sha256,
    )
    return handoff, context


def _request(tmp_path: Path):
    handoff, context = _authorities()
    return build_native_task_request(
        ExperimentConfig.load(PROTOCOL),
        _workspace(tmp_path),
        workspace_handoff=handoff,
        runtime_context_binding=context,
    )


def test_adapter_constructs_real_task_request(tmp_path: Path) -> None:
    request = _request(tmp_path)
    assert isinstance(request.task_request, TaskRequest)
    assert request.task_request.task_id == "rollout-Life_005_a0"


def test_agent_spec_selects_native_codex_harness(tmp_path: Path) -> None:
    request = _request(tmp_path).task_request
    assert request.agent.harness == "codex"
    assert isinstance(create_harness(request.agent), CodexHarness)


def test_managed_runtime_and_subscription_transcript_are_fixed(tmp_path: Path) -> None:
    request = _request(tmp_path).task_request
    assert request.runtime is not None
    assert request.runtime.image == MANAGED_RUNTIME_IMAGE
    assert request.runtime.profile == "managed_science"
    assert request.runtime.container_user == "host"
    assert request.runtime.allow_internet is False
    assert request.runtime.allow_model_control_plane_network is True
    assert request.agent.settings["auth_mode"] == "subscription"
    assert request.agent.settings["capture_mode"] == "transcript"
    assert request.builder.strategy == "agent_transcript"


def test_candidate_has_no_env_or_custom_mcp(tmp_path: Path) -> None:
    agent = _request(tmp_path).task_request.agent
    assert agent.env == {}
    assert agent.mcp_servers == []


def test_native_codex_run_step_uses_managed_binary(tmp_path: Path) -> None:
    harness = create_harness(_request(tmp_path).task_request.agent)
    command = harness.run_steps("task")[0].command
    assert "/opt/codex/bin/codex exec" in command
    assert "/usr/bin/codex exec" not in command


def test_genesis_context_injects_no_simulated_artifact(tmp_path: Path) -> None:
    request = _request(tmp_path)
    assert request.input_artifact_ids == ()
    assert request.task_request.runtime_context_binding.source == "empty_genesis"
    assert request.task_request.metadata["evolution"]["context_artifact_ids"] == []
    assert request.task_request.metadata["openevo"] == {
        "project_id": "project-rcb",
        "project_head_id": "head-genesis",
        "evolution_revision_id": "evolution-genesis",
        "runtime_context_snapshot_id": "runtime-genesis",
    }


def test_thin_executor_submits_canonical_task_request(tmp_path: Path) -> None:
    request = _request(tmp_path)
    task = request.task_request
    handoff = task.workspace_handoff
    assert handoff is not None
    provisional = WorkspaceResultReceiptV2.model_construct(
        workspace_result_contract_version="2",
        handoff_id=handoff.handoff_id,
        task_id=task.task_id,
        attempt_id=handoff.attempt_id,
        task_admission_id=handoff.task_admission_id,
        admission_sha256=handoff.admission_sha256,
        project_id=handoff.project_id,
        session_id="session-native-1",
        input_workspace_snapshot_id=handoff.input_workspace_snapshot.workspace_snapshot_id,
        input_workspace_manifest_sha256=handoff.input_workspace_snapshot.manifest_sha256,
        service_generation_sha256=handoff.service_generation_sha256,
        registry_sha256=handoff.registry_sha256,
        framework_lock_sha256=handoff.framework_lock_sha256,
        output_archive=handoff.input_archive,
        published_at="2026-07-28T00:01:00.000000Z",
        result_manifest_sha256="0" * 64,
    )
    workspace_result = WorkspaceResultReceiptV2.model_validate(
        {
            **provisional.model_dump(mode="python"),
            "result_manifest_sha256": hashlib.sha256(
                provisional.canonical_manifest_bytes()
            ).hexdigest(),
        }
    )
    session = SessionResult(
        session_id="session-native-1",
        task_id=task.task_id,
        status="COMPLETED",
        trajectory=Trajectory(
            status="COMPLETED",
            metadata={"capture_mode": "transcript"},
            traces=[
                Trace(
                    prompt_messages=[{"role": "user", "content": "task"}],
                    response_messages=[{"role": "assistant", "content": "done"}],
                )
            ],
        ),
        workspace_result=workspace_result,
    )

    class Client:
        def __init__(self):
            self.payload = None
            self.polls = 0

        def submit_task(self, payload):
            self.payload = payload
            return payload["task_id"]

        def get_task(self, task_id):
            self.polls += 1
            return {
                "task_id": task_id,
                "status": "completed",
                "total_sessions": 1,
                "completed_sessions": 1,
                "results": [session.model_dump(mode="json")],
            }

        def cancel_task(self, task_id):
            return {"task_id": task_id, "status": "cancelled"}

    client = Client()
    result = execute_native_candidate(
        NativeTaskRunOwner(client, poll_interval_seconds=0.001),
        request,
    )
    assert result.completed
    assert result.transcript_trace_count == 1
    assert client.payload["agent"]["harness"] == "codex"


def test_hidden_candidate_entry_is_rejected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace.candidate_workspace / "target_study").mkdir()
    handoff, context = _authorities()
    with pytest.raises(UnsafePathError):
        build_native_task_request(
            ExperimentConfig.load(PROTOCOL),
            workspace,
            workspace_handoff=handoff,
            runtime_context_binding=context,
        )


def test_candidate_source_has_no_direct_codex_or_credential_path() -> None:
    receipt = candidate_source_audit(
        PROJECT_ROOT / "OpenEvo/benchmarks/researchclawbench/src/openevo_researchclawbench"
    )
    assert receipt["passed"]
    assert receipt["candidate_codex_version"] == "0.144.1"
