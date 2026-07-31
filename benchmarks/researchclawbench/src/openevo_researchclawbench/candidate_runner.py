"""Thin adapter over OpenEvo's native Codex rollout authority.

There is intentionally no candidate subprocess, Docker invocation, timeout
loop, credential path, or JSONL parser in this module.  OpenEvo Core owns all
of those responsibilities and returns a validated ``SessionResult``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openevo.rollout.models import SessionResult, TaskRequest
from openevo.rollout.run_owner import NativeTaskRunOwner

from .config import (
    MANAGED_CODEX_BINARY,
    MANAGED_CODEX_MODEL,
    MANAGED_CODEX_VERSION,
    MANAGED_RUNTIME_IMAGE,
    MANAGED_RUNTIME_PROFILE,
    ExperimentConfig,
)
from .prompt_composer import compose_native_instruction
from .workspace import WorkspaceReceipt, assert_candidate_workspace_shape


@dataclass(frozen=True)
class NativeCandidateRequest:
    task_request: TaskRequest
    canonical_payload: dict[str, Any]
    payload_sha256: str
    instruction_sha256: str
    input_artifact_ids: tuple[str, ...]


@dataclass(frozen=True)
class NativeCandidateResult:
    rollout_task_id: str
    session_result: SessionResult
    completed: bool
    transcript_trace_count: int
    workspace_result_manifest_sha256: str


def build_native_task_request(
    config: ExperimentConfig,
    workspace: WorkspaceReceipt,
    *,
    workspace_handoff: Any,
    runtime_context_binding: Any,
    task_local_overlay: str | None = None,
) -> NativeCandidateRequest:
    """Construct the real OpenEvo ``TaskRequest`` admitted by Core.

    ``workspace_handoff`` and ``runtime_context_binding`` are opaque authorities
    issued by the OpenEvo run owner.  The adapter cannot synthesize them.  Their
    selected artifact IDs are the only accepted injection source.
    """

    from openevo.backend.runtime_context_binding_v2 import RuntimeContextBindingV2
    from openevo.backend.workspace_handoff_v2 import WorkspaceHandoffBindingV2
    from openevo.harness.models import AgentSpec
    from openevo.rollout.models import TaskRequest, canonicalize_task_request
    from openevo.runtime.codex_isolation import (
        CODEX_SUBSCRIPTION_CONTRACT_KEY,
        codex_subscription_contract,
    )
    from openevo.runtime.managed import (
        MANAGED_HOME,
        MANAGED_PATH,
        MANAGED_SUBSCRIPTION_PREPARE_COMMAND,
        MANAGED_WORKSPACE,
    )
    from openevo.runtime.models import PrepareAction, RuntimeSpec
    from openevo.trajectory.models import StrategySpec

    handoff = WorkspaceHandoffBindingV2.model_validate(workspace_handoff)
    context = RuntimeContextBindingV2.model_validate(runtime_context_binding)
    if handoff.attempt_id != workspace.run_id:
        raise ValueError("OpenEvo workspace handoff belongs to another attempt")
    if handoff.task_id == workspace.task_id:
        raise ValueError("rollout task identity must remain distinct from benchmark task identity")
    if context.project_head.project_id != handoff.project_id:
        raise ValueError("runtime context and workspace handoff project identities differ")
    if (
        context.service_generation_sha256 != handoff.service_generation_sha256
        or context.framework_lock_sha256 != handoff.framework_lock_sha256
    ):
        raise ValueError("runtime context and workspace handoff service authority differ")
    assert_candidate_workspace_shape(workspace.candidate_workspace)

    instruction = compose_native_instruction(
        workspace.instructions,
        task_local_overlay=task_local_overlay,
    )
    reasoning = str(config.require("candidate.reasoning_level"))
    request = TaskRequest(
        task_id=handoff.task_id,
        instruction=instruction.text,
        num_samples=1,
        timeout_seconds=float(config.require("candidate.attempt_timeout_seconds")),
        runtime=RuntimeSpec(
            backend="docker",
            profile=MANAGED_RUNTIME_PROFILE,
            container_user="host",
            image=MANAGED_RUNTIME_IMAGE,
            prepare=[
                PrepareAction(type="exec", command=MANAGED_SUBSCRIPTION_PREPARE_COMMAND)
            ],
            env={"HOME": MANAGED_HOME, "PATH": MANAGED_PATH},
            network="host",
            workdir=MANAGED_WORKSPACE,
            cpus=int(config.require("candidate.cpus")),
            memory_mb=int(config.require("candidate.memory_mb")),
            gpus=0,
            allow_internet=False,
            # Candidate tools remain offline.  Only the managed Codex process
            # may reach its subscription model control plane; Core rejects this
            # split for non-managed or non-subscription execution.
            allow_model_control_plane_network=True,
        ),
        agent=AgentSpec(
            harness="codex",
            model_name=MANAGED_CODEX_MODEL,
            settings={
                "auth_mode": "subscription",
                "capture_mode": "transcript",
                "reasoning_effort": reasoning,
                CODEX_SUBSCRIPTION_CONTRACT_KEY: codex_subscription_contract(),
            },
            env={},
            mcp_servers=[],
        ),
        builder=StrategySpec(strategy="agent_transcript"),
        metadata={
            "policy_version": context.project_head.evolution_revision.evolution_revision_id,
            "rollout_step": int(config.require("native_openevo.rollout_step")),
            "agent": {"harness": "codex", "model_name": MANAGED_CODEX_MODEL},
            "openevo": {
                "project_id": context.project_head.project_id,
                "project_head_id": context.project_head.project_head_id,
                "evolution_revision_id": (
                    context.project_head.evolution_revision.evolution_revision_id
                ),
                "runtime_context_snapshot_id": (
                    context.project_head.runtime_context_snapshot.runtime_context_snapshot_id
                ),
            },
            "evolution": {
                "context_artifact_ids": list(context.selected_artifact_ids),
            },
            "researchclawbench": {
                "benchmark_task_id": workspace.task_id,
                "attempt_id": workspace.run_id,
                "candidate_binary": MANAGED_CODEX_BINARY,
                "candidate_codex_version": MANAGED_CODEX_VERSION,
                "task_local_overlay_enabled": bool(task_local_overlay and task_local_overlay.strip()),
            },
        },
        workspace_handoff=handoff,
        runtime_context_binding=context,
    )
    canonical = canonicalize_task_request(request)
    return NativeCandidateRequest(
        task_request=canonical.request,
        canonical_payload=canonical.payload,
        payload_sha256=canonical.payload_sha256,
        instruction_sha256=instruction.sha256,
        input_artifact_ids=tuple(context.selected_artifact_ids),
    )


def execute_native_candidate(
    owner: NativeTaskRunOwner,
    request: NativeCandidateRequest,
) -> NativeCandidateResult:
    """Execute only through the public Core run owner and consume SessionResult."""

    from openevo.rollout.models import SessionStatus

    owned = owner.execute(request.task_request)
    if (
        getattr(owned, "task_id", None) != request.task_request.task_id
        or getattr(owned, "request_sha256", None) != request.payload_sha256
    ):
        raise ValueError("OpenEvo run owner returned another request identity")
    result = SessionResult.model_validate(getattr(owned, "session_result", None))
    capture_mode = result.trajectory.metadata.get("capture_mode")
    if capture_mode != "transcript":
        raise ValueError("trajectory was not produced by OpenEvo transcript capture")
    if result.workspace_result is None:
        raise ValueError("OpenEvo run owner did not return an opaque workspace result")
    return NativeCandidateResult(
        rollout_task_id=request.task_request.task_id,
        session_result=result,
        completed=result.status is SessionStatus.COMPLETED,
        transcript_trace_count=len(result.trajectory.traces),
        workspace_result_manifest_sha256=result.workspace_result.result_manifest_sha256,
    )


def candidate_source_audit(package_root: str | Path) -> dict[str, Any]:
    """Data-only evidence that the adapter has no direct candidate CLI path."""

    root = Path(package_root).resolve(strict=True)
    candidates = (root / "candidate_runner.py", root / "cli.py", root / "openevo_bridge.py")
    forbidden = (
        "Popen(" + '["codex"',
        "run(" + '["codex"',
        "codex" + " exec",
        "auth" + ".json",
        "~/" + ".codex",
        ".submit_" + "task(",
        ".get_" + "task(",
    )
    findings: list[str] = []
    for path in candidates:
        text = path.read_text(encoding="utf-8")
        findings.extend(f"{path.name}:{literal}" for literal in forbidden if literal in text)
    return {
        "candidate_direct_execution_findings": findings,
        "candidate_native_task_request": True,
        "candidate_binary": MANAGED_CODEX_BINARY,
        "candidate_codex_version": MANAGED_CODEX_VERSION,
        "passed": not findings,
    }
