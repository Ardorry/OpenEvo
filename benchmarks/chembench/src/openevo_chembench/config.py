"""Strict configuration models for the ChemBench benchmark package."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml

from openevo_chembench.artifacts import ArtifactKind


CHEMBENCH_CONFIGS: tuple[str, ...] = (
    "analytical_chemistry",
    "chemical_preference",
    "general_chemistry",
    "inorganic_chemistry",
    "materials_science",
    "organic_chemistry",
    "physical_chemistry",
    "technical_chemistry",
    "toxicity_and_safety",
)

_COMMIT_HASH = re.compile(r"[0-9a-f]{40}")
EXECUTION_BACKENDS: tuple[str, ...] = (
    "managed_codex",
    "local_codex_cli",
)


def _require_non_empty_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_commit_hash(value: object, field_name: str) -> None:
    if not isinstance(value, str) or _COMMIT_HASH.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a full lowercase 40-character commit hash")


@dataclass(frozen=True, slots=True)
class ChemBenchDatasetConfig:
    """Revision-independent dataset selection; revision is pinned at run level."""

    repository: str = "jablonkagroup/ChemBench"
    split: str = "train"
    configurations: tuple[str, ...] = CHEMBENCH_CONFIGS

    def __post_init__(self) -> None:
        if self.repository != "jablonkagroup/ChemBench":
            raise ValueError(
                "ChemBenchDatasetConfig.repository must be jablonkagroup/ChemBench"
            )
        if self.split != "train":
            raise ValueError("ChemBenchDatasetConfig.split must be train")
        if not isinstance(self.configurations, tuple) or not self.configurations:
            raise ValueError(
                "ChemBenchDatasetConfig.configurations must be a non-empty immutable tuple"
            )
        if len(self.configurations) != len(set(self.configurations)):
            raise ValueError(
                "ChemBenchDatasetConfig.configurations must not contain duplicates"
            )
        unknown = sorted(set(self.configurations) - set(CHEMBENCH_CONFIGS))
        if unknown:
            raise ValueError(f"unknown ChemBench configurations: {', '.join(unknown)}")


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Fixed identity of the evaluated agent runtime."""

    harness: str = "codex"
    model: str = "gpt-5.5"
    auth_mode: str = "subscription"
    capture_mode: str = "transcript"
    ephemeral: bool = True

    def __post_init__(self) -> None:
        if self.harness not in {"codex", "codex_cli"}:
            raise ValueError("AgentConfig.harness must be codex or codex_cli")
        if self.model != "gpt-5.5":
            raise ValueError("AgentConfig.model must be gpt-5.5")
        if self.auth_mode != "subscription":
            raise ValueError("AgentConfig.auth_mode must be subscription")
        if self.capture_mode != "transcript":
            raise ValueError("AgentConfig.capture_mode must be transcript")
        if self.ephemeral is not True:
            raise ValueError("AgentConfig.ephemeral must remain true")


