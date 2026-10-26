from __future__ import annotations

import json
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from openevo.evolution.methods import (
    CoreReflectorPromptPlan,
    build_core_reflector_prompt_plan,
    core_reflector_max_repair_attempts,
    render_codex_cli_reflector_prompt,
    review_core_reflector_output,
)
from openevo.evolution.models import (
    ArtifactRegisterRequest,
    ArtifactType,
    DatasetCreateRequest,
    DatasetQuery,
    EventIngestRequest,
    JobCreateRequest,
    WorkerClaimedJob,
    WorkerClaimRequest,
    WorkerCompleteRequest,
    WorkerFailRequest,
    WorkerHeartbeatRequest,
)
from openevo.evolution.store import EvolutionStore

from .hashing import canonical_sha256, file_sha256
from .models import ArtifactKind, TaskItem, Trajectory
from .runtime import CORE_MANAGED_CODEX_ROUTE, OpenEvoRolloutPort
from .three_artifact_models import (
    CORE_FULL_WORKER_PROMPT_PROFILE,
    CORE_NATIVE_THREE_ARTIFACT_PROTOCOL_LABEL,
    CORE_ROLE_WRAPPER_PROMPT_PROFILE,
    THREE_ARTIFACT_ORDER,
    ArtifactSeparationPolicy,
    RecoveredReflectorOutput,
    ThreeArtifactBundleReceipt,
    ThreeArtifactReceipt,
)

ReflectorPromptProfile = Literal["core_role_wrapper_v1", "core_full_worker_v1"]

_METHODS = {
    ArtifactKind.TEXT_MEMORY: "text_memory_reflector",
    ArtifactKind.SKILL_BUNDLE: "skill_bundle_reflector",
    ArtifactKind.AGENT_SYSTEM: "agent_system_reflector",
}
_CORE_TYPES = {
    ArtifactKind.TEXT_MEMORY: ArtifactType.TEXT_MEMORY,
    ArtifactKind.SKILL_BUNDLE: ArtifactType.SKILL_BUNDLE,
    ArtifactKind.AGENT_SYSTEM: ArtifactType.AGENT_SYSTEM,
}
_PHASE_NAMES = {
    ArtifactKind.TEXT_MEMORY: "reflector_memory",
    ArtifactKind.SKILL_BUNDLE: "reflector_skill_bundle",
    ArtifactKind.AGENT_SYSTEM: "reflector_agent_system",
}
_FORBIDDEN_FEEDBACK_KEYS = {
    "paper_evaluator",
    "paper_evaluator_feedback",
    "paper_grade",
    "human_expert",
    "human_score",
    "historical_answer",
    "historical_chemcrow",
    "historical_gpt4",
    "reference_answer",
    "ground_truth",
    "evolved_answer",
    "future_task",
}
_FORBIDDEN_FEEDBACK_TEXT_MARKERS = (
    "paper evaluator",
    "evaluatorgpt",
    "paper grade",
    "human expert",
    "human score",
    "historical chemcrow",
    "historical gpt-4",
    "historical gpt4",
    "reference answer",
    "hidden ground truth",
    "future g2",
    "evolved answer",
)
_REFLECTOR_ISOLATION_PREAMBLE = (
    "You are one of three mutually isolated OpenEvo task-local Reflectors. Your prompt was "
    "frozen before any sibling invocation. You cannot see and must not infer sibling outputs. "
    "Use only the supplied Core reflection context. Do not browse, call tools, introduce "
    "historical ChemCrow/GPT-4 answers, human scores, paper EvaluatorGPT grades, hidden ground "
    "truth, future G2 output, or later tasks."
)


def _core_reflector_contract_hash(
    kind: ArtifactKind,
    prompt_profile: ReflectorPromptProfile = CORE_ROLE_WRAPPER_PROMPT_PROFILE,
) -> str:
    if prompt_profile == CORE_ROLE_WRAPPER_PROMPT_PROFILE:
        return canonical_sha256(
            {
                "core_method": _METHODS[kind],
                "core_prompt_contract": render_codex_cli_reflector_prompt(_METHODS[kind], ""),
            }
        )
    return canonical_sha256(
        {
            "core_method": _METHODS[kind],
            "core_prompt_contract": render_codex_cli_reflector_prompt(_METHODS[kind], ""),
            "prompt_profile": prompt_profile,
            "full_context_builder": "build_core_reflector_prompt_plan",
        }
    )


def render_core_native_reflector_prompt(
    kind: ArtifactKind,
    evidence: dict[str, Any],
) -> str:
    evidence_prompt = (
        f"{_REFLECTOR_ISOLATION_PREAMBLE}\n\n"
        "EVIDENCE JSON:\n"
        f"{json.dumps(evidence, ensure_ascii=True, sort_keys=True)}"
    )
    return render_codex_cli_reflector_prompt(_METHODS[kind], evidence_prompt)


