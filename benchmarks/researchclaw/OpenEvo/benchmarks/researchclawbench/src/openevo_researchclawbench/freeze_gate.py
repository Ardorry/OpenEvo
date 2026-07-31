"""Final community freeze and official-40 fail-closed transition gate."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from .config import OFFICIAL_TASK_COUNT
from .hashing import canonical_json_sha256


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class FrozenProtocolReceipt:
    manifest: dict[str, Any]
    sha256: str


def freeze_community_artifacts(payload: dict[str, Any]) -> FrozenProtocolReceipt:
    required = {
        "agent_system_artifact_id",
        "agent_system_sha256",
        "text_memory_artifact_id",
        "text_memory_sha256",
        "skill_bundle_artifact_id",
        "skill_bundle_sha256",
        "composite_sha256",
        "openevo_commit",
        "adapter_tree_sha256",
        "managed_runtime_digest",
        "codex_cli_version",
        "candidate_model",
        "reasoning_level",
        "tool_policy_sha256",
        "budget_policy_sha256",
        "contamination_receipt_sha256",
    }
    if set(payload) != required:
        raise ValueError(f"freeze manifest fields are not closed: {sorted(set(payload) ^ required)}")
    for key in (
        "agent_system_sha256", "text_memory_sha256", "skill_bundle_sha256",
        "composite_sha256", "adapter_tree_sha256", "managed_runtime_digest",
        "tool_policy_sha256", "budget_policy_sha256", "contamination_receipt_sha256",
    ):
        if not isinstance(payload[key], str) or _SHA256.fullmatch(payload[key]) is None:
            raise ValueError(f"invalid freeze digest: {key}")
    if _COMMIT.fullmatch(str(payload["openevo_commit"])) is None:
        raise ValueError("invalid OpenEvo freeze commit")
    if payload["codex_cli_version"] != "0.144.1" or payload["candidate_model"] != "gpt-5.5":
        raise ValueError("official freeze differs from the managed candidate identity")
    manifest = {
        **payload,
        "evolution_enabled": False,
        "reflector_enabled": False,
        "teacher_enabled": False,
        "task_local_overlay_enabled": False,
        "cross_task_state_updates": False,
        "official_feedback_released": False,
        "official_candidate_run_count": OFFICIAL_TASK_COUNT,
        "scoring_gate": "ALL_40_CANDIDATE_RUNS_SEALED",
    }
    return FrozenProtocolReceipt(manifest, canonical_json_sha256(manifest))


def validate_official_run_set(
    run_manifests: Iterable[dict[str, Any]],
    *,
    frozen_protocol_sha256: str,
) -> None:
    records = list(run_manifests)
    if len(records) != OFFICIAL_TASK_COUNT:
        raise RuntimeError("OFFICIAL_SCORER_WITHHELD_UNTIL_40_RUNS_SEALED")
    task_ids = {record.get("task_id") for record in records}
    if len(task_ids) != OFFICIAL_TASK_COUNT:
        raise ValueError("official run set does not contain 40 distinct tasks")
    if any(
        record.get("frozen_protocol_sha256") != frozen_protocol_sha256
        or record.get("sealed") is not True
        or record.get("evolution_enabled") is not False
        or record.get("reflector_enabled") is not False
        or record.get("teacher_enabled") is not False
        or record.get("feedback_released") is not False
        for record in records
    ):
        raise ValueError("official run set changed the frozen protocol")
