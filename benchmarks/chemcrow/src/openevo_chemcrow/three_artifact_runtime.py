from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

import httpx

from .hashing import canonical_sha256
from .models import ArtifactKind, TaskItem, Trajectory
from .runtime import (
    OpenEvoRolloutError,
    OpenEvoRolloutPort,
    _normalize_rollout,
    _poll_rollout_until_terminal,
    build_task_request,
)
from .three_artifact_models import THREE_ARTIFACT_ORDER, CoreInjectionReceiptSummary


class ThreeArtifactRolloutPort(OpenEvoRolloutPort):
    """Core-only Candidate port that accepts exactly one artifact of each supported type."""

    def run_candidate_with_receipt(
        self,
        *,
        task: TaskItem,
        role: str,
        artifact_ids_by_type: dict[ArtifactKind, str],
        pair_id: str,
        mcp_url: str | None,
        run_id: str | None = None,
    ) -> tuple[Trajectory, CoreInjectionReceiptSummary | None]:
        artifact_ids = _ordered_candidate_artifact_ids(
            role=role,
            artifact_ids_by_type=artifact_ids_by_type,
        )

        if run_id is None:
            run_id = f"{pair_id}-{role}-{uuid.uuid4().hex[:10]}"
        elif (
            re.fullmatch(
                rf"{re.escape(pair_id)}-{re.escape(role)}-[0-9a-f]{{10}}",
                run_id,
            )
            is None
        ):
            raise ValueError("Candidate preallocated run ID differs from pair/role authority")
        # Reuse the audited bare-S0 builder for all runtime/agent/tool settings.
        # Evolution authority is added only after the bare request is constructed.
        payload = build_task_request(
            task=task,
            run_id=run_id,
            role="baseline",
            candidate=self.candidate,
            artifact_ids=[],
            mcp_url=mcp_url,
        )
        metadata = payload["metadata"]
        metadata["evolution"] = {"context_artifact_ids": artifact_ids}
        if artifact_ids:
            metadata["openevo"] = {
                "revision_id": "chemcrow-task-local-three:" + canonical_sha256(artifact_ids)
            }

        started = time.monotonic()
        tool_receipt_offset = self._tool_receipt_count(mcp_url)
        with httpx.Client(timeout=30.0, trust_env=False) as client:
            response = client.post(f"{self.base_url}/rollout/task/submit", json=payload)
            response.raise_for_status()
            status, polling_retries = _poll_rollout_until_terminal(
                client=client,
                url=f"{self.base_url}/rollout/task/{run_id}",
                started=started,
                admitted_timeout_seconds=float(payload["timeout_seconds"]),
                poll_seconds=self.poll_seconds,
            )
        receipt = _three_artifact_injection_receipt(
            status,
            expected=artifact_ids_by_type,
            role=role,
        )
        trajectory = _normalize_rollout(
            status,
            run_id=run_id,
            task_id=task.task_id,
            role=role,
            artifact_ids=artifact_ids,
            config_sha256=self.config_sha256,
            wall_time=time.monotonic() - started,
            tool_receipts=self._tool_receipts_since(mcp_url, tool_receipt_offset),
            declared_tool_bridge=mcp_url is not None,
            polling_retries=polling_retries,
        )
        return trajectory, receipt


def _ordered_candidate_artifact_ids(
    *, role: str, artifact_ids_by_type: dict[ArtifactKind, str]
) -> list[str]:
    if role not in {"baseline", "evolved"}:
        raise ValueError("candidate role must be baseline or evolved")
    if role == "baseline":
        if artifact_ids_by_type:
            raise ValueError("baseline cannot receive evolution artifacts")
        return []
    expected_types = set(THREE_ARTIFACT_ORDER)
    if set(artifact_ids_by_type) != expected_types:
        raise ValueError("evolved candidate requires exactly memory, skill_bundle, agent_system")
    artifact_ids = [artifact_ids_by_type[kind] for kind in THREE_ARTIFACT_ORDER]
    if len(artifact_ids) != len(set(artifact_ids)):
        raise ValueError("one artifact ID cannot be registered under multiple types")
    return artifact_ids


