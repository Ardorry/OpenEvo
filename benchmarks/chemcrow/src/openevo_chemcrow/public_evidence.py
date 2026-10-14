from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .hashing import canonical_sha256, file_sha256
from .models import TaskItem
from .paper_evaluator import FROZEN_PAPER_TASK_IDS
from .submission import TASK_MANIFEST_SHA256
from .three_artifact_aggregate import aggregate_three_artifact_results
from .three_artifact_models import ThreeArtifactPairResult


def _answer_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_public_per_task_evidence(
    *,
    run_root: Path,
    task_manifest: Path,
    experiment_id: str,
    completed_audit: Path,
    aggregate_path: Path,
    source_code_commit: str,
) -> dict[str, Any]:
    if file_sha256(task_manifest) != TASK_MANIFEST_SHA256:
        raise ValueError("public evidence task manifest differs")
    tasks = [
        TaskItem.model_validate_json(line)
        for line in task_manifest.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if tuple(item.task_id for item in tasks) != FROZEN_PAPER_TASK_IDS:
        raise ValueError("public evidence task order differs")
    audit = json.loads(completed_audit.read_text(encoding="utf-8"))
    if audit.get("status") != "PASS" or audit.get("task_count") != 14:
        raise ValueError("public evidence audit is not PASS for 14 tasks")
    results: list[ThreeArtifactPairResult] = []
    for task in tasks:
        pair_id = f"{experiment_id}--{task.task_id}"
        pair = ThreeArtifactPairResult.model_validate_json(
            (run_root / pair_id / "pair.result.json").read_text(encoding="utf-8")
        )
        if pair.pair_id != pair_id:
            raise ValueError("public evidence pair identity differs")
        results.append(pair)
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    if aggregate != aggregate_three_artifact_results(results):
        raise ValueError("public evidence aggregate differs from task rows")
    rows: list[dict[str, Any]] = []
    for pair in results:
        rows.append(
            {
                "task_id": pair.task_id,
                "pair_id": pair.pair_id,
                "task_prompt_sha256": pair.task_prompt_hash,
                "s0_sha256": pair.s0_hash,
                "baseline": {
                    "run_id": pair.baseline.run_id,
                    "answer_sha256": _answer_hash(pair.baseline.answer),
                    "internal_scores": pair.baseline_internal_evaluation.scores.model_dump(
                        mode="json"
                    ),
                },
                "evolved": {
                    "run_id": pair.evolved.run_id,
                    "answer_sha256": _answer_hash(pair.evolved.answer),
                    "internal_scores": pair.evolved_internal_evaluation.scores.model_dump(
                        mode="json"
                    ),
                },
                "artifacts": [
                    {
                        "artifact_type": item.artifact_type.value,
                        "artifact_id": item.artifact_id,
                        "artifact_sha256": item.artifact_hash,
                        "artifact_bytes": item.size_bytes,
                        "reflector_job_id": item.reflector_job_id,
                        "reflector_run_id": item.reflector_run_id,
                        "prompt_sha256": item.prompt_hash,
                        "input_evidence_sha256": item.input_evidence_hash,
                    }
                    for item in pair.artifact_bundle.artifacts
                ],
                "injection_receipt_sha256": pair.injection_receipt.receipt_sha256,
                "reset_receipt_sha256": pair.reset_receipt_sha256,
                "blind_evaluation": {
                    "baseline_scores": pair.final_evaluation.baseline_scores.model_dump(
                        mode="json"
                    ),
                    "evolved_scores": pair.final_evaluation.evolved_scores.model_dump(mode="json"),
                    "winner": pair.final_evaluation.winner,
                    "confidence": pair.final_evaluation.confidence,
                    "mapping_seal_sha256": pair.final_evaluation.mapping_seal_sha256,
                    "provisional_llm_judged": pair.final_evaluation.provisional_llm_judged,
                },
            }
        )
    payload: dict[str, Any] = {
        "schema_version": "chemcrow_public_per_task_evidence_v1",
        "run_kind": "authoritative_original",
        "experiment_id": experiment_id,
        "source_code_commit": source_code_commit,
        "task_manifest_sha256": TASK_MANIFEST_SHA256,
        "task_ids": list(FROZEN_PAPER_TASK_IDS),
        "task_count": 14,
        "artifact_count": 42,
        "completed_audit_sha256": file_sha256(completed_audit),
        "aggregate_sha256": file_sha256(aggregate_path),
        "raw_answers_included": False,
        "task_prompts_included": False,
        "tool_observations_included": False,
        "host_paths_included": False,
        "secrets_included": False,
        "human_review_status": "PROVISIONAL_NOT_COMPLETED",
        "results": rows,
    }
    payload["evidence_id"] = canonical_sha256(payload)
    return payload
