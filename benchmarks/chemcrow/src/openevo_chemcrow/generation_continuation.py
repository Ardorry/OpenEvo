"""Read-only authority for continuing a sealed generation-only run into G2."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .feedback import feedback_hash
from .hashing import canonical_sha256, file_sha256
from .models import ArtifactKind, EvaluatorFeedback, TaskItem, Trajectory
from .replacement_ledger import verified_no_effect_attempt_count
from .three_artifact_models import (
    THREE_ARTIFACT_ORDER,
    ThreeArtifactBundleReceipt,
    ThreeArtifactGenerationResult,
)

_SOURCE_PHASES = (
    "baseline_candidate",
    "baseline_internal_evaluator",
    "reflector_memory",
    "reflector_skill_bundle",
    "reflector_agent_system",
)
_CONTENT_NAMES = {
    ArtifactKind.TEXT_MEMORY: "memory.md",
    ArtifactKind.SKILL_BUNDLE: "SKILL.md",
    ArtifactKind.AGENT_SYSTEM: "AGENTS.md",
}


def _regular_bytes(path: Path, *, allowed_root: Path) -> bytes:
    root = Path(os.path.abspath(allowed_root))
    target = Path(os.path.abspath(path))
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"sealed continuation source escaped its root: {target}") from exc
    before = target.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
    ):
        raise ValueError(f"sealed continuation source is not a regular file: {target}")
    payload = target.read_bytes()
    after = target.lstat()
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if identity_before != identity_after or len(payload) != before.st_size:
        raise ValueError(f"sealed continuation source changed while reading: {target}")
    return payload


def _regular_json(path: Path, *, allowed_root: Path) -> tuple[dict[str, Any], str]:
    raw = _regular_bytes(path, allowed_root=allowed_root)
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"sealed continuation JSON is not an object: {path}")
    return value, hashlib.sha256(raw).hexdigest()


def _artifact_content_path(*, uri: str, kind: ArtifactKind) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise ValueError("sealed generation artifact URI is not a local file URI")
    root = Path(unquote(parsed.path))
    return root / _CONTENT_NAMES[kind] if kind is ArtifactKind.SKILL_BUNDLE else root


def _load_artifact_rows(db_path: Path, artifact_ids: list[str]) -> dict[str, dict[str, Any]]:
    before = db_path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
    ):
        raise ValueError("sealed generation Core database is not a regular file")
    placeholders = ",".join("?" for _ in artifact_ids)
    uri = f"file:{Path(os.path.abspath(db_path))}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        rows = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT artifact_id, type, state, uri, manifest_path, manifest_json,
                       staging_job_id
                FROM artifacts
                WHERE artifact_id IN ({placeholders})
                """,
                artifact_ids,
            )
        ]
    after = db_path.lstat()
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if before_identity != after_identity:
        raise ValueError("sealed generation Core database changed while reading")
    return {str(row["artifact_id"]): row for row in rows}


def _validate_terminal_claim(path: Path, *, phase: str) -> tuple[dict[str, Any], str]:
    payload, digest = _regular_json(path, allowed_root=path.parent)
    authority = payload.get("authority")
    receipt = payload.get("receipt")
    if (
        payload.get("phase") != phase
        or payload.get("status") != "terminal"
        or not isinstance(authority, dict)
        or payload.get("authority_sha256") != canonical_sha256(authority)
        or not isinstance(receipt, dict)
        or payload.get("receipt_sha256") != canonical_sha256(receipt)
    ):
        raise ValueError(f"sealed generation phase claim differs: {phase}")
    return payload, digest


