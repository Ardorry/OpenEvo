"""Native OpenEvo evolution-worker submission and result contracts.

The adapter creates plan-bound jobs but never executes Codex or calls an
evolution method itself.  A verified OpenEvo evolution worker claims each job,
runs the built-in reflector, and registers its outputs transactionally.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from openevo.evolution.admission import ArtifactProposalDecisionReceipt

from .config import ARTIFACT_TYPES, MANAGED_CODEX_VERSION, REFLECTOR_RUNTIME_BLOCKER


NATIVE_METHODS = {
    "agent_system": "agent_system_gepa_reflector",
    "text_memory": "text_memory_expel_reflector",
    "skill_bundle": "skill_bundle_reflector",
}
SERIAL_REQUEST_ORDER = ("agent_system", "text_memory", "skill_bundle")


class NativeEvolutionClient(Protocol):
    def create_plan_bound_job(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def get_internal_job_result(self, job_id: str) -> dict[str, Any]: ...

    def get_artifact(self, artifact_id: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class NativeEvolutionSubmission:
    artifact_type: str
    method_id: str
    job_id: str
    plan_id: str
    parent_artifact_ids: tuple[str, ...]
    dataset_artifact_id: str


@dataclass(frozen=True)
class NativeArtifactResult:
    artifact_type: str
    method_id: str
    job_id: str
    artifact_id: str
    proposal_artifact_ids: tuple[str, ...]
    registry_persisted: bool
    promoted: bool
    action: str
    manifest: dict[str, Any]
    decision_id: str
    decision_sha256: str


def _plan_bound_payload(spec: Any, legacy_payload: dict[str, Any]) -> dict[str, Any]:
    """Mirror the public runner's lossless plan-bound conversion."""

    user_config = spec.selection.config()
    legacy_config = legacy_payload.get("config")
    if not isinstance(legacy_config, dict):
        raise ValueError("compiled native evolution config is not an object")
    if any(legacy_config.get(key) != value for key, value in user_config.items()):
        raise ValueError("compiled evolution job changed normalized method config")
    core_config = {key: value for key, value in legacy_config.items() if key not in user_config}
    return {
        "plan": spec.plan.model_dump(mode="json"),
        "target_id": spec.target_id,
        "job_type": legacy_payload["job_type"],
        "input_bindings": legacy_payload["input_bindings"],
        "core_config": core_config,
        "priority": legacy_payload.get("priority", 100),
    }


def submit_native_triple_artifact_cycle(
    client: NativeEvolutionClient,
    *,
    compiled_experiment: Any,
    task_id: str,
    round_index: int,
    dataset_artifact_id: str,
    context_artifact_ids: Mapping[str, Sequence[str]],
) -> tuple[NativeEvolutionSubmission, ...]:
    """Submit three independent native requests; never run a reflector here."""

    specs = compiled_experiment.evolution_methods_for_round(
        round_index,
        prior_dataset_artifact_ids=tuple(context_artifact_ids.get("dataset", ())),
        task_id=task_id,
    )
    by_type = {spec.artifact_type: spec for spec in specs}
    if set(by_type) != set(ARTIFACT_TYPES):
        raise ValueError("native plan does not contain exactly the three required artifact types")
    submissions: list[NativeEvolutionSubmission] = []
    matching_tasks = [task for task in compiled_experiment.tasks if task.task_id == task_id]
    if len(matching_tasks) != 1:
        raise ValueError("compiled native experiment does not own exactly one requested task")
    compiled_task = matching_tasks[0]
    for artifact_type in SERIAL_REQUEST_ORDER:
        spec = by_type[artifact_type]
        if spec.method != NATIVE_METHODS[artifact_type]:
            raise ValueError(f"unexpected native method for {artifact_type}: {spec.method}")
        legacy = compiled_task.evolution_job_payloads_for_round(
            round_index,
            [spec],
            dataset_artifact_id=dataset_artifact_id,
            context_artifact_ids=context_artifact_ids,
        )[0]
        created = client.create_plan_bound_job(_plan_bound_payload(spec, legacy))
        job_id = created.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("OpenEvo did not return a native evolution job ID")
        submissions.append(
            NativeEvolutionSubmission(
                artifact_type=artifact_type,
                method_id=spec.method,
                job_id=job_id,
                plan_id=spec.plan_id,
                parent_artifact_ids=tuple(context_artifact_ids.get(artifact_type, ())),
                dataset_artifact_id=dataset_artifact_id,
            )
        )
    return tuple(submissions)


