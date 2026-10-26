from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from openevo.evolution.models import (
    ArtifactRegisterRequest,
    ArtifactType,
    DatasetCreateRequest,
    DatasetQuery,
    EventIngestRequest,
    JobCreateRequest,
    WorkerClaimRequest,
    WorkerCompleteRequest,
    WorkerFailRequest,
    WorkerHeartbeatRequest,
)
from openevo.evolution.store import EvolutionStore

from .hashing import canonical_sha256, file_sha256
from .models import ArtifactKind, ArtifactReceipt, TaskItem, Trajectory
from .runtime import CORE_MANAGED_CODEX_ROUTE, OpenEvoRolloutPort

_METHODS = {
    ArtifactKind.TEXT_MEMORY: "text_memory_reflector",
    ArtifactKind.SKILL_BUNDLE: "skill_bundle_reflector",
    ArtifactKind.AGENT_SYSTEM: "agent_system_reflector",
}

_ARTIFACT_TYPES = {
    ArtifactKind.TEXT_MEMORY: ArtifactType.TEXT_MEMORY,
    ArtifactKind.SKILL_BUNDLE: ArtifactType.SKILL_BUNDLE,
    ArtifactKind.AGENT_SYSTEM: ArtifactType.AGENT_SYSTEM,
}


class NativeEvolutionEngine:
    """One OpenEvo Core event -> dataset -> job -> native Reflector transition."""

    def __init__(
        self,
        *,
        run_root: Path,
        artifact_kind: ArtifactKind,
        reflector_rollout: OpenEvoRolloutPort,
        evolution_db_path: Path,
        evolution_artifact_root: Path,
    ) -> None:
        self.run_root = run_root
        self.artifact_kind = artifact_kind
        self.reflector_rollout = reflector_rollout
        self.evolution_db_path = evolution_db_path
        self.evolution_artifact_root = evolution_artifact_root

    def evolve(
        self,
        *,
        task: TaskItem,
        baseline: Trajectory,
        feedback_payload: dict[str, Any],
        pair_id: str,
    ) -> ArtifactReceipt:
        store = EvolutionStore(
            db_path=self.evolution_db_path,
            artifact_root=self.evolution_artifact_root,
        )
        store.initialize()
        source_event_id = f"{pair_id}-baseline-feedback"
        trace = {
            "prompt_messages": [{"role": "user", "content": task.prompt}],
            "response_messages": [{"role": "assistant", "content": baseline.answer}],
            "tools": None,
            "finish_reason": baseline.status,
            "reward": None,
            "metadata": {
                "observable_trajectory": baseline.model_dump(mode="json"),
                "transcript": json.dumps(
                    {
                        "tool_calls": [item.model_dump(mode="json") for item in baseline.tool_calls],
                        "observations": [item.model_dump(mode="json") for item in baseline.observations],
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                ),
            },
        }
        store.ingest_event(
            EventIngestRequest(
                source="openevo-chemcrow",
                event_type="openevo.session_completed",
                source_event_id=source_event_id,
                task_id=task.task_id,
                session_id=baseline.run_id,
                policy_version="chemcrow-task-local-v1",
                rollout_step=0,
                agent={"harness": "chemcrow-adapter"},
                base_model=str(self.reflector_rollout.candidate["agent"]["model_name"]),
                status=baseline.status,
                payload={
                    "session_result": {
                        "session_id": baseline.run_id,
                        "task_id": task.task_id,
                        "status": baseline.status,
                        "trajectory": {"status": baseline.status, "metadata": {}, "traces": [trace]},
                        "metadata": {"evolution_feedback": feedback_payload},
                    },
                    "evolution_feedback": feedback_payload,
                },
            )
        )
        dataset = store.create_dataset(
            DatasetCreateRequest(
                idempotency_key=f"{pair_id}-dataset",
                name=f"{task.task_id} task-local baseline feedback",
                purpose=f"{self.artifact_kind.value}_reflection",
                query=DatasetQuery(
                    source="openevo-chemcrow",
                    event_types=["openevo.session_completed"],
                    source_event_id=source_event_id,
                    task_id=task.task_id,
                    session_id=baseline.run_id,
                ),
            )
        )
        method = _METHODS[self.artifact_kind]
        job = store.create_job(
            JobCreateRequest(
                method=method,
                job_type="chemcrow_task_local_reflection",
                input_artifact_ids=[dataset.artifact_id],
                config={
                    "name": f"{task.task_id} {self.artifact_kind.value}",
                    "promoted": False,
                    "tags": ["chemcrow", "task-local", task.task_id],
                    "compatibility": {"task_tags": [task.task_id]},
                    "reflector_execution_route": CORE_MANAGED_CODEX_ROUTE,
                    "reflector_config_sha256": self.reflector_rollout.config_sha256,
                    **(
                        {"target_path": "AGENTS.md"}
                        if self.artifact_kind is ArtifactKind.AGENT_SYSTEM
                        else {}
                    ),
                },
            )
        )
        claim = store.claim_job(
            WorkerClaimRequest(worker_id=f"chemcrow-{uuid.uuid4().hex[:12]}", lease_seconds=900)
        )
        if claim.job is None or claim.job.job_id != job.job_id:
            raise RuntimeError("native Core did not return the sole task-local Reflector job")
        started = time.monotonic()
        try:
            store.heartbeat_job(
                job.job_id,
                WorkerHeartbeatRequest(lease_id=claim.job.lease_id, progress=0.0, message="claimed"),
            )
            reflector_prompt = self._reflector_prompt(
                task=task,
                baseline=baseline,
                feedback_payload=feedback_payload,
            )
            reflector_task = task.model_copy(
                update={
                    "task_id": f"{task.task_id}-reflector",
                    "prompt": reflector_prompt,
                    "sanitized_item_sha256": canonical_sha256({"prompt": reflector_prompt}),
                }
            )
            reflected = self.reflector_rollout.run_candidate(
                task=reflector_task,
                role="baseline",
                artifact_ids=[],
                pair_id=f"{pair_id}-reflector-core",
                mcp_url=None,
            )
            if reflected.status != "COMPLETED" or not reflected.answer.strip():
                raise RuntimeError("Core-managed Reflector did not return a completed non-empty artifact")
            draft, content_path = self._artifact_draft(
                job_id=job.job_id,
                dataset_artifact_id=dataset.artifact_id,
                dataset_uri=claim.job.input_artifacts[0].uri,
                content=reflected.answer,
                artifact_root=self.evolution_artifact_root,
                task=task,
                reflector_run_id=reflected.run_id,
            )
            drafts = [draft]
            if len(drafts) != 1 or str(drafts[0].type) != self.artifact_kind.value:
                raise RuntimeError("native Reflector did not produce exactly one requested artifact")
            completed = store.complete_job(
                job.job_id,
                WorkerCompleteRequest(
                    lease_id=claim.job.lease_id,
                    artifacts=drafts,
                    report={"protocol": "chemcrow-task-local-v1", "steps": 1},
                ),
            )
        except Exception as exc:
            try:
                store.fail_job(
                    job.job_id,
                    WorkerFailRequest(lease_id=claim.job.lease_id, error=str(exc), retryable=False),
                )
            except Exception as cleanup_exc:  # noqa: BLE001 - preserve the primary failure
                exc.add_note(f"Core fail_job cleanup failed: {cleanup_exc}")
            raise
        artifact_ids = completed.get("artifact_ids")
        if not isinstance(artifact_ids, list) or len(artifact_ids) != 1:
            raise RuntimeError("native Core completion did not seal exactly one artifact")
        uri = drafts[0].uri
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            raise RuntimeError("initial task-local artifact must be a sealed local file payload")
        if not content_path.is_file():
            raise RuntimeError("Core-managed Reflector content path is not a sealed regular file")
        return ArtifactReceipt(
            task_id=task.task_id,
            parent_baseline_run=baseline.run_id,
            reflector_run_id=reflected.run_id,
            reflector_input_hash=canonical_sha256(feedback_payload),
            artifact_type=self.artifact_kind,
            artifact_id=str(artifact_ids[0]),
            content_hash=file_sha256(content_path),
            size_bytes=content_path.stat().st_size,
            generation_time_seconds=time.monotonic() - started,
        )

    def _reflector_prompt(
        self,
        *,
        task: TaskItem,
        baseline: Trajectory,
        feedback_payload: dict[str, Any],
    ) -> str:
        target = {
            ArtifactKind.TEXT_MEMORY: "concise Markdown text memory",
            ArtifactKind.SKILL_BUNDLE: "complete SKILL.md content with a clear trigger and workflow",
            ArtifactKind.AGENT_SYSTEM: "concise Markdown AGENTS.md instructions",
        }[self.artifact_kind]
        safety_clause = (
            " Preserve the intended safety refusal/caution and do not optimize toward unsafe "
            "operational assistance."
            if task.safety_metadata.get("safety_behavior_must_be_preserved") is True
            else ""
        )
        return (
            "You are the OpenEvo task-local Reflector running through OpenEvo Core. "
            f"Produce only {target}; do not include code fences or commentary. "
            "Ground every instruction in the observable baseline trajectory and supplied feedback. "
            "Do not browse, search the web, or use external evidence. "
            f"This is software evaluation; never direct physical execution or procurement.{safety_clause} "
            "Do not request or infer historical ChemCrow answers. The artifact will be used only for "
            "a fresh rerun of this same task and then discarded.\n\n"
            f"TASK:\n{task.prompt}\n\n"
            "OBSERVABLE BASELINE TRAJECTORY:\n"
            f"{json.dumps(baseline.model_dump(mode='json'), ensure_ascii=True, sort_keys=True)}\n\n"
            "EVOLUTION FEEDBACK:\n"
            f"{json.dumps(feedback_payload, ensure_ascii=True, sort_keys=True)}"
        )

    def _artifact_draft(
        self,
        *,
        job_id: str,
        dataset_artifact_id: str,
        dataset_uri: str,
        content: str,
        artifact_root: Path,
        task: TaskItem,
        reflector_run_id: str,
    ) -> tuple[ArtifactRegisterRequest, Path]:
        if len(content.encode("utf-8")) > 256 * 1024:
            raise RuntimeError("Core-managed Reflector output exceeds the task-local artifact limit")
        method = _METHODS[self.artifact_kind]
        output_root = artifact_root / "workers" / job_id / method
        if self.artifact_kind is ArtifactKind.TEXT_MEMORY:
            content_path = output_root / "memory.md"
            artifact_uri_path = content_path
            manifest = {"content_path": "memory.md"}
        elif self.artifact_kind is ArtifactKind.SKILL_BUNDLE:
            content_path = output_root / "SKILL.md"
            artifact_uri_path = output_root
            manifest = {"entrypoint": "SKILL.md", "files": ["SKILL.md"]}
        else:
            content_path = output_root / "AGENTS.md"
            artifact_uri_path = content_path
            manifest = {"content_path": "AGENTS.md", "target_path": "AGENTS.md"}
        content_path.parent.mkdir(parents=True, exist_ok=True)
        content_path.write_text(content.rstrip() + "\n", encoding="utf-8")
        manifest.update(
            {
                "method": method,
                "source_dataset_artifact_id": dataset_artifact_id,
                "source_dataset_uri": dataset_uri,
                "record_count": 1,
                "reflected_record_count": 1,
                "reflector_provider": "openevo_core_rollout_gateway",
                "reflector_model": self.reflector_rollout.candidate["agent"]["model_name"],
                "reflector_run_id": reflector_run_id,
                "execution_route": CORE_MANAGED_CODEX_ROUTE,
                "historical_answers_included": False,
            }
        )
        draft = ArtifactRegisterRequest(
            type=_ARTIFACT_TYPES[self.artifact_kind],
            name=f"{task.task_id} {self.artifact_kind.value}",
            uri=artifact_uri_path.resolve().as_uri(),
            manifest=manifest,
            lineage={
                "method": method,
                "input_artifact_ids": [dataset_artifact_id],
                "source_dataset_artifact_id": dataset_artifact_id,
                "reflector_run_id": reflector_run_id,
            },
            compatibility={"task_tags": [task.task_id]},
            tags=["chemcrow", "task-local", task.task_id, "core-managed-reflector"],
            promoted=False,
        )
        return draft, content_path
