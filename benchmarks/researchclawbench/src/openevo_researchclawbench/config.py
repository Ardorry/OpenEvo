"""Frozen native OpenEvo/ResearchClawBench protocol parsing.

This module is deliberately data-only.  It never probes Codex credentials or
starts a runtime; those authorities belong to OpenEvo Core.
"""

from __future__ import annotations

import os
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


EXPERIMENT_ID = "sequential_task_reflector_evolution_v0"
REFACTOR_ID = "NATIVE_OPENEVO_CODEX_AND_TRIPLE_ARTIFACT_REFACTOR_V1"
FROZEN_TASKS = (
    "Astronomy_004",
    "Chemistry_004",
    "Earth_004",
    "Earth_005",
    "Energy_004",
    "Information_004",
    "Information_005",
    "Information_006",
    "Information_007",
    "Life_004",
    "Life_005",
    "Life_006",
    "Life_007",
    "Material_004",
    "Math_004",
    "Neuroscience_004",
    "Physics_004",
)
CANARY_TASK = "Life_005"
OFFICIAL_TASK_COUNT = 40
ATTEMPTS_PER_TASK = 3
REFLECTOR_ROUNDS_PER_TASK = 2
ARTIFACT_TYPES = ("agent_system", "text_memory", "skill_bundle")
EXPECTED_CANDIDATE_RUNS = len(FROZEN_TASKS) * ATTEMPTS_PER_TASK
EXPECTED_REFLECTOR_CYCLES = len(FROZEN_TASKS) * REFLECTOR_ROUNDS_PER_TASK
EXPECTED_ARTIFACT_EVOLUTION_REQUESTS = EXPECTED_REFLECTOR_CYCLES * len(ARTIFACT_TYPES)

MANAGED_RUNTIME_PROFILE = "managed_science"
MANAGED_RUNTIME_IMAGE = (
    "openevo/science-runtime@"
    "sha256:af67c6b8c9cb0debd3a29addc23f518a680369ad53ec5347a829ef7318529c5c"
)
MANAGED_RUNTIME_IMAGE_ID = (
    "sha256:7a0079f9cb1bce5768cff5bce3d1181811c6a231ad800cac8fb503d66852c81b"
)
MANAGED_CODEX_BINARY = "/opt/codex/bin/codex"
MANAGED_CODEX_VERSION = "0.144.1"
MANAGED_CODEX_MODEL = "gpt-5.5"
REFLECTOR_RUNTIME_BLOCKER = "REFLECTOR_CODEX_RUNTIME_NOT_REPRODUCIBLY_PINNED"
TEACHER_ATTACHMENT_BLOCKER = "NATIVE_POST_RUN_TRAINING_FEEDBACK_ATTACHMENT_UNAVAILABLE"

PLACEHOLDERS = {
    "PENDING_REQUIRED_CONFIGURATION",
    "PENDING_MAINTAINER_CONFIRMATION",
    "MISSING_REQUIRED_CONFIGURATION",
    "",
    None,
}


def _is_unresolved(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value in PLACEHOLDERS)


class ProtocolError(ValueError):
    """Protocol is incomplete or violates a frozen boundary."""