def consume_native_artifact_result(
    client: NativeEvolutionClient,
    submission: NativeEvolutionSubmission,
    *,
    decision: ArtifactProposalDecisionReceipt,
) -> NativeArtifactResult:
    """Require worker-owned proposals plus a Core-owned admission decision."""

    result = client.get_internal_job_result(submission.job_id)
    if result.get("job_id") != submission.job_id or result.get("state") != "succeeded":
        raise ValueError("native evolution worker has not completed this job")
    artifact_ids = result.get("artifact_ids")
    if not isinstance(artifact_ids, list) or not artifact_ids:
        raise ValueError("native evolution job did not register any artifact")
    artifacts = [client.get_artifact(str(artifact_id)) for artifact_id in artifact_ids]
    targets = [artifact for artifact in artifacts if artifact.get("type") == submission.artifact_type]
    if not targets:
        raise ValueError("native registry output does not contain a target artifact proposal")
    decision = ArtifactProposalDecisionReceipt.model_validate(decision)
    if (
        decision.job_id != submission.job_id
        or str(decision.artifact_type) != submission.artifact_type
        or set(decision.proposal_artifact_ids)
        != {str(item.get("artifact_id")) for item in targets}
    ):
        raise ValueError("Core admission decision does not bind these native proposals")
    if decision.active_artifact_id is None:
        raise ValueError("Core admission decision has no active registry artifact")
    artifact = client.get_artifact(decision.active_artifact_id)
    if artifact.get("type") != submission.artifact_type:
        raise ValueError("Core admission selected the wrong artifact type")
    manifest = artifact.get("manifest")
    if not isinstance(manifest, dict):
        raise ValueError("native registry output lacks a manifest")
    action = decision.action.value
    promoted = artifact.get("promoted") is True
    if action == "update" and (
        decision.selected_artifact_id != artifact.get("artifact_id") or not promoted
    ):
        raise ValueError("Core update decision did not promote its selected successor")
    if action in {"keep", "reject"} and decision.selected_artifact_id is not None:
        raise ValueError("Core keep/reject decision selected a successor")
    return NativeArtifactResult(
        artifact_type=submission.artifact_type,
        method_id=submission.method_id,
        job_id=submission.job_id,
        artifact_id=str(artifact.get("artifact_id")),
        proposal_artifact_ids=tuple(str(item.get("artifact_id")) for item in targets),
        registry_persisted=True,
        promoted=promoted,
        action=action,
        manifest=dict(manifest),
        decision_id=decision.decision_id,
        decision_sha256=decision.content_sha256,
    )


def require_native_admission_evidence(result: NativeArtifactResult) -> None:
    """Reject adapter-made or redaction-dependent artifacts before promotion."""

    audit = result.manifest.get("reflection_audit")
    if isinstance(audit, dict) and int(audit.get("redaction_count", 0)) != 0:
        raise ValueError("native reflector required literal redaction; revision is rejected")
    if not result.registry_persisted:
        raise ValueError("artifact does not have native registry persistence evidence")
    if result.action == "update" and not result.promoted:
        raise ValueError("updated artifact was not admitted and promoted by OpenEvo")
    if result.action == "reject" and not result.decision_id:
        raise ValueError("rejected native artifact lacks its immutable decision receipt")


def reflector_runtime_audit(openevo_root: str | Path) -> dict[str, Any]:
    """Statically prove the release worker's managed reflector ownership."""

    root = Path(openevo_root).resolve(strict=True)
    methods = (root / "src/openevo/evolution/methods.py").read_text(encoding="utf-8")
    worker_cli = (root / "src/openevo/evolution/cli.py").read_text(encoding="utf-8")
    builtins = (root / "src/openevo/evolution/framework/builtins.py").read_text(encoding="utf-8")
    managed = (root / "src/openevo/evolution/managed_reflector.py").read_text(encoding="utf-8")
    execution = (root / "src/openevo/evolution/framework/execution.py").read_text(encoding="utf-8")
    evidence = {
        "release_worker_inherits_prepared_credential_snapshot": (
            "PreparedCodexCredentialSnapshot.from_inherited_environment" in worker_cli
            and "ManagedCodexReflectorService" in worker_cli
        ),
        "managed_reflector_service": "ManagedCodexReflectorService" in managed,
        "managed_reflector_core_service_binding": "require_active_reflector_service" in methods,
        "managed_schema_requires_runtime": '"runtime"' in builtins and '"required"' in builtins,
        "managed_binary_exact": "MANAGED_CODEX_VERSION" in managed
        and "MANAGED_CODEX_BINARY" in managed
        and "MANAGED_CODEX_VERSION" in execution,
        "path_fallback_rejected": "path_fallback_allowed=False" in managed,
        "host_subprocess_is_legacy_only": "legacy_path" in methods,
        "required_version": MANAGED_CODEX_VERSION,
    }
    pinned = all(value for key, value in evidence.items() if key != "required_version")
    return {
        **evidence,
        "status": "STATICALLY_PINNED" if pinned else REFLECTOR_RUNTIME_BLOCKER,
        "runtime_readiness_receipt_required": True,
        "model_execution_allowed": pinned,
    }
