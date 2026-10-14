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

_PAPER_COMPARISONS = {
    "baseline": ("openevo_baseline", "historical_gpt4"),
    "evolved": ("openevo_evolved", "historical_gpt4"),
    "historical_control": ("historical_chemcrow", "historical_gpt4"),
}


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


def build_public_paper_judge_evidence(
    *, results_root: Path, aggregate_path: Path, run_id: str
) -> dict[str, Any]:
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    if (
        aggregate.get("schema_version") != "chemcrow_paper_evaluator_aggregate_v1"
        or aggregate.get("status") != "PROVISIONAL_LLM_JUDGED_RESULT"
        or aggregate.get("task_count") != 14
        or aggregate.get("result_count") != 42
    ):
        raise ValueError("public paper-Judge aggregate differs")

    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for path in sorted(results_root.glob("*.result.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        task_id = raw.get("task_id")
        comparison = raw.get("comparison")
        key = (str(task_id), str(comparison))
        expected_systems = _PAPER_COMPARISONS.get(str(comparison))
        assessment = raw.get("assessment")
        if (
            task_id not in FROZEN_PAPER_TASK_IDS
            or expected_systems is None
            or key in indexed
            or raw.get("schema_version") != "chemcrow_paper_evaluator_result_v1"
            or raw.get("student_a_system") != expected_systems[0]
            or raw.get("student_b_system") != expected_systems[1]
            or raw.get("provisional_llm_judged") is not True
            or raw.get("reflector_access") is not False
            or not isinstance(assessment, dict)
        ):
            raise ValueError(f"public paper-Judge result differs: {path.name}")
        grades: dict[str, float] = {}
        for student in ("student_a", "student_b"):
            student_assessment = assessment.get(student)
            grade = (
                student_assessment.get("grade")
                if isinstance(student_assessment, dict)
                else None
            )
            if not isinstance(grade, (int, float)) or isinstance(grade, bool):
                raise TypeError(f"public paper-Judge grade is invalid: {path.name}")
            if float(grade) < 0.0 or float(grade) > 10.0:
                raise ValueError(f"public paper-Judge grade is out of range: {path.name}")
            grades[student] = float(grade)
        indexed[key] = {
            "comparison": comparison,
            "call_id": raw["call_id"],
            "student_a_system": raw["student_a_system"],
            "student_a_grade": grades["student_a"],
            "student_b_system": raw["student_b_system"],
            "student_b_grade": grades["student_b"],
            "assessment_sha256": raw["assessment_sha256"],
            "prompt_sha256": raw["prompt_sha256"],
            "core_terminal_payload_sha256": raw["core_terminal_payload_sha256"],
            "openrouter_receipt_sha256": raw["openrouter_receipt_sha256"],
            "provisional_llm_judged": True,
        }
    expected_keys = {
        (task_id, comparison)
        for task_id in FROZEN_PAPER_TASK_IDS
        for comparison in _PAPER_COMPARISONS
    }
    if set(indexed) != expected_keys:
        raise ValueError("public paper-Judge result matrix is incomplete")

    rows = [
        {
            "task_id": task_id,
            "comparisons": [
                indexed[(task_id, comparison)] for comparison in _PAPER_COMPARISONS
            ],
        }
        for task_id in FROZEN_PAPER_TASK_IDS
    ]

    def target_grades(comparison: str) -> list[float]:
        return [indexed[(task_id, comparison)]["student_a_grade"] for task_id in FROZEN_PAPER_TASK_IDS]

    def mean(values: list[float]) -> float:
        return sum(values) / len(values)

    baseline = target_grades("baseline")
    evolved = target_grades("evolved")
    historical = target_grades("historical_control")
    deltas = [after - before for before, after in zip(baseline, evolved)]
    computed = {
        "mean_openevo_baseline_grade": mean(baseline),
        "mean_openevo_evolved_grade": mean(evolved),
        "mean_historical_chemcrow_grade": mean(historical),
        "mean_evolved_minus_baseline": mean(deltas),
        "evolved_vs_baseline_wins": sum(delta > 0 for delta in deltas),
        "evolved_vs_baseline_ties": sum(delta == 0 for delta in deltas),
        "evolved_vs_baseline_losses": sum(delta < 0 for delta in deltas),
    }
    for field, value in computed.items():
        observed = aggregate.get(field)
        if isinstance(value, float):
            if not isinstance(observed, (int, float)) or abs(float(observed) - value) > 1e-12:
                raise ValueError(f"public paper-Judge aggregate field differs: {field}")
        elif observed != value:
            raise ValueError(f"public paper-Judge aggregate field differs: {field}")

    payload: dict[str, Any] = {
        "schema_version": "chemcrow_public_paper_judge_evidence_v1",
        "run_kind": "authoritative_original",
        "run_id": run_id,
        "protocol": aggregate["protocol"],
        "model": aggregate["model"],
        "plan_sha256": aggregate["plan_sha256"],
        "aggregate_sha256": file_sha256(aggregate_path),
        "task_ids": list(FROZEN_PAPER_TASK_IDS),
        "task_count": 14,
        "result_count": 42,
        "computed_aggregate": computed,
        "human_review_status": "PROVISIONAL_NOT_COMPLETED",
        "answer_bodies_included": False,
        "assessment_text_included": False,
        "prompt_bodies_included": False,
        "provider_responses_included": False,
        "secrets_included": False,
        "host_paths_included": False,
        "results": rows,
    }
    payload["evidence_id"] = canonical_sha256(payload)
    return payload
