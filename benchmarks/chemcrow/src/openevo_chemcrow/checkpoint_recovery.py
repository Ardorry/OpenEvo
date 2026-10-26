from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .compatible_evaluation import (
    build_evolution_judge_task,
    parse_evolution_feedback_result,
)
from .hashing import canonical_sha256, file_sha256, text_sha256
from .ledger import PhaseLedger
from .models import EvaluatorFeedback, TaskItem, Trajectory
from .runtime import (
    CORE_MANAGED_CODEX_ROUTE,
    _message_content,
    _normalize_rollout,
    build_task_request,
    s0_config_hash,
)

_CHECKPOINT_NAME = "baseline-and-evaluator.checkpoint.json"
_RECEIPT_NAME = "baseline-and-evaluator.recovery.json"


def _completion_wall_time(payload: dict[str, Any]) -> float:
    timing = payload.get("timing")
    if not isinstance(timing, dict):
        raise TypeError("Core completion timing evidence is absent")
    values = [value for value in timing.values() if isinstance(value, (int, float))]
    if not values or any(value < 0 for value in values):
        raise ValueError("Core completion timing evidence is invalid")
    return sum(values) / 1000.0


def _load_core_completion(path: Path, *, expected_run_id: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        payload.get("task_id") != expected_run_id
        or payload.get("status") != "COMPLETED"
        or payload.get("error") is not None
        or not isinstance(payload.get("session_id"), str)
        or not payload["session_id"]
    ):
        raise ValueError("Core completion is not a successful unique result")
    metadata = payload.get("metadata")
    if (
        not isinstance(metadata, dict)
        or metadata.get("execution_route") != CORE_MANAGED_CODEX_ROUTE
        or metadata.get("host_codex_exec_forbidden") is not True
    ):
        raise ValueError("Core completion route authority differs")
    return payload


def _require_zero_tool_candidate(payload: dict[str, Any], *, pair_cache_root: Path) -> None:
    if pair_cache_root.exists() and any(path.is_file() for path in pair_cache_root.rglob("*")):
        raise ValueError("checkpoint recovery does not support a Candidate with tool receipts")
    traces = payload.get("trajectory", {}).get("traces", [])
    if not isinstance(traces, list):
        raise TypeError("Candidate trace inventory is invalid")
    for trace in traces:
        metadata = trace.get("metadata") if isinstance(trace, dict) else None
        transcript = metadata.get("transcript") if isinstance(metadata, dict) else None
        if not isinstance(transcript, str):
            continue
        for line in transcript.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item") if isinstance(event, dict) else None
            if isinstance(item, dict) and item.get("type") == "command_execution":
                raise ValueError(
                    "checkpoint recovery does not support Candidate shell executions"
                )


