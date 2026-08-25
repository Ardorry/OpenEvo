from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .hashing import canonical_sha256, file_sha256
from .models import PairResult, Trajectory
from .runtime import CORE_MANAGED_CODEX_ROUTE

_PHASES = {
    "baseline_candidate",
    "evolution_evaluator",
    "reflector",
    "evolved_candidate",
    "final_evaluator",
}


def audit_completed_run(
    *,
    run_root: Path,
    core_completion_root: Path,
    experiment_id: str,
    task_ids: list[str],
    expected_s0_hash: str,
) -> dict[str, Any]:
    if not task_ids or len(task_ids) != len(set(task_ids)):
        raise ValueError("audit task inventory must be non-empty and unique")
    artifacts: set[str] = set()
    source_counts: dict[str, int] = {}
    tool_calls = 0
    tool_errors = 0
    phase_claims = 0
    injection_receipts = 0
    for task_id in task_ids:
        pair_id = f"{experiment_id}--{task_id}"
        item_root = run_root / pair_id
        result_path = item_root / "pair.result.json"
        result = PairResult.model_validate_json(result_path.read_text(encoding="utf-8"))
        if result.task_id != task_id or result.s0_hash != expected_s0_hash:
            raise ValueError(f"task/S0 authority mismatch: {pair_id}")
        if result.baseline.status != "COMPLETED" or result.evolved.status != "COMPLETED":
            raise ValueError(f"non-completed Candidate trajectory: {pair_id}")
        if result.artifact.artifact_id in artifacts:
            raise ValueError(f"cross-task artifact reuse: {pair_id}")
        artifacts.add(result.artifact.artifact_id)
        _require_equal_json(item_root / "baseline.trajectory.json", result.baseline)
        _require_equal_json(item_root / "evolved.trajectory.json", result.evolved)
        artifact_payload = json.loads(
            (item_root / "artifact.receipt.json").read_text(encoding="utf-8")
        )
        if canonical_sha256(artifact_payload) != canonical_sha256(
            result.artifact.model_dump(mode="json")
        ):
            raise ValueError(f"artifact receipt differs from pair result: {pair_id}")
        feedback_payload = json.loads((item_root / "feedback.json").read_text(encoding="utf-8"))
        if canonical_sha256(feedback_payload) != result.feedback_hash:
            raise ValueError(f"feedback hash mismatch: {pair_id}")
        if result.evolved.run_id in json.dumps(feedback_payload, sort_keys=True):
            raise ValueError(f"evolved answer leaked backward into feedback: {pair_id}")
        reset_path = item_root / "reset.receipt.json"
        if file_sha256(reset_path) != result.reset_receipt_sha256:
            raise ValueError(f"reset receipt hash mismatch: {pair_id}")
        reset = json.loads(reset_path.read_text(encoding="utf-8"))
        if (
            reset.get("discarded_artifact_id") != result.artifact.artifact_id
            or reset.get("s0_hash_for_next_item") != expected_s0_hash
            or any(
                reset.get(key) != []
                for key in (
                    "active_artifact_ids_after",
                    "prior_artifact_ids_exported",
                    "memory_after",
                    "skill_bundle_after",
                    "agent_system_after",
                )
            )
        ):
            raise ValueError(f"task-local reset is incomplete: {pair_id}")
        claims_root = run_root / "claims" / pair_id
        claims = {path.stem: path for path in claims_root.glob("*.json")}
        if set(claims) != _PHASES:
            raise ValueError(f"phase claim inventory mismatch: {pair_id}")
        phase_claims += len(claims)
        evaluator_ids: dict[str, str] = {}
        for phase, path in claims.items():
            claim = json.loads(path.read_text(encoding="utf-8"))
            if claim.get("status") != "terminal" or claim.get("phase") != phase:
                raise ValueError(f"non-terminal phase claim: {pair_id}/{phase}")
            if canonical_sha256(claim.get("authority")) != claim.get("authority_sha256"):
                raise ValueError(f"phase authority hash mismatch: {pair_id}/{phase}")
            if canonical_sha256(claim.get("receipt")) != claim.get("receipt_sha256"):
                raise ValueError(f"phase receipt hash mismatch: {pair_id}/{phase}")
            authority = claim.get("authority")
            if not isinstance(authority, dict) or authority.get("task_id") != task_id:
                raise ValueError(f"phase task authority mismatch: {pair_id}/{phase}")
            if phase in {"evolution_evaluator", "final_evaluator"}:
                evaluator_ids[phase] = str(authority.get("evaluator_id"))
        if evaluator_ids["evolution_evaluator"] == evaluator_ids["final_evaluator"]:
            raise ValueError(f"evaluator separation failed: {pair_id}")
        for role, trajectory in (("baseline", result.baseline), ("evolved", result.evolved)):
            receipt = _core_injection_receipt(
                core_completion_root=core_completion_root,
                trajectory=trajectory,
                role=role,
                expected_artifact_id=result.artifact.artifact_id,
            )
            if role == "evolved":
                injection_receipts += 1
                if receipt.get("schema_version") != "3":
                    raise ValueError(f"unexpected Core injection receipt schema: {pair_id}")
            if len(trajectory.tool_calls) != len(trajectory.observations):
                raise ValueError(f"tool call/observation mismatch: {pair_id}/{role}")
            tool_calls += len(trajectory.tool_calls)
            for observation in trajectory.observations:
                source = str(observation.source)
                if source in {"fixture", "mock"}:
                    raise ValueError(f"non-real observation entered metrics: {pair_id}/{role}")
                source_counts[source] = source_counts.get(source, 0) + 1
                tool_errors += int(observation.error is not None)
    aggregate_path = run_root / "aggregate.json"
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    if aggregate.get("task_count") != len(task_ids):
        raise ValueError("aggregate task count differs from frozen task inventory")
    if aggregate.get("status") != "PROVISIONAL_LLM_JUDGED_RESULT":
        raise ValueError("aggregate result is not labeled provisional")
    return {
        "schema_version": "chemcrow_completed_run_audit_v1",
        "status": "PASS",
        "experiment_id": experiment_id,
        "task_ids": task_ids,
        "task_count": len(task_ids),
        "s0_config_sha256": expected_s0_hash,
        "unique_artifact_count": len(artifacts),
        "terminal_phase_claim_count": phase_claims,
        "core_evolved_injection_receipt_count": injection_receipts,
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "observation_source_counts": dict(sorted(source_counts.items())),
        "aggregate_sha256": file_sha256(aggregate_path),
        "answers_included": False,
        "mock_or_fixture_observations": 0,
    }


