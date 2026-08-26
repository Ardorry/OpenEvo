from __future__ import annotations

import hashlib
import json
import shlex
import sqlite3
from pathlib import Path
from typing import Any

import httpx
from openevo.harness.presets.codex import _codex_subscription_json_pipeline

from .hashing import canonical_sha256, file_sha256
from .models import ArtifactKind, EvaluatorFeedback, TaskItem, Trajectory
from .replacement_ledger import (
    VerifiedReplacementPhaseLedger,
    verified_no_effect_attempt_count,
)
from .runtime import CORE_MANAGED_CODEX_ROUTE
from .three_artifact_evolution import render_core_native_reflector_prompt

_CHECKPOINT_NAME = "reflector-boundary.checkpoint.json"
_RECEIPT_NAME = "reflector-no-effect.recovery.json"
_REFLECTOR_PHASE_METHODS = {
    "reflector_memory": "text_memory_reflector",
    "reflector_skill_bundle": "skill_bundle_reflector",
    "reflector_agent_system": "agent_system_reflector",
}
_PHASES = {
    "baseline_candidate",
    "baseline_internal_evaluator",
    *_REFLECTOR_PHASE_METHODS,
}
_NO_EFFECT_JOB_ERROR_PREFIX = "verified pre-execution no-effect: "
_LINUX_MAX_ARG_STRLEN_BYTES = 131_072


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"JSON object required: {path}")
    return payload