def project_core_native_evolution_feedback(
    feedback_payload: dict[str, Any],
) -> tuple[dict[str, Any], float | None]:
    """Project ChemCrow evaluator fields into Core's native feedback vocabulary.

    This is a field-for-field transport adapter: it neither summarizes nor rewrites
    the evaluator text.  Core needs the availability marker to admit stateful
    evolution feedback; rubric scores are separately exposed through its native
    trajectory reward field.
    """

    evaluator = feedback_payload.get("evaluator_feedback")
    if not isinstance(evaluator, dict):
        return {"mode": str(feedback_payload.get("mode", "F0"))}, None

    def exact_string_list(field: str) -> list[str]:
        value = evaluator.get(field)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise TypeError(f"evaluator feedback field {field!r} must be a string list")
        return list(value)

    projected: dict[str, Any] = {
        "status": "available_for_evolution",
        "feedback_id": str(evaluator.get("evaluator_run_id") or "internal-evaluator"),
        "decision": "evaluator-summary",
        "strengths": exact_string_list("strengths"),
        "observed_issues": exact_string_list("weaknesses"),
        "suggested_changes": exact_string_list("actionable_critique"),
    }
    scores = evaluator.get("scores")
    if not isinstance(scores, dict):
        raise TypeError("evaluator feedback scores must be an object")
    score_values: list[float] = []
    for field in ("chemical_correctness", "reasoning_quality", "task_completion"):
        value = scores.get(field)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError(f"evaluator score {field!r} must be numeric")
        numeric = float(value)
        if not 0.0 <= numeric <= 4.0:
            raise ValueError(f"evaluator score {field!r} is outside [0, 4]")
        score_values.append(numeric)
    return projected, sum(score_values) / 12.0


@dataclass
class _PendingReflection:
    kind: ArtifactKind
    phase_name: str
    job_id: str
    lease_id: str
    dataset_artifact_id: str
    dataset_uri: str
    reflector_run_id: str
    model: str
    system_prompt_hash: str
    prompt_hash: str
    content: str
    content_path: Path
    draft: ArtifactRegisterRequest
    generation_time_seconds: float
    reflector_attempt_run_ids: list[str]
    output_audit: dict[str, object]


@dataclass
class _PreparedReflection:
    kind: ArtifactKind
    phase_name: str
    job: WorkerClaimedJob
    dataset_artifact_id: str
    dataset_uri: str


