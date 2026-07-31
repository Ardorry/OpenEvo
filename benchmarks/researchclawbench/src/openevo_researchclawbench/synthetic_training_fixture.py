"""No-model production-interface fixture for supervisor integration tests."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

from openevo.runtime.codex_isolation import (
    CODEX_SUBSCRIPTION_POLICY_ID,
    CODEX_SUBSCRIPTION_POLICY_SHA256,
)

from .config import ARTIFACT_TYPES, CANARY_TASK
from .production_training_operations import ProductionOperationPort, ProductionPorts
from .training_state_store import canonical_sha256


class InjectedCrash(RuntimeError):
    pass


class DurableFakeAuthority(ProductionOperationPort):
    """Durable fake external authority; orchestration remains production code."""

    def __init__(self, root: Path, kind: str, *, crash_point: str | None = None) -> None:
        self.root = root / kind
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.kind = kind
        self.crash_point = crash_point

    def _path(self, key: str) -> Path:
        return self.root / f"{canonical_sha256({'key': key})}.json"

    def preflight(self, request: dict[str, Any]) -> dict[str, Any]:
        del request
        if self.kind == "evolution":
            return {
                "ready": True,
                "identity_matches": True,
                "authority_issued": True,
                "docker_mount_created": True,
                "container_path_visible": True,
                "container_user_can_read": True,
                "generation_matches": True,
                "release_identity_matches": True,
                "adoption_receipt_valid": True,
                "cleanup_verified": True,
                "secret_recorded": False,
                "codex_cli_started": False,
                "model_started": False,
                "synthetic": True,
            }
        if self.kind != "candidate":
            raise ValueError("synthetic Core readiness is unavailable for this port")
        return {
            "service_reachable": True,
            "bearer_present": True,
            "bearer_valid": True,
            "generation_matches": True,
            "candidate_port_authorized": True,
            "secret_recorded": False,
            "environment_fallback_used": False,
            "managed_candidate_runtime_ready": True,
            "managed_candidate_credential_mount_adopted": True,
            "managed_candidate_codex_cli_version": "0.144.1",
            "managed_candidate_model_started": False,
            "managed_candidate_subscription_isolation": {
                "authority_issued": True,
                "mount_adopted": True,
                "codex_cli_auth_visible": True,
                "host_source_hidden": True,
                "tool_sandbox_credential_hidden": True,
                "tool_environment_clean": True,
                "parent_process_secret_unreadable": True,
                "generation_matches": True,
                "release_identity_matches": True,
                "image_digest_matches": True,
                "cli_version_matches": True,
                "cleanup_verified": True,
                "policy_id": CODEX_SUBSCRIPTION_POLICY_ID,
                "policy_sha256": CODEX_SUBSCRIPTION_POLICY_SHA256,
                "model_started": False,
            },
            "service_identity_id": "synthetic-core-service",
            "generation": "1" * 32,
            "release_identity": "2" * 64,
        }

    def recover(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any] | None:
        path = self._path(idempotency_key)
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload["request_sha256"] != canonical_sha256(request):
            raise ValueError("synthetic external authority request drifted")
        return payload["result"]

    def execute(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        prior = self.recover(request, idempotency_key)
        if prior is not None:
            return prior
        result = self._result(request, idempotency_key)
        payload = {
            "request_sha256": canonical_sha256(request),
            "result": result,
            "external_billed_side_effect_count": 1 if self.kind in {"candidate", "evaluation", "evolution"} else 0,
        }
        path = self._path(idempotency_key)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            data = json.dumps(payload, sort_keys=True, allow_nan=False).encode() + b"\n"
            os.write(descriptor, data)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        temporary.replace(path)
        if self.crash_point == self.kind:
            self.crash_point = None
            raise InjectedCrash(f"synthetic crash after {self.kind} authority commit")
        return result

    def _result(self, request: dict[str, Any], key: str) -> dict[str, Any]:
        task = request.get("task_id", CANARY_TASK)
        nested_candidate = request.get("candidate")
        if "attempt_index" in request:
            attempt = int(request["attempt_index"])
        elif isinstance(nested_candidate, dict):
            marker = str(nested_candidate.get("run_id", "_a0")).split("_a", 1)[-1]
            attempt = int(marker.split("_", 1)[0])
        else:
            attempt = 0
        seed = canonical_sha256({"kind": self.kind, "key": key})
        if self.kind == "candidate":
            run_id = str(request["run_id"])
            return {
                "run_id": run_id,
                "core_project_id": request.get("core_project_id") or "project-synthetic-life-v2",
                "core_task_id": f"core-{task}-a{attempt}",
                "core_attempt_id": f"attempt-{task}-a{attempt}",
                "workspace_binding_id": f"workspace-{seed[:16]}",
                "task_request_id": f"request-{seed[16:32]}",
                "session_result_id": f"session-result-{seed[32:48]}",
                "session_id": f"session-{task}-a{attempt}",
                "dataset_id": f"dataset-{task}-a{attempt}",
                "dataset_revision": f"artifact-dataset-{task}-a{attempt}.v1",
                "successor_transition_id": f"transition-{task}-a{attempt}",
                "transcript_receipt": {"sha256": seed, "trace_count": 1},
                "completed": True,
                "runtime_seconds": float(30 - attempt * 2),
                "cost_total_usd": None,
                "composite_size_bytes": 1000 + attempt * 10,
                "candidate_exit_state": "COMPLETED",
                "tool_events": 3,
                "owned_resource_ids": [f"session-{task}-a{attempt}"],
                "owned_resources": [
                    {
                        "resource_id": f"session-{task}-a{attempt}",
                        "kind": "openevo_session",
                        "identity": {
                            "session_id": f"session-{task}-a{attempt}",
                            "task_id": task,
                            "attempt_index": attempt,
                        },
                        "active": False,
                    }
                ],
            }
        if self.kind == "validation":
            return {
                "artifact_valid": True,
                "artifact_root_sha256": seed,
                "completeness": 12,
                "validator_receipt_id": f"validator-{seed[:16]}",
                "outputs_frozen_read_only": True,
            }
        if self.kind == "evaluation":
            return {
                "total_score": [10.0, 20.0, 15.0][attempt],
                "feedback_class": "SOFT_JUDGE",
                "generic_failure_tags": [],
                "evaluation_receipt_id": f"evaluation-{seed[:16]}",
                "judge_model": "synthetic-no-model",
                "request_count": 0,
                "usage": None,
                "runtime_seconds": 0.0,
            }
        if self.kind == "attachment":
            return {
                "attachment_id": f"attachment-{task}-a{attempt}",
                "attachment_sha256": seed,
                "resolved_view_sha256": canonical_sha256({"view": seed}),
                "resolved_dataset_artifact_id": f"resolved-dataset-{task}-a{attempt}",
                "task_local_overlay_id": f"overlay-{task}-a{attempt}",
                "authority": "evaluator_only",
            }
        if self.kind == "evolution":
            jobs = []
            for artifact_type in ARTIFACT_TYPES:
                artifact_seed = canonical_sha256({"seed": seed, "type": artifact_type})
                jobs.append(
                    {
                        "job_id": f"job-{artifact_type}-{artifact_seed[:12]}",
                        "artifact_type": artifact_type,
                        "action": "update",
                        "parent_registry_id": f"parent-{artifact_type}-a{attempt}",
                        "successor_registry_id": f"registry-{artifact_type}-a{attempt + 1}",
                        "successor_sha256": artifact_seed,
                        "proposal_ids": [f"proposal-{artifact_seed[:12]}"],
                        "promotion_result": "promoted",
                        "admission_result": "accepted",
                        "dataset_view_sha256": request["attachment"]["resolved_view_sha256"],
                    }
                )
            return {"jobs": jobs, "successor_receipt_id": f"successor-{seed[:16]}"}
        if self.kind == "composite":
            jobs = request["evolution"]["jobs"]
            revision = attempt + 1
            return {
                "composite_id": f"c{revision:03d}-{task}",
                "parent_composite_id": request["parent_composite_id"],
                "registry_artifact_ids": [item["successor_registry_id"] for item in jobs],
                "artifact_hashes": {item["artifact_type"]: item["successor_sha256"] for item in jobs},
                "composite_sha256": seed,
                "admission_receipt_ids": [f"admission-{item['job_id']}" for item in jobs],
            }
        if self.kind == "task_local":
            return {"destroyed": True, "overlay_id": request.get("overlay_id")}
        if self.kind == "sanitizer":
            return {
                "passed": True,
                "composite_id": request["composite_id"],
                "sanitization_sha256": seed,
                "task_specific_literals": [],
            }
        if self.kind == "freeze":
            artifacts = {
                "agent_system": {
                    "registry_id": "registry-agent-system-final",
                    "sha256": seed,
                    "frozen": True,
                },
                "text_memory": {
                    "registry_id": "registry-text-memory-final",
                    "sha256": seed,
                    "frozen": True,
                },
                "skill_bundle": {
                    "registry_id": "registry-skill-bundle-final",
                    "sha256": seed,
                    "frozen": True,
                },
            }
            return {
                **artifacts,
                "freeze_receipt_id": f"freeze-{seed[:16]}",
                "authority_sha256": seed,
                "freeze_authority_sha256": seed,
                "core_authoritative": True,
                "composite_sha256": canonical_sha256({"final": seed}),
                "composite_id": request["selected_composite_id"],
                "core_project_id": "project-synthetic-life-v2",
                "core_project_head_id": f"head-frozen-{seed[:16]}",
                "core_project_head_manifest_sha256": canonical_sha256(
                    {"head": seed}
                ),
                "protocol_sha256": request["protocol_sha256"],
                "core_identity_sha256": request["core_identity_sha256"],
                "adapter_identity_sha256": request["adapter_identity_sha256"],
                "frozen": True,
                "evolution_enabled": False,
                "reflector_enabled": False,
                "training_feedback_attachment_enabled": False,
                "hard_gt_teacher_enabled": False,
                "teacher_enabled": False,
                "task_local_overlay_enabled": False,
                "cross_task_state_updates": False,
                "official_feedback_released": False,
            }
        raise ValueError(f"unknown synthetic authority: {self.kind}")

    def call_count(self) -> int:
        return len(list(self.root.glob("*.json")))


def build_synthetic_ports(root: str | Path, *, crash_point: str | None = None) -> tuple[ProductionPorts, dict[str, DurableFakeAuthority]]:
    base = Path(root)
    authorities = {
        kind: DurableFakeAuthority(base, kind, crash_point=crash_point)
        for kind in (
            "candidate", "validation", "evaluation", "attachment", "evolution",
            "composite", "task_local", "sanitizer", "freeze",
        )
    }
    return (
        ProductionPorts(
            candidate=authorities["candidate"],
            validation=authorities["validation"],
            evaluation=authorities["evaluation"],
            attachment=authorities["attachment"],
            evolution=authorities["evolution"],
            composite=authorities["composite"],
            task_local=authorities["task_local"],
            sanitizer=authorities["sanitizer"],
            freeze=authorities["freeze"],
        ),
        authorities,
    )


__all__ = ["DurableFakeAuthority", "InjectedCrash", "build_synthetic_ports"]
