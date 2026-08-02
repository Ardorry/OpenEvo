"""Closed configuration for Temperature full-evolve v1."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from openevo_chembench.chembench4k_models import CHEMBENCH4K_REVISION
from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes, sha256_bytes
from openevo_chembench.temperature_full_evolve_v1.formal_runtime import (
    FORMAL_FRAMEWORK_LOCK_RELATIVE,
)

CONFIG_SCHEMA = "chembench_temperature_full_evolve_config_v1"
PROTOCOL_ID = "chembench_temperature_full_evolve_v1"
CATEGORY = "Temperature_Prediction"
TRAIN_COUNT = 100
TEST_COUNT = 100
RESERVE_COUNT = 2
BATCH_SIZE = 25
SPLIT_NAMESPACE = "chembench-temperature-full-evolve-v1-split-v1-20260802"
TARGETS = ("text_memory", "skill_bundle", "agent_system")
HISTORICAL_EXPOSURE_POLICY = "fixed_official_temperature_pool_fresh_c0"
EXPECTED_CODEX_SHA256 = "a96f944d1a596dbfb7fdd84f482be5c50e34b04bb371126840d873e4ebf26902"
EXPECTED_CANDIDATE_IMAGE_ID = (
    "sha256:7a0079f9cb1bce5768cff5bce3d1181811c6a231ad800cac8fb503d66852c81b"
)


class TemperatureFullEvolveConfigError(RuntimeError):
    """Closed config validation failure."""


@dataclass(frozen=True, slots=True)
class TemperatureFullEvolveConfigV1:
    path: Path
    payload: dict[str, Any]
    digest: str

    @property
    def model(self) -> str:
        return str(self.payload["model"]["name"])

    @property
    def reasoning_effort(self) -> str:
        return str(self.payload["model"]["reasoning_effort"])

    @property
    def rollout_url(self) -> str:
        return str(self.payload["executor"]["rollout_url"])

    @property
    def task_timeout_seconds(self) -> int:
        return int(self.payload["executor"]["timeout_seconds"])

    @property
    def reflector_timeout_seconds(self) -> int:
        return int(self.payload["reflector"]["timeout_seconds"])

    @property
    def framework_lock_relative(self) -> str:
        return str(self.payload["core"]["framework_lock"])

    @property
    def state_root_relative(self) -> str:
        return str(self.payload["roots"]["state"])

    @property
    def result_root_relative(self) -> str:
        return str(self.payload["roots"]["results"])

    @property
    def report_root_relative(self) -> str:
        return str(self.payload["roots"]["reports"])

    @property
    def split_namespace(self) -> str:
        return str(self.payload["split"]["namespace"])


def load_temperature_full_evolve_config(path: Path) -> TemperatureFullEvolveConfigV1:
    if not isinstance(path, Path) or not path.is_absolute():
        raise TypeError("config path must be absolute")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise TemperatureFullEvolveConfigError("TEMPERATURE_CONFIG_UNAVAILABLE") from exc
    if type(payload) is not dict:
        raise TemperatureFullEvolveConfigError("TEMPERATURE_CONFIG_INVALID")
    _validate_config(payload)
    return TemperatureFullEvolveConfigV1(
        path=path,
        payload=payload,
        digest=sha256_bytes(canonical_json_bytes(payload)),
    )


def _validate_config(payload: dict[str, Any]) -> None:
    expected_top_level = {
        "schema_version",
        "protocol_id",
        "classification",
        "protocol_scope",
        "dataset",
        "split",
        "roots",
        "model",
        "codex_runtime",
        "executor",
        "reflector",
        "core",
        "parser_evaluator",
        "limits",
        "monitor",
    }
    if set(payload) != expected_top_level:
        raise TemperatureFullEvolveConfigError("TEMPERATURE_CONFIG_SCHEMA_INVALID")
    if payload["schema_version"] != CONFIG_SCHEMA or payload["protocol_id"] != PROTOCOL_ID:
        raise TemperatureFullEvolveConfigError("TEMPERATURE_CONFIG_IDENTITY_INVALID")
    if payload["classification"] != [
        "RESEARCH_ONLY_SUPERVISED_EVOLUTION",
        "TEMPERATURE_PREDICTION_ONLY",
        "FRESH_GENERATION_ZERO_CONTEXT",
        "CURRENT_RUN_PAIRED_HOLDOUT",
        "HISTORICAL_EXPOSURE_NOT_USED_FOR_SELECTION",
        "NOT_STRICT_NEVER_EXPOSED_GENERALIZATION",
        "NOT_A_STANDARD_CHEMBENCH4K_LEADERBOARD_RUN",
    ]:
        raise TemperatureFullEvolveConfigError("TEMPERATURE_CLASSIFICATION_INVALID")
    if payload["protocol_scope"] != {
        "protocol_fixed_at_local_date": "2026-08-02",
        "historical_exposure_policy": HISTORICAL_EXPOSURE_POLICY,
        "prior_artifact_import": "forbidden",
        "prior_completion_import": "forbidden",
        "prior_database_import": "forbidden",
    }:
        raise TemperatureFullEvolveConfigError("TEMPERATURE_PROTOCOL_SCOPE_INVALID")
    if payload["dataset"] != {
        "repository": "AI4Chem/ChemBench4K",
        "revision": CHEMBENCH4K_REVISION,
        "root": f"data/chembench4k/AI4Chem_ChemBench4K/{CHEMBENCH4K_REVISION}",
        "category": CATEGORY,
        "source_split": "test",
    }:
        raise TemperatureFullEvolveConfigError("TEMPERATURE_DATASET_IDENTITY_INVALID")
    split = payload["split"]
    if (
        type(split) is not dict
        or set(split)
        != {
            "namespace",
            "train_count",
            "test_count",
            "reserve_count",
            "batch_size",
            "group_policy",
        }
        or split.get("train_count") != TRAIN_COUNT
        or split.get("test_count") != TEST_COUNT
        or split.get("reserve_count") != RESERVE_COUNT
        or split.get("batch_size") != BATCH_SIZE
        or split.get("group_policy") != "temperature_public_fields_grouping_v1"
        or split.get("namespace") != SPLIT_NAMESPACE
    ):
        raise TemperatureFullEvolveConfigError("TEMPERATURE_SPLIT_POLICY_INVALID")
    if TRAIN_COUNT % BATCH_SIZE:
        raise AssertionError("frozen Train count must divide into complete batches")
    if payload["roots"] != {
        "results": "results/chembench_temperature_full_evolve_v1",
        "state": "state/chembench_temperature_full_evolve_v1",
        "reports": "reports/chembench_temperature_full_evolve_v1",
    }:
        raise TemperatureFullEvolveConfigError("TEMPERATURE_ROOTS_INVALID")
    if payload["model"] != {"name": "gpt-5.5", "reasoning_effort": "medium"}:
        raise TemperatureFullEvolveConfigError("TEMPERATURE_MODEL_IDENTITY_INVALID")
    runtime = payload["codex_runtime"]
    if (
        type(runtime) is not dict
        or set(runtime)
        != {
            "source",
            "shared_verified_runtime_protocol",
            "npm_package",
            "cli_version",
            "executable_sha256",
            "candidate_image_id",
        }
        or runtime.get("source") != "openevo_core_managed_codex_v1"
        or runtime.get("shared_verified_runtime_protocol")
        != "chembench_supervised_transfer_v2"
        or runtime.get("npm_package") != "@openai/codex@0.144.1"
        or runtime.get("cli_version") != "codex-cli 0.144.1"
        or runtime.get("executable_sha256") != EXPECTED_CODEX_SHA256
        or runtime.get("candidate_image_id") != EXPECTED_CANDIDATE_IMAGE_ID
    ):
        raise TemperatureFullEvolveConfigError("TEMPERATURE_CODEX_IDENTITY_INVALID")
    executor = payload["executor"]
    if (
        type(executor) is not dict
        or set(executor)
        != {
            "rollout_url",
            "runtime_services_state_root",
            "runtime_services_identity_required",
            "timeout_seconds",
            "runtime_profile",
            "auth_mode",
            "capture_mode",
            "reasoning_effort",
            "approval_policy",
            "tool_policy",
            "network_policy",
            "allow_internet",
            "mcp_servers",
            "infrastructure_retry_limit",
        }
        or executor.get("rollout_url") != "http://127.0.0.1:8080"
        or executor.get("runtime_services_state_root")
        != "state/chembench_temperature_full_evolve_v1/runtime_services"
        or executor.get("runtime_services_identity_required") is not True
        or executor.get("timeout_seconds") != 1200
        or executor.get("runtime_profile") != "managed_science"
        or executor.get("auth_mode") != "subscription"
        or executor.get("capture_mode") != "transcript"
        or executor.get("reasoning_effort") != "medium"
        or executor.get("approval_policy") != "never"
        or executor.get("tool_policy") != "zero_tool_transcript_audit"
        or executor.get("network_policy")
        != "model_transport_with_zero_tool_fail_closed"
        or executor.get("allow_internet") is not True
        or executor.get("mcp_servers") != []
        or executor.get("infrastructure_retry_limit") != 3
    ):
        raise TemperatureFullEvolveConfigError("TEMPERATURE_EXECUTOR_POLICY_INVALID")
    reflector = payload["reflector"]
    if (
        type(reflector) is not dict
        or set(reflector)
        != {
            "execution_path",
            "timeout_seconds",
            "model",
            "reasoning_effort",
            "auth_mode",
            "capture_mode",
            "approval_policy",
            "tool_policy",
            "network_policy",
            "mcp_servers",
            "logical_calls_per_batch",
        }
        or reflector.get("execution_path") != "TaskRequest_Rollout_Gateway_CodexHarness"
        or reflector.get("timeout_seconds") != 1800
        or reflector.get("model") != "gpt-5.5"
        or reflector.get("reasoning_effort") != "medium"
        or reflector.get("auth_mode") != "subscription"
        or reflector.get("capture_mode") != "transcript"
        or reflector.get("approval_policy") != "never"
        or reflector.get("tool_policy") != "zero_tool_transcript_audit"
        or reflector.get("network_policy")
        != "model_transport_with_zero_tool_fail_closed"
        or reflector.get("mcp_servers") != []
        or reflector.get("logical_calls_per_batch") != 1
    ):
        raise TemperatureFullEvolveConfigError("TEMPERATURE_REFLECTOR_POLICY_INVALID")
    core = payload["core"]
    if (
        type(core) is not dict
        or set(core)
        != {"framework_lock", "jobs_per_batch", "targets", "target_methods"}
        or core.get("framework_lock") != FORMAL_FRAMEWORK_LOCK_RELATIVE
        or core.get("jobs_per_batch") != 3
        or tuple(core.get("targets", ())) != TARGETS
        or core.get("target_methods")
        != {
            "text_memory": "text_memory",
            "skill_bundle": "skill_bundle",
            "agent_system": "agent_system",
        }
    ):
        raise TemperatureFullEvolveConfigError("TEMPERATURE_CORE_POLICY_INVALID")
    if payload["parser_evaluator"] != {
        "official_parser": "first_uppercase_opencompass_compatible",
        "strict_parser": "strict_single_letter_parser",
        "evaluator": "ChemBench4KPrivateEvaluator",
    }:
        raise TemperatureFullEvolveConfigError("TEMPERATURE_EVALUATION_POLICY_INVALID")
    if payload["limits"] != {
        "text_memory_max_utf8_bytes": 4096,
        "skill_bundle_max_utf8_bytes": 1536,
        "agent_system_max_utf8_bytes": 1024,
        "target_total_utf8_bytes": 6144,
        "hard_total_utf8_bytes": 8192,
        "reflector_response_max_utf8_bytes": 65536,
        "evidence_rule_limit": 64,
    }:
        raise TemperatureFullEvolveConfigError("TEMPERATURE_LIMITS_INVALID")
    if payload["monitor"] != {"interval_seconds": 600, "stagnant_poll_limit": 2}:
        raise TemperatureFullEvolveConfigError("TEMPERATURE_MONITOR_POLICY_INVALID")
    # Reject YAML-only values and keep the digest stable across loaders.
    canonical_json_bytes(payload)


__all__ = [
    "BATCH_SIZE",
    "CATEGORY",
    "CONFIG_SCHEMA",
    "EXPECTED_CANDIDATE_IMAGE_ID",
    "EXPECTED_CODEX_SHA256",
    "HISTORICAL_EXPOSURE_POLICY",
    "PROTOCOL_ID",
    "RESERVE_COUNT",
    "SPLIT_NAMESPACE",
    "TARGETS",
    "TEST_COUNT",
    "TRAIN_COUNT",
    "TemperatureFullEvolveConfigError",
    "TemperatureFullEvolveConfigV1",
    "load_temperature_full_evolve_config",
]
