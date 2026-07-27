"""Closed configuration and preregistered call budget for supervised transfer v1."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_REPOSITORY,
    CHEMBENCH4K_REVISION,
)
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    closed_mapping,
)
from openevo_chembench.supervised_transfer_v1.managed_codex import (
    MANAGED_CODEX_SOURCE,
)

PROTOCOL_ID = "chembench_supervised_transfer_v1"
CONFIG_SCHEMA = "chembench_supervised_transfer_config_v1"
CONTROL_TRAIN_PROTOCOL_ID = "supervised_transfer_control_train_v1"
ONLINE_TRAIN_PROTOCOL_ID = "supervised_taskwise_online_train_v1"
MODEL = "gpt-5.5"
REASONING_EFFORT = "medium"
SPLIT_SEED = "openevo-chembench-supervised-transfer-v1-balanced-split"
RESULT_ROOT = "results/chembench_supervised_transfer_v1"
STATE_ROOT = "state/chembench_supervised_transfer_v1"
MANIFEST_ROOT = "benchmarks/chembench/manifests/supervised_transfer_v1"
SOURCE_COMMIT_PLACEHOLDER = "BIND_CLEAN_HEAD_AT_LAUNCH"
CODEX_VERSION_PLACEHOLDER = "BIND_VERIFIED_CODEX_AT_LAUNCH"
CODEX_RUNTIME_SOURCE = MANAGED_CODEX_SOURCE
CODEX_RUNTIME_ROOT = f"{STATE_ROOT}/managed_codex"

TRAIN_PER_CATEGORY = 50
PROBE_PER_CATEGORY = 10
TEST_PER_CATEGORY = 50
TRAIN_CHECKPOINTS = (0, 10, 20, 30, 40, 50)
ROUNDS_PER_TRAIN_TASK = 3
UPDATES_PER_ONLINE_TASK = 2
EVOLUTION_TARGET_METHODS = (
    ("text_memory", "text_memory_expel_reflector"),
    ("skill_bundle", "skill_bundle"),
    ("agent_system", "agent_system"),
)
CORE_JOBS_PER_UPDATE = len(EVOLUTION_TARGET_METHODS)

CONTROL_TRAIN_TASK_CALLS = 450 * ROUNDS_PER_TRAIN_TASK
ONLINE_TRAIN_TASK_CALLS = 450 * ROUNDS_PER_TRAIN_TASK
REFLECTOR_CALLS = 450 * UPDATES_PER_ONLINE_TASK
PROBE_TASK_CALLS = len(TRAIN_CHECKPOINTS) * 90 * 2
TEST_TASK_CALLS = 450 * 2
TOTAL_MODEL_CALLS = (
    CONTROL_TRAIN_TASK_CALLS
    + ONLINE_TRAIN_TASK_CALLS
    + REFLECTOR_CALLS
    + PROBE_TASK_CALLS
    + TEST_TASK_CALLS
)
FORMAL_CORE_JOBS = REFLECTOR_CALLS * CORE_JOBS_PER_UPDATE
FORMAL_CORE_ARTIFACTS = FORMAL_CORE_JOBS
PREFLIGHT_CORE_JOBS = 19 * CORE_JOBS_PER_UPDATE
PREFLIGHT_CORE_ARTIFACTS = PREFLIGHT_CORE_JOBS


class SupervisedTransferConfigError(ValueError):
    """Closed configuration failure."""


def _clean_relative_path(value: object, field_name: str) -> str:
    if type(value) is not str or not value:
        raise SupervisedTransferConfigError(f"{field_name} must be non-empty text")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise SupervisedTransferConfigError(f"{field_name} must be a clean relative path")
    return value


@dataclass(frozen=True, slots=True)
class SupervisedMemoryLimitsV1:
    max_utf8_bytes: int = 24_576
    max_estimated_tokens: int = 6_144
    max_confirmed_rules: int = 40
    max_provisional_rules: int = 20
    max_failure_modes: int = 24
    max_retired_rules: int = 24

    def __post_init__(self) -> None:
        if self != SupervisedMemoryLimitsV1.__new_defaults__():
            raise SupervisedTransferConfigError("supervised memory limits are protocol-fixed")

    @classmethod
    def __new_defaults__(cls) -> SupervisedMemoryLimitsV1:
        instance = object.__new__(cls)
        object.__setattr__(instance, "max_utf8_bytes", 24_576)
        object.__setattr__(instance, "max_estimated_tokens", 6_144)
        object.__setattr__(instance, "max_confirmed_rules", 40)
        object.__setattr__(instance, "max_provisional_rules", 20)
        object.__setattr__(instance, "max_failure_modes", 24)
        object.__setattr__(instance, "max_retired_rules", 24)
        return instance

    def to_payload(self) -> dict[str, int]:
        return {
            "max_utf8_bytes": self.max_utf8_bytes,
            "max_estimated_tokens": self.max_estimated_tokens,
            "max_confirmed_rules": self.max_confirmed_rules,
            "max_provisional_rules": self.max_provisional_rules,
            "max_failure_modes": self.max_failure_modes,
            "max_retired_rules": self.max_retired_rules,
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.to_payload())).hexdigest()


SUPERVISED_MEMORY_LIMITS_V1 = SupervisedMemoryLimitsV1()


@dataclass(frozen=True, slots=True)
class SupervisedExecutorPolicyV1:
    backend: str
    harness: str
    timeout_seconds: int
    concurrency: int
    infrastructure_retries_before_completion: int
    tools_enabled: bool
    mcp_enabled: bool
    web_enabled: bool
    network_enabled: bool
    subagents_enabled: bool

    def __post_init__(self) -> None:
        if self.backend != "local_codex_cli" or self.harness != "codex_cli":
            raise SupervisedTransferConfigError("supervised transfer requires local Codex CLI")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or self.timeout_seconds < 1
        ):
            raise SupervisedTransferConfigError("executor timeout must be positive")
        if self.concurrency != 1 or self.infrastructure_retries_before_completion != 0:
            raise SupervisedTransferConfigError("sessions are serial and never model-retried")
        for field_name in (
            "tools_enabled",
            "mcp_enabled",
            "web_enabled",
            "network_enabled",
            "subagents_enabled",
        ):
            if getattr(self, field_name) is not False:
                raise SupervisedTransferConfigError(f"executor {field_name} must remain false")

    def to_payload(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "harness": self.harness,
            "timeout_seconds": self.timeout_seconds,
            "concurrency": self.concurrency,
            "infrastructure_retries_before_completion": (
                self.infrastructure_retries_before_completion
            ),
            "tools_enabled": self.tools_enabled,
            "mcp_enabled": self.mcp_enabled,
            "web_enabled": self.web_enabled,
            "network_enabled": self.network_enabled,
            "subagents_enabled": self.subagents_enabled,
        }


@dataclass(frozen=True, slots=True)
class SupervisedTransferConfigV1:
    dataset_root: str
    dataset_manifest: str
    historical_exposure_manifest: str
    split_summary: str
    result_root: str
    state_root: str
    framework_lock: str
    codex_runtime_source: str
    codex_runtime_root: str
    codex_cli_version: str
    source_commit: str
    reflector_method_id: str
    reflector_timeout_seconds: int
    executor: SupervisedExecutorPolicyV1
    memory_limits: SupervisedMemoryLimitsV1 = SUPERVISED_MEMORY_LIMITS_V1
    schema_version: str = CONFIG_SCHEMA
    protocol_id: str = PROTOCOL_ID
    dataset_repository: str = CHEMBENCH4K_REPOSITORY
    dataset_revision: str = CHEMBENCH4K_REVISION
    model: str = MODEL
    reasoning_effort: str = REASONING_EFFORT

    def __post_init__(self) -> None:
        if self.schema_version != CONFIG_SCHEMA or self.protocol_id != PROTOCOL_ID:
            raise SupervisedTransferConfigError("supervised transfer config identity mismatch")
        if (
            self.dataset_repository != CHEMBENCH4K_REPOSITORY
            or self.dataset_revision != CHEMBENCH4K_REVISION
        ):
            raise SupervisedTransferConfigError("dataset identity is not frozen")
        if self.model != MODEL or self.reasoning_effort != REASONING_EFFORT:
            raise SupervisedTransferConfigError("evaluated model identity is not frozen")
        if self.result_root != RESULT_ROOT or self.state_root != STATE_ROOT:
            raise SupervisedTransferConfigError("result/state roots are protocol-fixed")
        for value, field_name in (
            (self.dataset_root, "dataset_root"),
            (self.dataset_manifest, "dataset_manifest"),
            (self.historical_exposure_manifest, "historical_exposure_manifest"),
            (self.split_summary, "split_summary"),
            (self.result_root, "result_root"),
            (self.state_root, "state_root"),
            (self.framework_lock, "framework_lock"),
            (self.codex_runtime_root, "codex_runtime_root"),
        ):
            _clean_relative_path(value, field_name)
        if (
            self.codex_runtime_source != CODEX_RUNTIME_SOURCE
            or self.codex_runtime_root != CODEX_RUNTIME_ROOT
        ):
            raise SupervisedTransferConfigError(
                "Codex runtime must use the OpenEvo-managed package"
            )
        if self.reflector_method_id != "text_memory_expel_reflector":
            raise SupervisedTransferConfigError("only the verified Core reflector is admitted")
        if (
            isinstance(self.reflector_timeout_seconds, bool)
            or not isinstance(self.reflector_timeout_seconds, int)
            or not 0 < self.reflector_timeout_seconds <= 86_280
        ):
            raise SupervisedTransferConfigError("reflector timeout is invalid")
        if self.memory_limits != SUPERVISED_MEMORY_LIMITS_V1:
            raise SupervisedTransferConfigError("memory limits are not frozen")
        if self.codex_cli_version not in {CODEX_VERSION_PLACEHOLDER} and not (
            type(self.codex_cli_version) is str and self.codex_cli_version.startswith("codex-cli ")
        ):
            raise SupervisedTransferConfigError("Codex CLI version must be frozen or deferred")
        if self.source_commit != SOURCE_COMMIT_PLACEHOLDER and not (
            type(self.source_commit) is str
            and len(self.source_commit) == 40
            and all(character in "0123456789abcdef" for character in self.source_commit)
        ):
            raise SupervisedTransferConfigError("source commit must be frozen or deferred")

    @property
    def call_budget(self) -> dict[str, int]:
        return {
            "control_train_task_calls": CONTROL_TRAIN_TASK_CALLS,
            "online_train_task_calls": ONLINE_TRAIN_TASK_CALLS,
            "online_train_reflector_calls": REFLECTOR_CALLS,
            "probe_task_calls": PROBE_TASK_CALLS,
            "test_task_calls": TEST_TASK_CALLS,
            "total_model_calls": TOTAL_MODEL_CALLS,
        }

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "protocol_id": self.protocol_id,
            "dataset": {
                "repository": self.dataset_repository,
                "revision": self.dataset_revision,
                "root": self.dataset_root,
                "manifest": self.dataset_manifest,
            },
            "manifests": {
                "historical_exposure": self.historical_exposure_manifest,
                "split_summary": self.split_summary,
            },
            "roots": {"results": self.result_root, "state": self.state_root},
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "codex_runtime": {
                "source": self.codex_runtime_source,
                "root": self.codex_runtime_root,
            },
            "codex_cli_version": self.codex_cli_version,
            "source_commit": self.source_commit,
            "reflector": {
                "method_id": self.reflector_method_id,
                "timeout_seconds": self.reflector_timeout_seconds,
                "framework_lock": self.framework_lock,
            },
            "evolution_targets": dict(EVOLUTION_TARGET_METHODS),
            "executor": self.executor.to_payload(),
            "memory_limits": self.memory_limits.to_payload(),
            "split": {
                "seed": SPLIT_SEED,
                "train_per_category": TRAIN_PER_CATEGORY,
                "probe_per_category": PROBE_PER_CATEGORY,
                "test_per_category": TEST_PER_CATEGORY,
                "checkpoints": list(TRAIN_CHECKPOINTS),
            },
            "train": {
                "rounds_per_task": ROUNDS_PER_TRAIN_TASK,
                "online_updates_per_task": UPDATES_PER_ONLINE_TASK,
                "stop_when_correct": False,
                "select_best_round": False,
            },
            "call_budget": self.call_budget,
            "core_lifecycle_budget": {
                "jobs_per_update": CORE_JOBS_PER_UPDATE,
                "formal_jobs": FORMAL_CORE_JOBS,
                "formal_artifacts": FORMAL_CORE_ARTIFACTS,
                "preflight_jobs": PREFLIGHT_CORE_JOBS,
                "preflight_artifacts": PREFLIGHT_CORE_ARTIFACTS,
            },
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self.to_payload())).hexdigest()


def load_supervised_transfer_config_v1(path: Path) -> SupervisedTransferConfigV1:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SupervisedTransferConfigError("failed to read supervised transfer config") from exc
    root = closed_mapping(
        loaded,
        keys=frozenset(
            {
                "schema_version",
                "protocol_id",
                "dataset",
                "manifests",
                "roots",
                "model",
                "reasoning_effort",
                "codex_runtime",
                "codex_cli_version",
                "source_commit",
                "reflector",
                "evolution_targets",
                "executor",
                "memory_limits",
                "split",
                "train",
                "call_budget",
                "core_lifecycle_budget",
            }
        ),
        field_name="config",
    )
    dataset = closed_mapping(
        root["dataset"],
        keys=frozenset({"repository", "revision", "root", "manifest"}),
        field_name="dataset",
    )
    manifests = closed_mapping(
        root["manifests"],
        keys=frozenset({"historical_exposure", "split_summary"}),
        field_name="manifests",
    )
    roots = closed_mapping(
        root["roots"],
        keys=frozenset({"results", "state"}),
        field_name="roots",
    )
    reflector = closed_mapping(
        root["reflector"],
        keys=frozenset({"method_id", "timeout_seconds", "framework_lock"}),
        field_name="reflector",
    )
    codex_runtime = closed_mapping(
        root["codex_runtime"],
        keys=frozenset({"source", "root"}),
        field_name="codex_runtime",
    )
    executor = closed_mapping(
        root["executor"],
        keys=frozenset(
            {
                "backend",
                "harness",
                "timeout_seconds",
                "concurrency",
                "infrastructure_retries_before_completion",
                "tools_enabled",
                "mcp_enabled",
                "web_enabled",
                "network_enabled",
                "subagents_enabled",
            }
        ),
        field_name="executor",
    )
    memory_limits = closed_mapping(
        root["memory_limits"],
        keys=frozenset(SUPERVISED_MEMORY_LIMITS_V1.to_payload()),
        field_name="memory_limits",
    )
    expected_split = {
        "seed": SPLIT_SEED,
        "train_per_category": TRAIN_PER_CATEGORY,
        "probe_per_category": PROBE_PER_CATEGORY,
        "test_per_category": TEST_PER_CATEGORY,
        "checkpoints": list(TRAIN_CHECKPOINTS),
    }
    expected_train = {
        "rounds_per_task": ROUNDS_PER_TRAIN_TASK,
        "online_updates_per_task": UPDATES_PER_ONLINE_TASK,
        "stop_when_correct": False,
        "select_best_round": False,
    }
    expected_calls = {
        "control_train_task_calls": CONTROL_TRAIN_TASK_CALLS,
        "online_train_task_calls": ONLINE_TRAIN_TASK_CALLS,
        "online_train_reflector_calls": REFLECTOR_CALLS,
        "probe_task_calls": PROBE_TASK_CALLS,
        "test_task_calls": TEST_TASK_CALLS,
        "total_model_calls": TOTAL_MODEL_CALLS,
    }
    expected_core_lifecycle = {
        "jobs_per_update": CORE_JOBS_PER_UPDATE,
        "formal_jobs": FORMAL_CORE_JOBS,
        "formal_artifacts": FORMAL_CORE_ARTIFACTS,
        "preflight_jobs": PREFLIGHT_CORE_JOBS,
        "preflight_artifacts": PREFLIGHT_CORE_ARTIFACTS,
    }
    if root["split"] != expected_split or root["train"] != expected_train:
        raise SupervisedTransferConfigError("split/train contract is not frozen")
    if root["call_budget"] != expected_calls:
        raise SupervisedTransferConfigError("call budget is not frozen")
    if root["evolution_targets"] != dict(EVOLUTION_TARGET_METHODS):
        raise SupervisedTransferConfigError("evolution target set is not frozen")
    if root["core_lifecycle_budget"] != expected_core_lifecycle:
        raise SupervisedTransferConfigError("Core lifecycle budget is not frozen")
    return SupervisedTransferConfigV1(
        schema_version=root["schema_version"],
        protocol_id=root["protocol_id"],
        dataset_repository=dataset["repository"],
        dataset_revision=dataset["revision"],
        dataset_root=dataset["root"],
        dataset_manifest=dataset["manifest"],
        historical_exposure_manifest=manifests["historical_exposure"],
        split_summary=manifests["split_summary"],
        result_root=roots["results"],
        state_root=roots["state"],
        model=root["model"],
        reasoning_effort=root["reasoning_effort"],
        codex_runtime_source=codex_runtime["source"],
        codex_runtime_root=codex_runtime["root"],
        codex_cli_version=root["codex_cli_version"],
        source_commit=root["source_commit"],
        reflector_method_id=reflector["method_id"],
        reflector_timeout_seconds=reflector["timeout_seconds"],
        framework_lock=reflector["framework_lock"],
        executor=SupervisedExecutorPolicyV1(**executor),
        memory_limits=SupervisedMemoryLimitsV1(**memory_limits),
    )


__all__ = [
    "CODEX_RUNTIME_ROOT",
    "CODEX_RUNTIME_SOURCE",
    "CODEX_VERSION_PLACEHOLDER",
    "CONFIG_SCHEMA",
    "CONTROL_TRAIN_PROTOCOL_ID",
    "MANIFEST_ROOT",
    "MODEL",
    "ONLINE_TRAIN_PROTOCOL_ID",
    "PROBE_PER_CATEGORY",
    "PROTOCOL_ID",
    "REASONING_EFFORT",
    "RESULT_ROOT",
    "SOURCE_COMMIT_PLACEHOLDER",
    "SPLIT_SEED",
    "STATE_ROOT",
    "SUPERVISED_MEMORY_LIMITS_V1",
    "TEST_PER_CATEGORY",
    "TOTAL_MODEL_CALLS",
    "TRAIN_CHECKPOINTS",
    "TRAIN_PER_CATEGORY",
    "SupervisedExecutorPolicyV1",
    "SupervisedMemoryLimitsV1",
    "SupervisedTransferConfigError",
    "SupervisedTransferConfigV1",
    "load_supervised_transfer_config_v1",
]
