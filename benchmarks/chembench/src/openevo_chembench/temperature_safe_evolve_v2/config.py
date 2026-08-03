"""Closed, content-free configuration for Temperature Safe-Evolve V2."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import yaml

from openevo_chembench.chembench4k_models import CHEMBENCH4K_REVISION
from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes, sha256_bytes

CONFIG_SCHEMA: Final = "chembench_temperature_safe_evolve_config_v2"
PROTOCOL_ID: Final = "chembench_temperature_safe_evolve_v2"
CLASSIFICATION: Final = "reused_official_temperature_pool_independent_state_mechanism_experiment"
CATEGORY: Final = "Temperature_Prediction"
EXPECTED_POOL_COUNT: Final = 202
FOLD_NAMESPACE: Final = "chembench-temperature-safe-evolve-v2-folds-v1"
HALF_NAMESPACE: Final = "chembench-temperature-safe-evolve-v2-ablation-halves-v1"
EXPECTED_CODEX_SHA256: Final = "a96f944d1a596dbfb7fdd84f482be5c50e34b04bb371126840d873e4ebf26902"
EXPECTED_RUNTIME_V8_IDENTITY: Final = (
    "3ee72fc62662b5e09c64734d02f94275351a30a31bd2b891b20fabdb01cb2f2d"
)
FROZEN_C4: Final = MappingProxyType(
    {
        "text_memory": (3455, "0cd3998a0f773977b05803b4c482ad70009c3a7027593cb7501e0ba06af23a57"),
        "skill_bundle": (784, "128d771e8399a12bf6c967da8006d252ebb6262ef00de21d15550ca903e62f50"),
        "agent_system": (492, "ebf98e210c2c025b9c1f9ebb30b54057823e02ddbfdaab00538c5d5c6bc2a903"),
        "combined": (4731, "0c2e6272788d36822733c4bb6f3572e605830ba956fb14cbd76cb29c8286f6b4"),
    }
)
ARM_TARGETS: Final = MappingProxyType(
    {
        "A0": (),
        "A1": ("text_memory",),
        "A2": ("skill_bundle",),
        "A3": ("agent_system",),
        "A4": ("text_memory", "skill_bundle"),
        "A5": ("text_memory", "agent_system"),
        "A6": ("skill_bundle", "agent_system"),
        "A7": ("text_memory", "skill_bundle", "agent_system"),
    }
)
RUN_FOLDS: Final = MappingProxyType(
    {
        "R0": (("F0", "F1"), "F2", "F3"),
        "R1": (("F1", "F2"), "F3", "F0"),
        "R2": (("F2", "F3"), "F0", "F1"),
        "R3": (("F3", "F0"), "F1", "F2"),
    }
)


class SafeEvolveConfigError(RuntimeError):
    """Closed config finding."""

    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True)
class SafeEvolveConfigV2:
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
    def state_root_relative(self) -> str:
        return str(self.payload["roots"]["state"])

    @property
    def framework_lock_relative(self) -> str:
        return str(self.payload["core"]["framework_lock"])

    @property
    def cooldown_seconds(self) -> int:
        return int(self.payload["executor"]["post_durable_terminal_cooldown_seconds"])


def load_safe_evolve_config_v2(path: Path) -> SafeEvolveConfigV2:
    if not isinstance(path, Path) or not path.is_absolute():
        raise TypeError("config path must be absolute")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SafeEvolveConfigError("SAFE_EVOLVE_CONFIG_UNAVAILABLE") from exc
    if type(payload) is not dict:
        raise SafeEvolveConfigError("SAFE_EVOLVE_CONFIG_INVALID")
    _validate(payload)
    return SafeEvolveConfigV2(
        path=path,
        payload=payload,
        digest=sha256_bytes(canonical_json_bytes(payload)),
    )


def _validate(payload: dict[str, Any]) -> None:
    expected = {
        "schema_version",
        "protocol_id",
        "experiment_name",
        "classification",
        "scope_disclaimers",
        "prior_run",
        "dataset",
        "phase_a",
        "folds",
        "model",
        "managed_runtime",
        "executor",
        "reflector",
        "core",
        "parser_evaluator",
        "retrieval",
        "promotion_gate",
        "retirement_gate",
        "deployment_gate",
        "r0_continue_gate",
        "budgets",
        "roots",
        "monitor",
    }
    if set(payload) != expected:
        raise SafeEvolveConfigError("SAFE_EVOLVE_CONFIG_SCHEMA_INVALID")
    if payload["schema_version"] != CONFIG_SCHEMA or payload["protocol_id"] != PROTOCOL_ID:
        raise SafeEvolveConfigError("SAFE_EVOLVE_CONFIG_IDENTITY_INVALID")
    if payload["classification"] != CLASSIFICATION:
        raise SafeEvolveConfigError("SAFE_EVOLVE_CLASSIFICATION_INVALID")
    if payload["scope_disclaimers"] != [
        "current_run_train_validation_test_isolated",
        "fresh_state_database_artifact_lineage_and_model_calls",
        "repeated_official_pool_mechanism_evidence_only",
        "not_historical_never_exposed",
        "not_external_independent_generalization",
    ]:
        raise SafeEvolveConfigError("SAFE_EVOLVE_SCOPE_INVALID")
    prior = payload["prior_run"]
    if (
        prior.get("run_id") != "stv3-temperature-full-evolve-v1-20260802T191250Z"
        or prior.get("inference_commit") != "b625a8ceabc512636a474502d84a93ece1f50929"
        or prior.get("report_commit") != "8f8a2d4729c09ecec7afd308bac4a712b29d6a65"
        or prior.get("permission_fix_commit") != "2cf53173a20d0a275ccf5b008d05af2b5c5409d2"
        or prior.get("final_artifact_manifest_sha256")
        != "39eea4b9295d8d40c798d376c5a434f6c99661a5beb00d9fb699d82bda2da1ee"
        or {
            key: (value.get("utf8_bytes"), value.get("sha256"))
            for key, value in prior.get("artifacts", {}).items()
        }
        != dict(FROZEN_C4)
    ):
        raise SafeEvolveConfigError("SAFE_EVOLVE_PRIOR_IDENTITY_INVALID")
    if payload["dataset"] != {
        "repository": "AI4Chem/ChemBench4K",
        "revision": CHEMBENCH4K_REVISION,
        "root": f"data/chembench4k/AI4Chem_ChemBench4K/{CHEMBENCH4K_REVISION}",
        "category": CATEGORY,
        "source_split": "test",
        "expected_pool_count": EXPECTED_POOL_COUNT,
    }:
        raise SafeEvolveConfigError("SAFE_EVOLVE_DATASET_INVALID")
    phase_a = payload["phase_a"]
    if (
        phase_a.get("test_count") != 100
        or phase_a.get("half_count") != 50
        or phase_a.get("half_namespace") != HALF_NAMESPACE
        or {key: tuple(value) for key, value in phase_a.get("arms", {}).items()}
        != dict(ARM_TARGETS)
        or phase_a.get("expected_candidate_accepted_calls") != 800
        or phase_a.get("eligible_targets") != ["text_memory", "skill_bundle"]
        or phase_a.get("selection_gate")
        != {
            "utility_strictly_positive": True,
            "positive_flips_gt_negative_flips": True,
            "negative_flips_max": 2,
            "both_half_accuracy_delta_min_pp": 0,
            "both_half_utility_min": 0,
            "infrastructure_parity_required": True,
        }
        or phase_a.get("tie_break")
        != [
            "utility_desc",
            "negative_flips_asc",
            "accuracy_desc",
            "injected_bytes_asc",
            "skill_bundle",
        ]
    ):
        raise SafeEvolveConfigError("SAFE_EVOLVE_PHASE_A_INVALID")
    folds = payload["folds"]
    if (
        folds.get("namespace") != FOLD_NAMESPACE
        or folds.get("fold_sizes") != [50, 50, 50, 50]
        or folds.get("reserve_count") != 2
        or folds.get("train_count") != 100
        or folds.get("validation_count") != 50
        or folds.get("test_count") != 50
        or folds.get("batch_size") != 25
        or folds.get("validation_part_size") != 25
        or folds.get("grouping_algorithm") != "temperature_reaction_lexical_union_find_v1"
        or {
            key: (tuple(value["train"]), value["validation"], value["test"])
            for key, value in folds.get("runs", {}).items()
        }
        != dict(RUN_FOLDS)
    ):
        raise SafeEvolveConfigError("SAFE_EVOLVE_FOLDS_INVALID")
    if payload["model"] != {"name": "gpt-5.5", "reasoning_effort": "medium"}:
        raise SafeEvolveConfigError("SAFE_EVOLVE_MODEL_INVALID")
    runtime = payload["managed_runtime"]
    if (
        runtime.get("source") != "openevo_core_managed_codex_v1"
        or runtime.get("npm_package") != "@openai/codex@0.144.1"
        or runtime.get("cli_version") != "codex-cli 0.144.1"
        or runtime.get("executable_sha256") != EXPECTED_CODEX_SHA256
        or runtime.get("expected_runtime_v8_identity") != EXPECTED_RUNTIME_V8_IDENTITY
    ):
        raise SafeEvolveConfigError("SAFE_EVOLVE_MANAGED_RUNTIME_INVALID")
    executor = payload["executor"]
    if executor != {
        "rollout_url": "http://127.0.0.1:8080",
        "timeout_seconds": 1200,
        "infrastructure_retry_limit": 3,
        "candidate_max_workers": 1,
        "post_durable_terminal_cooldown_seconds": 15,
        "runtime_profile": "managed_science",
        "auth_mode": "subscription",
        "capture_mode": "transcript",
        "approval_policy": "never",
        "tool_policy": "zero_tool_transcript_audit",
        "model_visible_tools_enabled": False,
        "provider_transport_network_enabled": True,
        "mcp_servers": [],
    }:
        raise SafeEvolveConfigError("SAFE_EVOLVE_EXECUTOR_INVALID")
    if payload["reflector"] != {
        "timeout_seconds": 1800,
        "attempts_max": 3,
        "logical_calls_per_train_batch": 1,
        "closed_schema_required": True,
        "required_response_bindings": True,
    }:
        raise SafeEvolveConfigError("SAFE_EVOLVE_REFLECTOR_INVALID")
    if payload["core"] != {
        "framework_lock": "state/chembench_temperature_safe_evolve_v2/formal_runtime_v1/framework/framework-lock.json",
        "jobs_per_train_batch": 1,
        "selected_targets_allowed": ["text_memory", "skill_bundle"],
        "agent_system_always_empty": True,
    }:
        raise SafeEvolveConfigError("SAFE_EVOLVE_CORE_INVALID")
    if payload["parser_evaluator"] != {
        "official_parser": "first_uppercase_opencompass_compatible",
        "strict_parser": "strict_single_letter_parser",
        "evaluator": "ChemBench4KPrivateEvaluator",
    }:
        raise SafeEvolveConfigError("SAFE_EVOLVE_EVALUATOR_INVALID")
    if payload["retrieval"] != {
        "algorithm": "deterministic_temperature_lexical_retrieval_v1",
        "active_entry_limit": 6,
        "text_memory": {"top_k": 2, "target_bytes": 1000, "hard_bytes": 1200},
        "skill_bundle": {"top_k": 1, "target_bytes": 500, "hard_bytes": 700},
    }:
        raise SafeEvolveConfigError("SAFE_EVOLVE_RETRIEVAL_INVALID")
    _validate_gates_and_budgets(payload)
    if payload["roots"] != {
        "state": "state/chembench_temperature_safe_evolve_v2",
        "results": "results/chembench_temperature_safe_evolve_v2",
        "reports": "reports/chembench_temperature_safe_evolve_v2",
        "desktop_parent": "/mnt/c/Users/lhy-h/Desktop/ChemBench_STV3_final_feasibility_20260801",
    } or payload["monitor"] != {
        "interval_seconds": 600,
        "stagnant_poll_limit": 2,
        "tmux_session": "temperature-safe-evolve-v2",
    }:
        raise SafeEvolveConfigError("SAFE_EVOLVE_ROOT_OR_MONITOR_INVALID")
    canonical_json_bytes(payload)


def _validate_gates_and_budgets(payload: dict[str, Any]) -> None:
    expected = {
        "promotion_gate": {
            "incremental_utility_strictly_positive": True,
            "incremental_positive_gt_negative": True,
            "incremental_negative_max": 1,
            "candidate_accuracy_gte_active": True,
            "baseline_utility_min": 0,
            "candidate_accuracy_gte_g0": True,
            "infrastructure_parity_required": True,
            "schema_lineage_leakage_budget_required": True,
        },
        "retirement_gate": {
            "negative_flips_min": 2,
            "positive_not_gt_negative": True,
            "contradiction_support_ratio_gt": 0.5,
            "consecutive_nonpositive_utility_blocks": 2,
            "stale_supported_blocks": 2,
            "retire_on_stronger_unconditional_conflict": True,
            "retire_on_safety_failure": True,
        },
        "deployment_gate": {
            "utility_strictly_positive": True,
            "positive_gt_negative": True,
            "negative_max": 1,
            "evolved_accuracy_gte_g0": True,
            "cumulative_promoted_forward_utility_strictly_positive": True,
            "promotion_min": 1,
            "infrastructure_parity_required": True,
            "audit_required": True,
        },
        "r0_continue_gate": {
            "v2_deployed_real_evolved_required": True,
            "promotion_min": 1,
            "raw_test_accuracy_gte_g0": True,
            "raw_test_positive_gte_negative": True,
            "raw_test_negative_max": 2,
            "raw_test_utility_min": 0,
            "infrastructure_parity_required": True,
            "integrity_required": True,
        },
        "budgets": {
            "phase_a_candidate_accepted": 800,
            "fold_candidate_logical_context_max": 500,
            "fold_reflector_logical_max": 4,
            "fold_core_job_max": 4,
            "campaign_candidate_max_if_r0_stops": 1300,
            "campaign_candidate_max_if_four_folds": 2800,
            "campaign_reflector_max_if_r0_stops": 4,
            "campaign_reflector_max_if_four_folds": 16,
            "campaign_core_max_if_r0_stops": 4,
            "campaign_core_max_if_four_folds": 16,
        },
    }
    for key, value in expected.items():
        if payload[key] != value:
            raise SafeEvolveConfigError(f"SAFE_EVOLVE_{key.upper()}_INVALID")


__all__ = [
    "ARM_TARGETS",
    "CATEGORY",
    "CLASSIFICATION",
    "CONFIG_SCHEMA",
    "EXPECTED_CODEX_SHA256",
    "EXPECTED_POOL_COUNT",
    "EXPECTED_RUNTIME_V8_IDENTITY",
    "FOLD_NAMESPACE",
    "FROZEN_C4",
    "HALF_NAMESPACE",
    "PROTOCOL_ID",
    "RUN_FOLDS",
    "SafeEvolveConfigError",
    "SafeEvolveConfigV2",
    "load_safe_evolve_config_v2",
]
