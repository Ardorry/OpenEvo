from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .hashing import canonical_sha256, file_sha256
from .models import ArtifactKind
from .replacement_ledger import verified_no_effect_attempt_count
from .runtime import CORE_MANAGED_CODEX_ROUTE
from .three_artifact_models import THREE_ARTIFACT_ORDER, ThreeArtifactPairResult

_PHASES = {
    "baseline_candidate",
    "baseline_internal_evaluator",
    "reflector_memory",
    "reflector_skill_bundle",
    "reflector_agent_system",
    "evolved_candidate",
    "evolved_internal_evaluator",
    "final_evaluator",
}


def audit_three_artifact_run(
    *,
    run_root: Path,
    core_completion_root: Path,
    experiment_id: str,
    task_ids: list[str],
    expected_s0_hash: str,
    expected_reflector_model: str,
    require_aggregate: bool = True,
) -> dict[str, Any]:
    if not task_ids or len(task_ids) != len(set(task_ids)):
        raise ValueError("audit task inventory must be non-empty and unique")
    seen_artifact_ids: set[str] = set()
    source_counts: dict[str, int] = {}
    artifact_type_counts: dict[str, int] = {}
    sibling_isolation_evidence_count = 0
    reset_receipt_count = 0
    tool_calls = 0
    tool_errors = 0
    verified_pre_candidate_no_effect_attempts = 0
    for task_id in task_ids:
        pair_id = f"{experiment_id}--{task_id}"
        item_root = run_root / pair_id
        result = ThreeArtifactPairResult.model_validate_json(
            (item_root / "pair.result.json").read_text(encoding="utf-8")
        )
        if (
            result.task_id != task_id
            or result.pair_id != pair_id
            or result.s0_hash != expected_s0_hash
        ):
            raise ValueError(f"task/pair/S0 authority mismatch: {pair_id}")
        artifacts = result.artifact_bundle.artifacts
        if [item.artifact_type for item in artifacts] != list(THREE_ARTIFACT_ORDER):
            raise ValueError(f"typed artifact inventory differs: {pair_id}")
        ids = [item.artifact_id for item in artifacts]
        for item in artifacts:
            artifact_type_counts[item.artifact_type.value] = (
                artifact_type_counts.get(item.artifact_type.value, 0) + 1
            )
        if seen_artifact_ids.intersection(ids):
            raise ValueError(f"cross-task artifact reuse: {pair_id}")
        seen_artifact_ids.update(ids)
        if {item.model for item in artifacts} != {expected_reflector_model}:
            raise ValueError(f"Reflector model differs: {pair_id}")
        if len({item.reflector_job_id for item in artifacts}) != 3:
            raise ValueError(f"Reflector jobs are not independent: {pair_id}")
        if len({item.reflector_run_id for item in artifacts}) != 3:
            raise ValueError(f"Reflector invocations are not independent: {pair_id}")
        if len({item.prompt_hash for item in artifacts}) != 3:
            raise ValueError(f"Reflector prompt hashes are not independent: {pair_id}")
        _require_equal_json(item_root / "baseline.trajectory.json", result.baseline)
        _require_equal_json(item_root / "evolved.trajectory.json", result.evolved)
        _require_equal_json(item_root / "artifacts.receipt.json", result.artifact_bundle)
        _require_equal_json(item_root / "injection.receipt.summary.json", result.injection_receipt)
        feedback = json.loads((item_root / "feedback.json").read_text(encoding="utf-8"))
        if canonical_sha256(feedback) != result.feedback_hash:
            raise ValueError(f"feedback hash mismatch: {pair_id}")
        serialized_feedback = json.dumps(feedback, sort_keys=True)
        if result.evolved.run_id in serialized_feedback:
            raise ValueError(f"G2 evidence leaked backward: {pair_id}")
        if any(
            token in serialized_feedback.casefold()
            for token in (
                "paper_evaluator_feedback",
                "historical_chemcrow",
                "historical_gpt4",
                "human_expert",
                "reference_answer",
            )
        ):
            raise ValueError(f"forbidden scoring evidence entered Reflector input: {pair_id}")
        reset_path = item_root / "reset.receipt.json"
        if file_sha256(reset_path) != result.reset_receipt_sha256:
            raise ValueError(f"reset receipt hash mismatch: {pair_id}")
        reset = json.loads(reset_path.read_text(encoding="utf-8"))
        if (
            reset.get("discarded_task_local_artifact_ids")
            != result.artifact_bundle.artifact_id_by_type()
            or reset.get("s0_hash_for_next_item") != expected_s0_hash
            or reset.get("runtime_context_after") != "bare_s0"
            or any(
                reset.get(key) != []
                for key in (
                    "active_artifact_ids_after",
                    "artifact_inventory_after",
                    "prior_artifact_ids_exported",
                    "memory_after",
                    "skill_bundle_after",
                    "agent_system_after",
                )
            )
        ):
            raise ValueError(f"three-artifact reset is incomplete: {pair_id}")
        reset_receipt_count += 1

        claims_root = run_root / "claims" / pair_id
        claims = {path.stem: path for path in claims_root.glob("*.json")}
        if set(claims) != _PHASES:
            raise ValueError(f"phase claim inventory mismatch: {pair_id}")
        evaluator_ids: dict[str, str] = {}
        for phase, path in claims.items():
            claim = json.loads(path.read_text(encoding="utf-8"))
            if claim.get("status") != "terminal" or claim.get("phase") != phase:
                raise ValueError(f"non-terminal phase claim: {pair_id}/{phase}")
            if canonical_sha256(claim.get("authority")) != claim.get("authority_sha256"):
                raise ValueError(f"phase authority hash mismatch: {pair_id}/{phase}")
            if canonical_sha256(claim.get("receipt")) != claim.get("receipt_sha256"):
                raise ValueError(f"phase receipt hash mismatch: {pair_id}/{phase}")
            no_effect_attempts = verified_no_effect_attempt_count(
                claim,
                pair_id=pair_id,
                phase=phase,
            )
            if no_effect_attempts and phase != "baseline_candidate":
                raise ValueError(f"no-effect replacement attached to wrong phase: {pair_id}/{phase}")
            verified_pre_candidate_no_effect_attempts += no_effect_attempts
            authority = claim.get("authority")
            if not isinstance(authority, dict) or authority.get("task_id") != task_id:
                raise ValueError(f"phase task authority mismatch: {pair_id}/{phase}")
            if phase in {"baseline_internal_evaluator", "evolved_internal_evaluator"}:
                evaluator_ids[phase] = str(authority.get("evaluator_id"))
            if phase == "final_evaluator":
                evaluator_ids[phase] = str(authority.get("evaluator_id"))
            if phase.startswith("reflector_"):
                if authority.get("paper_evaluator_feedback_included") is not False:
                    raise ValueError(f"Reflector leakage receipt missing: {pair_id}/{phase}")
                if authority.get("sibling_artifact_ids") != []:
                    raise ValueError(f"Reflector saw sibling artifact IDs: {pair_id}/{phase}")
                sibling_isolation_evidence_count += 1
        if (
            evaluator_ids["baseline_internal_evaluator"]
            != evaluator_ids["evolved_internal_evaluator"]
        ):
            raise ValueError(f"G1/G2 internal evaluator config differs: {pair_id}")
        if evaluator_ids["baseline_internal_evaluator"] == evaluator_ids["final_evaluator"]:
            raise ValueError(f"internal/final evaluator separation failed: {pair_id}")

        baseline_receipt_hash = _audit_core_completion(
            core_completion_root=core_completion_root,
            run_id=result.baseline.run_id,
            expected_by_type={},
        )
        if baseline_receipt_hash is not None:
            raise ValueError(f"baseline unexpectedly returned an injection receipt: {pair_id}")
        evolved_receipt_hash = _audit_core_completion(
            core_completion_root=core_completion_root,
            run_id=result.evolved.run_id,
            expected_by_type={item.artifact_type.value: item.artifact_id for item in artifacts},
        )
        if evolved_receipt_hash != result.injection_receipt.receipt_sha256:
            raise ValueError(f"Core injection receipt hash differs from pair seal: {pair_id}")
        for run in (result.baseline, result.evolved):
            if len(run.tool_calls) != len(run.observations):
                raise ValueError(f"tool call/observation mismatch: {run.run_id}")
            tool_calls += len(run.tool_calls)
            for observation in run.observations:
                source = str(observation.source)
                if source in {"fixture", "mock"}:
                    raise ValueError(f"non-real observation entered metrics: {run.run_id}")
                source_counts[source] = source_counts.get(source, 0) + 1
                tool_errors += int(observation.error is not None)

    aggregate_sha256: str | None = None
    if require_aggregate:
        aggregate_path = run_root / "aggregate.json"
        aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
        if (
            aggregate.get("schema_version") != "chemcrow_three_artifact_aggregate_v1"
            or aggregate.get("task_count") != len(task_ids)
            or aggregate.get("artifact_count") != len(task_ids) * 3
            or aggregate.get("status") != "PROVISIONAL_LLM_JUDGED_RESULT"
        ):
            raise ValueError("three-artifact aggregate authority differs")
        aggregate_sha256 = file_sha256(aggregate_path)
    return {
        "schema_version": "chemcrow_three_artifact_completed_run_audit_v1",
        "status": "PASS",
        "artifact_protocol": "chemcrow-three-isolated-artifacts-v1",
        "experiment_id": experiment_id,
        "task_ids": task_ids,
        "task_count": len(task_ids),
        "s0_config_sha256": expected_s0_hash,
        "unique_artifact_count": len(seen_artifact_ids),
        "independent_reflector_job_count": len(seen_artifact_ids),
        "artifact_type_counts": dict(sorted(artifact_type_counts.items())),
        "memory_reflector_job_count": artifact_type_counts.get(
            ArtifactKind.TEXT_MEMORY.value, 0
        ),
        "skill_reflector_job_count": artifact_type_counts.get(
            ArtifactKind.SKILL_BUNDLE.value, 0
        ),
        "agent_system_reflector_job_count": artifact_type_counts.get(
            ArtifactKind.AGENT_SYSTEM.value, 0
        ),
        "artifact_registration_count": len(seen_artifact_ids),
        "sibling_isolation_evidence_count": sibling_isolation_evidence_count,
        "core_evolved_injection_receipt_count": len(task_ids),
        "reset_receipt_count": reset_receipt_count,
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "observation_source_counts": dict(sorted(source_counts.items())),
        "aggregate_sha256": aggregate_sha256,
        "answers_included": False,
        "mock_or_fixture_observations": 0,
        "verified_pre_candidate_no_effect_attempt_count": (
            verified_pre_candidate_no_effect_attempts
        ),
    }


