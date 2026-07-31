"""Atomic attempt receipts and sealed file inventories."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .hashing import canonical_json_sha256, tree_sha256, write_sha256_manifest


REQUIRED_ATTEMPT_FIELDS = {
    "experiment_id", "task_id", "attempt_id", "attempt_index",
    "input_composite_revision", "agent_system_revision", "text_memory_revision",
    "skill_bundle_revision", "protocol_sha256", "researchclawbench_commit",
    "openevo_commit", "adapter_diff_sha256", "candidate_image_id", "candidate_model",
    "reasoning_level", "network", "started_at", "finished_at", "completed",
    "exit_code", "runtime_seconds", "cost_total_usd", "artifact_valid",
    "output_artifact_hash", "feedback_sha256", "transfer_only",
    "rollout_task_id", "native_session_id", "task_request_sha256",
    "workspace_result_manifest_sha256", "input_artifact_ids",
}


class ManifestError(ValueError):
    """Attempt manifest is incomplete or unsafe."""


def validate_attempt_manifest(payload: dict[str, Any]) -> None:
    missing = REQUIRED_ATTEMPT_FIELDS - set(payload)
    if missing:
        raise ManifestError(f"attempt manifest missing fields: {sorted(missing)}")
    if payload["network"] != "disabled" or payload["transfer_only"] is not False:
        raise ManifestError("sequential Community Dev attempts must be network-disabled training runs")
    if payload["attempt_index"] not in {0, 1, 2}:
        raise ManifestError("attempt_index must be 0, 1, or 2")
    if not isinstance(payload["input_artifact_ids"], list) or len(payload["input_artifact_ids"]) != len(set(payload["input_artifact_ids"])):
        raise ManifestError("input artifact IDs must be a unique array")
    if not all(isinstance(payload[name], str) and payload[name] for name in ("rollout_task_id", "native_session_id")):
        raise ManifestError("native rollout/session identity is missing")


def atomic_write_json(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_attempt_manifest(path: str | Path, payload: dict[str, Any]) -> str:
    validate_attempt_manifest(payload)
    atomic_write_json(path, payload)
    return canonical_json_sha256(payload)


def seal_run_inventory(run_root: str | Path) -> str:
    root = Path(run_root).resolve(strict=True)
    destination = root / "file_manifest_sha256.tsv"
    write_sha256_manifest(root, destination)
    return tree_sha256(root)
