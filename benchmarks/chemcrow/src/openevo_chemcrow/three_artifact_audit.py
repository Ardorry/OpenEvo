from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .hashing import canonical_sha256, file_sha256
from .models import ArtifactKind
from .replacement_ledger import verified_no_effect_attempt_count
from .runtime import CORE_MANAGED_CODEX_ROUTE
from .three_artifact_models import (
    THREE_ARTIFACT_ORDER,
    ThreeArtifactGenerationResult,
    ThreeArtifactPairResult,
    three_artifact_protocol_label,
)

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
_REFLECTOR_PHASES = {
    "reflector_memory",
    "reflector_skill_bundle",
    "reflector_agent_system",
}
_GENERATION_PHASES = {
    "baseline_candidate",
    "baseline_internal_evaluator",
    *_REFLECTOR_PHASES,
}
_EVOLVED_AUDIT_AUTHORITY_KEYS = {
    "task_id",
    "replacement_run_id",
    "input_evidence_sha256",
    "artifact_ids_by_type",
    "reflector_job_ids",
    "reflector_run_ids",
    "reflector_prompt_hashes",
}


def _record_globally_unique_three(
    seen: set[str],
    observed: list[str],
    *,
    authority_name: str,
    pair_id: str,
) -> None:
    if len(observed) != 3 or len(set(observed)) != 3:
        raise ValueError(f"{authority_name} inventory is not three distinct IDs: {pair_id}")
    reused = seen.intersection(observed)
    if reused:
        raise ValueError(f"cross-task {authority_name} reuse: {pair_id}")
    seen.update(observed)


def _audit_verified_no_effect_attempts(
    *,
    claim: dict[str, Any],
    pair_id: str,
    phase: str,
    expected_task_id: str,
    expected_evolved_authority: dict[str, Any],
) -> tuple[int, int, int, int]:
    """Return strict G1, G1-evaluator, Reflector, and G2 attempt counts."""

    if (
        claim.get("schema_version") not in {"chemcrow_phase_claim_v1", "chemcrow_phase_claim_v2"}
        or claim.get("status") != "terminal"
        or claim.get("phase") != phase
        or canonical_sha256(claim.get("authority")) != claim.get("authority_sha256")
    ):
        raise ValueError(f"no-effect phase claim authority differs: {pair_id}/{phase}")
    count = verified_no_effect_attempt_count(
        claim,
        pair_id=pair_id,
        phase=phase,
    )
    if not count:
        return 0, 0, 0, 0
    if phase == "baseline_candidate":
        return count, 0, 0, 0
    if phase == "baseline_internal_evaluator":
        return 0, count, 0, 0
    if phase in _REFLECTOR_PHASES:
        return 0, 0, count, 0
    if phase != "evolved_candidate":
        raise ValueError(f"no-effect replacement attached to wrong phase: {pair_id}/{phase}")
    if set(expected_evolved_authority) != _EVOLVED_AUDIT_AUTHORITY_KEYS:
        raise TypeError("evolved no-effect audit authority schema differs")

    attempts = claim.get("failed_attempts")
    if not isinstance(attempts, list) or len(attempts) != count:
        raise ValueError(f"evolved no-effect attempt inventory differs: {pair_id}")
    receipts: list[dict[str, Any]] = []
    for attempt in attempts:
        receipt = attempt.get("receipt") if isinstance(attempt, dict) else None
        if (
            not isinstance(receipt, dict)
            or receipt.get("schema_version") != "chemcrow_evolved_candidate_no_effect_recovery_v1"
            or receipt.get("phase") != "evolved_candidate"
            or receipt.get("task_id") != expected_task_id
            or len(receipt.get("no_effect_predicates", {})) != 15
            or set(receipt.get("no_effect_predicates", {}).values()) != {True}
        ):
            raise ValueError(f"evolved no-effect receipt content differs: {pair_id}")
        observed_authority = {
            "task_id": receipt.get("task_id"),
            "input_evidence_sha256": receipt.get("input_evidence_sha256"),
            "artifact_ids_by_type": receipt.get("artifact_ids_by_type"),
            "reflector_job_ids": receipt.get("reflector_job_ids"),
            "reflector_run_ids": receipt.get("reflector_run_ids"),
            "reflector_prompt_hashes": receipt.get("reflector_prompt_hashes"),
        }
        if any(
            observed_authority[key] != expected_evolved_authority[key]
            for key in observed_authority
        ):
            raise ValueError(f"evolved no-effect receipt scientific authority differs: {pair_id}")
        receipts.append(receipt)
    if (
        receipts[-1].get("replacement_run_id") != expected_evolved_authority["replacement_run_id"]
        or claim.get("active_attempt_run_id") != expected_evolved_authority["replacement_run_id"]
    ):
        raise ValueError(f"evolved no-effect replacement run authority differs: {pair_id}")
    return 0, 0, 0, count


