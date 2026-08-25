from __future__ import annotations

import json
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass
from difflib import SequenceMatcher
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
from .models import ArtifactKind, TaskItem, Trajectory
from .runtime import CORE_MANAGED_CODEX_ROUTE, OpenEvoRolloutPort
from .three_artifact_models import (
    THREE_ARTIFACT_ORDER,
    ArtifactSeparationPolicy,
    ThreeArtifactBundleReceipt,
    ThreeArtifactReceipt,
)

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
_CONTENT_KEYS = {
    ArtifactKind.TEXT_MEMORY: "memory",
    ArtifactKind.SKILL_BUNDLE: "skill_markdown",
    ArtifactKind.AGENT_SYSTEM: "agent_system_markdown",
}
_PHASE_NAMES = {
    ArtifactKind.TEXT_MEMORY: "reflector_memory",
    ArtifactKind.SKILL_BUNDLE: "reflector_skill_bundle",
    ArtifactKind.AGENT_SYSTEM: "reflector_agent_system",
}
_SYSTEM_CONTRACTS = {
    ArtifactKind.TEXT_MEMORY: (
        "You are Reflector-Memory. Produce short-lived same-task memory only: observed facts, "
        "missed tool evidence, verified corrections, and baseline failure lessons. Do not copy the "
        "complete baseline answer, prescribe a long procedure, or write system policy. Explicitly "
        "label at least one observation, correction, lesson, or fact."
    ),
    ArtifactKind.SKILL_BUNDLE: (
        "You are Reflector-Skill. Produce an executable same-task problem-solving workflow: tool "
        "selection rules, reasoning procedure, verification checklist, and recovery steps. Focus "
        "on how to solve and verify; do not write a fact memory or high-level agent policy. Explicitly "
        "label at least one procedure, workflow, checklist, step, or tool-selection rule."
    ),
    ArtifactKind.AGENT_SYSTEM: (
        "You are Reflector-AgentSystem. Produce same-task high-level behavioral rules for evidence "
        "discipline, tool grounding, hallucination suppression, verification, and final answer "
        "composition. Focus on agent behavior; do not write a factual notebook or detailed workflow. "
        "Explicitly label at least one behavior, policy, discipline, hallucination control, or final-answer rule."
    ),
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
_RESPONSIBILITY_MARKERS = {
    ArtifactKind.TEXT_MEMORY: (
        "observed",
        "observation",
        "remember",
        "baseline",
        "correction",
        "corrected",
        "lesson",
        "fact",
    ),
    ArtifactKind.SKILL_BUNDLE: (
        "procedure",
        "workflow",
        "checklist",
        "step",
        "steps",
        "tool selection",
        "tool-selection",
    ),
    ArtifactKind.AGENT_SYSTEM: (
        "behavior",
        "policy",
        "discipline",
        "hallucination",
        "unsupported",
        "final answer",
        "must",
        "never",
    ),
}


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
        evidence = {
            "task": {
                "task_id": task.task_id,
                "prompt": task.prompt,
                "category": task.broad_category,
                "allowed_tool_metadata": task.allowed_tool_metadata,
                "safety_metadata": task.safety_metadata,
                "sanitized_item_sha256": task.sanitized_item_sha256,
            },
            "baseline": baseline.model_dump(mode="json"),
            "feedback": feedback_payload,
        }
        evidence_hash = canonical_sha256(evidence)
        prompts = {
            kind: self._reflector_prompt(kind=kind, task=task, evidence=evidence)
            for kind in THREE_ARTIFACT_ORDER
        }
        prompt_hashes = {
            kind: canonical_sha256({"prompt": prompt}) for kind, prompt in prompts.items()
        }
        if len(set(prompt_hashes.values())) != 3:
            raise ValueError("three Reflector prompts are not independent")

        store = EvolutionStore(
            db_path=self.evolution_db_path,
            artifact_root=self.evolution_artifact_root,
        )
        store.initialize()
        pending: list[_PendingReflection] = []
        completed_job_ids: set[str] = set()
        try:
            # Each job is claimed and invoked independently. Prompts were frozen
            # before the first invocation, so no sibling output can enter another.
            for kind in THREE_ARTIFACT_ORDER:
                pending.append(
                    self._run_reflector_subjob(
                        store=store,
                        kind=kind,
                        task=task,
                        baseline=baseline,
                        evidence=evidence,
                        evidence_hash=evidence_hash,
                        prompt=prompts[kind],
                        prompt_hash=prompt_hashes[kind],
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
                            "protocol": "chemcrow-three-isolated-artifacts-v1",
                            "steps": 1,
                            "artifact_type": item.kind.value,
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
                    )
                )
            return ThreeArtifactBundleReceipt(
                task_id=task.task_id,
                pair_id=pair_id,
                parent_run_id=baseline.run_id,
                input_evidence_hash=evidence_hash,
                separation_policy=self.separation_policy,
                artifacts=receipts,
                **duplicate_findings,
            )
        except Exception as exc:
            for item in pending:
                if item.job_id in completed_job_ids:
                    continue
                try:
                    store.fail_job(
                        item.job_id,
                        WorkerFailRequest(
                            lease_id=item.lease_id,
                            error=str(exc),
                            retryable=False,
                        ),
                    )
                except Exception as cleanup_exc:  # noqa: BLE001
                    exc.add_note(f"Core fail_job cleanup failed for {item.job_id}: {cleanup_exc}")
            raise

    def _run_reflector_subjob(
        self,
        *,
        store: EvolutionStore,
        kind: ArtifactKind,
        task: TaskItem,
        baseline: Trajectory,
        evidence: dict[str, Any],
        evidence_hash: str,
        prompt: str,
        prompt_hash: str,
        pair_id: str,
    ) -> _PendingReflection:
        phase_name = _PHASE_NAMES[kind]
        source_event_id = f"{pair_id}-{kind.value}-baseline-feedback"
        trace = {
            "prompt_messages": [{"role": "user", "content": task.prompt}],
            "response_messages": [{"role": "assistant", "content": baseline.answer}],
            "tools": None,
            "finish_reason": baseline.status,
            "reward": None,
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
                policy_version="chemcrow-task-local-three-isolated-v1",
                rollout_step=0,
                agent={"harness": "chemcrow-adapter", "reflector_role": kind.value},
                base_model=str(rollout.candidate["agent"]["model_name"]),
                status=baseline.status,
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
                    "reflector_prompt_sha256": prompt_hash,
                    "reflector_system_sha256": canonical_sha256(
                        {"system_contract": _SYSTEM_CONTRACTS[kind]}
                    ),
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
                # pass the duplicate guard. Cover the three sequential 900-second
                # Core rollout ceilings without expiring an earlier sibling job.
                lease_seconds=3600,
            )
        )
        if claim.job is None or claim.job.job_id != job.job_id:
            raise RuntimeError(f"Core did not return the isolated {kind.value} Reflector job")
        store.heartbeat_job(
            job.job_id,
            WorkerHeartbeatRequest(lease_id=claim.job.lease_id, progress=0.0, message="claimed"),
        )
        reflected_task = task.model_copy(
            update={
                "task_id": f"{task.task_id}-{phase_name}",
                "prompt": prompt,
                "sanitized_item_sha256": canonical_sha256({"prompt": prompt}),
            }
        )
        started = time.monotonic()
        reflected = rollout.run_candidate(
            task=reflected_task,
            role="baseline",
            artifact_ids=[],
            pair_id=f"{pair_id}-{phase_name}-core",
            mcp_url=None,
        )
        elapsed = time.monotonic() - started
        if reflected.status != "COMPLETED" or not reflected.answer.strip():
            raise RuntimeError(f"Core-managed {phase_name} returned no completed artifact")
        content = _strict_reflector_content(reflected.answer, kind=kind)
        draft, content_path = self._artifact_draft(
            kind=kind,
            job_id=job.job_id,
            dataset_artifact_id=dataset.artifact_id,
            dataset_uri=claim.job.input_artifacts[0].uri,
            content=content,
            task=task,
            pair_id=pair_id,
            parent_run_id=baseline.run_id,
            reflector_run_id=reflected.run_id,
            model=str(rollout.candidate["agent"]["model_name"]),
            prompt_hash=prompt_hash,
            system_prompt_hash=canonical_sha256({"system_contract": _SYSTEM_CONTRACTS[kind]}),
            evidence_hash=evidence_hash,
        )
        return _PendingReflection(
            kind=kind,
            phase_name=phase_name,
            job_id=job.job_id,
            lease_id=claim.job.lease_id,
            dataset_artifact_id=dataset.artifact_id,
            dataset_uri=claim.job.input_artifacts[0].uri,
            reflector_run_id=reflected.run_id,
            model=str(rollout.candidate["agent"]["model_name"]),
            system_prompt_hash=canonical_sha256({"system_contract": _SYSTEM_CONTRACTS[kind]}),
            prompt_hash=prompt_hash,
            content=content,
            content_path=content_path,
            draft=draft,
            generation_time_seconds=elapsed,
        )

    def _reflector_prompt(
        self,
        *,
        kind: ArtifactKind,
        task: TaskItem,
        evidence: dict[str, Any],
    ) -> str:
        content_key = _CONTENT_KEYS[kind]
        safety_clause = (
            " Preserve the intended safety refusal/caution; never optimize toward unsafe operational assistance."
            if task.safety_metadata.get("safety_behavior_must_be_preserved") is True
            else ""
        )
        schema = {"artifact_type": kind.value, content_key: "non-empty Markdown string"}
        return (
            f"SYSTEM CONTRACT ({kind.value}):\n{_SYSTEM_CONTRACTS[kind]}\n\n"
            "You are one of three mutually isolated OpenEvo task-local Reflectors. Your prompt was "
            "frozen before any sibling invocation. You cannot see and must not infer sibling outputs. "
            "Use only the EVIDENCE JSON below. Do not browse, call tools, introduce historical "
            "ChemCrow/GPT-4 answers, human scores, paper EvaluatorGPT grades, hidden ground truth, "
            "future G2 output, or later tasks. This is software evaluation; never direct physical "
            f"execution or procurement.{safety_clause}\n\n"
            "Return one strict JSON object only: no Markdown fence, preamble, or extra keys. "
            f"Required schema: {json.dumps(schema, sort_keys=True)}\n\n"
            "EVIDENCE JSON:\n"
            f"{json.dumps(evidence, ensure_ascii=True, sort_keys=True)}"
        )

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
            "protocol": "chemcrow-three-isolated-artifacts-v1",
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
                    "three-isolated-v1",
                    task.task_id,
                    kind.value,
                ],
                promoted=False,
            ),
            content_path,
        )