@dataclass(frozen=True, slots=True)
class EvolutionConfig:
    """Closed set of non-parametric artifact switches."""

    max_rounds: int = 0
    enable_text_memory: bool = True
    enable_skill_bundle: bool = False
    enable_agent_system: bool = False
    enable_parametric_memory: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.max_rounds, bool) or not isinstance(self.max_rounds, int):
            raise TypeError("EvolutionConfig.max_rounds must be an integer")
        if self.max_rounds < 0:
            raise ValueError("EvolutionConfig.max_rounds must be non-negative")
        for field_name in (
            "enable_text_memory",
            "enable_skill_bundle",
            "enable_agent_system",
            "enable_parametric_memory",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(f"EvolutionConfig.{field_name} must be boolean")
        if self.enable_parametric_memory:
            raise ValueError(
                "parametric_memory is prohibited for the ChemBench evaluation protocol"
            )

    @property
    def enabled_artifact_kinds(self) -> tuple[ArtifactKind, ...]:
        enabled: list[ArtifactKind] = []
        if self.enable_text_memory:
            enabled.append(ArtifactKind.TEXT_MEMORY)
        if self.enable_skill_bundle:
            enabled.append(ArtifactKind.SKILL_BUNDLE)
        if self.enable_agent_system:
            enabled.append(ArtifactKind.AGENT_SYSTEM)
        return tuple(enabled)


@dataclass(frozen=True, slots=True)
class RuntimeIsolationConfig:
    """Hard safety settings that cannot be relaxed by experiment YAML."""

    raw_event_export_enabled: bool = False
    gateway_evaluator_enabled: bool = False
    network_enabled: bool = False
    network_tools_enabled: bool = False
    browser_enabled: bool = False
    external_web_enabled: bool = False
    private_dataset_mounted: bool = False
    mcp_servers: tuple[str, ...] = ()
    num_samples: int = 1

    def __post_init__(self) -> None:
        forbidden_true = {
            "raw_event_export_enabled": self.raw_event_export_enabled,
            "gateway_evaluator_enabled": self.gateway_evaluator_enabled,
            "network_enabled": self.network_enabled,
            "network_tools_enabled": self.network_tools_enabled,
            "browser_enabled": self.browser_enabled,
            "external_web_enabled": self.external_web_enabled,
            "private_dataset_mounted": self.private_dataset_mounted,
        }
        for field_name, value in forbidden_true.items():
            if not isinstance(value, bool):
                raise TypeError(f"RuntimeIsolationConfig.{field_name} must be boolean")
            if value:
                raise ValueError(f"RuntimeIsolationConfig.{field_name} must remain false")
        if not isinstance(self.mcp_servers, tuple):
            raise TypeError("RuntimeIsolationConfig.mcp_servers must be an immutable tuple")
        if self.mcp_servers:
            raise ValueError("RuntimeIsolationConfig.mcp_servers must remain empty")
        if self.num_samples != 1:
            raise ValueError("RuntimeIsolationConfig.num_samples must remain 1")


@dataclass(frozen=True, slots=True)
class ExperimentConfig:
    """Reproducible top-level experiment identity and safety configuration."""

    openevo_revision: str
    chembench_revision: str
    codex_cli_version: str
    prompt_version: str
    execution_backend: str = "managed_codex"
    seed: int = 0
    protocol_id: str = "chembench.single_task_multi_artifact.v1"
    schema_version: int = 1
    dataset: ChemBenchDatasetConfig = field(default_factory=ChemBenchDatasetConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    evolution: EvolutionConfig = field(default_factory=EvolutionConfig)
    runtime: RuntimeIsolationConfig = field(default_factory=RuntimeIsolationConfig)

    def __post_init__(self) -> None:
        _require_commit_hash(self.openevo_revision, "ExperimentConfig.openevo_revision")
        _require_commit_hash(
            self.chembench_revision,
            "ExperimentConfig.chembench_revision",
        )
        _require_non_empty_text(
            self.codex_cli_version,
            "ExperimentConfig.codex_cli_version",
        )
        _require_non_empty_text(self.prompt_version, "ExperimentConfig.prompt_version")
        if self.execution_backend not in EXECUTION_BACKENDS:
            raise ValueError(
                "ExperimentConfig.execution_backend must be managed_codex "
                "or local_codex_cli"
            )
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise TypeError("ExperimentConfig.seed must be an integer")
        if self.seed < 0:
            raise ValueError("ExperimentConfig.seed must be non-negative")
        if self.protocol_id != "chembench.single_task_multi_artifact.v1":
            raise ValueError(
                "ExperimentConfig.protocol_id must be "
                "chembench.single_task_multi_artifact.v1"
            )
        if self.schema_version != 1:
            raise ValueError("ExperimentConfig.schema_version must be 1")
        if not isinstance(self.dataset, ChemBenchDatasetConfig):
            raise TypeError("ExperimentConfig.dataset must be ChemBenchDatasetConfig")
        if not isinstance(self.agent, AgentConfig):
            raise TypeError("ExperimentConfig.agent must be AgentConfig")
        expected_harness = (
            "codex"
            if self.execution_backend == "managed_codex"
            else "codex_cli"
        )
        if self.agent.harness != expected_harness:
            raise ValueError(
                "ExperimentConfig agent harness must agree with execution_backend"
            )
        if not isinstance(self.evolution, EvolutionConfig):
            raise TypeError("ExperimentConfig.evolution must be EvolutionConfig")
        if not isinstance(self.runtime, RuntimeIsolationConfig):
            raise TypeError("ExperimentConfig.runtime must be RuntimeIsolationConfig")


@dataclass(frozen=True, slots=True)
class DebugExecutionConfig:
    """Non-benchmark controls for the fixed ten-task real-execution smoke run."""

    config_name: str = "general_chemistry"
    task_count: int = 10
    rollout_url: str = "http://127.0.0.1:8080"
    result_root: str = "results/debug"
    task_timeout_seconds: float = 600.0
    poll_interval_seconds: float = 1.0
    max_poll_attempts: int = 900

    def __post_init__(self) -> None:
        if self.config_name not in CHEMBENCH_CONFIGS:
            raise ValueError("DebugExecutionConfig.config_name is not a ChemBench config")
        if self.task_count != 10:
            raise ValueError("DebugExecutionConfig.task_count must remain 10")
        if self.result_root not in {
            "results/debug",
            "results/debug_local_codex",
        }:
            raise ValueError(
                "DebugExecutionConfig.result_root must be results/debug "
                "or results/debug_local_codex"
            )
        parsed_url = urlparse(self.rollout_url)
        if (
            parsed_url.scheme != "http"
            or parsed_url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.path not in {"", "/"}
            or parsed_url.params
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError(
                "DebugExecutionConfig.rollout_url must be an uncredentialed loopback HTTP URL"
            )
        try:
            port = parsed_url.port
        except ValueError as exc:
            raise ValueError("DebugExecutionConfig.rollout_url has an invalid port") from exc
        if port is None:
            raise ValueError("DebugExecutionConfig.rollout_url must include a port")
        for value, field_name in (
            (self.task_timeout_seconds, "task_timeout_seconds"),
            (self.poll_interval_seconds, "poll_interval_seconds"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or float(value) <= 0
            ):
                raise ValueError(f"DebugExecutionConfig.{field_name} must be positive")
        if (
            isinstance(self.max_poll_attempts, bool)
            or not isinstance(self.max_poll_attempts, int)
            or self.max_poll_attempts < 1
        ):
            raise ValueError(
                "DebugExecutionConfig.max_poll_attempts must be a positive integer"
            )


@dataclass(frozen=True, slots=True)
class LoadedDebugConfig:
    """Strictly parsed experiment identity plus safe local execution controls."""

    experiment: ExperimentConfig
    execution: DebugExecutionConfig

    def __post_init__(self) -> None:
        if type(self.experiment) is not ExperimentConfig:
            raise TypeError("LoadedDebugConfig.experiment must be exact ExperimentConfig")
        if type(self.execution) is not DebugExecutionConfig:
            raise TypeError("LoadedDebugConfig.execution must be exact DebugExecutionConfig")
        if self.experiment.evolution.max_rounds != 1:
            raise ValueError("debug evolution config must use max_rounds=1")
        if self.execution.config_name not in self.experiment.dataset.configurations:
            raise ValueError("debug config_name must be selected by the dataset config")
        expected_result_root = (
            "results/debug"
            if self.experiment.execution_backend == "managed_codex"
            else "results/debug_local_codex"
        )
        if self.execution.result_root != expected_result_root:
            raise ValueError(
                "debug result_root must agree with execution_backend"
            )


def load_debug_config(path: Path) -> LoadedDebugConfig:
    """Load the closed debug YAML without accepting runtime-policy extensions."""

    if not isinstance(path, Path):
        raise TypeError("load_debug_config.path must be pathlib.Path")
    try:
        with path.open("r", encoding="utf-8") as stream:
            loaded = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError("failed to load debug experiment config") from exc
    root = _closed_mapping(
        loaded,
        expected={
            "schema_version",
            "protocol_id",
            "revisions",
            "execution_backend",
            "codex_cli_version",
            "prompt_version",
            "seed",
            "dataset",
            "agent",
            "evolution",
            "artifact",
            "runtime",
            "debug",
        },
        field_name="config",
    )
    revisions = _closed_mapping(
        root["revisions"],
        expected={"openevo", "chembench"},
        field_name="revisions",
    )
    dataset = _closed_mapping(
        root["dataset"],
        expected={"repository", "split", "configurations"},
        field_name="dataset",
    )
    agent = _closed_mapping(
        root["agent"],
        expected={"harness", "model", "auth_mode", "capture_mode", "ephemeral"},
        field_name="agent",
    )
    evolution = _closed_mapping(
        root["evolution"],
        expected={"max_rounds"},
        field_name="evolution",
    )
    artifact = _closed_mapping(
        root["artifact"],
        expected={"text_memory", "skill_bundle", "agent_system", "parametric_memory"},
        field_name="artifact",
    )
    runtime = _closed_mapping(
        root["runtime"],
        expected={
            "raw_event_export_enabled",
            "gateway_evaluator_enabled",
            "network_enabled",
            "network_tools_enabled",
            "browser_enabled",
            "external_web_enabled",
            "private_dataset_mounted",
            "mcp_servers",
            "num_samples",
        },
        field_name="runtime",
    )
    debug = _closed_mapping(
        root["debug"],
        expected={
            "config_name",
            "task_count",
            "rollout_url",
            "result_root",
            "task_timeout_seconds",
            "poll_interval_seconds",
            "max_poll_attempts",
        },
        field_name="debug",
    )

    configurations = _closed_sequence(
        dataset["configurations"],
        field_name="dataset.configurations",
    )
    mcp_servers = _closed_sequence(
        runtime["mcp_servers"],
        field_name="runtime.mcp_servers",
    )
    experiment = ExperimentConfig(
        openevo_revision=revisions["openevo"],
        chembench_revision=revisions["chembench"],
        codex_cli_version=root["codex_cli_version"],
        prompt_version=root["prompt_version"],
        execution_backend=root["execution_backend"],
        seed=root["seed"],
        protocol_id=root["protocol_id"],
        schema_version=root["schema_version"],
        dataset=ChemBenchDatasetConfig(
            repository=dataset["repository"],
            split=dataset["split"],
            configurations=tuple(configurations),
        ),
        agent=AgentConfig(
            harness=agent["harness"],
            model=agent["model"],
            auth_mode=agent["auth_mode"],
            capture_mode=agent["capture_mode"],
            ephemeral=agent["ephemeral"],
        ),
        evolution=EvolutionConfig(
            max_rounds=evolution["max_rounds"],
            enable_text_memory=artifact["text_memory"],
            enable_skill_bundle=artifact["skill_bundle"],
            enable_agent_system=artifact["agent_system"],
            enable_parametric_memory=artifact["parametric_memory"],
        ),
        runtime=RuntimeIsolationConfig(
            raw_event_export_enabled=runtime["raw_event_export_enabled"],
            gateway_evaluator_enabled=runtime["gateway_evaluator_enabled"],
            network_enabled=runtime["network_enabled"],
            network_tools_enabled=runtime["network_tools_enabled"],
            browser_enabled=runtime["browser_enabled"],
            external_web_enabled=runtime["external_web_enabled"],
            private_dataset_mounted=runtime["private_dataset_mounted"],
            mcp_servers=tuple(mcp_servers),
            num_samples=runtime["num_samples"],
        ),
    )
    execution = DebugExecutionConfig(
        config_name=debug["config_name"],
        task_count=debug["task_count"],
        rollout_url=debug["rollout_url"],
        result_root=debug["result_root"],
        task_timeout_seconds=debug["task_timeout_seconds"],
        poll_interval_seconds=debug["poll_interval_seconds"],
        max_poll_attempts=debug["max_poll_attempts"],
    )
    return LoadedDebugConfig(experiment=experiment, execution=execution)


def _closed_mapping(
    value: object,
    *,
    expected: set[str],
    field_name: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    if not all(type(key) is str for key in value):
        raise TypeError(f"{field_name} keys must be strings")
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        detail = []
        if missing:
            detail.append(f"missing={','.join(missing)}")
        if unknown:
            detail.append(f"unknown={','.join(unknown)}")
        raise ValueError(f"{field_name} fields are invalid: {' '.join(detail)}")
    return value


def _closed_sequence(value: object, *, field_name: str) -> tuple[object, ...]:
    if not isinstance(value, list):
        raise TypeError(f"{field_name} must be a YAML sequence")
    return tuple(value)


__all__ = [
    "CHEMBENCH_CONFIGS",
    "EXECUTION_BACKENDS",
    "AgentConfig",
    "ChemBenchDatasetConfig",
    "DebugExecutionConfig",
    "EvolutionConfig",
    "ExperimentConfig",
    "LoadedDebugConfig",
    "RuntimeIsolationConfig",
    "load_debug_config",
]