def audit_three_artifact_run(
    *,
    run_root: Path,
    core_completion_root: Path,
    experiment_id: str,
    task_ids: list[str],
    expected_s0_hash: str,
    expected_reflector_model: str,
    require_aggregate: bool = True,
    source_generation_run_root: Path | None = None,
) -> dict[str, Any]:
    if not task_ids or len(task_ids) != len(set(task_ids)):
        raise ValueError("audit task inventory must be non-empty and unique")
    seen_artifact_ids: set[str] = set()
    seen_reflector_job_ids: set[str] = set()
    seen_reflector_run_ids: set[str] = set()
    source_counts: dict[str, int] = {}
    artifact_type_counts: dict[str, int] = {}
    sibling_isolation_evidence_count = 0
    reset_receipt_count = 0
    tool_calls = 0
    tool_errors = 0
    verified_pre_candidate_no_effect_attempts = 0
    verified_baseline_evaluator_no_effect_attempts = 0
    verified_reflector_no_effect_attempts = 0
    verified_evolved_candidate_no_effect_attempts = 0
    completed_call_checkpoint_count = 0
    confidence_transport_adaptation_count = 0
    bundle_protocols: set[str] = set()
    for task_id in task_ids:
        pair_id = f"{experiment_id}--{task_id}"
        item_root = run_root / pair_id
        result = ThreeArtifactPairResult.model_validate_json(
            (item_root / "pair.result.json").read_text(encoding="utf-8")
        )
        bundle_protocols.add(result.artifact_bundle.protocol)
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
        reflector_job_ids = [item.reflector_job_id for item in artifacts]
        reflector_run_ids = [item.reflector_run_id for item in artifacts]
        for item in artifacts:
            artifact_type_counts[item.artifact_type.value] = (
                artifact_type_counts.get(item.artifact_type.value, 0) + 1
            )
        _record_globally_unique_three(
            seen_artifact_ids,
            ids,
            authority_name="artifact ID",
            pair_id=pair_id,
        )
        _record_globally_unique_three(
            seen_reflector_job_ids,
            reflector_job_ids,
            authority_name="Reflector job ID",
            pair_id=pair_id,
        )
        _record_globally_unique_three(
            seen_reflector_run_ids,
            reflector_run_ids,
            authority_name="Reflector run ID",
            pair_id=pair_id,
        )
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
        if source_generation_run_root is None:
            if set(claims) != _PHASES:
                raise ValueError(f"phase claim inventory mismatch: {pair_id}")
        else:
            expected_continuation_phases = _PHASES - _GENERATION_PHASES
            if set(claims) != expected_continuation_phases:
                raise ValueError(
                    f"continuation phase claim inventory mismatch: {pair_id}"
                )
            source_claim_root = source_generation_run_root / "claims" / pair_id
            source_claims = {
                path.stem: path for path in source_claim_root.glob("*.json")
            }
            if set(source_claims) != _GENERATION_PHASES:
                raise ValueError(f"source generation claim inventory mismatch: {pair_id}")
            claims = {**source_claims, **claims}
            continuation_receipt = json.loads(
                (item_root / "continuation.source.receipt.json").read_text(
                    encoding="utf-8"
                )
            )
            if (
                continuation_receipt.get("schema_version")
                != "chemcrow_sealed_generation_g2_continuation_v1"
                or continuation_receipt.get("status") != "READY_FOR_FRESH_G2"
                or continuation_receipt.get("model_calls") != 0
                or continuation_receipt.get("task_id") != task_id
                or continuation_receipt.get("pair_id") != pair_id
                or continuation_receipt.get("baseline_run_id") != result.baseline.run_id
                or continuation_receipt.get("artifact_authority_by_type") is None
                or continuation_receipt.get("prior_g2_candidate_calls") != 0
                or continuation_receipt.get("prior_g2_internal_evaluator_calls") != 0
                or continuation_receipt.get("prior_final_evaluator_calls") != 0
                or continuation_receipt.get("source_files_mutated") is not False
            ):
                raise ValueError(f"continuation source receipt differs: {pair_id}")
        evaluator_ids: dict[str, str] = {}
        for phase, path in claims.items():
            claim = json.loads(path.read_text(encoding="utf-8"))
            if claim.get("status") != "terminal" or claim.get("phase") != phase:
                raise ValueError(f"non-terminal phase claim: {pair_id}/{phase}")
            if canonical_sha256(claim.get("authority")) != claim.get("authority_sha256"):
                raise ValueError(f"phase authority hash mismatch: {pair_id}/{phase}")
            if canonical_sha256(claim.get("receipt")) != claim.get("receipt_sha256"):
                raise ValueError(f"phase receipt hash mismatch: {pair_id}/{phase}")
            (
                pre_candidate_no_effect_attempts,
                baseline_evaluator_no_effect_attempts,
                reflector_no_effect_attempts,
                evolved_candidate_no_effect_attempts,
            ) = _audit_verified_no_effect_attempts(
                claim=claim,
                pair_id=pair_id,
                phase=phase,
                expected_task_id=task_id,
                expected_evolved_authority={
                    "task_id": task_id,
                    "replacement_run_id": result.evolved.run_id,
                    "input_evidence_sha256": result.artifact_bundle.input_evidence_hash,
                    "artifact_ids_by_type": result.artifact_bundle.artifact_id_by_type(),
                    "reflector_job_ids": reflector_job_ids,
                    "reflector_run_ids": reflector_run_ids,
                    "reflector_prompt_hashes": [item.prompt_hash for item in artifacts],
                },
            )
            verified_pre_candidate_no_effect_attempts += pre_candidate_no_effect_attempts
            verified_baseline_evaluator_no_effect_attempts += (
                baseline_evaluator_no_effect_attempts
            )
            verified_reflector_no_effect_attempts += reflector_no_effect_attempts
            verified_evolved_candidate_no_effect_attempts += evolved_candidate_no_effect_attempts
            phase_receipt = claim.get("receipt")
            if not isinstance(phase_receipt, dict):
                raise TypeError(f"phase receipt is not an object: {pair_id}/{phase}")
            confidence_receipt = phase_receipt.get("confidence_parse_receipt")
            if confidence_receipt is not None:
                if (
                    not isinstance(confidence_receipt, dict)
                    or confidence_receipt.get("schema_version")
                    != "chemcrow_internal_confidence_parse_receipt_v1"
                    or confidence_receipt.get("policy_id")
                    != "chemcrow_internal_confidence_transport_v1"
                    or confidence_receipt.get("rubric_scores_changed") is not False
                    or confidence_receipt.get("prompt_changed") is not False
                ):
                    raise ValueError(f"confidence parse receipt is invalid: {pair_id}/{phase}")
                confidence_transport_adaptation_count += int(
                    confidence_receipt.get("adapted") is True
                )
            if "completed_call_checkpoint_sha256" in phase_receipt:
                if phase != "baseline_internal_evaluator":
                    raise ValueError(
                        f"completed-call checkpoint attached to wrong phase: {pair_id}/{phase}"
                    )
                completed_call_checkpoint_count += 1
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
    if len(bundle_protocols) != 1:
        raise ValueError("run mixes three-artifact protocol versions")
    artifact_protocol = three_artifact_protocol_label(bundle_protocols.pop())
    if require_aggregate:
        aggregate_path = run_root / "aggregate.json"
        aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
        if (
            aggregate.get("schema_version") != "chemcrow_three_artifact_aggregate_v1"
            or aggregate.get("task_count") != len(task_ids)
            or aggregate.get("artifact_count") != len(task_ids) * 3
            or aggregate.get("status") != "PROVISIONAL_LLM_JUDGED_RESULT"
            or aggregate.get("artifact_protocol") != artifact_protocol
        ):
            raise ValueError("three-artifact aggregate authority differs")
        aggregate_sha256 = file_sha256(aggregate_path)
    return {
        "schema_version": "chemcrow_three_artifact_completed_run_audit_v1",
        "status": "PASS",
        "artifact_protocol": artifact_protocol,
        "experiment_id": experiment_id,
        "task_ids": task_ids,
        "task_count": len(task_ids),
        "s0_config_sha256": expected_s0_hash,
        "unique_artifact_count": len(seen_artifact_ids),
        "unique_reflector_job_count": len(seen_reflector_job_ids),
        "unique_reflector_run_count": len(seen_reflector_run_ids),
        "independent_reflector_job_count": len(seen_reflector_job_ids),
        "independent_reflector_run_count": len(seen_reflector_run_ids),
        "artifact_type_counts": dict(sorted(artifact_type_counts.items())),
        "memory_reflector_job_count": artifact_type_counts.get(ArtifactKind.TEXT_MEMORY.value, 0),
        "skill_reflector_job_count": artifact_type_counts.get(ArtifactKind.SKILL_BUNDLE.value, 0),
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
        "verified_baseline_evaluator_no_effect_attempt_count": (
            verified_baseline_evaluator_no_effect_attempts
        ),
        "verified_reflector_no_effect_attempt_count": (verified_reflector_no_effect_attempts),
        "verified_evolved_candidate_no_effect_attempt_count": (
            verified_evolved_candidate_no_effect_attempts
        ),
        "completed_call_checkpoint_count": completed_call_checkpoint_count,
        "confidence_transport_adaptation_count": (confidence_transport_adaptation_count),
    }


