"""Closed configuration for the simplified supervised transfer v3 protocol."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from openevo_chembench.chembench4k_models import CHEMBENCH4K_REVISION
from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes, sha256_bytes

PROTOCOL_ID = "chembench_supervised_transfer_v3_three_answer_online_only"
TWO_ROUND_ONE_EVOLUTION_PROTOCOL_ID = (
    "chembench_supervised_transfer_v3_two_round_one_evolution_online_only"
)
CONFIG_SCHEMA = "chembench_supervised_transfer_config_v3"
SOURCE_FAMILY_ID = "chembench_supervised_transfer_v3_source_family"
SOURCE_SPLIT_PROTOCOL = "chembench_supervised_transfer_v2"
TRAIN_COUNT = 450
TEST_COUNT = 450
RESERVE_COUNT = 3109
TRAIN_PER_CATEGORY = 50
TEST_PER_CATEGORY = 50
TRAIN_ROUNDS = 3
EVOLUTION_CYCLES = 2
_TRAIN_SCHEDULES = {
    PROTOCOL_ID: (TRAIN_ROUNDS, EVOLUTION_CYCLES),
    TWO_ROUND_ONE_EVOLUTION_PROTOCOL_ID: (2, 1),
}
TARGETS = ("text_memory", "skill_bundle", "agent_system")
RESULT_ROOT = "results/chembench_supervised_transfer_v3"
STATE_ROOT = "state/chembench_supervised_transfer_v3"
REPORT_ROOT = "reports/chembench_supervised_transfer_v3"
MANIFEST_ROOT = "benchmarks/chembench/manifests/supervised_transfer_v3"
SOURCE_MANIFEST_RELATIVE = f"{MANIFEST_ROOT}/source_manifest_v3.json"
SPLIT_REFERENCE_RELATIVE = f"{MANIFEST_ROOT}/split_reference_receipt_v3.json"
V2_MANIFEST_ROOT = "benchmarks/chembench/manifests/supervised_transfer_v2"


class SupervisedTransferV3ConfigError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SupervisedTransferConfigV3:
    path: Path
    payload: dict[str, Any]
    digest: str

    @property
    def protocol_id(self) -> str:
        return str(self.payload["protocol_id"])

    @property
    def train_rounds(self) -> int:
        return int(self.payload["executor"]["task_attempts_per_train_item"])

    @property
    def evolution_cycles(self) -> int:
        return int(self.payload["executor"]["evolution_cycles_per_train_item"])

    @property
    def train_sequence(self) -> tuple[str, ...]:
        sequence: list[str] = []
        for round_index in range(self.train_rounds):
            suffix = "_FINAL" if round_index == self.train_rounds - 1 else ""
            sequence.append(f"ROUND_{round_index}{suffix}")
            if round_index < self.evolution_cycles:
                sequence.append(f"CYCLE_{round_index + 1}")
        return tuple(sequence)

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
    def reflector_timeout_seconds(self) -> int:
        return int(self.payload["reflector"]["timeout_seconds"])

    @property
    def rollout_url(self) -> str:
        return str(self.payload["executor"]["rollout_url"])

    @property
    def framework_lock(self) -> str:
        return str(self.payload["reflector"]["framework_lock"])

    @property
    def call_budget(self) -> dict[str, int]:
        online_candidate = TRAIN_COUNT * self.train_rounds
        reflector = TRAIN_COUNT * self.evolution_cycles
        test_candidate = TEST_COUNT
        candidate = online_candidate + test_candidate
        answer_and_reflector = candidate + reflector
        readiness_minimum = candidate
        readiness_maximum = candidate * 2
        return {
            "online_candidate_task_calls": online_candidate,
            "reflector_calls": reflector,
            "evolved_test_candidate_calls": test_candidate,
            "candidate_task_calls": candidate,
            "candidate_subscription_readiness_calls_minimum": readiness_minimum,
            "candidate_subscription_readiness_calls_maximum": readiness_maximum,
            "answer_and_reflector_model_calls": answer_and_reflector,
            "total_model_calls": answer_and_reflector + readiness_minimum,
            "maximum_model_calls": answer_and_reflector + readiness_maximum,
            "core_jobs": reflector * len(TARGETS),
            "typed_artifacts": reflector * len(TARGETS),
            "context_resolutions": reflector * len(TARGETS),
        }


def load_config_v3(path: Path) -> SupervisedTransferConfigV3:
    if not isinstance(path, Path) or not path.is_absolute():
        raise TypeError("config path must be absolute")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SupervisedTransferV3ConfigError("V3_CONFIG_UNAVAILABLE") from exc
    if type(payload) is not dict:
        raise SupervisedTransferV3ConfigError("V3_CONFIG_INVALID")
    _validate_config(payload)
    digest = sha256_bytes(canonical_json_bytes(payload))
    return SupervisedTransferConfigV3(path=path, payload=payload, digest=digest)


def _validate_config(payload: dict[str, Any]) -> None:
    required = {
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
    }
    if set(payload) != required:
        raise SupervisedTransferV3ConfigError("V3_CONFIG_SCHEMA_INVALID")
    if payload["schema_version"] != CONFIG_SCHEMA or payload["protocol_id"] not in _TRAIN_SCHEDULES:
        raise SupervisedTransferV3ConfigError("V3_CONFIG_IDENTITY_INVALID")
    if payload["dataset"] != {
        "repository": "AI4Chem/ChemBench4K",
        "revision": CHEMBENCH4K_REVISION,
        "root": (
            "data/chembench4k/AI4Chem_ChemBench4K/"
            f"{CHEMBENCH4K_REVISION}"
        ),
    }:
        raise SupervisedTransferV3ConfigError("V3_DATASET_IDENTITY_INVALID")
    if payload["split"] != {
        "source_protocol": SOURCE_SPLIT_PROTOCOL,
        "source_manifest_root": V2_MANIFEST_ROOT,
        "reference_receipt": SPLIT_REFERENCE_RELATIVE,
        "train_per_category": TRAIN_PER_CATEGORY,
        "test_per_category": TEST_PER_CATEGORY,
    }:
        raise SupervisedTransferV3ConfigError("V3_SPLIT_IDENTITY_INVALID")
    if payload["roots"] != {
        "results": RESULT_ROOT,
        "state": STATE_ROOT,
        "reports": REPORT_ROOT,
    }:
        raise SupervisedTransferV3ConfigError("V3_ROOTS_INVALID")
    if payload["model"] != {"name": "gpt-5.5", "reasoning_effort": "medium"}:
        raise SupervisedTransferV3ConfigError("V3_MODEL_INVALID")
    executor = payload["executor"]
    train_rounds, evolution_cycles = _TRAIN_SCHEDULES[str(payload["protocol_id"])]
    if (
        type(executor) is not dict
        or executor.get("task_attempts_per_train_item") != train_rounds
        or executor.get("evolution_cycles_per_train_item") != evolution_cycles
        or train_rounds != evolution_cycles + 1
        or executor.get("final_test_attempts_per_item") != 1
        or executor.get("control_train_enabled") is not False
        or executor.get("control_test_enabled") is not False
        or executor.get("probe_enabled") is not False
        or executor.get("timeout_seconds") != 1200
        or executor.get("auth_mode") != "subscription"
        or executor.get("capture_mode") != "transcript"
        or executor.get("mcp_servers") != []
    ):
        raise SupervisedTransferV3ConfigError("V3_EXECUTOR_POLICY_INVALID")
    reflector = payload["reflector"]
    if (
        type(reflector) is not dict
        or reflector.get("timeout_seconds") != 1800
        or reflector.get("model") != "gpt-5.5"
        or reflector.get("reasoning_effort") != "medium"
        or reflector.get("call_strategy")
        != "one_structured_call_per_cycle_three_typed_artifacts"
        or reflector.get("mcp_servers") != []
    ):
        raise SupervisedTransferV3ConfigError("V3_REFLECTOR_POLICY_INVALID")
    if tuple(payload["targets"]) != TARGETS:
        raise SupervisedTransferV3ConfigError("V3_TARGETS_INVALID")
    # Canonical JSON round-trip rejects non-JSON YAML extensions and keeps the digest stable.
    json.loads(canonical_json_bytes(payload))


__all__ = [
    "CONFIG_SCHEMA",
    "EVOLUTION_CYCLES",
    "MANIFEST_ROOT",
    "PROTOCOL_ID",
    "REPORT_ROOT",
    "RESERVE_COUNT",
    "RESULT_ROOT",
    "SOURCE_FAMILY_ID",
    "SOURCE_MANIFEST_RELATIVE",
    "SOURCE_SPLIT_PROTOCOL",
    "SPLIT_REFERENCE_RELATIVE",
    "STATE_ROOT",
    "TARGETS",
    "TEST_COUNT",
    "TEST_PER_CATEGORY",
    "TRAIN_COUNT",
    "TRAIN_PER_CATEGORY",
    "TRAIN_ROUNDS",
    "TWO_ROUND_ONE_EVOLUTION_PROTOCOL_ID",
    "V2_MANIFEST_ROOT",
    "SupervisedTransferConfigV3",
    "SupervisedTransferV3ConfigError",
    "load_config_v3",
]
