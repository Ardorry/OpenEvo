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
from .three_artifact_evolution import (
    _core_reflector_contract_hash,
    _strict_reflector_markdown,
    render_core_native_reflector_prompt,
)
from .three_artifact_models import RecoveredReflectorOutput

_CHECKPOINT_NAME = "reflector-boundary.checkpoint.json"
_RECEIPT_NAME = "reflector-no-effect.recovery.json"
_PARTIAL_CHECKPOINT_NAME = "partial-reflector-boundary.checkpoint.json"
_PARTIAL_RECEIPT_NAME = "partial-reflector-boundary.recovery.json"
_PAUSED_BATCH_JOB_ERROR_PREFIX = "verified paused reflector batch: "
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
_BATCH_ABORT_JOB_ERROR = "Core-managed reflector_memory returned no completed artifact"
_CREDENTIAL_ISOLATION_VALIDATION_ERROR = (
    "agent execution failed: Codex subscription credential isolation could not be "
    "proven (validation_failed)"
)
_LINUX_MAX_ARG_STRLEN_BYTES = 131_072

_PHASE_KINDS = {
    "reflector_memory": ArtifactKind.TEXT_MEMORY,
    "reflector_skill_bundle": ArtifactKind.SKILL_BUNDLE,
    "reflector_agent_system": ArtifactKind.AGENT_SYSTEM,
}
_PHASE_NAMES_BY_KIND = {kind: phase for phase, kind in _PHASE_KINDS.items()}
_PHASE_CONTENT_PATHS = {
    "reflector_memory": ("text_memory_reflector", "memory.md"),
    "reflector_skill_bundle": ("skill_bundle_reflector", "SKILL.md"),
    "reflector_agent_system": ("agent_system_reflector", "AGENTS.md"),
}


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"JSON object required: {path}")
    return payload