@dataclass(frozen=True)
class ExperimentConfig:
    path: Path
    raw: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path) -> "ExperimentConfig":
        protocol_path = Path(path).resolve(strict=True)
        payload = yaml.safe_load(protocol_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ProtocolError("protocol must be a YAML object")
        instance = cls(protocol_path, payload)
        instance.validate_static()
        return instance

    def require(self, key: str) -> Any:
        value: Any = self.raw
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                raise ProtocolError(f"missing protocol field: {key}")
            value = value[part]
        return value

    @property
    def project_root(self) -> Path:
        return Path(self.require("paths.project_root")).resolve(strict=True)

    @property
    def experiment_root(self) -> Path:
        value = Path(self.require("paths.experiment_root")).resolve(strict=True)
        if not value.is_relative_to(self.project_root):
            raise ProtocolError("experiment_root escapes project_root")
        return value

    @property
    def researchclawbench_root(self) -> Path:
        value = Path(self.require("paths.researchclawbench_root")).resolve(strict=True)
        if not value.is_relative_to(self.project_root):
            raise ProtocolError("ResearchClawBench root escapes project root")
        return value

    @property
    def rollout_url(self) -> str:
        return str(self.require("native_openevo.rollout_url")).rstrip("/")

    @property
    def evolution_url(self) -> str:
        return str(self.require("native_openevo.evolution_url")).rstrip("/")

    def validate_static(self) -> None:
        if self.require("experiment_id") != EXPERIMENT_ID:
            raise ProtocolError("unexpected experiment_id")
        if self.require("refactor_id") != REFACTOR_ID:
            raise ProtocolError("unexpected refactor_id")
        if tuple(self.require("tasks")) != FROZEN_TASKS:
            raise ProtocolError("task sequence differs from the frozen 17-task sequence")
        expected = {
            "community_training_tasks": len(FROZEN_TASKS),
            "attempts_per_task": ATTEMPTS_PER_TASK,
            "reflector_rounds_per_task": REFLECTOR_ROUNDS_PER_TASK,
            "candidate_runs": EXPECTED_CANDIDATE_RUNS,
            "reflector_cycles_per_artifact": EXPECTED_REFLECTOR_CYCLES,
            "artifact_evolution_requests": EXPECTED_ARTIFACT_EVOLUTION_REQUESTS,
            "official_frozen_test_runs": OFFICIAL_TASK_COUNT,
        }
        for key, value in expected.items():
            if self.require(key) != value:
                raise ProtocolError(f"{key} must be {value}")
        if self.require("candidate.network") != "disabled":
            raise ProtocolError("candidate network must be disabled")
        if self.require("candidate.tool_network") != "disabled":
            raise ProtocolError("candidate tool network must be disabled")
        if self.require("candidate.model_control_plane_network") != "managed_only":
            raise ProtocolError("candidate model control plane must remain Core-managed")
        if self.require("candidate.model") != MANAGED_CODEX_MODEL:
            raise ProtocolError("candidate model must be gpt-5.5")
        if self.require("candidate.codex_binary") != MANAGED_CODEX_BINARY:
            raise ProtocolError("candidate binary is not the managed Codex binary")
        if self.require("candidate.codex_cli_version") != MANAGED_CODEX_VERSION:
            raise ProtocolError("candidate Codex version must be 0.144.1")
        if self.require("candidate.runtime_profile") != MANAGED_RUNTIME_PROFILE:
            raise ProtocolError("candidate runtime profile must be managed_science")
        if self.require("candidate.runtime_image") != MANAGED_RUNTIME_IMAGE:
            raise ProtocolError("candidate runtime image differs from the Core release pin")
        if self.require("candidate.auth_mode") != "subscription":
            raise ProtocolError("candidate auth mode must be subscription")
        if self.require("candidate.capture_mode") != "transcript":
            raise ProtocolError("candidate capture mode must be transcript")
        if self.require("candidate.mcp_enabled") is not False:
            raise ProtocolError("candidate custom MCP must remain disabled")
        artifacts = self.require("artifacts")
        for artifact_type in ARTIFACT_TYPES:
            if artifacts.get(artifact_type, {}).get("enabled") is not True:
                raise ProtocolError(f"{artifact_type} must be enabled")
        if artifacts.get("parametric_memory", {}).get("enabled") is not False:
            raise ProtocolError("parametric_memory must be disabled")
        if self.require("evolution.level") != "attempt":
            raise ProtocolError("only attempt-level evolution is authorized")
        if self.require("reflector.provider") != "codex_cli":
            raise ProtocolError("reflector provider must be codex_cli")
        if self.require("reflector.model") != MANAGED_CODEX_MODEL:
            raise ProtocolError("reflector model must be gpt-5.5")
        reflector_exact = {
            "reflector.runtime_mode": "managed",
            "reflector.runtime_profile": MANAGED_RUNTIME_PROFILE,
            "reflector.codex_binary": MANAGED_CODEX_BINARY,
            "reflector.codex_cli_version": MANAGED_CODEX_VERSION,
            "reflector.auth_mode": "subscription",
            "reflector.capture_mode": "transcript",
            "reflector.host_path_fallback_allowed": False,
        }
        for key, expected_value in reflector_exact.items():
            if self.require(key) != expected_value:
                raise ProtocolError(f"{key} differs from the managed reflector pin")
        expected_digest = MANAGED_RUNTIME_IMAGE.split("@", 1)[1]
        if self.require("reflector.runtime_image_digest") != expected_digest:
            raise ProtocolError("reflector runtime digest differs from the Core release pin")
        if self.require("candidate.max_tool_steps") != 300:
            raise ProtocolError("candidate max_tool_steps must remain 300")
        if self.require("candidate.token_limit") != 131072:
            raise ProtocolError("candidate token_limit must remain 131072")
        if self.require("reflector.max_tool_steps") != 80:
            raise ProtocolError("reflector max_tool_steps must remain 80")
        if self.require("reflector.max_runtime_seconds") != 900:
            raise ProtocolError("reflector max runtime must remain 900 seconds")
        if self.require("budgets.max_candidate_model_calls") != EXPECTED_CANDIDATE_RUNS:
            raise ProtocolError("candidate call budget must equal the frozen schedule")
        if self.require("budgets.max_reflector_model_calls") != 170:
            raise ProtocolError("reflector call budget must include three GEPA proposals")
        if self.require("official_freeze.evolution_enabled") is not False:
            raise ProtocolError("official freeze must disable evolution")
        if self.require("official_freeze.reflector_enabled") is not False:
            raise ProtocolError("official freeze must disable reflector")
        if self.require("official_freeze.teacher_enabled") is not False:
            raise ProtocolError("official freeze must disable teacher")
        if self.require("official_freeze.feedback_released") is not False:
            raise ProtocolError("official feedback must remain withheld")

    def unresolved_fields(self) -> list[str]:
        fields = (
            "candidate.reasoning_level",
            "candidate.max_tool_steps",
            "candidate.token_limit",
            "candidate.cost_limit_usd",
            "reflector.reasoning_level",
            "judge.model",
            "judge.provider",
            "budgets.cumulative_runtime_seconds",
            "budgets.cumulative_cost_usd",
            "randomness_policy.seed",
            "randomness_policy.candidate_sampling",
            "native_openevo.framework_lock",
        )
        return sorted(field for field in fields if _is_unresolved(self.require(field)))

    def reflector_readiness_receipt_valid(self) -> bool:
        try:
            path = Path(self.require("reflector.readiness_receipt")).resolve(strict=True)
            if not path.is_relative_to(self.experiment_root):
                return False
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False
        return bool(
            isinstance(payload, dict)
            and payload.get("schema_version") == "openevo.managed_reflector_readiness.v1"
            and payload.get("runtime_profile") == MANAGED_RUNTIME_PROFILE
            and payload.get("runtime_digest") == MANAGED_RUNTIME_IMAGE.split("@", 1)[1]
            and payload.get("codex_binary") == MANAGED_CODEX_BINARY
            and payload.get("actual_cli_version") == MANAGED_CODEX_VERSION
            and payload.get("auth_mode") == "subscription"
            and payload.get("capture_mode") == "transcript"
            and payload.get("path_fallback_allowed") is False
            and payload.get("exit_status") == 0
        )

    def environment_readiness(self, environ: dict[str, str] | None = None) -> dict[str, Any]:
        source = os.environ if environ is None else environ
        judge_names = ("JUDGE_API_KEY", "JUDGE_API_BASE", "JUDGE_MODEL_NAME")
        contamination = self.require("gates.contamination_receipt_status")
        reflector_status = self.require("gates.reflector_runtime_status")
        teacher_status = self.require("feedback.native_post_run_attachment_status")
        production_attachment_transport = self.require(
            "gates.production_training_feedback_transport_status"
        )
        formal_orchestrator = self.require(
            "gates.formal_training_orchestrator_status"
        )
        framework_lock_present = Path(
            self.require("native_openevo.framework_lock")
        ).is_file()
        reflector_receipt_valid = self.reflector_readiness_receipt_valid()
        static_ready = (
            not self.unresolved_fields()
            and contamination == "PASS"
            and reflector_status == "PINNED"
            and reflector_receipt_valid
            and teacher_status == "ATTACHED_TO_NATIVE_COMPLETED_TASK_DATASET"
            and production_attachment_transport == "READY"
            and formal_orchestrator == "READY"
            and framework_lock_present
        )
        blockers: list[str] = []
        if reflector_status != "PINNED":
            blockers.append(REFLECTOR_RUNTIME_BLOCKER)
        elif not reflector_receipt_valid:
            blockers.append("REFLECTOR_RUNTIME_RECEIPT_INVALID")
        if teacher_status != "ATTACHED_TO_NATIVE_COMPLETED_TASK_DATASET":
            blockers.append(TEACHER_ATTACHMENT_BLOCKER)
        if production_attachment_transport != "READY":
            blockers.append("PRODUCTION_TRAINING_FEEDBACK_TRANSPORT_UNAVAILABLE")
        if formal_orchestrator != "READY":
            blockers.append("FORMAL_TRAINING_ORCHESTRATOR_UNAVAILABLE")
        if not framework_lock_present:
            blockers.append("VERIFIED_FRAMEWORK_LOCK_MISSING")
        missing_judge = [name for name in judge_names if not source.get(name)]
        if missing_judge:
            blockers.append("JUDGE_CREDENTIALS_MISSING")
        return {
            "native_candidate_contract_complete": True,
            "candidate_runtime_pinned": True,
            "reflector_runtime_status": reflector_status,
            "reflector_runtime_receipt_valid": reflector_receipt_valid,
            "teacher_attachment_status": teacher_status,
            "production_training_feedback_transport_status": production_attachment_transport,
            "formal_training_orchestrator_status": formal_orchestrator,
            "contamination_receipt_status": contamination,
            "judge_environment_present": {name: bool(source.get(name)) for name in judge_names},
            "missing_judge_environment": missing_judge,
            "unresolved_protocol_fields": self.unresolved_fields(),
            "framework_lock_present": framework_lock_present,
            "ready": static_ready and all(bool(source.get(name)) for name in judge_names),
            "blockers": blockers,
        }