def _require_equal_json(path: Path, expected: Any) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if canonical_sha256(payload) != canonical_sha256(expected.model_dump(mode="json")):
        raise ValueError(f"sealed JSON differs from pair result: {path}")


def _audit_core_completion(
    *,
    core_completion_root: Path,
    run_id: str,
    expected_by_type: dict[str, str],
) -> str | None:
    matches = list(core_completion_root.glob(f"task_{run_id}/*.json"))
    if len(matches) != 1:
        raise ValueError(f"Core completion authority is not unique: {run_id}")
    payload = json.loads(matches[0].read_text(encoding="utf-8"))
    metadata = payload.get("trajectory", {}).get("metadata", {}).get("task_metadata", {})
    if metadata.get("execution_route") != CORE_MANAGED_CODEX_ROUTE:
        raise ValueError(f"Candidate bypassed mandatory Core route: {run_id}")
    if metadata.get("host_codex_exec_forbidden") is not True:
        raise ValueError(f"host Codex prohibition receipt is missing: {run_id}")
    evolution = metadata.get("evolution")
    if not isinstance(evolution, dict):
        raise TypeError(f"Core evolution metadata is absent: {run_id}")
    receipt = evolution.get("runtime_injection_receipt")
    if not expected_by_type:
        if evolution.get("context_artifact_ids") != [] or receipt is not None:
            raise ValueError(f"baseline Core context was not bare S0: {run_id}")
        return None
    if evolution.get("context_injected") is not True or not isinstance(receipt, dict):
        raise ValueError(f"Core injection receipt is missing: {run_id}")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 3:
        raise ValueError(f"Core did not inject exactly three artifacts: {run_id}")
    observed = {
        str(item.get("artifact_type")): str(item.get("artifact_id"))
        for item in artifacts
        if isinstance(item, dict)
    }
    if observed != expected_by_type or set(observed) != {
        ArtifactKind.TEXT_MEMORY.value,
        ArtifactKind.SKILL_BUNDLE.value,
        ArtifactKind.AGENT_SYSTEM.value,
    }:
        raise ValueError(f"Core artifact type mapping differs: {run_id}")
    return canonical_sha256(receipt)
