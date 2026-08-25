"""Fail-closed composite closure for a stopped 12-pair run plus 2-pair repair."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .audit import audit_completed_run
from .hashing import canonical_sha256, file_sha256
from .models import PairResult
from .paper_evaluator import FROZEN_PAPER_TASK_IDS

PRIMARY_SEALED_TASK_IDS = FROZEN_PAPER_TASK_IDS[:12]
REPAIR_TASK_IDS = FROZEN_PAPER_TASK_IDS[12:]


def audit_paper_composite_repair(
    *,
    primary_run_root: Path,
    primary_experiment_id: str,
    repair_run_root: Path,
    repair_experiment_id: str,
    core_completion_root: Path,
    expected_s0_hash: str,
    duplicate_authorization_receipt: Path,
    composite_manifest_path: Path,
) -> dict[str, Any]:
    """Seal a 14-task composite without treating the interrupted pair as complete."""
    stopped = primary_run_root / "STOPPED_BY_USER.json"
    if not stopped.is_file():
        raise ValueError("primary source is not bound to its user-stop receipt")
    if any((repair_run_root / marker).exists() for marker in ("STOPPED_BY_USER.json", "INVALIDATED.json")):
        raise ValueError("repair source is stopped or invalidated")
    authorization = validate_duplicate_authorization_receipt(
        path=duplicate_authorization_receipt,
        primary_run_root=primary_run_root,
        primary_experiment_id=primary_experiment_id,
    )
    primary_audit = audit_completed_run(
        run_root=primary_run_root,
        core_completion_root=core_completion_root,
        experiment_id=primary_experiment_id,
        task_ids=list(PRIMARY_SEALED_TASK_IDS),
        expected_s0_hash=expected_s0_hash,
        require_aggregate=False,
    )
    repair_audit = audit_completed_run(
        run_root=repair_run_root,
        core_completion_root=core_completion_root,
        experiment_id=repair_experiment_id,
        task_ids=list(REPAIR_TASK_IDS),
        expected_s0_hash=expected_s0_hash,
        require_aggregate=True,
    )
    entries: list[dict[str, Any]] = []
    artifact_ids: set[str] = set()
    for task_id in FROZEN_PAPER_TASK_IDS:
        if task_id in PRIMARY_SEALED_TASK_IDS:
            root = primary_run_root
            experiment_id = primary_experiment_id
            source = "primary_sealed_subset"
        else:
            root = repair_run_root
            experiment_id = repair_experiment_id
            source = "fresh_repair_run"
        pair_path = root / f"{experiment_id}--{task_id}" / "pair.result.json"
        pair = PairResult.model_validate_json(pair_path.read_text(encoding="utf-8"))
        if pair.task_id != task_id or pair.s0_hash != expected_s0_hash:
            raise ValueError(f"composite pair authority mismatch: {task_id}")
        if pair.artifact.artifact_id in artifact_ids:
            raise ValueError(f"artifact reused across composite sources: {task_id}")
        artifact_ids.add(pair.artifact.artifact_id)
        entries.append(
            {
                "task_id": task_id,
                "source": source,
                "source_experiment_id": experiment_id,
                "pair_result_path": str(pair_path.resolve()),
                "pair_result_sha256": file_sha256(pair_path),
                "baseline_run_id": pair.baseline.run_id,
                "evolved_run_id": pair.evolved.run_id,
                "artifact_id": pair.artifact.artifact_id,
            }
        )
    manifest = {
        "schema_version": "chemcrow_paper_composite_manifest_v1",
        "status": "PASS",
        "task_ids": list(FROZEN_PAPER_TASK_IDS),
        "task_count": len(entries),
        "s0_config_sha256": expected_s0_hash,
        "primary_experiment_id": primary_experiment_id,
        "repair_experiment_id": repair_experiment_id,
        "primary_stop_receipt_sha256": file_sha256(stopped),
        "duplicate_authorization_receipt_sha256": file_sha256(
            duplicate_authorization_receipt
        ),
        "duplicate_authorization": authorization,
        "primary_subset_audit": primary_audit,
        "repair_completed_run_audit": repair_audit,
        "entries": entries,
        "answers_included": False,
        "historical_answers_included": False,
    }
    _write_or_require_identical(composite_manifest_path, manifest)
    return {
        "schema_version": "chemcrow_paper_composite_audit_v1",
        "status": "PASS",
        "task_count": len(entries),
        "task_ids": list(FROZEN_PAPER_TASK_IDS),
        "s0_config_sha256": expected_s0_hash,
        "composite_manifest_path": str(composite_manifest_path.resolve()),
        "composite_manifest_sha256": file_sha256(composite_manifest_path),
        "primary_subset_audit_sha256": canonical_sha256(primary_audit),
        "repair_completed_run_audit_sha256": canonical_sha256(repair_audit),
        "answers_included": False,
    }


def load_composite_pairs(
    *, manifest_path: Path, task_ids: list[str]
) -> dict[str, PairResult]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("schema_version") != "chemcrow_paper_composite_manifest_v1"
        or manifest.get("status") != "PASS"
        or manifest.get("task_ids") != task_ids
        or manifest.get("task_count") != len(task_ids)
    ):
        raise ValueError("paper composite manifest authority is invalid")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or len(entries) != len(task_ids):
        raise ValueError("paper composite manifest entries are incomplete")
    pairs: dict[str, PairResult] = {}
    for expected_task_id, entry in zip(task_ids, entries, strict=True):
        if not isinstance(entry, dict) or entry.get("task_id") != expected_task_id:
            raise ValueError("paper composite task order differs from frozen order")
        path = Path(str(entry.get("pair_result_path")))
        if not path.is_file() or file_sha256(path) != entry.get("pair_result_sha256"):
            raise ValueError(f"paper composite pair hash mismatch: {expected_task_id}")
        pair = PairResult.model_validate_json(path.read_text(encoding="utf-8"))
        if pair.task_id != expected_task_id:
            raise ValueError(f"paper composite pair task mismatch: {expected_task_id}")
        pairs[expected_task_id] = pair
    return pairs


def validate_duplicate_authorization_receipt(
    *, path: Path, primary_run_root: Path, primary_experiment_id: str
) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    prior_claim = (
        primary_run_root
        / "claims"
        / f"{primary_experiment_id}--chemcrow-14"
        / "evolved_candidate.json"
    )
    expected = {
        "schema_version": "chemcrow_duplicate_task_authorization_v1",
        "status": "AUTHORIZED",
        "task_id": "chemcrow-14",
        "prior_experiment_id": primary_experiment_id,
        "prior_phase": "evolved_candidate",
        "prior_claim_sha256": file_sha256(prior_claim),
        "authorization_literal": "I_AUTHORIZE_FRESH_CHEMCROW_14_PAIR_AFTER_USER_STOP",
        "reason": "prior pair interrupted before sealing; no prior pair result is eligible",
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ValueError(f"duplicate task authorization mismatch: {key}")
    if payload.get("authorized_at") in (None, ""):
        raise ValueError("duplicate task authorization timestamp is absent")
    if payload.get("authorized_by") != "user":
        raise ValueError("duplicate task authorization is not user-bound")
    return {
        "task_id": payload["task_id"],
        "prior_experiment_id": payload["prior_experiment_id"],
        "prior_phase": payload["prior_phase"],
        "prior_claim_sha256": payload["prior_claim_sha256"],
        "authorized_at": payload["authorized_at"],
        "authorized_by": payload["authorized_by"],
        "reason": payload["reason"],
    }


def _write_or_require_identical(path: Path, payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != serialized:
            raise ValueError("existing paper composite manifest differs")
        return
    path.write_text(serialized, encoding="utf-8")
