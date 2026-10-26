from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .cache import PairObservationCache
from .hashing import canonical_sha256, file_sha256, text_sha256
from .models import TaskItem, ToolObservation, Trajectory
from .replacement_ledger import VerifiedReplacementPhaseLedger
from .runtime import CORE_MANAGED_CODEX_ROUTE, _normalize_rollout, s0_config_hash

_RECOVERABLE_ERROR = (
    "agent execution failed: Codex subscription credential isolation could not be "
    "proven (validation_failed)"
)
_CHECKPOINT_NAME = "baseline-before-evaluator.checkpoint.json"
_RECEIPT_NAME = "baseline-evaluator.attempt-1.no-effect.json"


def _completion_wall_time(payload: dict[str, Any]) -> float:
    timing = payload.get("timing")
    if not isinstance(timing, dict):
        raise TypeError("Core completion timing evidence is absent")
    values = [value for value in timing.values() if isinstance(value, (int, float))]
    if not values or any(value < 0 for value in values):
        raise ValueError("Core completion timing evidence is invalid")
    return sum(values) / 1000.0


def _load_successful_baseline(path: Path, *, expected_run_id: str) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    metadata = payload.get("metadata")
    if (
        payload.get("task_id") != expected_run_id
        or payload.get("status") != "COMPLETED"
        or payload.get("error") is not None
        or not isinstance(payload.get("session_id"), str)
        or not payload["session_id"]
        or not isinstance(metadata, dict)
        or metadata.get("execution_route") != CORE_MANAGED_CODEX_ROUTE
        or metadata.get("host_codex_exec_forbidden") is not True
    ):
        raise ValueError("baseline Core completion authority differs")
    return payload


def _reconstruct_tool_receipts(
    completion: dict[str, Any], *, pair_cache_root: Path
) -> list[dict[str, Any]]:
    """Recover the exact persisted ChemCrow observations from the Core transcript."""

    traces = completion.get("trajectory", {}).get("traces", [])
    if not isinstance(traces, list):
        raise TypeError("baseline trace inventory is invalid")
    receipts: list[dict[str, Any]] = []
    seen_call_ids: set[str] = set()
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
            if (
                not isinstance(item, dict)
                or item.get("type") != "command_execution"
                or item.get("status") != "completed"
                or "/tool/" not in str(item.get("command") or "")
            ):
                continue
            raw_output = item.get("aggregated_output")
            try:
                output = json.loads(raw_output) if isinstance(raw_output, str) else None
            except json.JSONDecodeError as exc:
                raise ValueError("completed ChemCrow bridge output is not JSON") from exc
            if not isinstance(output, dict):
                raise TypeError("completed ChemCrow bridge output is not an object")
            arguments = output.get("arguments")
            call_id = output.get("call_id")
            tool_name = output.get("tool")
            if (
                not isinstance(arguments, dict)
                or not isinstance(call_id, str)
                or not call_id
                or not isinstance(tool_name, str)
                or not tool_name
                or call_id in seen_call_ids
            ):
                raise ValueError("completed ChemCrow bridge receipt identity is invalid")
            seen_call_ids.add(call_id)
            observation = ToolObservation(
                call_id=call_id,
                tool_name=tool_name,
                canonical_arguments_sha256=str(output.get("canonical_arguments_sha256")),
                result=output.get("result"),
                error=output.get("error"),
                source=output.get("observation_source"),
                elapsed_seconds=float(output.get("elapsed_seconds") or 0.0),
            )
            if observation.canonical_arguments_sha256 != canonical_sha256(arguments):
                raise ValueError("recovered ChemCrow arguments hash differs")
            cache_path = pair_cache_root / (
                PairObservationCache.key(tool_name, arguments) + ".json"
            )
            if not cache_path.is_file():
                raise ValueError("recovered ChemCrow observation cache entry is absent")
            cached = ToolObservation.model_validate_json(cache_path.read_text(encoding="utf-8"))
            if cached.model_dump(mode="json") != observation.model_dump(mode="json"):
                raise ValueError("Core transcript and ChemCrow observation cache differ")
            receipts.append(
                {
                    "arguments": arguments,
                    "observation": observation.model_dump(mode="json"),
                }
            )
    return receipts


