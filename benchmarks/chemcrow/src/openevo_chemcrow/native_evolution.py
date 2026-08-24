from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from openevo.evolution.methods import run_method
from openevo.evolution.models import (
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

_METHODS = {
    ArtifactKind.TEXT_MEMORY: "text_memory_reflector",
    ArtifactKind.SKILL_BUNDLE: "skill_bundle_reflector",
    ArtifactKind.AGENT_SYSTEM: "agent_system_reflector",
}


class NativeEvolutionEngine:
    """One OpenEvo Core event -> dataset -> job -> native Reflector transition."""

    def __init__(
        self,
        *,
        run_root: Path,
        artifact_kind: ArtifactKind,
        reflector_config: dict[str, Any],
    ) -> None:
        self.run_root = run_root
        self.artifact_kind = artifact_kind
        self.reflector_config = json.loads(json.dumps(reflector_config))

    def evolve(
        self,
        *,
        task: TaskItem,
        baseline: Trajectory,
        feedback_payload: dict[str, Any],
        pair_id: str,
    ) -> ArtifactReceipt:
        item_root = self.run_root / pair_id / "evolution"
        store = EvolutionStore(
            db_path=item_root / "core.sqlite3",
            artifact_root=item_root / "artifacts",
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
                base_model=str(self.reflector_config.get("candidate_model") or "frozen-candidate"),
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
                    "reflector_llm": self.reflector_config,
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
            drafts = run_method(claim.job, artifact_root=item_root / "artifacts")
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
        content_path = Path(unquote(parsed.path))
        return ArtifactReceipt(
            task_id=task.task_id,
            parent_baseline_run=baseline.run_id,
            reflector_run_id=job.job_id,
            reflector_input_hash=canonical_sha256(feedback_payload),
            artifact_type=self.artifact_kind,
            artifact_id=str(artifact_ids[0]),
            content_hash=file_sha256(content_path),
            size_bytes=content_path.stat().st_size,
            generation_time_seconds=time.monotonic() - started,
        )