class ThreeIsolatedEvolutionEngine:
    """Three independent Core Reflector jobs over one frozen baseline evidence object."""

    def __init__(
        self,
        *,
        run_root: Path,
        reflector_rollouts: dict[ArtifactKind, OpenEvoRolloutPort],
        evolution_db_path: Path,
        evolution_artifact_root: Path,
        separation_policy: ArtifactSeparationPolicy,
        prompt_profile: ReflectorPromptProfile = CORE_ROLE_WRAPPER_PROMPT_PROFILE,
    ) -> None:
        if set(reflector_rollouts) != set(THREE_ARTIFACT_ORDER):
            raise ValueError("three independent Reflector rollout configs are required")
        models = {
            str(port.candidate["agent"]["model_name"]) for port in reflector_rollouts.values()
        }
        if len(models) != 1:
            raise ValueError("all three Reflectors must use the same frozen base model")
        self.run_root = run_root
        self.reflector_rollouts = reflector_rollouts
        self.evolution_db_path = evolution_db_path
        self.evolution_artifact_root = evolution_artifact_root
        self.separation_policy = separation_policy
        if prompt_profile not in {
            CORE_ROLE_WRAPPER_PROMPT_PROFILE,
            CORE_FULL_WORKER_PROMPT_PROFILE,
        }:
            raise ValueError(f"unsupported Reflector prompt profile: {prompt_profile}")
        self.prompt_profile = prompt_profile

    @property
    def model(self) -> str:
        first = self.reflector_rollouts[THREE_ARTIFACT_ORDER[0]]
        return str(first.candidate["agent"]["model_name"])

    def evolve_all(
        self,
        *,
        task: TaskItem,
        baseline: Trajectory,
        feedback_payload: dict[str, Any],
        pair_id: str,
        recovered_outputs: dict[ArtifactKind, RecoveredReflectorOutput] | None = None,
    ) -> ThreeArtifactBundleReceipt:
        if baseline.role != "baseline" or baseline.artifact_ids:
            raise ValueError("Reflectors require a bare-S0 baseline parent")
        forbidden = sorted(_find_forbidden_keys(feedback_payload))
        if forbidden:
            raise ValueError(
                "Reflector feedback contains forbidden scoring/leakage keys: "
                + ", ".join(forbidden)
            )
        forbidden_text = sorted(_find_forbidden_text(feedback_payload))
        if forbidden_text:
            raise ValueError(
                "Reflector feedback contains forbidden scoring/leakage text: "
                + ", ".join(forbidden_text)
            )
        recovered_outputs = dict(recovered_outputs or {})
        if any(kind not in THREE_ARTIFACT_ORDER for kind in recovered_outputs):
            raise ValueError("recovered Reflector output type is invalid")
        evidence = {
            "task": {
                "task_id": task.task_id,
                "prompt": task.prompt,
                "sanitized_item_sha256": task.sanitized_item_sha256,
            },
            "baseline": baseline.model_dump(mode="json"),
            "feedback": feedback_payload,
        }
        evidence_hash = canonical_sha256(evidence)
        store = EvolutionStore(
            db_path=self.evolution_db_path,
            artifact_root=self.evolution_artifact_root,
        )
        store.initialize()
        prepared: list[_PreparedReflection] = []
        pending: list[_PendingReflection] = []
        completed_job_ids: set[str] = set()
        try:
            if self.prompt_profile == CORE_ROLE_WRAPPER_PROMPT_PROFILE:
                prompts = {
                    kind: self._reflector_prompt(kind=kind, evidence=evidence)
                    for kind in THREE_ARTIFACT_ORDER
                }
                prompt_plans: dict[ArtifactKind, CoreReflectorPromptPlan | None] = {
                    kind: None for kind in THREE_ARTIFACT_ORDER
                }
                prompt_hashes = {
                    kind: canonical_sha256({"prompt": prompt})
                    for kind, prompt in prompts.items()
                }
                if len(set(prompt_hashes.values())) != 3:
                    raise ValueError("three Reflector prompts are not independent")
                # All prompts are frozen before the first sibling invocation.
                for kind in THREE_ARTIFACT_ORDER:
                    item = self._prepare_reflector_subjob(
                        store=store,
                        kind=kind,
                        task=task,
                        baseline=baseline,
                        evidence=evidence,
                        evidence_hash=evidence_hash,
                        prompt_hash=prompt_hashes[kind],
                        pair_id=pair_id,
                        include_core_feedback=False,
                    )
                    prepared.append(item)
                    pending.append(
                        self._invoke_prepared_reflector(
                            prepared=item,
                            task=task,
                            baseline=baseline,
                            evidence_hash=evidence_hash,
                            prompt=prompts[kind],
                            prompt_hash=prompt_hashes[kind],
                            prompt_plan=prompt_plans[kind],
                            pair_id=pair_id,
                        )
                    )
            else:
                # Full Core contexts depend on the actual dataset snapshots and
                # claimed job IDs. Prepare and claim all three jobs first, then
                # freeze all prompts before any model invocation.
                for kind in THREE_ARTIFACT_ORDER:
                    prepared.append(
                        self._prepare_reflector_subjob(
                            store=store,
                            kind=kind,
                            task=task,
                            baseline=baseline,
                            evidence=evidence,
                            evidence_hash=evidence_hash,
                            prompt_hash=None,
                            pair_id=pair_id,
                            include_core_feedback=True,
                        )
                    )
                full_plans = {
                    item.kind: build_core_reflector_prompt_plan(
                        item.job,
                        isolation_preamble=_REFLECTOR_ISOLATION_PREAMBLE,
                    )
                    for item in prepared
                }
                frozen = self._load_frozen_prompts(pair_id=pair_id)
                if frozen is None:
                    prompts = {kind: plan.codex_prompt for kind, plan in full_plans.items()}
                    prompt_hashes = {
                        kind: canonical_sha256({"prompt": prompt})
                        for kind, prompt in prompts.items()
                    }
                else:
                    prompts, prompt_hashes = frozen
                if len(set(prompt_hashes.values())) != 3:
                    raise ValueError("three full Core Reflector prompts are not independent")
                if frozen is None:
                    self._persist_frozen_prompts(
                        pair_id=pair_id,
                        plans=full_plans,
                        prompt_hashes=prompt_hashes,
                    )
                for item in prepared:
                    recovered = recovered_outputs.get(item.kind)
                    pending.append(
                        self._rehydrate_prepared_reflector(
                            prepared=item,
                            recovered=recovered,
                            task=task,
                            baseline=baseline,
                            evidence_hash=evidence_hash,
                            prompt_hash=prompt_hashes[item.kind],
                            prompt_plan=full_plans[item.kind],
                            pair_id=pair_id,
                        )
                        if recovered is not None
                        else self._invoke_prepared_reflector(
                            prepared=item,
                            task=task,
                            baseline=baseline,
                            evidence_hash=evidence_hash,
                            prompt=prompts[item.kind],
                            prompt_hash=prompt_hashes[item.kind],
                            prompt_plan=full_plans[item.kind],
                            pair_id=pair_id,
                        )
                    )
            duplicate_findings = detect_artifact_duplicates(
                {item.kind: item.content for item in pending},
                baseline_answer=baseline.answer,
                policy=self.separation_policy,
            )
            if any(duplicate_findings.values()):
                raise ValueError(
                    "artifact separation failed before registration: "
                    + json.dumps(duplicate_findings, sort_keys=True)
                )

            receipts: list[ThreeArtifactReceipt] = []
            for item in pending:
                completed = store.complete_job(
                    item.job_id,
                    WorkerCompleteRequest(
                        lease_id=item.lease_id,
                        artifacts=[item.draft],
                        report={
                            "protocol": CORE_NATIVE_THREE_ARTIFACT_PROTOCOL_LABEL,
                            "steps": len(item.reflector_attempt_run_ids) or 1,
                            "artifact_type": item.kind.value,
                            "prompt_profile": self.prompt_profile,
                            "output_audit": item.output_audit,
                            "sibling_outputs_visible": False,
                        },
                    ),
                )
                completed_job_ids.add(item.job_id)
                artifact_ids = completed.get("artifact_ids")
                if not isinstance(artifact_ids, list) or len(artifact_ids) != 1:
                    raise RuntimeError("Core sub-job did not seal exactly one requested artifact")
                artifact_id = str(artifact_ids[0])
                receipts.append(
                    ThreeArtifactReceipt(
                        task_id=task.task_id,
                        pair_id=pair_id,
                        parent_run_id=baseline.run_id,
                        reflector_job_id=item.job_id,
                        reflector_run_id=item.reflector_run_id,
                        model=item.model,
                        system_prompt_hash=item.system_prompt_hash,
                        prompt_hash=item.prompt_hash,
                        input_evidence_hash=evidence_hash,
                        artifact_type=item.kind,
                        artifact_id=artifact_id,
                        artifact_hash=file_sha256(item.content_path),
                        normalized_text_hash=canonical_sha256(
                            {"normalized_text": normalize_artifact_text(item.content)}
                        ),
                        size_bytes=item.content_path.stat().st_size,
                        generation_time_seconds=item.generation_time_seconds,
                        registration_receipt_sha256=canonical_sha256(completed),
                        reflector_attempt_run_ids=item.reflector_attempt_run_ids,
                        output_audit=item.output_audit,
                    )
                )
            return ThreeArtifactBundleReceipt(
                task_id=task.task_id,
                pair_id=pair_id,
                parent_run_id=baseline.run_id,
                input_evidence_hash=evidence_hash,
                prompt_profile=self.prompt_profile,
                separation_policy=self.separation_policy,
                artifacts=receipts,
                **duplicate_findings,
            )
        except Exception as exc:
            for item in prepared:
                if item.job.job_id in completed_job_ids:
                    continue
                try:
                    store.fail_job(
                        item.job.job_id,
                        WorkerFailRequest(
                            lease_id=item.job.lease_id,
                            error=str(exc),
                            retryable=False,
                        ),
                    )
                except Exception as cleanup_exc:  # noqa: BLE001
                    exc.add_note(
                        f"Core fail_job cleanup failed for {item.job.job_id}: {cleanup_exc}"
                    )
            raise

    def _rehydrate_prepared_reflector(
        self,
        *,
        prepared: _PreparedReflection,
        recovered: RecoveredReflectorOutput,
        task: TaskItem,
        baseline: Trajectory,
        evidence_hash: str,
        prompt_hash: str,
        prompt_plan: CoreReflectorPromptPlan,
        pair_id: str,
    ) -> _PendingReflection:
        kind = prepared.kind
        rollout = self.reflector_rollouts[kind]
        if (
            recovered.artifact_type is not kind
            or recovered.input_evidence_hash != evidence_hash
            or recovered.prompt_hash != prompt_hash
            or recovered.system_prompt_hash
            != _core_reflector_contract_hash(kind, self.prompt_profile)
            or recovered.model != str(rollout.candidate["agent"]["model_name"])
            or canonical_sha256({"content": recovered.content})
            != recovered.content_hash
        ):
            raise ValueError("recovered Reflector output authority differs")
        raw_content = _strict_reflector_markdown(recovered.content)
        review = review_core_reflector_output(
            prepared.job,
            prompt_plan=prompt_plan,
            markdown=raw_content,
        )
        if review.repair_prompt is not None or _strict_reflector_markdown(
            review.content
        ) != raw_content:
            raise ValueError("recovered Reflector output no longer passes Core review")
        output_audit = {
            **review.audit_report,
            "repair_count": 0,
            "raw_output_sha256": recovered.content_hash,
            "recovered_completed_model_call": True,
            "original_reflector_job_id": recovered.original_job_id,
        }
        draft, content_path = self._artifact_draft(
            kind=kind,
            job_id=prepared.job.job_id,
            dataset_artifact_id=prepared.dataset_artifact_id,
            dataset_uri=prepared.dataset_uri,
            content=raw_content,
            task=task,
            pair_id=pair_id,
            parent_run_id=baseline.run_id,
            reflector_run_id=recovered.reflector_run_id,
            model=recovered.model,
            prompt_hash=prompt_hash,
            system_prompt_hash=recovered.system_prompt_hash,
            evidence_hash=evidence_hash,
            reflector_attempt_run_ids=recovered.reflector_attempt_run_ids,
            output_audit=output_audit,
        )
        return _PendingReflection(
            kind=kind,
            phase_name=prepared.phase_name,
            job_id=prepared.job.job_id,
            lease_id=prepared.job.lease_id,
            dataset_artifact_id=prepared.dataset_artifact_id,
            dataset_uri=prepared.dataset_uri,
            reflector_run_id=recovered.reflector_run_id,
            model=recovered.model,
            system_prompt_hash=recovered.system_prompt_hash,
            prompt_hash=prompt_hash,
            content=raw_content,
            content_path=content_path,
            draft=draft,
            generation_time_seconds=recovered.generation_time_seconds,
            reflector_attempt_run_ids=recovered.reflector_attempt_run_ids,
            output_audit=output_audit,
        )

    def _prepare_reflector_subjob(
        self,
        *,
        store: EvolutionStore,
        kind: ArtifactKind,
        task: TaskItem,
        baseline: Trajectory,
        evidence: dict[str, Any],
        evidence_hash: str,
        prompt_hash: str | None,
        pair_id: str,
        include_core_feedback: bool,
    ) -> _PreparedReflection:
        phase_name = _PHASE_NAMES[kind]
        source_event_id = f"{pair_id}-{kind.value}-baseline-feedback"
        core_feedback, normalized_reward = project_core_native_evolution_feedback(
            evidence["feedback"]
        )
        trace = {
            "prompt_messages": [{"role": "user", "content": task.prompt}],
            "response_messages": [{"role": "assistant", "content": baseline.answer}],
            "tools": None,
            "finish_reason": baseline.status,
            "reward": normalized_reward,
            "metadata": {
                "observable_trajectory": baseline.model_dump(mode="json"),
                "input_evidence_sha256": evidence_hash,
                "historical_answers_included": False,
            },
        }
        rollout = self.reflector_rollouts[kind]
        store.ingest_event(
            EventIngestRequest(
                source="openevo-chemcrow-three-isolated",
                event_type="openevo.session_completed",
                source_event_id=source_event_id,
                task_id=task.task_id,
                session_id=baseline.run_id,
                policy_version="chemcrow-task-local-three-isolated-core-native-v2",
                rollout_step=0,
                agent={"harness": "chemcrow-adapter", "reflector_role": kind.value},
                base_model=str(rollout.candidate["agent"]["model_name"]),
                status=baseline.status,
                reward=normalized_reward,
                payload={
                    "session_result": {
                        "session_id": baseline.run_id,
                        "task_id": task.task_id,
                        "status": baseline.status,
                        "trajectory": {
                            "status": baseline.status,
                            "metadata": {},
                            "traces": [trace],
                        },
                        "metadata": {
                            "input_evidence": evidence,
                            "input_evidence_sha256": evidence_hash,
                            **(
                                {"evolution_feedback": core_feedback}
                                if include_core_feedback
                                else {}
                            ),
                        },
                    },
                },
            )
        )
        dataset = store.create_dataset(
            DatasetCreateRequest(
                idempotency_key=f"{pair_id}-{kind.value}-dataset",
                name=f"{task.task_id} {kind.value} isolated baseline evidence",
                purpose=f"{kind.value}_isolated_reflection",
                query=DatasetQuery(
                    source="openevo-chemcrow-three-isolated",
                    event_types=["openevo.session_completed"],
                    source_event_id=source_event_id,
                    task_id=task.task_id,
                    session_id=baseline.run_id,
                ),
            )
        )
        job = store.create_job(
            JobCreateRequest(
                method=_METHODS[kind],
                job_type="chemcrow_task_local_isolated_reflection",
                input_artifact_ids=[dataset.artifact_id],
                config={
                    "name": f"{task.task_id} {kind.value} isolated",
                    "promoted": False,
                    "tags": ["chemcrow", "task-local", "isolated", task.task_id, kind.value],
                    "compatibility": {"task_tags": [task.task_id]},
                    "reflector_execution_route": CORE_MANAGED_CODEX_ROUTE,
                    "reflector_config_sha256": rollout.config_sha256,
                    **(
                        {"reflector_prompt_sha256": prompt_hash}
                        if prompt_hash is not None
                        else {}
                    ),
                    "reflector_system_sha256": _core_reflector_contract_hash(
                        kind, self.prompt_profile
                    ),
                    "reflector_prompt_authority": "openevo_core_builtin",
                    "reflector_prompt_profile": self.prompt_profile,
                    "input_evidence_sha256": evidence_hash,
                    "sibling_outputs_visible": False,
                    **({"target_path": "AGENTS.md"} if kind is ArtifactKind.AGENT_SYSTEM else {}),
                },
            )
        )
        claim = store.claim_job(
            WorkerClaimRequest(
                worker_id=f"chemcrow-{kind.value}-{uuid.uuid4().hex[:10]}",
                # Registration is intentionally delayed until all three outputs
                # pass the duplicate guard. The full Core agent-system audit may
                # add two managed repair invocations, so keep every sibling lease
                # valid across the complete frozen batch.
                lease_seconds=(
                    10_800
                    if self.prompt_profile == CORE_FULL_WORKER_PROMPT_PROFILE
                    else 3_600
                ),
            )
        )
        if claim.job is None or claim.job.job_id != job.job_id:
            raise RuntimeError(f"Core did not return the isolated {kind.value} Reflector job")
        store.heartbeat_job(
            job.job_id,
            WorkerHeartbeatRequest(lease_id=claim.job.lease_id, progress=0.0, message="claimed"),
        )
        return _PreparedReflection(
            kind=kind,
            phase_name=phase_name,
            job=claim.job,
            dataset_artifact_id=dataset.artifact_id,
            dataset_uri=claim.job.input_artifacts[0].uri,
        )

    def _invoke_prepared_reflector(
        self,
        *,
        prepared: _PreparedReflection,
        task: TaskItem,
        baseline: Trajectory,
        evidence_hash: str,
        prompt: str,
        prompt_hash: str,
        prompt_plan: CoreReflectorPromptPlan | None,
        pair_id: str,
    ) -> _PendingReflection:
        kind = prepared.kind
        phase_name = prepared.phase_name
        rollout = self.reflector_rollouts[kind]
        current_prompt = prompt
        max_repairs = (
            core_reflector_max_repair_attempts(prepared.job)
            if prompt_plan is not None
            else 0
        )
        attempt_run_ids: list[str] = []
        output_audit: dict[str, object] = {}
        started = time.monotonic()
        content = ""
        for attempt in range(max_repairs + 1):
            attempt_suffix = "" if attempt == 0 else f"-repair-{attempt}"
            reflected_task = task.model_copy(
                update={
                    "task_id": f"{task.task_id}-{phase_name}{attempt_suffix}",
                    "prompt": current_prompt,
                    "sanitized_item_sha256": canonical_sha256({"prompt": current_prompt}),
                }
            )
            reflected = rollout.run_candidate(
                task=reflected_task,
                role="baseline",
                artifact_ids=[],
                pair_id=f"{pair_id}-{phase_name}-core{attempt_suffix}",
                mcp_url=None,
            )
            attempt_run_ids.append(reflected.run_id)
            if reflected.status != "COMPLETED" or not reflected.answer.strip():
                raise RuntimeError(f"Core-managed {phase_name} returned no completed artifact")
            raw_content = _strict_reflector_markdown(reflected.answer)
            if prompt_plan is None:
                content = raw_content
                break
            review = review_core_reflector_output(
                prepared.job,
                prompt_plan=prompt_plan,
                markdown=raw_content,
            )
            output_audit = {
                **review.audit_report,
                "repair_count": attempt,
                "raw_output_sha256": canonical_sha256({"content": raw_content}),
            }
            if review.repair_prompt is None:
                content = _strict_reflector_markdown(review.content)
                break
            if attempt >= max_repairs:
                raise ValueError(
                    f"{phase_name} output failed Core audit after {max_repairs} repairs"
                )
            current_prompt = review.repair_prompt
        elapsed = time.monotonic() - started
        reflector_run_id = attempt_run_ids[-1]
        draft, content_path = self._artifact_draft(
            kind=kind,
            job_id=prepared.job.job_id,
            dataset_artifact_id=prepared.dataset_artifact_id,
            dataset_uri=prepared.dataset_uri,
            content=content,
            task=task,
            pair_id=pair_id,
            parent_run_id=baseline.run_id,
            reflector_run_id=reflector_run_id,
            model=str(rollout.candidate["agent"]["model_name"]),
            prompt_hash=prompt_hash,
            system_prompt_hash=_core_reflector_contract_hash(kind, self.prompt_profile),
            evidence_hash=evidence_hash,
            reflector_attempt_run_ids=attempt_run_ids,
            output_audit=output_audit,
        )
        return _PendingReflection(
            kind=kind,
            phase_name=phase_name,
            job_id=prepared.job.job_id,
            lease_id=prepared.job.lease_id,
            dataset_artifact_id=prepared.dataset_artifact_id,
            dataset_uri=prepared.dataset_uri,
            reflector_run_id=reflector_run_id,
            model=str(rollout.candidate["agent"]["model_name"]),
            system_prompt_hash=_core_reflector_contract_hash(kind, self.prompt_profile),
            prompt_hash=prompt_hash,
            content=content,
            content_path=content_path,
            draft=draft,
            generation_time_seconds=elapsed,
            reflector_attempt_run_ids=(
                attempt_run_ids
                if self.prompt_profile == CORE_FULL_WORKER_PROMPT_PROFILE
                else []
            ),
            output_audit=output_audit,
        )

    def _persist_frozen_prompts(
        self,
        *,
        pair_id: str,
        plans: dict[ArtifactKind, CoreReflectorPromptPlan],
        prompt_hashes: dict[ArtifactKind, str],
    ) -> None:
        prompt_root = self.run_root / pair_id / "reflector_prompts"
        prompt_root.mkdir(parents=True, exist_ok=True)
        for kind in THREE_ARTIFACT_ORDER:
            path = prompt_root / f"{kind.value}.md"
            content = plans[kind].codex_prompt.rstrip() + "\n"
            if path.exists() and path.read_text(encoding="utf-8") != content:
                raise ValueError(f"frozen Reflector prompt drift: {kind.value}")
            path.write_text(content, encoding="utf-8")
        receipt = {
            "schema_version": "chemcrow_core_full_worker_prompt_freeze_v1",
            "prompt_profile": self.prompt_profile,
            "sibling_outputs_visible": False,
            "prompts_frozen_before_first_invocation": True,
            "prompt_sha256_by_type": {
                kind.value: prompt_hashes[kind] for kind in THREE_ARTIFACT_ORDER
            },
        }
        (prompt_root / "freeze.receipt.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _load_frozen_prompts(
        self,
        *,
        pair_id: str,
    ) -> tuple[dict[ArtifactKind, str], dict[ArtifactKind, str]] | None:
        prompt_root = self.run_root / pair_id / "reflector_prompts"
        receipt_path = prompt_root / "freeze.receipt.json"
        prompt_paths = {
            kind: prompt_root / f"{kind.value}.md" for kind in THREE_ARTIFACT_ORDER
        }
        existing = [receipt_path.exists(), *(path.exists() for path in prompt_paths.values())]
        if not any(existing):
            return None
        if not all(existing):
            raise ValueError("frozen Reflector prompt inventory is incomplete")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (
            receipt.get("schema_version") != "chemcrow_core_full_worker_prompt_freeze_v1"
            or receipt.get("prompt_profile") != self.prompt_profile
            or receipt.get("sibling_outputs_visible") is not False
            or receipt.get("prompts_frozen_before_first_invocation") is not True
        ):
            raise ValueError("frozen Reflector prompt receipt authority differs")
        raw_hashes = receipt.get("prompt_sha256_by_type")
        if not isinstance(raw_hashes, dict) or set(raw_hashes) != {
            kind.value for kind in THREE_ARTIFACT_ORDER
        }:
            raise ValueError("frozen Reflector prompt hash inventory differs")
        prompts = {
            kind: prompt_paths[kind].read_text(encoding="utf-8")
            for kind in THREE_ARTIFACT_ORDER
        }
        hashes = {kind: str(raw_hashes[kind.value]) for kind in THREE_ARTIFACT_ORDER}
        if any(
            canonical_sha256({"prompt": prompts[kind]}) != hashes[kind]
            for kind in THREE_ARTIFACT_ORDER
        ):
            raise ValueError("frozen Reflector prompt content hash differs")
        return prompts, hashes

    def _reflector_prompt(
        self,
        *,
        kind: ArtifactKind,
        evidence: dict[str, Any],
    ) -> str:
        return render_core_native_reflector_prompt(kind, evidence)

    def _artifact_draft(
        self,
        *,
        kind: ArtifactKind,
        job_id: str,
        dataset_artifact_id: str,
        dataset_uri: str,
        content: str,
        task: TaskItem,
        pair_id: str,
        parent_run_id: str,
        reflector_run_id: str,
        model: str,
        prompt_hash: str,
        system_prompt_hash: str,
        evidence_hash: str,
        reflector_attempt_run_ids: list[str],
        output_audit: dict[str, object],
    ) -> tuple[ArtifactRegisterRequest, Path]:
        if len(content.encode("utf-8")) > 256 * 1024:
            raise RuntimeError("Core-managed Reflector output exceeds the artifact limit")
        method = _METHODS[kind]
        output_root = self.evolution_artifact_root / "workers" / job_id / method
        if kind is ArtifactKind.TEXT_MEMORY:
            content_path = output_root / "memory.md"
            uri_path = content_path
            manifest: dict[str, Any] = {"content_path": "memory.md"}
        elif kind is ArtifactKind.SKILL_BUNDLE:
            content_path = output_root / "SKILL.md"
            uri_path = output_root
            manifest = {"entrypoint": "SKILL.md", "files": ["SKILL.md"]}
        else:
            content_path = output_root / "AGENTS.md"
            uri_path = content_path
            manifest = {"content_path": "AGENTS.md", "target_path": "AGENTS.md"}
        content_path.parent.mkdir(parents=True, exist_ok=True)
        content_path.write_text(content.rstrip() + "\n", encoding="utf-8")
        if urlparse(uri_path.resolve().as_uri()).scheme != "file":
            raise RuntimeError("artifact payload must be a sealed local file URI")
        provenance = {
            "protocol": CORE_NATIVE_THREE_ARTIFACT_PROTOCOL_LABEL,
            "method": method,
            "task_id": task.task_id,
            "pair_id": pair_id,
            "parent_run_id": parent_run_id,
            "source_dataset_artifact_id": dataset_artifact_id,
            "source_dataset_uri": dataset_uri,
            "reflector_job_id": job_id,
            "reflector_run_id": reflector_run_id,
            "reflector_model": model,
            "reflector_prompt_sha256": prompt_hash,
            "reflector_system_sha256": system_prompt_hash,
            "reflector_prompt_profile": self.prompt_profile,
            "reflector_attempt_run_ids": reflector_attempt_run_ids,
            "reflector_output_audit": output_audit,
            "input_evidence_sha256": evidence_hash,
            "execution_route": CORE_MANAGED_CODEX_ROUTE,
            "sibling_outputs_visible": False,
            "historical_answers_included": False,
            "paper_evaluator_feedback_included": False,
        }
        manifest.update(provenance)
        return (
            ArtifactRegisterRequest(
                type=_CORE_TYPES[kind],
                name=f"{task.task_id} isolated {kind.value}",
                uri=uri_path.resolve().as_uri(),
                manifest=manifest,
                lineage={
                    **provenance,
                    "input_artifact_ids": [dataset_artifact_id],
                },
                compatibility={"task_tags": [task.task_id]},
                tags=[
                    "chemcrow",
                    "task-local",
                    "three-isolated-core-native-v2",
                    task.task_id,
                    kind.value,
                ],
                promoted=False,
            ),
            content_path,
        )