def reconcile_completed_baseline_evaluator_checkpoint(
    *,
    config_path: Path,
    run_root: Path,
    ledger_root: Path,
    cache_root: Path,
    experiment_id: str,
    task: TaskItem,
    candidate_config: dict[str, Any],
    evaluator_config: dict[str, Any],
    evaluator_id: str,
    baseline_completion_path: Path,
    evaluator_completion_path: Path,
    repository_root: Path | None = None,
) -> tuple[Path, Path, Trajectory, EvaluatorFeedback]:
    """Checkpoint two completed Core calls without redispatching either call."""

    pair_id = f"{experiment_id}--{task.task_id}"
    pair_root = run_root / pair_id
    if (pair_root / "pair.result.json").exists():
        raise ValueError("sealed pair cannot be checkpoint-recovered")
    claims_root = ledger_root / pair_id
    claim_paths = {path.stem: path for path in claims_root.glob("*.json")}
    if set(claim_paths) != {"baseline_candidate", "baseline_internal_evaluator"}:
        raise ValueError("checkpoint recovery phase inventory differs")
    baseline_claim = json.loads(claim_paths["baseline_candidate"].read_text(encoding="utf-8"))
    evaluator_claim = json.loads(
        claim_paths["baseline_internal_evaluator"].read_text(encoding="utf-8")
    )
    if (
        baseline_claim.get("status") != "terminal"
        or baseline_claim.get("phase") != "baseline_candidate"
        or evaluator_claim.get("status") != "claimed"
        or evaluator_claim.get("phase") != "baseline_internal_evaluator"
    ):
        raise ValueError("checkpoint recovery claim states differ")
    baseline_receipt = baseline_claim.get("receipt")
    evaluator_authority = evaluator_claim.get("authority")
    if not isinstance(baseline_receipt, dict) or not isinstance(evaluator_authority, dict):
        raise TypeError("checkpoint recovery claim authority is absent")
    baseline_run_id = baseline_receipt.get("run_id")
    if (
        not isinstance(baseline_run_id, str)
        or evaluator_authority.get("task_id") != task.task_id
        or evaluator_authority.get("run_id") != baseline_run_id
        or evaluator_authority.get("evaluator_id") != evaluator_id
        or evaluator_authority.get("paper_evaluator") is not False
    ):
        raise ValueError("checkpoint recovery evaluator authority differs")

    baseline_completion = _load_core_completion(
        baseline_completion_path,
        expected_run_id=baseline_run_id,
    )
    _require_zero_tool_candidate(
        baseline_completion,
        pair_cache_root=cache_root / pair_id,
    )
    baseline = _normalize_rollout(
        {"results": [baseline_completion]},
        run_id=baseline_run_id,
        task_id=task.task_id,
        role="baseline",
        artifact_ids=[],
        config_sha256=s0_config_hash(candidate_config),
        wall_time=_completion_wall_time(baseline_completion),
        tool_receipts=[],
        declared_tool_bridge=True,
        polling_retries=0,
    )
    if baseline.status != "COMPLETED" or not baseline.answer.strip():
        raise ValueError("reconstructed baseline Candidate is incomplete")

    judge_task = build_evolution_judge_task(task=task, trajectory=baseline)
    evaluator_run_id = evaluator_completion_path.parent.name.removeprefix("task_")
    evaluator_completion = _load_core_completion(
        evaluator_completion_path,
        expected_run_id=evaluator_run_id,
    )
    expected_request = build_task_request(
        task=judge_task,
        run_id=evaluator_run_id,
        role="baseline",
        candidate=evaluator_config,
        artifact_ids=[],
        mcp_url=None,
    )
    traces = evaluator_completion.get("trajectory", {}).get("traces", [])
    prompt_messages = traces[0].get("prompt_messages", []) if len(traces) == 1 else []
    if (
        len(prompt_messages) != 1
        or not isinstance(prompt_messages[0], dict)
        or prompt_messages[0].get("role") != "user"
        or _message_content(prompt_messages[0]) != expected_request["instruction"]
    ):
        raise ValueError("completed evaluator input differs from reconstructed checkpoint")
    evaluator_result = _normalize_rollout(
        {"results": [evaluator_completion]},
        run_id=evaluator_run_id,
        task_id=judge_task.task_id,
        role="baseline",
        artifact_ids=[],
        config_sha256=s0_config_hash(evaluator_config),
        wall_time=_completion_wall_time(evaluator_completion),
        tool_receipts=[],
        declared_tool_bridge=False,
        polling_retries=0,
    )
    baseline_evaluation, confidence_receipt = parse_evolution_feedback_result(
        evaluator_result
    )

    checkpoint = {
        "schema_version": "chemcrow_completed_call_checkpoint_v1",
        "status": "READY_FOR_NO_REDISPATCH_RESUME",
        "pair_id": pair_id,
        "task_id": task.task_id,
        "baseline": baseline.model_dump(mode="json"),
        "baseline_internal_evaluation": baseline_evaluation.model_dump(mode="json"),
    }
    recovery_root = run_root / "recovery" / pair_id
    recovery_root.mkdir(parents=True, exist_ok=True)
    checkpoint_path = recovery_root / _CHECKPOINT_NAME
    checkpoint_serialized = json.dumps(checkpoint, indent=2, sort_keys=True) + "\n"
    if checkpoint_path.exists() and checkpoint_path.read_text() != checkpoint_serialized:
        raise ValueError("existing completed-call checkpoint differs")
    checkpoint_path.write_text(checkpoint_serialized, encoding="utf-8")

    repo_root = repository_root or Path(__file__).resolve().parents[4]
    source_paths = {
        "compatible_evaluation": (
            repo_root
            / "benchmarks/chemcrow/src/openevo_chemcrow/compatible_evaluation.py"
        ),
        "original_evaluation_prompt": (
            repo_root / "benchmarks/chemcrow/src/openevo_chemcrow/evaluation.py"
        ),
        "runtime_normalizer": (
            repo_root / "benchmarks/chemcrow/src/openevo_chemcrow/runtime.py"
        ),
    }
    if any(not path.is_file() for path in source_paths.values()):
        raise ValueError("checkpoint recovery source authority is absent")
    recovery_receipt = {
        "schema_version": "chemcrow_completed_call_checkpoint_recovery_v1",
        "status": "VERIFIED_COMPLETED_CALLS_NO_REDISPATCH",
        "pair_id": pair_id,
        "task_id": task.task_id,
        "completed_phases": ["baseline_candidate", "baseline_internal_evaluator"],
        "config_sha256": file_sha256(config_path),
        "checkpoint_sha256": file_sha256(checkpoint_path),
        "baseline_core_completion_sha256": file_sha256(baseline_completion_path),
        "evaluator_core_completion_sha256": file_sha256(evaluator_completion_path),
        "baseline_core_session_id_sha256": text_sha256(
            str(baseline_completion["session_id"])
        ),
        "evaluator_core_session_id_sha256": text_sha256(
            str(evaluator_completion["session_id"])
        ),
        "baseline_original_trajectory_sha256": baseline_receipt.get(
            "trajectory_sha256"
        ),
        "baseline_reconstructed_trajectory_sha256": canonical_sha256(
            baseline.model_dump(mode="json")
        ),
        "evaluator_feedback_sha256": canonical_sha256(
            baseline_evaluation.model_dump(mode="json")
        ),
        "confidence_parse_receipt": confidence_receipt,
        "candidate_redispatched": False,
        "evaluator_redispatched": False,
        "candidate_answer_preserved": True,
        "candidate_tool_receipt_count": 0,
        "prompt_reconstruction_exact": True,
        "recorded_before_successor_phase_dispatch": True,
        "source_authority_sha256": {
            name: file_sha256(path) for name, path in sorted(source_paths.items())
        },
    }
    recovery_path = recovery_root / _RECEIPT_NAME
    recovery_serialized = json.dumps(recovery_receipt, indent=2, sort_keys=True) + "\n"
    if recovery_path.exists() and recovery_path.read_text() != recovery_serialized:
        raise ValueError("existing completed-call recovery receipt differs")
    recovery_path.write_text(recovery_serialized, encoding="utf-8")
    PhaseLedger(ledger_root, pair_id=pair_id).terminal(
        "baseline_internal_evaluator",
        {
            "evaluator_run_id": baseline_evaluation.evaluator_run_id,
            "feedback_sha256": recovery_receipt["evaluator_feedback_sha256"],
            "completed_call_checkpoint_sha256": recovery_receipt["checkpoint_sha256"],
            "checkpoint_recovery_receipt_sha256": file_sha256(recovery_path),
            "confidence_parse_receipt": confidence_receipt,
        },
    )
    return checkpoint_path, recovery_path, baseline, baseline_evaluation