def reconcile_baseline_evaluator_no_effect_failure(
    *,
    config_path: Path,
    run_root: Path,
    ledger_root: Path,
    cache_root: Path,
    experiment_id: str,
    task: TaskItem,
    candidate_config: dict[str, Any],
    evaluator_id: str,
    baseline_completion_path: Path,
    evaluator_completion_path: Path,
    repository_root: Path | None = None,
) -> tuple[Path, Path, Trajectory, dict[str, Any]]:
    """Preserve G1 and prove a pre-model evaluator failure before one replacement."""

    pair_id = f"{experiment_id}--{task.task_id}"
    if (run_root / pair_id / "pair.result.json").exists() or (
        run_root / pair_id / "artifact.study.result.json"
    ).exists():
        raise ValueError("sealed pair cannot be recovered")
    claims_root = ledger_root / pair_id
    claim_paths = {path.stem: path for path in claims_root.glob("*.json")}
    if set(claim_paths) != {"baseline_candidate", "baseline_internal_evaluator"}:
        raise ValueError("baseline evaluator recovery phase inventory differs")
    baseline_claim = json.loads(claim_paths["baseline_candidate"].read_text(encoding="utf-8"))
    evaluator_claim = json.loads(
        claim_paths["baseline_internal_evaluator"].read_text(encoding="utf-8")
    )
    baseline_receipt = baseline_claim.get("receipt")
    evaluator_authority = evaluator_claim.get("authority")
    if (
        baseline_claim.get("phase") != "baseline_candidate"
        or baseline_claim.get("status") != "terminal"
        or evaluator_claim.get("phase") != "baseline_internal_evaluator"
        or evaluator_claim.get("status") != "claimed"
        or not isinstance(baseline_receipt, dict)
        or not isinstance(evaluator_authority, dict)
    ):
        raise ValueError("baseline evaluator recovery claim states differ")
    baseline_run_id = baseline_receipt.get("run_id")
    if (
        not isinstance(baseline_run_id, str)
        or evaluator_authority.get("task_id") != task.task_id
        or evaluator_authority.get("run_id") != baseline_run_id
        or evaluator_authority.get("evaluator_id") != evaluator_id
        or evaluator_authority.get("paper_evaluator") is not False
    ):
        raise ValueError("baseline evaluator recovery authority differs")

    baseline_completion = _load_successful_baseline(
        baseline_completion_path, expected_run_id=baseline_run_id
    )
    tool_receipts = _reconstruct_tool_receipts(
        baseline_completion, pair_cache_root=cache_root / pair_id
    )
    baseline = _normalize_rollout(
        {"results": [baseline_completion]},
        run_id=baseline_run_id,
        task_id=task.task_id,
        role="baseline",
        artifact_ids=[],
        config_sha256=s0_config_hash(candidate_config),
        wall_time=_completion_wall_time(baseline_completion),
        tool_receipts=tool_receipts,
        declared_tool_bridge=True,
        polling_retries=0,
    )
    if baseline.status != "COMPLETED" or not baseline.answer.strip():
        raise ValueError("reconstructed baseline Candidate is incomplete")

    failure = json.loads(evaluator_completion_path.read_text(encoding="utf-8"))
    evaluator_run_id = failure.get("task_id")
    if not isinstance(evaluator_run_id, str) or re.fullmatch(
        rf"{re.escape(baseline_run_id)}-evolution-eval-baseline-[0-9a-f]{{10}}",
        evaluator_run_id,
    ) is None:
        raise ValueError("failed evaluator Core task identity differs")
    trajectory = failure.get("trajectory")
    metadata = failure.get("metadata")
    if not isinstance(trajectory, dict) or not isinstance(metadata, dict):
        raise TypeError("failed evaluator Core completion evidence is incomplete")
    trajectory_metadata = trajectory.get("metadata")
    openevo_metadata = metadata.get("openevo")
    credential_contract = (
        openevo_metadata.get("credential_isolation")
        if isinstance(openevo_metadata, dict)
        else None
    )
    no_effect_predicates = {
        "core_status_error": failure.get("status") == "ERROR",
        "exact_evaluator_setup_error": failure.get("error") == _RECOVERABLE_ERROR,
        "trajectory_has_zero_records": isinstance(trajectory_metadata, dict)
        and trajectory_metadata.get("record_count") == 0,
        "trajectory_has_zero_traces": trajectory.get("traces") == [],
        "workspace_result_absent": failure.get("workspace_result") is None,
        "core_route_bound": metadata.get("execution_route") == CORE_MANAGED_CODEX_ROUTE,
        "host_codex_exec_forbidden": metadata.get("host_codex_exec_forbidden") is True,
        "evaluator_artifact_inventory_empty": metadata.get("evolution", {}).get(
            "context_artifact_ids"
        )
        == [],
        "credential_canary_receipt_not_published": isinstance(credential_contract, dict)
        and "status" not in credential_contract,
    }
    if set(no_effect_predicates.values()) != {True}:
        failed_keys = sorted(
            key for key, value in no_effect_predicates.items() if not value
        )
        raise ValueError(
            "baseline evaluator no-effect proof failed: " + ", ".join(failed_keys)
        )
    session_id = failure.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("failed evaluator Core session identity is absent")

    recovery_root = run_root / "recovery" / pair_id
    recovery_root.mkdir(parents=True, exist_ok=True)
    checkpoint = {
        "schema_version": "chemcrow_baseline_before_evaluator_checkpoint_v1",
        "status": "READY_FOR_EVALUATOR_REPLACEMENT",
        "pair_id": pair_id,
        "task_id": task.task_id,
        "baseline": baseline.model_dump(mode="json"),
    }
    checkpoint_path = recovery_root / _CHECKPOINT_NAME
    checkpoint_serialized = json.dumps(checkpoint, indent=2, sort_keys=True) + "\n"
    if checkpoint_path.exists() and checkpoint_path.read_text(encoding="utf-8") != checkpoint_serialized:
        raise ValueError("existing baseline-before-evaluator checkpoint differs")
    checkpoint_path.write_text(checkpoint_serialized, encoding="utf-8")

    repo_root = repository_root or Path(__file__).resolve().parents[4]
    source_paths = {
        "baseline_evaluator_recovery": Path(__file__),
        "runtime_normalizer": repo_root
        / "benchmarks/chemcrow/src/openevo_chemcrow/runtime.py",
    }
    receipt = {
        "schema_version": "chemcrow_baseline_evaluator_no_effect_recovery_v1",
        "status": "VERIFIED_NO_CANDIDATE_EFFECT_REPLACEMENT_READY",
        "pair_id": pair_id,
        "task_id": task.task_id,
        "phase": "baseline_internal_evaluator",
        "attempt_ordinal": 1,
        "authority_sha256": evaluator_claim["authority_sha256"],
        "original_claim_sha256": file_sha256(
            claim_paths["baseline_internal_evaluator"]
        ),
        "config_sha256": file_sha256(config_path),
        "checkpoint_sha256": file_sha256(checkpoint_path),
        "baseline_core_completion_sha256": file_sha256(baseline_completion_path),
        "baseline_core_session_id_sha256": text_sha256(
            str(baseline_completion["session_id"])
        ),
        "baseline_original_trajectory_sha256": baseline_receipt.get(
            "trajectory_sha256"
        ),
        "baseline_reconstructed_trajectory_sha256": canonical_sha256(
            baseline.model_dump(mode="json")
        ),
        "baseline_answer_preserved": True,
        "baseline_tool_receipt_count": len(tool_receipts),
        "core_task_id": evaluator_run_id,
        "core_session_id_sha256": text_sha256(session_id),
        "core_completion_sha256": file_sha256(evaluator_completion_path),
        "core_completion_status": "ERROR",
        "error_category": "credential_isolation_validation_failed",
        "no_effect_predicates": no_effect_predicates,
        "infrastructure_canary_model_call_may_have_occurred": True,
        "evaluator_model_call_proven_absent": True,
        "candidate_redispatched": False,
        "duplicate_scientific_call": False,
        "source_authority_sha256": {
            name: file_sha256(path) for name, path in sorted(source_paths.items())
        },
        "recorded_before_replacement_dispatch": True,
    }
    receipt_path = recovery_root / _RECEIPT_NAME
    receipt_serialized = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if receipt_path.exists() and receipt_path.read_text(encoding="utf-8") != receipt_serialized:
        raise ValueError("existing baseline evaluator no-effect receipt differs")
    receipt_path.write_text(receipt_serialized, encoding="utf-8")
    VerifiedReplacementPhaseLedger(
        ledger_root, pair_id=pair_id
    ).reconcile_verified_no_effect_failure("baseline_internal_evaluator", receipt)
    return checkpoint_path, receipt_path, baseline, receipt