def audit_three_artifact_generation_run(
    *,
    run_root: Path,
    core_completion_root: Path,
    experiment_id: str,
    task_ids: list[str],
    expected_s0_hash: str,
    expected_reflector_model: str,
    require_aggregate: bool = True,
) -> dict[str, Any]:
    """Audit a sealed G1-plus-artifacts study without implying G2 execution."""

    if not task_ids or len(task_ids) != len(set(task_ids)):
        raise ValueError("audit task inventory must be non-empty and unique")
    seen_artifact_ids: set[str] = set()
    seen_reflector_job_ids: set[str] = set()
    seen_reflector_run_ids: set[str] = set()
    artifact_type_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    bundle_protocols: set[str] = set()
    prompt_profiles: set[str] = set()
    sibling_isolation_evidence_count = 0
    reset_receipt_count = 0
    tool_calls = 0
    tool_errors = 0
    verified_pre_candidate_no_effect_attempts = 0
    verified_baseline_evaluator_no_effect_attempts = 0
    verified_reflector_no_effect_attempts = 0
    confidence_transport_adaptation_count = 0

    for task_id in task_ids:
        pair_id = f"{experiment_id}--{task_id}"
        item_root = run_root / pair_id
        result = ThreeArtifactGenerationResult.model_validate_json(
            (item_root / "artifact.study.result.json").read_text(encoding="utf-8")
        )
        bundle_protocols.add(result.artifact_bundle.protocol)
        prompt_profiles.add(result.artifact_bundle.prompt_profile)
        if (
            result.task_id != task_id
            or result.pair_id != pair_id
            or result.s0_hash != expected_s0_hash
        ):
            raise ValueError(f"generation task/pair/S0 authority mismatch: {pair_id}")
        artifacts = result.artifact_bundle.artifacts
        if [item.artifact_type for item in artifacts] != list(THREE_ARTIFACT_ORDER):
            raise ValueError(f"generation typed artifact inventory differs: {pair_id}")
        ids = [item.artifact_id for item in artifacts]
        reflector_job_ids = [item.reflector_job_id for item in artifacts]
        reflector_run_ids = [item.reflector_run_id for item in artifacts]
        _record_globally_unique_three(
            seen_artifact_ids,
            ids,
            authority_name="artifact ID",
            pair_id=pair_id,
        )
        _record_globally_unique_three(
            seen_reflector_job_ids,
            reflector_job_ids,
            authority_name="Reflector job ID",
            pair_id=pair_id,
        )
        _record_globally_unique_three(
            seen_reflector_run_ids,
            reflector_run_ids,
            authority_name="Reflector run ID",
            pair_id=pair_id,
        )
        if {item.model for item in artifacts} != {expected_reflector_model}:
            raise ValueError(f"generation Reflector model differs: {pair_id}")
        if len({item.prompt_hash for item in artifacts}) != 3:
            raise ValueError(f"generation Reflector prompt hashes differ: {pair_id}")
        for item in artifacts:
            artifact_type_counts[item.artifact_type.value] = (
                artifact_type_counts.get(item.artifact_type.value, 0) + 1
            )
            _audit_reflector_completion(
                core_completion_root=core_completion_root,
                run_id=item.reflector_run_id,
                expected_model=expected_reflector_model,
            )

        _require_equal_json(item_root / "baseline.trajectory.json", result.baseline)
        _require_equal_json(item_root / "artifacts.receipt.json", result.artifact_bundle)
        feedback = json.loads((item_root / "feedback.json").read_text(encoding="utf-8"))
        if canonical_sha256(feedback) != result.feedback_hash:
            raise ValueError(f"generation feedback hash mismatch: {pair_id}")
        serialized_feedback = json.dumps(feedback, sort_keys=True)
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
            raise ValueError(f"forbidden scoring evidence entered generation input: {pair_id}")

        boundary_path = item_root / "generation.boundary.receipt.json"
        boundary = json.loads(boundary_path.read_text(encoding="utf-8"))
        if (
            boundary.get("schema_version")
            != "chemcrow_g1_artifact_generation_boundary_v1"
            or boundary.get("status") != "G1_AND_ARTIFACTS_SEALED"
            or boundary.get("task_id") != task_id
            or boundary.get("pair_id") != pair_id
            or boundary.get("g2_dispatched") is not False
            or boundary.get("g2_evaluator_dispatched") is not False
            or boundary.get("final_evaluator_dispatched") is not False
            or boundary.get("baseline_trajectory_sha256")
            != file_sha256(item_root / "baseline.trajectory.json")
            or boundary.get("feedback_sha256") != file_sha256(item_root / "feedback.json")
            or boundary.get("artifact_bundle_sha256")
            != file_sha256(item_root / "artifacts.receipt.json")
            or boundary.get("artifact_ids_by_type")
            != result.artifact_bundle.artifact_id_by_type()
        ):
            raise ValueError(f"generation boundary authority differs: {pair_id}")

        reset_path = item_root / "reset.receipt.json"
        if file_sha256(reset_path) != result.reset_receipt_sha256:
            raise ValueError(f"generation reset receipt hash mismatch: {pair_id}")
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
            raise ValueError(f"generation task-local reset differs: {pair_id}")
        reset_receipt_count += 1

        claims_root = run_root / "claims" / pair_id
        claims = {path.stem: path for path in claims_root.glob("*.json")}
        if set(claims) != _GENERATION_PHASES:
            raise ValueError(f"generation phase claim inventory mismatch: {pair_id}")
        receipts_by_phase = {
            "reflector_memory": artifacts[0],
            "reflector_skill_bundle": artifacts[1],
            "reflector_agent_system": artifacts[2],
        }
        for phase, path in claims.items():
            claim = json.loads(path.read_text(encoding="utf-8"))
            if (
                claim.get("status") != "terminal"
                or claim.get("phase") != phase
                or canonical_sha256(claim.get("authority"))
                != claim.get("authority_sha256")
                or canonical_sha256(claim.get("receipt")) != claim.get("receipt_sha256")
            ):
                raise ValueError(f"generation terminal claim differs: {pair_id}/{phase}")
            authority = claim.get("authority")
            if not isinstance(authority, dict) or authority.get("task_id") != task_id:
                raise ValueError(f"generation phase task authority differs: {pair_id}/{phase}")
            attempt_count = verified_no_effect_attempt_count(
                claim,
                pair_id=pair_id,
                phase=phase,
            )
            if phase == "baseline_candidate":
                verified_pre_candidate_no_effect_attempts += attempt_count
            elif phase == "baseline_internal_evaluator":
                verified_baseline_evaluator_no_effect_attempts += attempt_count
                receipt = claim.get("receipt", {})
                if (
                    receipt.get("evaluator_run_id")
                    != result.baseline_internal_evaluation.evaluator_run_id
                    or receipt.get("feedback_sha256")
                    != canonical_sha256(
                        result.baseline_internal_evaluation.model_dump(mode="json")
                    )
                ):
                    raise ValueError(f"generation evaluator claim differs: {pair_id}")
                confidence_receipt = receipt.get("confidence_parse_receipt")
                if confidence_receipt is not None:
                    if (
                        confidence_receipt.get("schema_version")
                        != "chemcrow_internal_confidence_parse_receipt_v1"
                        or confidence_receipt.get("policy_id")
                        != "chemcrow_internal_confidence_transport_v1"
                        or confidence_receipt.get("rubric_scores_changed") is not False
                        or confidence_receipt.get("prompt_changed") is not False
                    ):
                        raise ValueError(f"generation confidence receipt differs: {pair_id}")
                    confidence_transport_adaptation_count += int(
                        confidence_receipt.get("adapted") is True
                    )
            elif phase in _REFLECTOR_PHASES:
                verified_reflector_no_effect_attempts += attempt_count
                item = receipts_by_phase[phase]
                receipt = claim.get("receipt", {})
                if (
                    authority.get("paper_evaluator_feedback_included") is not False
                    or authority.get("sibling_artifact_ids") != []
                    or receipt.get("artifact_id") != item.artifact_id
                    or receipt.get("reflector_job_id") != item.reflector_job_id
                    or receipt.get("reflector_run_id") != item.reflector_run_id
                    or receipt.get("prompt_hash") != item.prompt_hash
                ):
                    raise ValueError(f"generation Reflector claim differs: {pair_id}/{phase}")
                sibling_isolation_evidence_count += 1

        if _audit_core_completion(
            core_completion_root=core_completion_root,
            run_id=result.baseline.run_id,
            expected_by_type={},
        ) is not None:
            raise ValueError(f"generation baseline was not bare S0: {pair_id}")
        forbidden_completion_names = [
            path.parent.name
            for path in core_completion_root.glob(f"task_{pair_id}-*/*.json")
            if any(
                token in path.parent.name.casefold()
                for token in ("-evolved-", "final-evaluator", "paper-evaluator")
            )
        ]
        if forbidden_completion_names:
            raise ValueError(f"post-artifact evaluator/Candidate completion exists: {pair_id}")
        if len(result.baseline.tool_calls) != len(result.baseline.observations):
            raise ValueError(f"generation tool call/observation mismatch: {pair_id}")
        tool_calls += len(result.baseline.tool_calls)
        for observation in result.baseline.observations:
            source = str(observation.source)
            if source in {"fixture", "mock"}:
                raise ValueError(f"non-real generation observation entered metrics: {pair_id}")
            source_counts[source] = source_counts.get(source, 0) + 1
            tool_errors += int(observation.error is not None)

    if len(bundle_protocols) != 1 or len(prompt_profiles) != 1:
        raise ValueError("generation run mixes artifact protocol or prompt profile")
    artifact_protocol = three_artifact_protocol_label(bundle_protocols.pop())
    prompt_profile = prompt_profiles.pop()
    aggregate_sha256: str | None = None
    if require_aggregate:
        aggregate_path = run_root / "aggregate.json"
        aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
        if (
            aggregate.get("schema_version")
            != "chemcrow_three_artifact_generation_aggregate_v1"
            or aggregate.get("status") != "G1_AND_ARTIFACT_GENERATION_COMPLETE"
            or aggregate.get("task_count") != len(task_ids)
            or aggregate.get("artifact_count") != len(task_ids) * 3
            or aggregate.get("unique_artifact_count") != len(task_ids) * 3
            or aggregate.get("unique_reflector_job_count") != len(task_ids) * 3
            or aggregate.get("g2_candidate_call_count") != 0
            or aggregate.get("g2_internal_evaluator_call_count") != 0
            or aggregate.get("final_evaluator_call_count") != 0
            or aggregate.get("artifact_protocol") != artifact_protocol
            or aggregate.get("prompt_profile") != prompt_profile
        ):
            raise ValueError("generation aggregate authority differs")
        aggregate_sha256 = file_sha256(aggregate_path)

    return {
        "schema_version": "chemcrow_three_artifact_generation_completed_run_audit_v1",
        "status": "PASS",
        "execution_scope": "g1_and_artifact_generation_only",
        "artifact_protocol": artifact_protocol,
        "reflector_prompt_profile": prompt_profile,
        "experiment_id": experiment_id,
        "task_ids": task_ids,
        "task_count": len(task_ids),
        "s0_config_sha256": expected_s0_hash,
        "g1_candidate_call_count": len(task_ids),
        "g1_internal_evaluator_call_count": len(task_ids),
        "g2_candidate_call_count": 0,
        "g2_internal_evaluator_call_count": 0,
        "final_evaluator_call_count": 0,
        "paper_evaluator_call_count": 0,
        "unique_artifact_count": len(seen_artifact_ids),
        "unique_reflector_job_count": len(seen_reflector_job_ids),
        "unique_reflector_run_count": len(seen_reflector_run_ids),
        "independent_reflector_job_count": len(seen_reflector_job_ids),
        "independent_reflector_run_count": len(seen_reflector_run_ids),
        "artifact_type_counts": dict(sorted(artifact_type_counts.items())),
        "artifact_registration_count": len(seen_artifact_ids),
        "sibling_isolation_evidence_count": sibling_isolation_evidence_count,
        "core_evolved_injection_receipt_count": 0,
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
        "verified_baseline_evaluator_no_effect_attempt_count": (
            verified_baseline_evaluator_no_effect_attempts
        ),
        "verified_reflector_no_effect_attempt_count": (
            verified_reflector_no_effect_attempts
        ),
        "verified_evolved_candidate_no_effect_attempt_count": 0,
        "confidence_transport_adaptation_count": (
            confidence_transport_adaptation_count
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


def _audit_reflector_completion(
    *,
    core_completion_root: Path,
    run_id: str,
    expected_model: str,
) -> None:
    matches = list(core_completion_root.glob(f"task_{run_id}/*.json"))
    if len(matches) != 1:
        raise ValueError(f"Reflector Core completion authority is not unique: {run_id}")
    payload = json.loads(matches[0].read_text(encoding="utf-8"))
    metadata = payload.get("trajectory", {}).get("metadata", {}).get("task_metadata", {})
    credential = metadata.get("openevo", {}).get("credential_isolation_receipt", {})
    if (
        payload.get("status") != "COMPLETED"
        or payload.get("task_id") != run_id
        or metadata.get("execution_route") != CORE_MANAGED_CODEX_ROUTE
        or metadata.get("host_codex_exec_forbidden") is not True
        or metadata.get("agent_model_name") != expected_model
        or credential.get("status") != "passed"
    ):
        raise ValueError(f"Reflector Core completion provenance differs: {run_id}")