def _baseline_claim_binds_current_trajectory(
    *,
    baseline_claim: dict[str, Any],
    evaluator_claim: dict[str, Any],
    baseline_sha256: str,
    pair_id: str,
) -> bool:
    original_sha256 = baseline_claim["receipt"].get("trajectory_sha256")
    if original_sha256 == baseline_sha256:
        return True
    attempts = evaluator_claim.get("failed_attempts")
    verified_no_effect_attempt_count(
        evaluator_claim,
        pair_id=pair_id,
        phase="baseline_internal_evaluator",
    )
    return bool(
        isinstance(attempts, list)
        and any(
            isinstance(attempt, dict)
            and isinstance(attempt.get("receipt"), dict)
            and attempt["receipt"].get("schema_version")
            == "chemcrow_baseline_evaluator_no_effect_recovery_v1"
            and attempt["receipt"].get("baseline_original_trajectory_sha256")
            == original_sha256
            and attempt["receipt"].get("baseline_reconstructed_trajectory_sha256")
            == baseline_sha256
            and attempt["receipt"].get("candidate_redispatched") is False
            and attempt["receipt"].get("evaluator_model_call_proven_absent") is True
            and attempt["receipt"].get("duplicate_scientific_call") is False
            for attempt in attempts
        )
    )


def load_sealed_generation_continuation(
    *,
    source_run_root: Path,
    source_ledger_root: Path,
    source_completed_run_audit: Path,
    expected_source_audit_sha256: str,
    expected_source_aggregate_sha256: str,
    source_experiment_id: str,
    task: TaskItem,
    pair_id: str,
    expected_s0_hash: str,
    expected_evaluator_id: str,
    evolution_db_path: Path,
    evolution_artifact_root: Path,
    continuation_run_root: Path,
) -> tuple[Trajectory, EvaluatorFeedback, ThreeArtifactBundleReceipt, dict[str, Any]]:
    """Verify one immutable generation boundary and prepare a no-call G2 checkpoint."""

    audit, audit_sha256 = _regular_json(
        source_completed_run_audit,
        allowed_root=source_completed_run_audit.parent,
    )
    aggregate_path = source_run_root / "aggregate.json"
    if (
        audit_sha256 != expected_source_audit_sha256
        or audit.get("status") != "PASS"
        or audit.get("schema_version")
        != "chemcrow_three_artifact_generation_completed_run_audit_v1"
        or audit.get("execution_scope") != "g1_and_artifact_generation_only"
        or audit.get("experiment_id") != source_experiment_id
        or task.task_id not in audit.get("task_ids", [])
        or audit.get("g2_candidate_call_count") != 0
        or audit.get("g2_internal_evaluator_call_count") != 0
        or audit.get("final_evaluator_call_count") != 0
        or audit.get("paper_evaluator_call_count") != 0
        or audit.get("aggregate_sha256") != expected_source_aggregate_sha256
        or file_sha256(aggregate_path) != expected_source_aggregate_sha256
    ):
        raise ValueError("sealed generation completed-run audit differs")

    item_root = source_run_root / pair_id
    result_path = item_root / "artifact.study.result.json"
    baseline_path = item_root / "baseline.trajectory.json"
    feedback_path = item_root / "feedback.json"
    artifacts_path = item_root / "artifacts.receipt.json"
    boundary_path = item_root / "generation.boundary.receipt.json"
    reset_path = item_root / "reset.receipt.json"
    result_raw = _regular_bytes(result_path, allowed_root=source_run_root)
    result = ThreeArtifactGenerationResult.model_validate_json(result_raw)
    baseline = Trajectory.model_validate_json(
        _regular_bytes(baseline_path, allowed_root=source_run_root)
    )
    feedback_payload, feedback_file_sha256 = _regular_json(
        feedback_path, allowed_root=source_run_root
    )
    bundle = ThreeArtifactBundleReceipt.model_validate_json(
        _regular_bytes(artifacts_path, allowed_root=source_run_root)
    )
    boundary, boundary_sha256 = _regular_json(boundary_path, allowed_root=source_run_root)
    _, reset_sha256 = _regular_json(reset_path, allowed_root=source_run_root)
    if (
        result.task_id != task.task_id
        or result.pair_id != pair_id
        or result.s0_hash != expected_s0_hash
        or result.baseline != baseline
        or result.baseline_internal_evaluation.evaluator_role != "evolution_evaluator"
        or result.feedback_hash != feedback_hash(feedback_payload)
        or result.artifact_bundle != bundle
        or result.reset_receipt_sha256 != reset_sha256
        or bundle.prompt_profile != "core_full_worker_v1"
        or [item.artifact_type for item in bundle.artifacts]
        != list(THREE_ARTIFACT_ORDER)
        or boundary.get("status") != "G1_AND_ARTIFACTS_SEALED"
        or boundary.get("task_id") != task.task_id
        or boundary.get("pair_id") != pair_id
        or boundary.get("baseline_trajectory_sha256") != file_sha256(baseline_path)
        or boundary.get("feedback_sha256") != feedback_file_sha256
        or boundary.get("artifact_bundle_sha256") != file_sha256(artifacts_path)
        or boundary.get("artifact_ids_by_type") != bundle.artifact_id_by_type()
        or boundary.get("g2_dispatched") is not False
        or boundary.get("g2_evaluator_dispatched") is not False
        or boundary.get("final_evaluator_dispatched") is not False
    ):
        raise ValueError("sealed generation item boundary differs")

    claim_root = source_ledger_root / pair_id
    claim_paths = sorted(claim_root.glob("*.json"))
    if {path.stem for path in claim_paths} != set(_SOURCE_PHASES):
        raise ValueError("sealed generation source ledger has unexpected phases")
    claims: dict[str, dict[str, Any]] = {}
    claim_hashes: dict[str, str] = {}
    for phase in _SOURCE_PHASES:
        claims[phase], claim_hashes[phase] = _validate_terminal_claim(
            claim_root / f"{phase}.json", phase=phase
        )
    baseline_claim = claims["baseline_candidate"]
    evaluator_claim = claims["baseline_internal_evaluator"]
    evaluation = result.baseline_internal_evaluation
    baseline_sha256 = canonical_sha256(baseline.model_dump(mode="json"))
    if (
        baseline_claim["receipt"].get("run_id") != baseline.run_id
        or baseline_claim["authority"].get("task_id") != task.task_id
        or baseline_claim["authority"].get("s0_hash") != expected_s0_hash
        or baseline_claim["authority"].get("artifact_ids") != []
        or baseline_claim["authority"].get("artifact_inventory") != {}
        or not _baseline_claim_binds_current_trajectory(
            baseline_claim=baseline_claim,
            evaluator_claim=evaluator_claim,
            baseline_sha256=baseline_sha256,
            pair_id=pair_id,
        )
        or baseline_claim["receipt"].get("runtime_context") != "bare_s0"
        or evaluator_claim["authority"].get("task_id") != task.task_id
        or evaluator_claim["authority"].get("run_id") != baseline.run_id
        or evaluator_claim["authority"].get("evaluator_id") != expected_evaluator_id
        or evaluator_claim["authority"].get("paper_evaluator") is not False
        or evaluator_claim["receipt"].get("evaluator_run_id") != evaluation.evaluator_run_id
        or evaluator_claim["receipt"].get("feedback_sha256")
        != canonical_sha256(evaluation.model_dump(mode="json"))
    ):
        raise ValueError("sealed generation G1/evaluator ledger binding differs")

    artifact_ids = bundle.artifact_ids()
    rows = _load_artifact_rows(evolution_db_path, artifact_ids)
    if set(rows) != set(artifact_ids):
        raise ValueError("sealed generation artifacts are absent from the Core store")
    artifact_authority: dict[str, Any] = {}
    phase_by_kind = {
        ArtifactKind.TEXT_MEMORY: "reflector_memory",
        ArtifactKind.SKILL_BUNDLE: "reflector_skill_bundle",
        ArtifactKind.AGENT_SYSTEM: "reflector_agent_system",
    }
    for receipt in bundle.artifacts:
        row = rows[receipt.artifact_id]
        phase = phase_by_kind[receipt.artifact_type]
        claim = claims[phase]
        manifest = json.loads(str(row["manifest_json"]))
        manifest_path = Path(str(row["manifest_path"]))
        manifest_payload, manifest_sha256 = _regular_json(
            manifest_path, allowed_root=evolution_artifact_root
        )
        content_path = _artifact_content_path(
            uri=str(row["uri"]), kind=receipt.artifact_type
        )
        content = _regular_bytes(content_path, allowed_root=evolution_artifact_root)
        if (
            row.get("type") != receipt.artifact_type.value
            or row.get("state") != "active"
            or row.get("staging_job_id") is not None
            or hashlib.sha256(content).hexdigest() != receipt.artifact_hash
            or len(content) != receipt.size_bytes
            or manifest.get("task_id") != task.task_id
            or manifest.get("pair_id") != pair_id
            or manifest.get("parent_run_id") != baseline.run_id
            or manifest.get("reflector_job_id") != receipt.reflector_job_id
            or manifest.get("reflector_run_id") != receipt.reflector_run_id
            or manifest.get("reflector_prompt_sha256") != receipt.prompt_hash
            or manifest.get("reflector_system_sha256") != receipt.system_prompt_hash
            or manifest.get("reflector_prompt_profile") != bundle.prompt_profile
            or manifest.get("input_evidence_sha256") != bundle.input_evidence_hash
            or manifest_payload.get("artifact_id") != receipt.artifact_id
            or manifest_payload.get("manifest") != manifest
            or claim["authority"].get("task_id") != task.task_id
            or claim["authority"].get("pair_id") != pair_id
            or claim["authority"].get("parent_run_id") != baseline.run_id
            or claim["authority"].get("artifact_type")
            != receipt.artifact_type.value
            or claim["authority"].get("input_evidence_hash")
            != bundle.input_evidence_hash
            or claim["authority"].get("sibling_artifact_ids") != []
            or claim["authority"].get("paper_evaluator_feedback_included") is not False
            or claim["receipt"].get("artifact_id") != receipt.artifact_id
            or claim["receipt"].get("artifact_hash") != receipt.artifact_hash
            or claim["receipt"].get("reflector_job_id") != receipt.reflector_job_id
            or claim["receipt"].get("reflector_run_id") != receipt.reflector_run_id
            or claim["receipt"].get("prompt_hash") != receipt.prompt_hash
            or claim["receipt"].get("registration_receipt_sha256")
            != receipt.registration_receipt_sha256
        ):
            raise ValueError(
                f"sealed generation artifact authority differs: {receipt.artifact_type.value}"
            )
        artifact_authority[receipt.artifact_type.value] = {
            "artifact_id": receipt.artifact_id,
            "artifact_sha256": receipt.artifact_hash,
            "manifest_sha256": manifest_sha256,
            "reflector_job_id": receipt.reflector_job_id,
            "reflector_run_id": receipt.reflector_run_id,
            "reflector_prompt_sha256": receipt.prompt_hash,
        }

    receipt_payload = {
        "schema_version": "chemcrow_sealed_generation_g2_continuation_v1",
        "status": "READY_FOR_FRESH_G2",
        "model_calls": 0,
        "task_id": task.task_id,
        "pair_id": pair_id,
        "source_experiment_id": source_experiment_id,
        "source_completed_run_audit_sha256": audit_sha256,
        "source_aggregate_sha256": expected_source_aggregate_sha256,
        "source_generation_result_sha256": hashlib.sha256(result_raw).hexdigest(),
        "source_generation_boundary_sha256": boundary_sha256,
        "source_reset_receipt_sha256": reset_sha256,
        "source_phase_claim_sha256_by_phase": claim_hashes,
        "baseline_run_id": baseline.run_id,
        "baseline_trajectory_sha256": canonical_sha256(
            baseline.model_dump(mode="json")
        ),
        "baseline_evaluator_run_id": evaluation.evaluator_run_id,
        "baseline_evaluation_sha256": canonical_sha256(
            evaluation.model_dump(mode="json")
        ),
        "input_evidence_sha256": bundle.input_evidence_hash,
        "artifact_authority_by_type": artifact_authority,
        "prior_g2_candidate_calls": 0,
        "prior_g2_internal_evaluator_calls": 0,
        "prior_final_evaluator_calls": 0,
        "source_files_mutated": False,
    }
    destination = continuation_run_root / pair_id / "continuation.source.receipt.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(receipt_payload, indent=2, sort_keys=True) + "\n"
    if destination.exists():
        if destination.read_text(encoding="utf-8") != serialized:
            raise ValueError("existing continuation source receipt differs")
    else:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
    return baseline, evaluation, bundle, receipt_payload