def _job_rows(db_path: Path, *, evidence_hash: str) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(jobs)")}
        created_at_expression = (
            "created_at" if "created_at" in columns else "'' AS created_at"
        )
        updated_at_expression = (
            "updated_at" if "updated_at" in columns else "'' AS updated_at"
        )
        rows = conn.execute(
            f"""
            SELECT job_id, method, state, lease_id, error, config_json,
                   {created_at_expression}, {updated_at_expression}
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


def _latest_failed_job_batch(
    job_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split the newest adapter-aborted batch from earlier failed attempts.

    Core stores timestamps at one-second resolution. Creating the three sibling
    jobs can cross a second boundary, so ``created_at`` equality is not a valid
    batch identity. The adapter aborts all three rows in one transaction/path,
    which gives the batch a shared ``updated_at``. Older test schemas without
    ``updated_at`` retain the historical ``created_at`` behavior.
    """

    latest_updated_at = max(
        (str(row.get("updated_at", "")) for row in job_rows), default=""
    )
    timestamp_field = "updated_at" if latest_updated_at else "created_at"
    latest_timestamp = (
        latest_updated_at
        if latest_updated_at
        else max((str(row.get("created_at", "")) for row in job_rows), default="")
    )
    latest = [
        row
        for row in job_rows
        if str(row.get(timestamp_field, "")) == latest_timestamp
    ]
    historical = [row for row in job_rows if row not in latest]
    return latest, historical


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


def _fail_paused_batch_job(
    *,
    backend_url: str,
    db_path: Path,
    job_id: str,
    lease_id: str,
    reason: str,
) -> None:
    job_error = _PAUSED_BATCH_JOB_ERROR_PREFIX + reason
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        raw = conn.execute(
            "SELECT state, lease_id, error FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    if raw is None:
        raise ValueError("paused Reflector job is absent")
    if raw["state"] == "failed" and raw["error"] == job_error:
        return
    if raw["state"] != "running" or raw["lease_id"] != lease_id:
        raise ValueError("paused Reflector job lease authority differs")
    response = httpx.post(
        f"{backend_url.rstrip('/')}/v1/jobs/{job_id}/fail",
        json={"lease_id": lease_id, "error": job_error, "retryable": False},
        timeout=10.0,
        trust_env=False,
    )
    response.raise_for_status()
    result = response.json()
    if result.get("job_id") != job_id or result.get("state") != "failed":
        raise ValueError("Core did not seal the paused Reflector job as failed")


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
    ) or any(claims[phase].get("status") != "claimed" for phase in _REFLECTOR_PHASE_METHODS):
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
    frozen_prompt_path = run_root / pair_id / "reflector_prompts" / "text_memory.md"
    reflector_prompt = (
        frozen_prompt_path.read_text(encoding="utf-8")
        if frozen_prompt_path.is_file()
        else render_core_native_reflector_prompt(ArtifactKind.TEXT_MEMORY, evidence)
    )
    reflector_prompt_bytes = len(reflector_prompt.encode("utf-8"))
    # The first carrier repair moved the exact old harness command into a
    # session script, but that command still launched an inner ``bash -c``.
    # This conservative lower bound omits the Codex executable and flags while
    # retaining the quoted prompt and terminal validator, so crossing the Linux
    # single-argv ceiling proves the inner shell could not start Codex.
    legacy_inner_shell_argv_minimum_bytes = len(shlex.quote(reflector_prompt).encode()) + len(
        _codex_subscription_json_pipeline("", "/openevo/session/logs/agent/codex.txt").encode()
    )
    direct_docker_argv_error = completion.get("error") == (
        "agent execution failed: [Errno 7] Argument list too long: '/usr/bin/docker'"
    )
    masked_codex_argv_error = (
        completion.get("error")
        == ("subscription finalization failed: subscription transcript could not be read safely")
        and legacy_inner_shell_argv_minimum_bytes > _LINUX_MAX_ARG_STRLEN_BYTES
    )
    credential_isolation_validation_error = (
        completion.get("error") == _CREDENTIAL_ISOLATION_VALIDATION_ERROR
    )
    if direct_docker_argv_error:
        failure_kind = "docker_argv_limit_before_reflector_process_start"
    elif masked_codex_argv_error:
        failure_kind = "codex_prompt_argv_limit_before_reflector_process_start"
    elif credential_isolation_validation_error:
        failure_kind = "credential_isolation_validation_failed_before_reflector_process_start"
    else:
        failure_kind = "unproven"
    no_effect_predicates = {
        "completion_status_error": completion.get("status") == "ERROR",
        "pre_reflector_process_failure_proven": failure_kind != "unproven",
        "zero_trajectory_traces": isinstance(trajectory, dict) and trajectory.get("traces") == [],
        "zero_transcript_records": isinstance(trajectory_metadata, dict)
        and trajectory_metadata.get("record_count") == 0,
        "workspace_result_absent": completion.get("workspace_result") is None,
        "core_route_verified": isinstance(completion_metadata, dict)
        and completion_metadata.get("execution_route") == CORE_MANAGED_CODEX_ROUTE
        and completion_metadata.get("host_codex_exec_forbidden") is True,
        "run_id_matches_claimed_phase": isinstance(completion.get("task_id"), str)
        and completion["task_id"].startswith(expected_prefix),
        "job_artifacts_absent": _job_artifact_count(evolution_db_path, job_id=failed_job_id) == 0,
    }
    if credential_isolation_validation_error:
        openevo_metadata = (
            completion_metadata.get("openevo") if isinstance(completion_metadata, dict) else None
        )
        credential_contract = (
            openevo_metadata.get("credential_isolation")
            if isinstance(openevo_metadata, dict)
            else None
        )
        no_effect_predicates["credential_canary_receipt_not_published"] = (
            isinstance(credential_contract, dict) and "status" not in credential_contract
        )
    if set(no_effect_predicates.values()) != {True}:
        failed = sorted(key for key, value in no_effect_predicates.items() if not value)
        raise ValueError(f"Reflector no-effect predicates failed: {failed}")

    job_rows = _job_rows(evolution_db_path, evidence_hash=str(evidence_hash))
    failed_candidates = [row for row in job_rows if row["job_id"] == failed_job_id]
    methods = {str(row["method"]) for row in job_rows}
    expected_methods = set(_REFLECTOR_PHASE_METHODS.values())
    all_job_artifacts_absent = all(
        _job_artifact_count(evolution_db_path, job_id=str(row["job_id"])) == 0
        for row in job_rows
    )
    other_jobs_terminal_no_effect = all(
        row["job_id"] == failed_job_id
        or (
            row["state"] == "failed"
            and (
                str(row["error"] or "").startswith(_NO_EFFECT_JOB_ERROR_PREFIX)
                or row["error"] == _BATCH_ABORT_JOB_ERROR
            )
        )
        for row in job_rows
    )
    if (
        not methods.issubset(expected_methods)
        or "text_memory_reflector" not in methods
        or len(failed_candidates) != 1
        or failed_candidates[0]["method"] != "text_memory_reflector"
        or not all_job_artifacts_absent
        or not other_jobs_terminal_no_effect
    ):
        raise ValueError("Reflector dispatch inventory differs from sequential no-effect proof")
    failed_row = failed_candidates[0]
    if failed_row["state"] == "running" and isinstance(failed_row["lease_id"], str):
        _fail_no_effect_job(
            backend_url=backend_url,
            db_path=evolution_db_path,
            job_id=failed_job_id,
            lease_id=str(failed_row["lease_id"]),
            failure_kind=failure_kind,
        )
    elif not (
        failed_row["state"] == "failed" and failed_row["error"] == _BATCH_ABORT_JOB_ERROR
    ):
        raise ValueError("failed Reflector job terminal state differs")

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
        phase: verified_no_effect_attempt_count(claims[phase], pair_id=pair_id, phase=phase)
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
                "matching_core_job_absent_or_batch_aborted": (
                    method not in methods
                    or all(
                        row["state"] == "failed"
                        and row["error"] == _BATCH_ABORT_JOB_ERROR
                        for row in job_rows
                        if row["method"] == method
                    )
                ),
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
            "original_claim_sha256": hashlib.sha256(original_claim_path.read_bytes()).hexdigest(),
            "input_evidence_sha256": evidence_hash,
            "dispatch_started": dispatched,
            "failed_job_id": failed_job_id if dispatched else None,
            "core_completion_sha256": (file_sha256(core_completion_path) if dispatched else None),
            "failure_kind": failure_kind if dispatched else "not_dispatched",
            "reflector_prompt_bytes": reflector_prompt_bytes if dispatched else None,
            "legacy_inner_shell_argv_minimum_bytes": (
                legacy_inner_shell_argv_minimum_bytes if dispatched else None
            ),
            "no_effect_predicates": predicates,
            "infrastructure_canary_model_call_may_have_occurred": (
                dispatched and credential_isolation_validation_error
            ),
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
            phase: canonical_sha256(receipt) for phase, receipt in sorted(phase_receipts.items())
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


def _baseline_evidence_matches_claims(
    *,
    baseline: Trajectory,
    baseline_claim: dict[str, Any],
    evaluator_claim: dict[str, Any],
) -> bool:
    reconstructed_sha256 = canonical_sha256(baseline.model_dump(mode="json"))
    if baseline_claim.get("receipt", {}).get("trajectory_sha256") == reconstructed_sha256:
        return True
    failed_attempts = evaluator_claim.get("failed_attempts")
    return isinstance(failed_attempts, list) and any(
        isinstance(attempt, dict)
        and attempt.get("receipt", {}).get("schema_version")
        == "chemcrow_baseline_evaluator_no_effect_recovery_v1"
        and attempt.get("receipt", {}).get(
            "baseline_reconstructed_trajectory_sha256"
        )
        == reconstructed_sha256
        and attempt.get("receipt", {}).get("candidate_redispatched") is False
        for attempt in failed_attempts
    )


def _completion_duration_seconds(completion: dict[str, Any]) -> float:
    timing = completion.get("timing")
    if not isinstance(timing, dict):
        raise TypeError("Reflector completion timing is absent")
    values = [value for value in timing.values() if isinstance(value, int | float)]
    if not values or any(value < 0 for value in values):
        raise ValueError("Reflector completion timing is invalid")
    return sum(values) / 1000.0


def reconcile_partial_reflector_batch_failure(
    *,
    config_path: Path,
    run_root: Path,
    ledger_root: Path,
    experiment_id: str,
    task: TaskItem,
    expected_candidate_config_sha256: str,
    expected_evaluator_id: str,
    evolution_db_path: Path,
    evolution_artifact_root: Path,
    failed_phase: str,
    failed_job_id: str,
    failed_completion_path: Path,
    evidence_event_path: Path,
    completed_sibling_paths: dict[str, Path],
) -> tuple[Path, Path, dict[str, Any]]:
    """Preserve completed native outputs and replace only one no-effect sibling."""

    ordered_phases = list(_REFLECTOR_PHASE_METHODS)
    if failed_phase not in ordered_phases:
        raise ValueError("partial Reflector failed phase is invalid")
    failed_phase_index = ordered_phases.index(failed_phase)
    expected_completed = set(ordered_phases[:failed_phase_index])
    replacement_phases = ordered_phases[failed_phase_index:]
    if set(completed_sibling_paths) != expected_completed:
        raise ValueError("completed Reflector sibling inventory differs from call order")
    pair_id = f"{experiment_id}--{task.task_id}"
    if (run_root / pair_id / "pair.result.json").exists() or (
        run_root / pair_id / "artifact.study.result.json"
    ).exists():
        raise ValueError("sealed pair cannot be reflector-recovered")
    claims_root = ledger_root / pair_id
    claims = {path.stem: _read_json(path) for path in claims_root.glob("*.json")}
    if set(claims) != _PHASES:
        raise ValueError("partial Reflector recovery phase inventory differs")
    if any(
        claims[phase].get("status") != "terminal"
        for phase in ("baseline_candidate", "baseline_internal_evaluator")
    ) or any(claims[phase].get("status") != "claimed" for phase in ordered_phases):
        raise ValueError("partial Reflector recovery claim states differ")

    evidence_event = _read_json(evidence_event_path)
    session_result = evidence_event.get("payload", {}).get("session_result", {})
    event_metadata = (
        session_result.get("metadata", {}) if isinstance(session_result, dict) else {}
    )
    evidence = event_metadata.get("input_evidence")
    evidence_hash = event_metadata.get("input_evidence_sha256")
    if not isinstance(evidence, dict) or canonical_sha256(evidence) != evidence_hash:
        raise ValueError("partial Reflector evidence event hash differs")
    if evidence.get("task") != {
        "task_id": task.task_id,
        "prompt": task.prompt,
        "sanitized_item_sha256": task.sanitized_item_sha256,
    }:
        raise ValueError("partial Reflector task authority differs")
    if {
        claims[phase].get("authority", {}).get("input_evidence_hash")
        for phase in ordered_phases
    } != {evidence_hash}:
        raise ValueError("partial Reflector claims do not share frozen evidence")
    baseline = Trajectory.model_validate(evidence.get("baseline"))
    evaluation = EvaluatorFeedback.model_validate(
        evidence.get("feedback", {}).get("evaluator_feedback")
    )
    if (
        baseline.task_id != task.task_id
        or baseline.role != "baseline"
        or baseline.status != "COMPLETED"
        or baseline.artifact_ids
        or baseline.candidate_config_sha256 != expected_candidate_config_sha256
        or not _baseline_evidence_matches_claims(
            baseline=baseline,
            baseline_claim=claims["baseline_candidate"],
            evaluator_claim=claims["baseline_internal_evaluator"],
        )
        or evaluation.evaluator_role != "evolution_evaluator"
        or claims["baseline_internal_evaluator"].get("authority", {}).get(
            "evaluator_id"
        )
        != expected_evaluator_id
        or claims["baseline_internal_evaluator"].get("receipt", {}).get(
            "evaluator_run_id"
        )
        != evaluation.evaluator_run_id
        or claims["baseline_internal_evaluator"].get("receipt", {}).get(
            "feedback_sha256"
        )
        != canonical_sha256(evaluation.model_dump(mode="json"))
    ):
        raise ValueError("partial Reflector recovered G1 authority differs")

    all_job_rows = _job_rows(evolution_db_path, evidence_hash=str(evidence_hash))
    job_rows, historical_job_rows = _latest_failed_job_batch(all_job_rows)
    if (
        len(job_rows) != 3
        or {row["method"] for row in job_rows}
        != set(_REFLECTOR_PHASE_METHODS.values())
        or any(row["state"] != "failed" for row in job_rows)
        or any(_job_artifact_count(evolution_db_path, job_id=row["job_id"]) for row in job_rows)
        or any(row["state"] != "failed" for row in historical_job_rows)
        or any(
            _job_artifact_count(evolution_db_path, job_id=str(row["job_id"]))
            for row in historical_job_rows
        )
    ):
        raise ValueError("partial Reflector Core job inventory differs")
    rows_by_method = {str(row["method"]): row for row in job_rows}
    failed_method = _REFLECTOR_PHASE_METHODS[failed_phase]
    failed_row = rows_by_method[failed_method]
    if failed_row["job_id"] != failed_job_id:
        raise ValueError("partial Reflector failed job identity differs")

    failure = _read_json(failed_completion_path)
    trajectory = failure.get("trajectory")
    trajectory_metadata = trajectory.get("metadata") if isinstance(trajectory, dict) else None
    metadata = failure.get("metadata")
    credential = (
        metadata.get("openevo", {}).get("credential_isolation")
        if isinstance(metadata, dict)
        else None
    )
    no_effect_predicates = {
        "completion_status_error": failure.get("status") == "ERROR",
        "exact_credential_isolation_error": failure.get("error")
        == _CREDENTIAL_ISOLATION_VALIDATION_ERROR,
        "zero_trajectory_traces": isinstance(trajectory, dict)
        and trajectory.get("traces") == [],
        "zero_transcript_records": isinstance(trajectory_metadata, dict)
        and trajectory_metadata.get("record_count") == 0,
        "workspace_result_absent": failure.get("workspace_result") is None,
        "core_route_verified": isinstance(metadata, dict)
        and metadata.get("execution_route") == CORE_MANAGED_CODEX_ROUTE
        and metadata.get("host_codex_exec_forbidden") is True,
        "run_id_matches_failed_phase": isinstance(failure.get("task_id"), str)
        and failure["task_id"].startswith(f"{pair_id}-{failed_phase}-core-baseline-"),
        "failed_job_artifacts_absent": _job_artifact_count(
            evolution_db_path, job_id=failed_job_id
        )
        == 0,
        "credential_canary_receipt_not_published": isinstance(credential, dict)
        and "status" not in credential,
    }
    if set(no_effect_predicates.values()) != {True}:
        failed_keys = sorted(
            key for key, value in no_effect_predicates.items() if not value
        )
        raise ValueError(f"partial Reflector no-effect proof failed: {failed_keys}")

    core_completion_root = failed_completion_path.parent.parent
    later_completion_inventory = {
        phase: sorted(
            path.resolve()
            for path in core_completion_root.glob(
                f"task_{pair_id}-{phase}-core*/ses_*.json"
            )
            if path.is_file()
        )
        for phase in replacement_phases[1:]
    }
    if any(later_completion_inventory.values()):
        raise ValueError("later Reflector completion exists after failed sibling")

    prompt_root = run_root / pair_id / "reflector_prompts"
    freeze_receipt = _read_json(prompt_root / "freeze.receipt.json")
    prompt_hashes = freeze_receipt.get("prompt_sha256_by_type")
    if not isinstance(prompt_hashes, dict):
        raise TypeError("frozen Reflector prompt hashes are absent")
    recovered_outputs: list[RecoveredReflectorOutput] = []
    completion_hashes: dict[str, str] = {}
    for phase in ordered_phases[: ordered_phases.index(failed_phase)]:
        completion_path = completed_sibling_paths[phase]
        completion = _read_json(completion_path)
        row = rows_by_method[_REFLECTOR_PHASE_METHODS[phase]]
        traces = completion.get("trajectory", {}).get("traces", [])
        trace = traces[0] if isinstance(traces, list) and len(traces) == 1 else None
        prompt_messages = trace.get("prompt_messages", []) if isinstance(trace, dict) else []
        response_messages = trace.get("response_messages", []) if isinstance(trace, dict) else []
        prompt_text = (
            prompt_messages[0].get("content")
            if len(prompt_messages) == 1 and isinstance(prompt_messages[0], dict)
            else None
        )
        response_text = (
            response_messages[-1].get("content")
            if response_messages and isinstance(response_messages[-1], dict)
            else None
        )
        kind = _PHASE_KINDS[phase]
        frozen_prompt = (prompt_root / f"{kind.value}.md").read_text(encoding="utf-8")
        completion_metadata = completion.get("metadata")
        task_metadata = completion.get("trajectory", {}).get("metadata", {}).get(
            "task_metadata", {}
        )
        method, filename = _PHASE_CONTENT_PATHS[phase]
        output_path = evolution_artifact_root / "workers" / str(row["job_id"]) / method / filename
        content = _strict_reflector_markdown(str(response_text or ""))
        if (
            completion.get("status") != "COMPLETED"
            or completion.get("error") is not None
            or not isinstance(completion.get("task_id"), str)
            or not completion["task_id"].startswith(f"{pair_id}-{phase}-core-baseline-")
            or not isinstance(completion_metadata, dict)
            or completion_metadata.get("execution_route") != CORE_MANAGED_CODEX_ROUTE
            or completion_metadata.get("host_codex_exec_forbidden") is not True
            or task_metadata.get("agent_model_name") != "gpt-5.5"
            or not isinstance(prompt_text, str)
            or not prompt_text.startswith(frozen_prompt.rstrip())
            or canonical_sha256({"prompt": frozen_prompt}) != prompt_hashes.get(kind.value)
            or not output_path.is_file()
            or output_path.read_text(encoding="utf-8") != content.rstrip() + "\n"
        ):
            raise ValueError(f"completed Reflector output authority differs: {phase}")
        recovered_outputs.append(
            RecoveredReflectorOutput(
                artifact_type=kind,
                original_job_id=str(row["job_id"]),
                reflector_run_id=str(completion["task_id"]),
                reflector_attempt_run_ids=[str(completion["task_id"])],
                model="gpt-5.5",
                system_prompt_hash=str(row["config"]["reflector_system_sha256"]),
                prompt_hash=str(prompt_hashes[kind.value]),
                input_evidence_hash=str(evidence_hash),
                content=content,
                content_hash=canonical_sha256({"content": content}),
                generation_time_seconds=_completion_duration_seconds(completion),
            )
        )
        completion_hashes[phase] = file_sha256(completion_path)

    replacement_attempt_ordinals = {
        phase: verified_no_effect_attempt_count(
            claims[phase], pair_id=pair_id, phase=phase
        )
        + 1
        for phase in replacement_phases
    }
    if len(set(replacement_attempt_ordinals.values())) != 1:
        raise ValueError("partial Reflector replacement attempt ordinals diverged")
    batch_attempt_ordinal = next(iter(replacement_attempt_ordinals.values()))
    checkpoint = {
        "schema_version": "chemcrow_partial_reflector_boundary_checkpoint_v1",
        "status": "READY_FOR_PARTIAL_REFLECTOR_RESUME",
        "pair_id": pair_id,
        "task_id": task.task_id,
        "baseline": baseline.model_dump(mode="json"),
        "baseline_internal_evaluation": evaluation.model_dump(mode="json"),
        "input_evidence_sha256": evidence_hash,
        "recovered_outputs": [
            output.model_dump(mode="json") for output in recovered_outputs
        ],
        "replacement_phases": replacement_phases,
    }
    recovery_root = run_root / "recovery" / pair_id
    recovery_root.mkdir(parents=True, exist_ok=True)
    checkpoint_path = recovery_root / (
        _PARTIAL_CHECKPOINT_NAME
        if batch_attempt_ordinal == 1
        else f"partial-reflector-boundary.checkpoint.attempt-{batch_attempt_ordinal}.json"
    )
    checkpoint_bytes = (json.dumps(checkpoint, indent=2, sort_keys=True) + "\n").encode()
    if checkpoint_path.exists() and checkpoint_path.read_bytes() != checkpoint_bytes:
        raise ValueError("existing partial Reflector checkpoint differs")
    checkpoint_path.write_bytes(checkpoint_bytes)

    phase_receipts: dict[str, dict[str, Any]] = {}
    expected_batch_error = f"Core-managed {failed_phase} returned no completed artifact"
    for phase in replacement_phases:
        claim_path = claims_root / f"{phase}.json"
        row = rows_by_method[_REFLECTOR_PHASE_METHODS[phase]]
        dispatched = phase == failed_phase
        predicates = (
            no_effect_predicates
            if dispatched
            else {
                "prior_sequential_phase_failed_before_return": True,
                "matching_core_completion_absent": not later_completion_inventory[phase],
                "core_job_batch_aborted": row["state"] == "failed"
                and row["error"] == expected_batch_error,
                "artifact_registration_absent": _job_artifact_count(
                    evolution_db_path, job_id=str(row["job_id"])
                )
                == 0,
                "sibling_output_absent": True,
            }
        )
        attempt_ordinal = replacement_attempt_ordinals[phase]
        phase_receipts[phase] = {
            "schema_version": "chemcrow_reflector_no_effect_recovery_v1",
            "status": "VERIFIED_NO_CANDIDATE_EFFECT_REPLACEMENT_READY",
            "pair_id": pair_id,
            "task_id": task.task_id,
            "phase": phase,
            "attempt_ordinal": attempt_ordinal,
            "authority_sha256": claims[phase]["authority_sha256"],
            "original_claim_sha256": hashlib.sha256(claim_path.read_bytes()).hexdigest(),
            "input_evidence_sha256": evidence_hash,
            "dispatch_started": dispatched,
            "failed_job_id": str(row["job_id"]),
            "core_completion_sha256": (
                file_sha256(failed_completion_path) if dispatched else None
            ),
            "failure_kind": (
                "credential_isolation_validation_failed_before_reflector_process_start"
                if dispatched
                else "not_dispatched_after_prior_sibling_failure"
            ),
            "no_effect_predicates": predicates,
            "infrastructure_canary_model_call_may_have_occurred": dispatched,
            "reflector_model_call_proven_absent": True,
            "duplicate_scientific_call": False,
            "recorded_before_replacement_dispatch": True,
        }
        VerifiedReplacementPhaseLedger(
            ledger_root, pair_id=pair_id
        ).reconcile_verified_no_effect_failure(phase, phase_receipts[phase])
    recovery_receipt = {
        "schema_version": "chemcrow_partial_reflector_boundary_recovery_v1",
        "status": "VERIFIED_PARTIAL_REFLECTOR_RESUME_READY",
        "pair_id": pair_id,
        "task_id": task.task_id,
        "config_sha256": file_sha256(config_path),
        "checkpoint_sha256": file_sha256(checkpoint_path),
        "evidence_event_sha256": file_sha256(evidence_event_path),
        "failed_completion_sha256": file_sha256(failed_completion_path),
        "completed_sibling_completion_sha256": completion_hashes,
        "failed_phase": failed_phase,
        "failed_job_id": failed_job_id,
        "preserved_model_call_phases": sorted(expected_completed),
        "replacement_phases": replacement_phases,
        "baseline_redispatched": False,
        "baseline_evaluator_redispatched": False,
        "completed_sibling_model_calls_redispatched": False,
        "model_calls_during_reconciliation": 0,
        "phase_receipt_sha256": {
            phase: canonical_sha256(receipt)
            for phase, receipt in sorted(phase_receipts.items())
        },
    }
    recovery_path = recovery_root / (
        _PARTIAL_RECEIPT_NAME
        if batch_attempt_ordinal == 1
        else f"partial-reflector-boundary.recovery.attempt-{batch_attempt_ordinal}.json"
    )
    recovery_bytes = (json.dumps(recovery_receipt, indent=2, sort_keys=True) + "\n").encode()
    if recovery_path.exists() and recovery_path.read_bytes() != recovery_bytes:
        raise ValueError("existing partial Reflector recovery receipt differs")
    recovery_path.write_bytes(recovery_bytes)
    return checkpoint_path, recovery_path, recovery_receipt


def reconcile_paused_reflector_batch(
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
    evidence_event_path: Path,
    core_completion_root: Path,
    completed_sibling_paths: dict[str, Path],
) -> tuple[Path, Path, dict[str, Any]]:
    """Recover a deliberately stopped batch without replaying its completed prefix."""

    ordered_phases = list(_REFLECTOR_PHASE_METHODS)
    completed_phases = list(completed_sibling_paths)
    if not completed_phases or completed_phases != ordered_phases[: len(completed_phases)]:
        raise ValueError("paused Reflector completions must be one ordered non-empty prefix")
    replacement_phases = ordered_phases[len(completed_phases) :]
    pair_id = f"{experiment_id}--{task.task_id}"
    if (run_root / pair_id / "pair.result.json").exists() or (
        run_root / pair_id / "artifact.study.result.json"
    ).exists():
        raise ValueError("sealed pair cannot be reflector-recovered")
    claims_root = ledger_root / pair_id
    claims = {path.stem: _read_json(path) for path in claims_root.glob("*.json")}
    if set(claims) != _PHASES:
        raise ValueError("paused Reflector recovery phase inventory differs")
    if any(
        claims[phase].get("status") != "terminal"
        for phase in ("baseline_candidate", "baseline_internal_evaluator")
    ) or any(claims[phase].get("status") != "claimed" for phase in ordered_phases):
        raise ValueError("paused Reflector recovery claim states differ")

    evidence_event = _read_json(evidence_event_path)
    session_result = evidence_event.get("payload", {}).get("session_result", {})
    event_metadata = (
        session_result.get("metadata", {}) if isinstance(session_result, dict) else {}
    )
    evidence = event_metadata.get("input_evidence")
    evidence_hash = event_metadata.get("input_evidence_sha256")
    if not isinstance(evidence, dict) or canonical_sha256(evidence) != evidence_hash:
        raise ValueError("paused Reflector evidence event hash differs")
    if evidence.get("task") != {
        "task_id": task.task_id,
        "prompt": task.prompt,
        "sanitized_item_sha256": task.sanitized_item_sha256,
    }:
        raise ValueError("paused Reflector task authority differs")
    if {
        claims[phase].get("authority", {}).get("input_evidence_hash")
        for phase in ordered_phases
    } != {evidence_hash}:
        raise ValueError("paused Reflector claims do not share frozen evidence")
    baseline = Trajectory.model_validate(evidence.get("baseline"))
    evaluation = EvaluatorFeedback.model_validate(
        evidence.get("feedback", {}).get("evaluator_feedback")
    )
    if (
        baseline.task_id != task.task_id
        or baseline.role != "baseline"
        or baseline.status != "COMPLETED"
        or baseline.artifact_ids
        or baseline.candidate_config_sha256 != expected_candidate_config_sha256
        or not _baseline_evidence_matches_claims(
            baseline=baseline,
            baseline_claim=claims["baseline_candidate"],
            evaluator_claim=claims["baseline_internal_evaluator"],
        )
        or evaluation.evaluator_role != "evolution_evaluator"
        or claims["baseline_internal_evaluator"].get("authority", {}).get(
            "evaluator_id"
        )
        != expected_evaluator_id
        or claims["baseline_internal_evaluator"].get("receipt", {}).get(
            "evaluator_run_id"
        )
        != evaluation.evaluator_run_id
        or claims["baseline_internal_evaluator"].get("receipt", {}).get(
            "feedback_sha256"
        )
        != canonical_sha256(evaluation.model_dump(mode="json"))
    ):
        raise ValueError("paused Reflector recovered G1 authority differs")

    job_rows = _job_rows(evolution_db_path, evidence_hash=str(evidence_hash))
    if (
        len(job_rows) != 3
        or {row["method"] for row in job_rows}
        != set(_REFLECTOR_PHASE_METHODS.values())
        or any(row["state"] != "running" for row in job_rows)
        or any(not isinstance(row["lease_id"], str) for row in job_rows)
        or any(
            _job_artifact_count(evolution_db_path, job_id=str(row["job_id"]))
            for row in job_rows
        )
    ):
        raise ValueError("paused Reflector Core job inventory differs")
    rows_by_method = {str(row["method"]): row for row in job_rows}

    completion_inventory: dict[str, list[Path]] = {}
    for phase in ordered_phases:
        prefix = f"task_{pair_id}-{phase}-core"
        completion_inventory[phase] = sorted(
            path.resolve()
            for path in core_completion_root.glob(f"{prefix}*/ses_*.json")
            if path.is_file()
        )
    expected_completion_inventory = {
        phase: [completed_sibling_paths[phase].resolve()]
        if phase in completed_sibling_paths
        else []
        for phase in ordered_phases
    }
    if completion_inventory != expected_completion_inventory:
        raise ValueError("paused Reflector Core completion inventory differs")

    prompt_root = run_root / pair_id / "reflector_prompts"
    freeze_receipt = _read_json(prompt_root / "freeze.receipt.json")
    prompt_hashes = freeze_receipt.get("prompt_sha256_by_type")
    if not isinstance(prompt_hashes, dict):
        raise TypeError("frozen Reflector prompt hashes are absent")
    recovered_outputs: list[RecoveredReflectorOutput] = []
    completion_hashes: dict[str, str] = {}
    for phase in completed_phases:
        completion_path = completed_sibling_paths[phase].resolve()
        completion = _read_json(completion_path)
        row = rows_by_method[_REFLECTOR_PHASE_METHODS[phase]]
        traces = completion.get("trajectory", {}).get("traces", [])
        trace = traces[0] if isinstance(traces, list) and len(traces) == 1 else None
        prompt_messages = trace.get("prompt_messages", []) if isinstance(trace, dict) else []
        response_messages = (
            trace.get("response_messages", []) if isinstance(trace, dict) else []
        )
        prompt_text = (
            prompt_messages[0].get("content")
            if len(prompt_messages) == 1 and isinstance(prompt_messages[0], dict)
            else None
        )
        response_text = (
            response_messages[-1].get("content")
            if response_messages and isinstance(response_messages[-1], dict)
            else None
        )
        kind = _PHASE_KINDS[phase]
        frozen_prompt = (prompt_root / f"{kind.value}.md").read_text(encoding="utf-8")
        completion_metadata = completion.get("metadata")
        task_metadata = completion.get("trajectory", {}).get("metadata", {}).get(
            "task_metadata", {}
        )
        content = _strict_reflector_markdown(str(response_text or ""))
        system_prompt_hash = str(row["config"].get("reflector_system_sha256", ""))
        if (
            completion.get("status") != "COMPLETED"
            or completion.get("error") is not None
            or not isinstance(completion.get("task_id"), str)
            or not completion["task_id"].startswith(f"{pair_id}-{phase}-core-baseline-")
            or not isinstance(completion_metadata, dict)
            or completion_metadata.get("execution_route") != CORE_MANAGED_CODEX_ROUTE
            or completion_metadata.get("host_codex_exec_forbidden") is not True
            or task_metadata.get("agent_model_name") != "gpt-5.5"
            or not isinstance(prompt_text, str)
            or not prompt_text.startswith(frozen_prompt.rstrip())
            or canonical_sha256({"prompt": frozen_prompt}) != prompt_hashes.get(kind.value)
            or system_prompt_hash != _core_reflector_contract_hash(kind, "core_full_worker_v1")
        ):
            raise ValueError(f"paused completed Reflector authority differs: {phase}")
        recovered_outputs.append(
            RecoveredReflectorOutput(
                artifact_type=kind,
                original_job_id=str(row["job_id"]),
                reflector_run_id=str(completion["task_id"]),
                reflector_attempt_run_ids=[str(completion["task_id"])],
                model="gpt-5.5",
                system_prompt_hash=system_prompt_hash,
                prompt_hash=str(prompt_hashes[kind.value]),
                input_evidence_hash=str(evidence_hash),
                content=content,
                content_hash=canonical_sha256({"content": content}),
                generation_time_seconds=_completion_duration_seconds(completion),
            )
        )
        completion_hashes[phase] = file_sha256(completion_path)

    original_job_state = {
        phase: {
            "job_id": str(rows_by_method[method]["job_id"]),
            "state": str(rows_by_method[method]["state"]),
            "lease_id_sha256": canonical_sha256(
                {"lease_id": str(rows_by_method[method]["lease_id"])}
            ),
            "completion_count": len(completion_inventory[phase]),
            "artifact_count": _job_artifact_count(
                evolution_db_path, job_id=str(rows_by_method[method]["job_id"])
            ),
        }
        for phase, method in _REFLECTOR_PHASE_METHODS.items()
    }
    for phase, method in _REFLECTOR_PHASE_METHODS.items():
        row = rows_by_method[method]
        _fail_paused_batch_job(
            backend_url=backend_url,
            db_path=evolution_db_path,
            job_id=str(row["job_id"]),
            lease_id=str(row["lease_id"]),
            reason=(
                "completed output preserved before registration"
                if phase in completed_sibling_paths
                else "stopped before model dispatch"
            ),
        )

    checkpoint = {
        "schema_version": "chemcrow_partial_reflector_boundary_checkpoint_v1",
        "status": "READY_FOR_PARTIAL_REFLECTOR_RESUME",
        "pair_id": pair_id,
        "task_id": task.task_id,
        "baseline": baseline.model_dump(mode="json"),
        "baseline_internal_evaluation": evaluation.model_dump(mode="json"),
        "input_evidence_sha256": evidence_hash,
        "recovered_outputs": [
            output.model_dump(mode="json") for output in recovered_outputs
        ],
        "replacement_phases": replacement_phases,
    }
    recovery_root = run_root / "recovery" / pair_id
    recovery_root.mkdir(parents=True, exist_ok=True)
    checkpoint_path = recovery_root / _PARTIAL_CHECKPOINT_NAME
    checkpoint_bytes = (json.dumps(checkpoint, indent=2, sort_keys=True) + "\n").encode()
    if checkpoint_path.exists() and checkpoint_path.read_bytes() != checkpoint_bytes:
        raise ValueError("existing paused Reflector checkpoint differs")
    checkpoint_path.write_bytes(checkpoint_bytes)

    phase_receipts: dict[str, dict[str, Any]] = {}
    for phase in replacement_phases:
        claim_path = claims_root / f"{phase}.json"
        attempt_ordinal = (
            verified_no_effect_attempt_count(
                claims[phase], pair_id=pair_id, phase=phase
            )
            + 1
        )
        row = rows_by_method[_REFLECTOR_PHASE_METHODS[phase]]
        phase_receipts[phase] = {
            "schema_version": "chemcrow_reflector_no_effect_recovery_v1",
            "status": "VERIFIED_NO_CANDIDATE_EFFECT_REPLACEMENT_READY",
            "pair_id": pair_id,
            "task_id": task.task_id,
            "phase": phase,
            "attempt_ordinal": attempt_ordinal,
            "authority_sha256": claims[phase]["authority_sha256"],
            "original_claim_sha256": hashlib.sha256(claim_path.read_bytes()).hexdigest(),
            "input_evidence_sha256": evidence_hash,
            "dispatch_started": False,
            "failed_job_id": str(row["job_id"]),
            "core_completion_sha256": None,
            "failure_kind": "operator_pause_before_reflector_model_dispatch",
            "no_effect_predicates": {
                "core_completion_absent": not completion_inventory[phase],
                "core_job_had_no_registered_artifacts": (
                    original_job_state[phase]["artifact_count"] == 0
                ),
                "core_job_was_claimed_not_completed": (
                    original_job_state[phase]["state"] == "running"
                ),
                "completed_prefix_precedes_phase": all(
                    ordered_phases.index(completed) < ordered_phases.index(phase)
                    for completed in completed_phases
                ),
                "frozen_prompt_exists": (
                    prompt_root / f"{_PHASE_KINDS[phase].value}.md"
                ).is_file(),
            },
            "infrastructure_canary_model_call_may_have_occurred": False,
            "reflector_model_call_proven_absent": True,
            "duplicate_scientific_call": False,
            "recorded_before_replacement_dispatch": True,
        }
        VerifiedReplacementPhaseLedger(
            ledger_root, pair_id=pair_id
        ).reconcile_verified_no_effect_failure(phase, phase_receipts[phase])

    recovery_receipt = {
        "schema_version": "chemcrow_partial_reflector_boundary_recovery_v1",
        "status": "VERIFIED_PARTIAL_REFLECTOR_RESUME_READY",
        "recovery_mode": "operator_paused_after_completed_prefix",
        "pair_id": pair_id,
        "task_id": task.task_id,
        "config_sha256": file_sha256(config_path),
        "checkpoint_sha256": file_sha256(checkpoint_path),
        "evidence_event_sha256": file_sha256(evidence_event_path),
        "completed_sibling_completion_sha256": completion_hashes,
        "original_job_state": original_job_state,
        "preserved_model_call_phases": completed_phases,
        "replacement_phases": replacement_phases,
        "phase_receipt_sha256": {
            phase: canonical_sha256(receipt)
            for phase, receipt in sorted(phase_receipts.items())
        },
        "baseline_redispatched": False,
        "baseline_evaluator_redispatched": False,
        "completed_sibling_model_calls_redispatched": False,
        "model_calls_during_reconciliation": 0,
    }
    recovery_path = recovery_root / _PARTIAL_RECEIPT_NAME
    recovery_bytes = (json.dumps(recovery_receipt, indent=2, sort_keys=True) + "\n").encode()
    if recovery_path.exists() and recovery_path.read_bytes() != recovery_bytes:
        raise ValueError("existing paused Reflector recovery receipt differs")
    recovery_path.write_bytes(recovery_bytes)
    return checkpoint_path, recovery_path, recovery_receipt


def _latest_partial_reflector_recovery_paths(
    recovery_root: Path,
) -> tuple[Path, Path] | None:
    path_pairs: dict[int, tuple[Path, Path]] = {}
    base_checkpoint = recovery_root / _PARTIAL_CHECKPOINT_NAME
    base_receipt = recovery_root / _PARTIAL_RECEIPT_NAME
    if base_checkpoint.exists() or base_receipt.exists():
        if not base_checkpoint.is_file() or not base_receipt.is_file():
            raise ValueError("partial Reflector recovery evidence is incomplete")
        path_pairs[1] = (base_checkpoint, base_receipt)
    checkpoint_prefix = "partial-reflector-boundary.checkpoint.attempt-"
    receipt_prefix = "partial-reflector-boundary.recovery.attempt-"
    indexed_checkpoints: dict[int, Path] = {}
    indexed_receipts: dict[int, Path] = {}
    for path in recovery_root.glob(f"{checkpoint_prefix}*.json"):
        suffix = path.name.removeprefix(checkpoint_prefix).removesuffix(".json")
        if not suffix.isdigit() or int(suffix) < 2:
            raise ValueError("partial Reflector checkpoint attempt name is invalid")
        indexed_checkpoints[int(suffix)] = path
    for path in recovery_root.glob(f"{receipt_prefix}*.json"):
        suffix = path.name.removeprefix(receipt_prefix).removesuffix(".json")
        if not suffix.isdigit() or int(suffix) < 2:
            raise ValueError("partial Reflector receipt attempt name is invalid")
        indexed_receipts[int(suffix)] = path
    if set(indexed_checkpoints) != set(indexed_receipts):
        raise ValueError("partial Reflector indexed recovery evidence is incomplete")
    path_pairs.update(
        {
            ordinal: (indexed_checkpoints[ordinal], indexed_receipts[ordinal])
            for ordinal in indexed_checkpoints
        }
    )
    if not path_pairs:
        return None
    if set(path_pairs) != set(range(1, max(path_pairs) + 1)):
        raise ValueError("partial Reflector recovery attempts are not contiguous")
    return path_pairs[max(path_pairs)]


def load_partial_reflector_boundary_checkpoint(
    *,
    run_root: Path,
    ledger_root: Path,
    pair_id: str,
    task: TaskItem,
    expected_candidate_config_sha256: str,
    expected_evaluator_id: str,
) -> tuple[
    Trajectory,
    EvaluatorFeedback,
    frozenset[str],
    dict[ArtifactKind, RecoveredReflectorOutput],
] | None:
    recovery_root = run_root / "recovery" / pair_id
    recovery_paths = _latest_partial_reflector_recovery_paths(recovery_root)
    if recovery_paths is None:
        return None
    checkpoint_path, recovery_path = recovery_paths
    checkpoint = _read_json(checkpoint_path)
    receipt = _read_json(recovery_path)
    if (
        checkpoint.get("schema_version")
        != "chemcrow_partial_reflector_boundary_checkpoint_v1"
        or checkpoint.get("status") != "READY_FOR_PARTIAL_REFLECTOR_RESUME"
        or checkpoint.get("pair_id") != pair_id
        or checkpoint.get("task_id") != task.task_id
        or receipt.get("status") != "VERIFIED_PARTIAL_REFLECTOR_RESUME_READY"
        or receipt.get("checkpoint_sha256") != file_sha256(checkpoint_path)
        or receipt.get("baseline_redispatched") is not False
        or receipt.get("baseline_evaluator_redispatched") is not False
        or receipt.get("completed_sibling_model_calls_redispatched") is not False
        or receipt.get("model_calls_during_reconciliation") != 0
    ):
        raise ValueError("partial Reflector recovery authority differs")
    claims = {path.stem: _read_json(path) for path in (ledger_root / pair_id).glob("*.json")}
    replacement_phases = frozenset(checkpoint.get("replacement_phases", []))
    outputs = [
        RecoveredReflectorOutput.model_validate(item)
        for item in checkpoint.get("recovered_outputs", [])
    ]
    output_map = {item.artifact_type: item for item in outputs}
    recovered_phases = {_PHASE_NAMES_BY_KIND[kind] for kind in output_map}
    if (
        set(claims) != _PHASES
        or any(
            claims[phase].get("status") != "terminal"
            for phase in ("baseline_candidate", "baseline_internal_evaluator")
        )
        or any(claims[phase].get("status") != "claimed" for phase in recovered_phases)
        or any(
            not VerifiedReplacementPhaseLedger(
                ledger_root, pair_id=pair_id
            ).replacement_ready(phase)
            for phase in replacement_phases
        )
        or recovered_phases | set(replacement_phases) != set(_REFLECTOR_PHASE_METHODS)
    ):
        raise ValueError("partial Reflector recovery claim states differ")
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
        or claims["baseline_internal_evaluator"].get("authority", {}).get(
            "evaluator_id"
        )
        != expected_evaluator_id
    ):
        raise ValueError("partial Reflector recovery semantic authority differs")
    return baseline, evaluation, replacement_phases, output_map


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
        checkpoint.get("status") != "READY_FOR_REFLECTOR_REPLACEMENT_WITHOUT_G1_REDISPATCH"
        or checkpoint.get("pair_id") != pair_id
        or checkpoint.get("task_id") != task.task_id
        or receipt.get("status") != "VERIFIED_REFLECTOR_BOUNDARY_REPLACEMENT_READY"
        or receipt.get("checkpoint_sha256") != file_sha256(checkpoint_path)
        or receipt.get("baseline_redispatched") is not False
        or receipt.get("baseline_evaluator_redispatched") is not False
        or receipt.get("model_calls_during_reconciliation") != 0
    ):
        raise ValueError("Reflector recovery checkpoint authority differs")
    claims = {path.stem: _read_json(path) for path in (ledger_root / pair_id).glob("*.json")}
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
        if verified_no_effect_attempt_count(claims[phase], pair_id=pair_id, phase=phase) < 1:
            raise ValueError("Reflector replacement attempt evidence differs")
    baseline = Trajectory.model_validate(checkpoint.get("baseline"))
    evaluation = EvaluatorFeedback.model_validate(checkpoint.get("baseline_internal_evaluation"))
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
    "load_partial_reflector_boundary_checkpoint",
    "load_reflector_boundary_checkpoint",
    "reconcile_partial_reflector_batch_failure",
    "reconcile_paused_reflector_batch",
    "reconcile_reflector_no_effect_failure",
]