def _strict_reflector_content(text: str, *, kind: ArtifactKind) -> str:
    if "```" in text:
        raise ValueError("Reflector returned a Markdown fence instead of strict JSON")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("Reflector did not return strict JSON") from exc
    content_key = _CONTENT_KEYS[kind]
    if not isinstance(value, dict) or set(value) != {"artifact_type", content_key}:
        raise ValueError(f"{kind.value} Reflector schema differs from authority")
    if value.get("artifact_type") != kind.value:
        raise ValueError("Reflector returned the wrong artifact type")
    content = value.get(content_key)
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Reflector returned empty artifact content")
    return content.strip()


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
    responsibility_violations = _artifact_responsibility_violations(
        artifacts,
        policy=policy,
    )
    return {
        "byte_identical_pairs": byte_pairs,
        "normalized_identical_pairs": normalized_pairs,
        "near_duplicate_pairs": near_pairs,
        "baseline_answer_copy": baseline_copy,
        "responsibility_violations": responsibility_violations,
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


def _artifact_responsibility_violations(
    artifacts: dict[ArtifactKind, str],
    *,
    policy: ArtifactSeparationPolicy,
) -> list[dict[str, object]]:
    violations: list[dict[str, object]] = []
    for kind in THREE_ARTIFACT_ORDER:
        normalized = normalize_artifact_text(artifacts[kind])
        tokens = normalized.split()
        markers = _RESPONSIBILITY_MARKERS[kind]
        matched = sorted(marker for marker in markers if marker in normalized)
        reasons: list[str] = []
        if len(tokens) < policy.minimum_tokens_for_responsibility_check:
            reasons.append("too_short")
        if not matched:
            reasons.append("missing_type_specific_responsibility_marker")
        if reasons:
            violations.append(
                {
                    "artifact_type": kind.value,
                    "policy_version": policy.responsibility_policy_version,
                    "reasons": reasons,
                    "token_count": len(tokens),
                    "matched_markers": matched,
                }
            )
    return violations