def _job_rows(db_path: Path, *, evidence_hash: str) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT job_id, method, state, lease_id, error, config_json
            FROM jobs
            WHERE job_type = 'chemcrow_task_local_isolated_reflection'
            """
        ).fetchall()
    matching: list[dict[str, Any]] = []
    for row in rows:
        config = json.loads(str(row["config_json"]))
        if config.get("input_evidence_sha256") == evidence_hash:
            matching.append({**dict(row), "config": config})
    return matching


def _job_artifact_count(db_path: Path, *, job_id: str) -> int:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE staging_job_id = ?", (job_id,)
        ).fetchone()
    return int(row[0])


def _fail_no_effect_job(
    *,
    backend_url: str,
    db_path: Path,
    job_id: str,
    lease_id: str,
    failure_kind: str,
) -> None:
    job_error = _NO_EFFECT_JOB_ERROR_PREFIX + failure_kind
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        raw = conn.execute(
            "SELECT state, lease_id, error FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    if raw is None:
        raise ValueError("failed Reflector job is absent")
    if raw["state"] == "failed" and raw["error"] == job_error:
        return
    if raw["state"] != "running" or raw["lease_id"] != lease_id:
        raise ValueError("failed Reflector job lease authority differs")
    response = httpx.post(
        f"{backend_url.rstrip('/')}/v1/jobs/{job_id}/fail",
        json={"lease_id": lease_id, "error": job_error, "retryable": False},
        timeout=10.0,
        trust_env=False,
    )
    response.raise_for_status()
    result = response.json()
    if result.get("job_id") != job_id or result.get("state") != "failed":
        raise ValueError("Core did not seal the no-effect Reflector job as failed")


def reconcile_reflector_no_effect_failure(
    *,
    config_path: Path,
    run_root: Path,
    ledger_root: Path,
    experiment_id: str,
    task: TaskItem,
    expected_candidate_config_sha256: str,
    expected_evaluator_id: str,
    backend_url: str,
    evolution_db_path: Path,
    failed_phase: str,
    failed_job_id: str,
    core_completion_path: Path,
    evidence_event_path: Path,
) -> tuple[Path, Path, dict[str, Any]]:
    """Preserve one proven pre-execution Reflector failure and reopen its boundary."""

    if failed_phase != "reflector_memory":
        raise ValueError("current recovery supports only the first sequential Reflector phase")
    pair_id = f"{experiment_id}--{task.task_id}"
    if (run_root / pair_id / "pair.result.json").exists():
        raise ValueError("sealed pair cannot be reflector-recovered")
    claims_root = ledger_root / pair_id
    claims = {path.stem: _read_json(path) for path in claims_root.glob("*.json")}
    if set(claims) != _PHASES:
        raise ValueError("Reflector recovery phase inventory differs")
    if any(
        claims[phase].get("status") != "terminal"
        for phase in ("baseline_candidate", "baseline_internal_evaluator")
    ) or any(
        claims[phase].get("status") != "claimed" for phase in _REFLECTOR_PHASE_METHODS
    ):
        raise ValueError("Reflector recovery claim states differ")

    evidence_event = _read_json(evidence_event_path)
    session_result = evidence_event.get("payload", {}).get("session_result", {})
    metadata = session_result.get("metadata", {}) if isinstance(session_result, dict) else {}
    evidence = metadata.get("input_evidence") if isinstance(metadata, dict) else None
    evidence_hash = metadata.get("input_evidence_sha256") if isinstance(metadata, dict) else None
    if not isinstance(evidence, dict) or canonical_sha256(evidence) != evidence_hash:
        raise ValueError("Core evidence event payload hash differs")
    expected_task_evidence = {
        "task_id": task.task_id,
        "prompt": task.prompt,
        "sanitized_item_sha256": task.sanitized_item_sha256,
    }
    if evidence.get("task") != expected_task_evidence:
        raise ValueError("Core evidence event task authority differs")
    reflector_hashes = {
        claims[phase].get("authority", {}).get("input_evidence_hash")
        for phase in _REFLECTOR_PHASE_METHODS
    }
    if reflector_hashes != {evidence_hash}:
        raise ValueError("Reflector claims do not bind the recovered evidence")
    baseline = Trajectory.model_validate(evidence.get("baseline"))
    evaluation = EvaluatorFeedback.model_validate(
        evidence.get("feedback", {}).get("evaluator_feedback")
    )
    baseline_receipt = claims["baseline_candidate"].get("receipt", {})
    evaluator_receipt = claims["baseline_internal_evaluator"].get("receipt", {})
    evaluator_authority = claims["baseline_internal_evaluator"].get("authority", {})
    if (
        baseline.task_id != task.task_id
        or baseline.role != "baseline"
        or baseline.status != "COMPLETED"
        or baseline.artifact_ids
        or baseline.candidate_config_sha256 != expected_candidate_config_sha256
        or baseline_receipt.get("run_id") != baseline.run_id
        or baseline_receipt.get("trajectory_sha256")
        != canonical_sha256(baseline.model_dump(mode="json"))
        or evaluation.evaluator_role != "evolution_evaluator"
        or evaluator_authority.get("evaluator_id") != expected_evaluator_id
        or evaluator_receipt.get("evaluator_run_id") != evaluation.evaluator_run_id
        or evaluator_receipt.get("feedback_sha256")
        != canonical_sha256(evaluation.model_dump(mode="json"))
    ):
        raise ValueError("recovered baseline/evaluator checkpoint authority differs")

    completion = _read_json(core_completion_path)
    trajectory = completion.get("trajectory")
    trajectory_metadata = trajectory.get("metadata") if isinstance(trajectory, dict) else None
    completion_metadata = completion.get("metadata")
    expected_prefix = f"{pair_id}-{failed_phase}-core-baseline-"
    reflector_prompt = render_core_native_reflector_prompt(ArtifactKind.TEXT_MEMORY, evidence)
    reflector_prompt_bytes = len(reflector_prompt.encode("utf-8"))
    # The first carrier repair moved the exact old harness command into a
    # session script, but that command still launched an inner ``bash -c``.
    # This conservative lower bound omits the Codex executable and flags while
    # retaining the quoted prompt and terminal validator, so crossing the Linux
    # single-argv ceiling proves the inner shell could not start Codex.
    legacy_inner_shell_argv_minimum_bytes = len(shlex.quote(reflector_prompt).encode()) + len(
        _codex_subscription_json_pipeline(
            "", "/openevo/session/logs/agent/codex.txt"
        ).encode()
    )
    direct_docker_argv_error = completion.get("error") == (
        "agent execution failed: [Errno 7] Argument list too long: '/usr/bin/docker'"
    )
    masked_codex_argv_error = completion.get("error") == (
        "subscription finalization failed: subscription transcript could not be read safely"
    ) and legacy_inner_shell_argv_minimum_bytes > _LINUX_MAX_ARG_STRLEN_BYTES
    if direct_docker_argv_error:
        failure_kind = "docker_argv_limit_before_reflector_process_start"
    elif masked_codex_argv_error:
        failure_kind = "codex_prompt_argv_limit_before_reflector_process_start"
    else:
        failure_kind = "unproven"
    no_effect_predicates = {
        "completion_status_error": completion.get("status") == "ERROR",
        "pre_exec_argv_failure_proven": failure_kind != "unproven",
        "zero_trajectory_traces": isinstance(trajectory, dict)
        and trajectory.get("traces") == [],
        "zero_transcript_records": isinstance(trajectory_metadata, dict)
        and trajectory_metadata.get("record_count") == 0,
        "workspace_result_absent": completion.get("workspace_result") is None,
        "core_route_verified": isinstance(completion_metadata, dict)
        and completion_metadata.get("execution_route") == CORE_MANAGED_CODEX_ROUTE
        and completion_metadata.get("host_codex_exec_forbidden") is True,
        "run_id_matches_claimed_phase": isinstance(completion.get("task_id"), str)
        and completion["task_id"].startswith(expected_prefix),
        "job_artifacts_absent": _job_artifact_count(
            evolution_db_path, job_id=failed_job_id
        )
        == 0,
    }
    if set(no_effect_predicates.values()) != {True}:
        failed = sorted(key for key, value in no_effect_predicates.items() if not value)
        raise ValueError(f"Reflector no-effect predicates failed: {failed}")

    job_rows = _job_rows(evolution_db_path, evidence_hash=str(evidence_hash))
    failed_candidates = [row for row in job_rows if row["job_id"] == failed_job_id]
    methods = {str(row["method"]) for row in job_rows}
    if (
        methods != {"text_memory_reflector"}
        or len(failed_candidates) != 1
        or failed_candidates[0]["method"] != "text_memory_reflector"
        or not isinstance(failed_candidates[0]["lease_id"], str)
        or any(
            row["job_id"] != failed_job_id
            and (
                row["state"] != "failed"
                or not str(row["error"] or "").startswith(_NO_EFFECT_JOB_ERROR_PREFIX)
            )
            for row in job_rows
        )
    ):
        raise ValueError("Reflector dispatch inventory differs from sequential no-effect proof")
    failed_row = failed_candidates[0]
    lease_id = str(failed_row["lease_id"])
    _fail_no_effect_job(
        backend_url=backend_url,
        db_path=evolution_db_path,
        job_id=failed_job_id,
        lease_id=lease_id,
        failure_kind=failure_kind,
    )

    checkpoint = {
        "schema_version": "chemcrow_reflector_boundary_checkpoint_v1",
        "status": "READY_FOR_REFLECTOR_REPLACEMENT_WITHOUT_G1_REDISPATCH",
        "pair_id": pair_id,
        "task_id": task.task_id,
        "baseline": baseline.model_dump(mode="json"),
        "baseline_internal_evaluation": evaluation.model_dump(mode="json"),
        "input_evidence_sha256": evidence_hash,
    }
    recovery_root = run_root / "recovery" / pair_id
    recovery_root.mkdir(parents=True, exist_ok=True)
    checkpoint_path = recovery_root / _CHECKPOINT_NAME
    checkpoint_bytes = (json.dumps(checkpoint, indent=2, sort_keys=True) + "\n").encode()
    if checkpoint_path.exists() and checkpoint_path.read_bytes() != checkpoint_bytes:
        raise ValueError("existing Reflector boundary checkpoint differs")
    checkpoint_path.write_bytes(checkpoint_bytes)

    prior_attempt_counts = {
        phase: verified_no_effect_attempt_count(
            claims[phase], pair_id=pair_id, phase=phase
        )
        for phase in _REFLECTOR_PHASE_METHODS
    }
    if len(set(prior_attempt_counts.values())) != 1:
        raise ValueError("Reflector replacement attempt ordinals diverged")
    attempt_ordinal = next(iter(prior_attempt_counts.values())) + 1
    phase_receipts: dict[str, dict[str, Any]] = {}
    for phase, method in _REFLECTOR_PHASE_METHODS.items():
        original_claim_path = claims_root / f"{phase}.json"
        dispatched = phase == failed_phase
        predicates = (
            no_effect_predicates
            if dispatched
            else {
                "prior_sequential_phase_failed_before_return": True,
                "matching_core_job_absent": method not in methods,
                "artifact_registration_absent": True,
                "sibling_output_absent": True,
            }
        )
        phase_receipts[phase] = {
            "schema_version": "chemcrow_reflector_no_effect_recovery_v1",
            "status": "VERIFIED_NO_CANDIDATE_EFFECT_REPLACEMENT_READY",
            "pair_id": pair_id,
            "task_id": task.task_id,
            "phase": phase,
            "attempt_ordinal": attempt_ordinal,
            "authority_sha256": claims[phase]["authority_sha256"],
            "original_claim_sha256": hashlib.sha256(
                original_claim_path.read_bytes()
            ).hexdigest(),
            "input_evidence_sha256": evidence_hash,
            "dispatch_started": dispatched,
            "failed_job_id": failed_job_id if dispatched else None,
            "core_completion_sha256": (
                file_sha256(core_completion_path) if dispatched else None
            ),
            "failure_kind": failure_kind if dispatched else "not_dispatched",
            "reflector_prompt_bytes": reflector_prompt_bytes if dispatched else None,
            "legacy_inner_shell_argv_minimum_bytes": (
                legacy_inner_shell_argv_minimum_bytes if dispatched else None
            ),
            "no_effect_predicates": predicates,
            "reflector_model_call_proven_absent": True,
            "duplicate_scientific_call": False,
            "recorded_before_replacement_dispatch": True,
        }
        VerifiedReplacementPhaseLedger(
            ledger_root, pair_id=pair_id
        ).reconcile_verified_no_effect_failure(phase, phase_receipts[phase])

    recovery_receipt = {
        "schema_version": "chemcrow_reflector_boundary_recovery_v1",
        "status": "VERIFIED_REFLECTOR_BOUNDARY_REPLACEMENT_READY",
        "pair_id": pair_id,
        "task_id": task.task_id,
        "config_sha256": file_sha256(config_path),
        "checkpoint_sha256": file_sha256(checkpoint_path),
        "evidence_event_sha256": file_sha256(evidence_event_path),
        "core_completion_sha256": file_sha256(core_completion_path),
        "failed_job_id": failed_job_id,
        "failed_job_terminal_state": "failed",
        "reflector_replacement_attempt_ordinal": attempt_ordinal,
        "baseline_redispatched": False,
        "baseline_evaluator_redispatched": False,
        "reflector_replacement_phases": list(_REFLECTOR_PHASE_METHODS),
        "phase_receipt_sha256": {
            phase: canonical_sha256(receipt)
            for phase, receipt in sorted(phase_receipts.items())
        },
        "model_calls_during_reconciliation": 0,
    }
    recovery_path = recovery_root / (
        _RECEIPT_NAME
        if attempt_ordinal == 1
        else f"reflector-no-effect.recovery.attempt-{attempt_ordinal}.json"
    )
    recovery_bytes = (json.dumps(recovery_receipt, indent=2, sort_keys=True) + "\n").encode()
    if recovery_path.exists() and recovery_path.read_bytes() != recovery_bytes:
        raise ValueError("existing Reflector no-effect recovery receipt differs")
    recovery_path.write_bytes(recovery_bytes)
    return checkpoint_path, recovery_path, recovery_receipt


def load_reflector_boundary_checkpoint(
    *,
    run_root: Path,
    ledger_root: Path,
    pair_id: str,
    task: TaskItem,
    expected_candidate_config_sha256: str,
    expected_evaluator_id: str,
) -> tuple[Trajectory, EvaluatorFeedback, frozenset[str]] | None:
    recovery_root = run_root / "recovery" / pair_id
    checkpoint_path = recovery_root / _CHECKPOINT_NAME
    recovery_path = recovery_root / _RECEIPT_NAME
    if not checkpoint_path.exists() and not recovery_path.exists():
        return None
    if not checkpoint_path.is_file() or not recovery_path.is_file():
        raise ValueError("Reflector recovery checkpoint evidence is incomplete")
    checkpoint = _read_json(checkpoint_path)
    receipt = _read_json(recovery_path)
    if (
        checkpoint.get("status")
        != "READY_FOR_REFLECTOR_REPLACEMENT_WITHOUT_G1_REDISPATCH"
        or checkpoint.get("pair_id") != pair_id
        or checkpoint.get("task_id") != task.task_id
        or receipt.get("status") != "VERIFIED_REFLECTOR_BOUNDARY_REPLACEMENT_READY"
        or receipt.get("checkpoint_sha256") != file_sha256(checkpoint_path)
        or receipt.get("baseline_redispatched") is not False
        or receipt.get("baseline_evaluator_redispatched") is not False
        or receipt.get("model_calls_during_reconciliation") != 0
    ):
        raise ValueError("Reflector recovery checkpoint authority differs")
    claims = {
        path.stem: _read_json(path) for path in (ledger_root / pair_id).glob("*.json")
    }
    if set(claims) != _PHASES:
        raise ValueError("Reflector recovery claim inventory differs")
    if any(
        claims[phase].get("status") != "terminal"
        for phase in ("baseline_candidate", "baseline_internal_evaluator")
    ):
        raise ValueError("recovered G1 claim state differs")
    for phase in _REFLECTOR_PHASE_METHODS:
        if claims[phase].get("status") != "replacement_ready":
            raise ValueError("Reflector replacement is not ready")
        if verified_no_effect_attempt_count(claims[phase], pair_id=pair_id, phase=phase) != 1:
            raise ValueError("Reflector replacement attempt evidence differs")
    baseline = Trajectory.model_validate(checkpoint.get("baseline"))
    evaluation = EvaluatorFeedback.model_validate(
        checkpoint.get("baseline_internal_evaluation")
    )
    if (
        baseline.task_id != task.task_id
        or baseline.status != "COMPLETED"
        or baseline.role != "baseline"
        or baseline.artifact_ids
        or baseline.candidate_config_sha256 != expected_candidate_config_sha256
        or evaluation.evaluator_role != "evolution_evaluator"
        or claims["baseline_internal_evaluator"].get("authority", {}).get("evaluator_id")
        != expected_evaluator_id
    ):
        raise ValueError("Reflector recovery semantic authority differs")
    return baseline, evaluation, frozenset(_REFLECTOR_PHASE_METHODS)


__all__ = [
    "load_reflector_boundary_checkpoint",
    "reconcile_reflector_no_effect_failure",
]