def load_completed_baseline_evaluator_checkpoint(
    *,
    run_root: Path,
    ledger_root: Path,
    pair_id: str,
    task: TaskItem,
    expected_candidate_config_sha256: str,
    expected_evaluator_id: str,
) -> tuple[Trajectory, EvaluatorFeedback] | None:
    recovery_root = run_root / "recovery" / pair_id
    checkpoint_path = recovery_root / _CHECKPOINT_NAME
    recovery_path = recovery_root / _RECEIPT_NAME
    if not checkpoint_path.exists() and not recovery_path.exists():
        return None
    if not checkpoint_path.is_file() or not recovery_path.is_file():
        raise ValueError("completed-call checkpoint evidence is incomplete")
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    receipt = json.loads(recovery_path.read_text(encoding="utf-8"))
    if (
        checkpoint.get("schema_version") != "chemcrow_completed_call_checkpoint_v1"
        or checkpoint.get("status") != "READY_FOR_NO_REDISPATCH_RESUME"
        or checkpoint.get("pair_id") != pair_id
        or checkpoint.get("task_id") != task.task_id
        or receipt.get("status") != "VERIFIED_COMPLETED_CALLS_NO_REDISPATCH"
        or receipt.get("checkpoint_sha256") != file_sha256(checkpoint_path)
        or receipt.get("candidate_redispatched") is not False
        or receipt.get("evaluator_redispatched") is not False
        or receipt.get("prompt_reconstruction_exact") is not True
    ):
        raise ValueError("completed-call checkpoint authority differs")
    claims = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in (ledger_root / pair_id).glob("*.json")
    }
    if set(claims) != {"baseline_candidate", "baseline_internal_evaluator"} or any(
        claim.get("status") != "terminal" for claim in claims.values()
    ):
        raise ValueError("completed-call checkpoint claim inventory differs")
    evaluator_claim_receipt = claims["baseline_internal_evaluator"].get("receipt")
    if (
        not isinstance(evaluator_claim_receipt, dict)
        or evaluator_claim_receipt.get("checkpoint_recovery_receipt_sha256")
        != file_sha256(recovery_path)
    ):
        raise ValueError("completed-call checkpoint claim binding differs")
    baseline = Trajectory.model_validate(checkpoint.get("baseline"))
    evaluation = EvaluatorFeedback.model_validate(
        checkpoint.get("baseline_internal_evaluation")
    )
    if (
        baseline.task_id != task.task_id
        or baseline.role != "baseline"
        or baseline.status != "COMPLETED"
        or baseline.artifact_ids
        or baseline.candidate_config_sha256 != expected_candidate_config_sha256
        or evaluation.evaluator_role != "evolution_evaluator"
        or claims["baseline_internal_evaluator"].get("authority", {}).get("evaluator_id")
        != expected_evaluator_id
    ):
        raise ValueError("completed-call checkpoint semantic authority differs")
    return baseline, evaluation


__all__ = [
    "load_completed_baseline_evaluator_checkpoint",
    "reconcile_completed_baseline_evaluator_checkpoint",
]