def _three_artifact_injection_receipt(
    payload: dict[str, Any],
    *,
    expected: dict[ArtifactKind, str],
    role: str,
) -> CoreInjectionReceiptSummary | None:
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise OpenEvoRolloutError("rollout did not return exactly one session result")
    trajectory = results[0].get("trajectory")
    metadata = trajectory.get("metadata") if isinstance(trajectory, dict) else None
    task_metadata = metadata.get("task_metadata") if isinstance(metadata, dict) else None
    evolution = task_metadata.get("evolution") if isinstance(task_metadata, dict) else None
    if not isinstance(evolution, dict):
        raise OpenEvoRolloutError("Core evolution metadata is absent")
    raw_receipt = evolution.get("runtime_injection_receipt")
    if role == "baseline":
        if evolution.get("context_artifact_ids") != [] or raw_receipt is not None:
            raise OpenEvoRolloutError("baseline Core runtime was not bare S0")
        return None
    if evolution.get("context_injected") is not True or not isinstance(raw_receipt, dict):
        raise OpenEvoRolloutError("evolved run lacks a Core runtime readback receipt")
    raw_artifacts = raw_receipt.get("artifacts")
    if not isinstance(raw_artifacts, list) or len(raw_artifacts) != 3:
        raise OpenEvoRolloutError("Core runtime must inject exactly three artifacts")
    observed: dict[str, str] = {}
    observed_order: list[str] = []
    for item in raw_artifacts:
        if not isinstance(item, dict):
            raise OpenEvoRolloutError("Core runtime artifact receipt is malformed")
        artifact_type = item.get("artifact_type")
        artifact_id = item.get("artifact_id")
        runtime_paths = item.get("runtime_paths")
        if (
            not isinstance(artifact_type, str)
            or not isinstance(artifact_id, str)
            or not isinstance(runtime_paths, list)
            or not runtime_paths
        ):
            raise OpenEvoRolloutError("Core runtime artifact receipt is incomplete")
        if artifact_type in observed:
            raise OpenEvoRolloutError("Core injected duplicate artifact types")
        observed[artifact_type] = artifact_id
        observed_order.append(artifact_id)
    expected_mapping = {kind.value: artifact_id for kind, artifact_id in expected.items()}
    if observed != expected_mapping:
        raise OpenEvoRolloutError("Core injected artifact type/ID mapping differs from authority")
    expected_order = [expected[kind] for kind in THREE_ARTIFACT_ORDER]
    if observed_order != expected_order:
        raise OpenEvoRolloutError("Core injected artifacts in a non-authoritative order")
    context_ids = evolution.get("context_artifact_ids")
    if context_ids != expected_order:
        raise OpenEvoRolloutError("Core context artifact inventory differs from G2 authority")
    unexpected = sorted(set(observed_order) - set(expected_order))
    return CoreInjectionReceiptSummary(
        schema_version=str(raw_receipt.get("schema_version")),
        receipt_sha256=canonical_sha256(raw_receipt),
        memory_artifact_id=expected[ArtifactKind.TEXT_MEMORY],
        skill_artifact_id=expected[ArtifactKind.SKILL_BUNDLE],
        agent_system_artifact_id=expected[ArtifactKind.AGENT_SYSTEM],
        unexpected_artifact_ids=unexpected,
    )


def candidate_pair_request_parity(
    *,
    task: TaskItem,
    candidate: dict[str, Any],
    mcp_url: str,
) -> dict[str, Any]:
    """No-call proof that only evolution authority differs across the pair."""

    baseline = build_task_request(
        task=task,
        run_id="parity-baseline",
        role="baseline",
        candidate=candidate,
        artifact_ids=[],
        mcp_url=mcp_url,
    )
    evolved = json.loads(json.dumps(baseline))
    evolved["task_id"] = "parity-evolved"
    evolved["metadata"]["run_id"] = "parity-evolved"
    ids = ["art-memory", "art-skill", "art-agent-system"]
    evolved["metadata"]["evolution"] = {"context_artifact_ids": ids}
    evolved["metadata"]["openevo"] = {
        "revision_id": "chemcrow-task-local-three:" + canonical_sha256(ids)
    }
    invariant_keys = (
        "instruction",
        "timeout_seconds",
        "runtime",
        "agent",
        "builder",
        "evaluator",
        "workspace_handoff",
        "runtime_context_binding",
    )
    equal = {key: baseline.get(key) == evolved.get(key) for key in invariant_keys}
    return {
        "all_invariant_fields_equal": all(equal.values()),
        "field_equality": equal,
        "only_allowed_metadata_drift": set(evolved["metadata"]) - set(baseline["metadata"])
        == {"openevo"},
        "baseline_context_artifact_ids": baseline["metadata"]["evolution"]["context_artifact_ids"],
        "evolved_artifact_types": [kind.value for kind in THREE_ARTIFACT_ORDER],
    }
