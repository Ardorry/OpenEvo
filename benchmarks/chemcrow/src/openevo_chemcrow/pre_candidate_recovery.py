from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .hashing import file_sha256, text_sha256
from .replacement_ledger import VerifiedReplacementPhaseLedger
from .runtime import CORE_MANAGED_CODEX_ROUTE

_RECOVERABLE_ERROR = (
    "agent execution failed: Codex subscription credential isolation could not be "
    "proven (validation_failed)"
)


def reconcile_pre_candidate_no_effect_failure(
    *,
    config_path: Path,
    run_root: Path,
    ledger_root: Path,
    experiment_id: str,
    task_id: str,
    core_completion_path: Path,
    repository_root: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Verify a Core setup failure and preserve it before enabling one replacement."""

    pair_id = f"{experiment_id}--{task_id}"
    claim_path = ledger_root / pair_id / "baseline_candidate.json"
    if not claim_path.is_file():
        raise ValueError("baseline Candidate claim is absent")
    if (run_root / pair_id / "pair.result.json").exists():
        raise ValueError("sealed pair cannot be recovered")
    claim = json.loads(claim_path.read_text(encoding="utf-8"))
    if (
        claim.get("schema_version") != "chemcrow_phase_claim_v1"
        or claim.get("phase") != "baseline_candidate"
        or claim.get("status") != "claimed"
    ):
        raise ValueError("baseline Candidate claim is not an unreconciled v1 claim")
    authority = claim.get("authority")
    if (
        not isinstance(authority, dict)
        or authority.get("task_id") != task_id
        or authority.get("artifact_ids") != []
        or authority.get("artifact_inventory") != {}
    ):
        raise ValueError("baseline Candidate claim authority is invalid")

    completion = json.loads(core_completion_path.read_text(encoding="utf-8"))
    core_task_id = completion.get("task_id")
    if not isinstance(core_task_id, str) or re.fullmatch(
        rf"{re.escape(pair_id)}-baseline-[0-9a-f]{{10}}", core_task_id
    ) is None:
        raise ValueError("Core completion task identity differs")
    trajectory = completion.get("trajectory")
    metadata = completion.get("metadata")
    if not isinstance(trajectory, dict) or not isinstance(metadata, dict):
        raise TypeError("Core completion evidence is incomplete")
    trajectory_metadata = trajectory.get("metadata")
    openevo_metadata = metadata.get("openevo")
    credential_contract = (
        openevo_metadata.get("credential_isolation")
        if isinstance(openevo_metadata, dict)
        else None
    )
    no_effect_predicates = {
        "core_status_error": completion.get("status") == "ERROR",
        "exact_pre_candidate_setup_error": completion.get("error") == _RECOVERABLE_ERROR,
        "trajectory_has_zero_records": isinstance(trajectory_metadata, dict)
        and trajectory_metadata.get("record_count") == 0,
        "trajectory_has_zero_traces": trajectory.get("traces") == [],
        "workspace_result_absent": completion.get("workspace_result") is None,
        "core_route_bound": metadata.get("execution_route") == CORE_MANAGED_CODEX_ROUTE,
        "host_codex_exec_forbidden": metadata.get("host_codex_exec_forbidden") is True,
        "baseline_artifact_inventory_empty": metadata.get("evolution", {}).get(
            "context_artifact_ids"
        )
        == [],
        "credential_canary_receipt_not_published": isinstance(credential_contract, dict)
        and "status" not in credential_contract,
    }
    if set(no_effect_predicates.values()) != {True}:
        failed = sorted(key for key, value in no_effect_predicates.items() if not value)
        raise ValueError(f"Candidate no-effect proof failed: {', '.join(failed)}")
    session_id = completion.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("Core completion session identity is absent")

    repo_root = repository_root or Path(__file__).resolve().parents[4]
    source_paths = {
        "codex_harness": repo_root / "src/openevo/harness/presets/codex.py",
    }
    if any(not path.is_file() for path in source_paths.values()):
        raise ValueError("recovery source authority is absent")
    receipt = {
        "schema_version": "chemcrow_pre_candidate_no_effect_recovery_v1",
        "status": "VERIFIED_NO_CANDIDATE_EFFECT_REPLACEMENT_READY",
        "pair_id": pair_id,
        "task_id": task_id,
        "phase": "baseline_candidate",
        "attempt_ordinal": 1,
        "authority_sha256": claim["authority_sha256"],
        "original_claim_sha256": file_sha256(claim_path),
        "config_sha256": file_sha256(config_path),
        "core_task_id": core_task_id,
        "core_session_id_sha256": text_sha256(session_id),
        "core_completion_sha256": file_sha256(core_completion_path),
        "core_completion_status": "ERROR",
        "error_category": "credential_isolation_validation_failed",
        "no_effect_predicates": no_effect_predicates,
        "infrastructure_canary_model_call_may_have_occurred": True,
        "candidate_model_call_proven_absent": True,
        "duplicate_scientific_call": False,
        "source_authority_sha256": {
            name: file_sha256(path) for name, path in sorted(source_paths.items())
        },
        "recorded_before_replacement_dispatch": True,
    }
    receipt_path = (
        run_root
        / "recovery"
        / pair_id
        / "baseline_candidate.attempt-1.no-effect.json"
    )
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if receipt_path.exists() and receipt_path.read_text(encoding="utf-8") != serialized:
        raise ValueError("existing no-effect recovery receipt differs")
    receipt_path.write_text(serialized, encoding="utf-8")
    VerifiedReplacementPhaseLedger(
        ledger_root, pair_id=pair_id
    ).reconcile_verified_no_effect_failure(
        "baseline_candidate",
        receipt,
    )
    return receipt_path, receipt


__all__ = ["reconcile_pre_candidate_no_effect_failure"]
