"""Closed configuration and paid-call plan for supervised transfer v2."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from openevo.runtime.managed import MANAGED_CODEX_NPM_PACKAGE, MANAGED_CODEX_VERSION

from openevo_chembench.chembench4k_models import CHEMBENCH4K_REVISION

PROTOCOL_ID = "chembench_supervised_transfer_v2"
CONFIG_SCHEMA = "chembench_supervised_transfer_config_v2"
SPLIT_SEED = "openevo-chembench-supervised-transfer-v2-train-test"
TRAIN_PER_CATEGORY = 50
TEST_PER_CATEGORY = 50
TRAIN_COUNT = 450
TEST_COUNT = 450
TRAIN_ROUNDS = 4
EVOLUTION_CYCLES = 3
TARGETS = ("text_memory", "skill_bundle", "agent_system")
RESULT_ROOT = "results/chembench_supervised_transfer_v2"
STATE_ROOT = "state/chembench_supervised_transfer_v2"
REPORT_ROOT = "reports/chembench_supervised_transfer_v2"
MANIFEST_ROOT = "benchmarks/chembench/manifests/supervised_transfer_v2"
CODEX_ROOT = f"{STATE_ROOT}/managed_codex"
CODEX_SOURCE = "openevo_core_managed_codex_v1"
EXPECTED_CODEX_CLI_VERSION = f"codex-cli {MANAGED_CODEX_VERSION}"
EXPECTED_CODEX_SHA256 = "a96f944d1a596dbfb7fdd84f482be5c50e34b04bb371126840d873e4ebf26902"
REFLECTOR_CALL_STRATEGY = "one_structured_call_per_cycle_three_typed_artifacts"


class SupervisedTransferV2ConfigError(ValueError):
    """Closed configuration error."""


@dataclass(frozen=True, slots=True)
class SupervisedTransferConfigV2:
    path: Path
    payload: dict[str, Any]

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical(self.payload)).hexdigest()

    @property
    def model(self) -> str:
        return str(self.payload["model"]["name"])

    @property
    def reasoning_effort(self) -> str:
        return str(self.payload["model"]["reasoning_effort"])

    @property
    def task_timeout_seconds(self) -> int:
        return int(self.payload["executor"]["timeout_seconds"])

    @property
    def rollout_url(self) -> str:
        return str(self.payload["executor"]["rollout_url"])

    @property
    def reflector_timeout_seconds(self) -> int:
        return int(self.payload["reflector"]["timeout_seconds"])

    @property
    def framework_lock(self) -> str:
        return str(self.payload["reflector"]["framework_lock"])

    @property
    def call_budget(self) -> dict[str, int]:
        control_train = TRAIN_COUNT * TRAIN_ROUNDS
        online_train = TRAIN_COUNT * TRAIN_ROUNDS
        reflector = TRAIN_COUNT * EVOLUTION_CYCLES
        final_test = TEST_COUNT * 2
        return {
            "control_train_task_calls": control_train,
            "online_train_task_calls": online_train,
            "online_train_reflector_calls": reflector,
            "online_train_core_jobs": reflector * len(TARGETS),
            "final_test_task_calls": final_test,
            "total_model_calls": control_train + online_train + reflector + final_test,
        }


def load_config_v2(path: Path) -> SupervisedTransferConfigV2:
    if not isinstance(path, Path) or not path.is_absolute():
        raise TypeError("config path must be absolute")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SupervisedTransferV2ConfigError("CONFIG_UNAVAILABLE") from exc
    if type(payload) is not dict:
        raise SupervisedTransferV2ConfigError("CONFIG_SCHEMA_INVALID")
    _require_keys(
        payload,
        {
            "schema_version",
            "protocol_id",
            "classification",
            "dataset",
            "split",
            "roots",
            "model",
            "codex_runtime",
            "executor",
            "reflector",
            "targets",
            "limits",
        },
    )
    if payload["schema_version"] != CONFIG_SCHEMA or payload["protocol_id"] != PROTOCOL_ID:
        raise SupervisedTransferV2ConfigError("CONFIG_IDENTITY_INVALID")
    dataset = _mapping(payload["dataset"])
    split = _mapping(payload["split"])
    roots = _mapping(payload["roots"])
    model = _mapping(payload["model"])
    codex = _mapping(payload["codex_runtime"])
    executor = _mapping(payload["executor"])
    reflector = _mapping(payload["reflector"])
    limits = _mapping(payload["limits"])
    if (
        dataset
        != {
            "repository": "AI4Chem/ChemBench4K",
            "revision": CHEMBENCH4K_REVISION,
            "root": f"data/chembench4k/AI4Chem_ChemBench4K/{CHEMBENCH4K_REVISION}",
        }
        or split
        != {
            "seed": SPLIT_SEED,
            "train_per_category": TRAIN_PER_CATEGORY,
            "test_per_category": TEST_PER_CATEGORY,
            "manifest_root": MANIFEST_ROOT,
        }
        or roots != {"results": RESULT_ROOT, "state": STATE_ROOT, "reports": REPORT_ROOT}
        or model != {"name": "gpt-5.5", "reasoning_effort": "medium"}
    ):
        raise SupervisedTransferV2ConfigError("CONFIG_FROZEN_VALUE_INVALID")
    if codex != {
        "source": CODEX_SOURCE,
        "npm_package": MANAGED_CODEX_NPM_PACKAGE,
        "cli_version": EXPECTED_CODEX_CLI_VERSION,
        "executable_sha256": EXPECTED_CODEX_SHA256,
        "root": CODEX_ROOT,
    }:
        raise SupervisedTransferV2ConfigError("MANAGED_CODEX_CONFIG_INVALID")
    if executor != {
        "rollout_url": "http://127.0.0.1:8080",
        "runtime_services_topology": (
            "benchmarks/chembench/configs/supervised_transfer_v2/"
            "openevo_runtime_services_v2.yaml"
        ),
        "runtime_services_state_root": f"{STATE_ROOT}/runtime_services",
        "runtime_services_identity_required": True,
        "timeout_seconds": 1200,
        "runtime_profile": "managed_science",
        "auth_mode": "subscription",
        "capture_mode": "transcript",
        "approval_policy": "never",
        "sandbox_policy": "openevo_codex_subscription_v1",
        "tool_policy": "zero_tool_transcript_audit",
        "network_policy": "model_transport_with_zero_tool_fail_closed",
        "allow_internet": True,
        "mcp_servers": [],
        "max_output_tokens": None,
        "task_attempts_per_train_item": TRAIN_ROUNDS,
        "evolution_cycles_per_train_item": EVOLUTION_CYCLES,
        "final_test_attempts_per_item": 1,
    }:
        raise SupervisedTransferV2ConfigError("EXECUTOR_POLICY_INVALID")
    if reflector != {
        "method_id": "text_memory_expel_reflector",
        "framework_lock": f"{STATE_ROOT}/framework/framework-lock.json",
        "timeout_seconds": 1800,
        "model": "gpt-5.5",
        "reasoning_effort": "medium",
        "auth_mode": "subscription",
        "approval_policy": "never",
        "sandbox_policy": "bubblewrap_read_only",
        "tool_policy": "zero_tool_event_gate",
        "network_policy": "model_transport_only",
        "mcp_servers": [],
        "max_output_tokens": None,
        "max_last_message_bytes": 65536,
        "call_strategy": REFLECTOR_CALL_STRATEGY,
    }:
        raise SupervisedTransferV2ConfigError("REFLECTOR_POLICY_INVALID")
    if tuple(payload["targets"]) != TARGETS:
        raise SupervisedTransferV2ConfigError("TARGET_ORDER_INVALID")
    if limits != {
        "text_memory": {
            "max_utf8_bytes": 24576,
            "max_estimated_tokens": 6144,
            "max_confirmed_rules": 48,
            "max_provisional_rules": 24,
            "max_retired_rules": 24,
        },
        "skill_bundle": {
            "max_utf8_bytes": 24576,
            "max_estimated_tokens": 6144,
            "max_workflow_steps": 48,
            "max_failure_rules": 24,
        },
        "agent_system": {
            "max_utf8_bytes": 12288,
            "max_estimated_tokens": 3072,
            "max_instruction_rules": 36,
        },
    }:
        raise SupervisedTransferV2ConfigError("TARGET_LIMITS_INVALID")
    config = SupervisedTransferConfigV2(path=path, payload=payload)
    if config.call_budget["total_model_calls"] != 5850:
        raise AssertionError("paid-call plan drifted")
    return config


def _mapping(value: object) -> dict[str, Any]:
    if type(value) is not dict:
        raise SupervisedTransferV2ConfigError("CONFIG_SCHEMA_INVALID")
    return value


def _require_keys(value: dict[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise SupervisedTransferV2ConfigError("CONFIG_SCHEMA_INVALID")


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


__all__ = [
    "CODEX_ROOT",
    "EVOLUTION_CYCLES",
    "EXPECTED_CODEX_CLI_VERSION",
    "EXPECTED_CODEX_SHA256",
    "MANIFEST_ROOT",
    "PROTOCOL_ID",
    "REFLECTOR_CALL_STRATEGY",
    "REPORT_ROOT",
    "RESULT_ROOT",
    "SPLIT_SEED",
    "STATE_ROOT",
    "TARGETS",
    "TEST_COUNT",
    "TEST_PER_CATEGORY",
    "TRAIN_COUNT",
    "TRAIN_PER_CATEGORY",
    "TRAIN_ROUNDS",
    "SupervisedTransferConfigV2",
    "SupervisedTransferV2ConfigError",
    "load_config_v2",
]