def load_baseline_before_evaluator_checkpoint(
    *,
    run_root: Path,
    ledger_root: Path,
    pair_id: str,
    task: TaskItem,
    expected_candidate_config_sha256: str,
    expected_evaluator_id: str,
) -> Trajectory | None:
    recovery_root = run_root / "recovery" / pair_id
    checkpoint_path = recovery_root / _CHECKPOINT_NAME
    receipt_path = recovery_root / _RECEIPT_NAME
    if not checkpoint_path.exists() and not receipt_path.exists():
        return None
    if not checkpoint_path.is_file() or not receipt_path.is_file():
        raise ValueError("baseline evaluator recovery evidence is incomplete")
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if (
        checkpoint.get("schema_version")
        != "chemcrow_baseline_before_evaluator_checkpoint_v1"
        or checkpoint.get("status") != "READY_FOR_EVALUATOR_REPLACEMENT"
        or checkpoint.get("pair_id") != pair_id
        or checkpoint.get("task_id") != task.task_id
        or receipt.get("schema_version")
        != "chemcrow_baseline_evaluator_no_effect_recovery_v1"
        or receipt.get("checkpoint_sha256") != file_sha256(checkpoint_path)
        or receipt.get("candidate_redispatched") is not False
        or receipt.get("evaluator_model_call_proven_absent") is not True
    ):
        raise ValueError("baseline evaluator recovery authority differs")
    claims = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in (ledger_root / pair_id).glob("*.json")
    }
    if (
        set(claims) != {"baseline_candidate", "baseline_internal_evaluator"}
        or claims["baseline_candidate"].get("status") != "terminal"
        or claims["baseline_internal_evaluator"].get("status")
        != "replacement_ready"
        or claims["baseline_internal_evaluator"].get("authority", {}).get(
            "evaluator_id"
        )
        != expected_evaluator_id
    ):
        raise ValueError("baseline evaluator recovery claim inventory differs")
    baseline = Trajectory.model_validate(checkpoint.get("baseline"))
    if (
        baseline.task_id != task.task_id
        or baseline.role != "baseline"
        or baseline.status != "COMPLETED"
        or not baseline.answer.strip()
        or baseline.artifact_ids
        or baseline.candidate_config_sha256 != expected_candidate_config_sha256
        or canonical_sha256(baseline.model_dump(mode="json"))
        != receipt.get("baseline_reconstructed_trajectory_sha256")
    ):
        raise ValueError("baseline-before-evaluator checkpoint semantics differ")
    return baseline


__all__ = [
    "load_baseline_before_evaluator_checkpoint",
    "reconcile_baseline_evaluator_no_effect_failure",
]