def _strict_reflector_markdown(text: str) -> str:
    if "```" in text:
        raise ValueError("Reflector returned a Markdown fence instead of raw Markdown")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Reflector returned empty artifact content")
    return text.strip()


def normalize_artifact_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(re.findall(r"[\w]+", normalized, flags=re.UNICODE))


def detect_artifact_duplicates(
    artifacts: dict[ArtifactKind, str],
    *,
    baseline_answer: str,
    policy: ArtifactSeparationPolicy,
) -> dict[str, list[Any]]:
    if set(artifacts) != set(THREE_ARTIFACT_ORDER):
        raise ValueError("duplicate guard requires exactly three typed artifacts")
    byte_pairs: list[list[str]] = []
    normalized_pairs: list[list[str]] = []
    near_pairs: list[dict[str, object]] = []
    kinds = list(THREE_ARTIFACT_ORDER)
    normalized = {kind: normalize_artifact_text(artifacts[kind]) for kind in kinds}
    for index, left in enumerate(kinds):
        for right in kinds[index + 1 :]:
            labels = [left.value, right.value]
            if artifacts[left].encode("utf-8") == artifacts[right].encode("utf-8"):
                byte_pairs.append(labels)
                continue
            if normalized[left] == normalized[right]:
                normalized_pairs.append(labels)
                continue
            left_tokens = normalized[left].split()
            right_tokens = normalized[right].split()
            sequence_ratio = SequenceMatcher(
                None, normalized[left], normalized[right], autojunk=False
            ).ratio()
            jaccard: float | None = None
            if (
                min(len(left_tokens), len(right_tokens))
                >= policy.minimum_tokens_for_near_duplicate_check
            ):
                left_shingles = _token_shingles(left_tokens, policy.normalized_token_ngram_size)
                right_shingles = _token_shingles(right_tokens, policy.normalized_token_ngram_size)
                union = left_shingles | right_shingles
                jaccard = len(left_shingles & right_shingles) / len(union) if union else 1.0
            if sequence_ratio >= policy.near_duplicate_sequence_threshold or (
                jaccard is not None and jaccard >= policy.near_duplicate_jaccard_threshold
            ):
                near_pairs.append(
                    {
                        "artifact_types": labels,
                        "sequence_ratio": round(sequence_ratio, 6),
                        "token_ngram_jaccard": None if jaccard is None else round(jaccard, 6),
                    }
                )
    baseline_copy: list[dict[str, object]] = []
    normalized_baseline = normalize_artifact_text(baseline_answer)
    for kind in kinds:
        if not normalized_baseline:
            continue
        ratio = SequenceMatcher(
            None, normalized[kind], normalized_baseline, autojunk=False
        ).ratio()
        if normalized[kind] == normalized_baseline or (
            min(len(normalized[kind].split()), len(normalized_baseline.split()))
            >= policy.minimum_tokens_for_near_duplicate_check
            and ratio >= policy.baseline_answer_sequence_threshold
        ):
            baseline_copy.append({"artifact_type": kind.value, "sequence_ratio": round(ratio, 6)})
    return {
        "byte_identical_pairs": byte_pairs,
        "normalized_identical_pairs": normalized_pairs,
        "near_duplicate_pairs": near_pairs,
        "baseline_answer_copy": baseline_copy,
    }


def _token_shingles(tokens: list[str], size: int) -> set[tuple[str, ...]]:
    if len(tokens) < size:
        return {tuple(tokens)} if tokens else set()
    return {tuple(tokens[index : index + size]) for index in range(len(tokens) - size + 1)}


def _find_forbidden_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).casefold()
            if normalized in _FORBIDDEN_FEEDBACK_KEYS:
                found.add(str(key))
            found.update(_find_forbidden_keys(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_find_forbidden_keys(child))
    return found


def _find_forbidden_text(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for child in value.values():
            found.update(_find_forbidden_text(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_find_forbidden_text(child))
    elif isinstance(value, str):
        normalized = unicodedata.normalize("NFKC", value).casefold()
        found.update(marker for marker in _FORBIDDEN_FEEDBACK_TEXT_MARKERS if marker in normalized)
    return found