def _require_equal_json(path: Path, expected: Trajectory) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if canonical_sha256(payload) != canonical_sha256(expected.model_dump(mode="json")):
        raise ValueError(f"sealed trajectory differs from pair result: {path}")


def _core_injection_receipt(
    *,
    core_completion_root: Path,
    trajectory: Trajectory,
    role: str,
    expected_artifact_id: str,
) -> dict[str, Any]:
    matches = list(core_completion_root.glob(f"task_{trajectory.run_id}/*.json"))
    if len(matches) != 1:
        raise ValueError(f"Core completion authority is not unique: {trajectory.run_id}")
    payload = json.loads(matches[0].read_text(encoding="utf-8"))
    metadata = payload.get("trajectory", {}).get("metadata", {}).get("task_metadata", {})
    if metadata.get("execution_route") != CORE_MANAGED_CODEX_ROUTE:
        raise ValueError(f"Candidate bypassed the mandatory Core route: {trajectory.run_id}")
    if metadata.get("host_codex_exec_forbidden") is not True:
        raise ValueError(f"host Codex prohibition receipt is missing: {trajectory.run_id}")
    evolution = metadata.get("evolution")
    if not isinstance(evolution, dict) or evolution.get("context_injected") is not True:
        raise ValueError(f"Core context injection receipt is missing: {trajectory.run_id}")
    receipt = evolution.get("runtime_injection_receipt")
    if role == "baseline" and receipt is None:
        if evolution.get("context_artifact_ids") != []:
            raise ValueError(f"baseline Core context was not exactly empty: {trajectory.run_id}")
        return {}
    if not isinstance(receipt, dict):
        raise TypeError(f"Core runtime readback receipt is missing: {trajectory.run_id}")
    artifact_ids = [
        item.get("artifact_id")
        for item in receipt.get("artifacts", [])
        if isinstance(item, dict)
    ]
    expected = [] if role == "baseline" else [expected_artifact_id]
    if artifact_ids != expected:
        raise ValueError(f"Core injected the wrong artifact inventory: {trajectory.run_id}")
    transcript = payload.get("trajectory", {}).get("traces", [])
    for trace in transcript:
        raw = trace.get("metadata", {}).get("transcript") if isinstance(trace, dict) else None
        if not isinstance(raw, str):
            continue
        for line in raw.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item") if isinstance(event, dict) else None
            if isinstance(item, dict) and item.get("type") == "web_search":
                raise ValueError(f"undeclared built-in web search entered run: {trajectory.run_id}")
    return receipt
