"""Closed configuration loader for full ChemBench benchmark launches."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlparse

import yaml

from openevo_chembench.config import (
    CHEMBENCH_CONFIGS,
    AgentConfig,
    ChemBenchDatasetConfig,
    EvolutionConfig,
    ExperimentConfig,
    RuntimeIsolationConfig,
)

_FORMAL_LAUNCH_ID = re.compile(
    r"(?:baseline|evolution)_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{12}"
)


@dataclass(frozen=True, slots=True)
class FormalExecutionConfig:
    """Controller-only controls for one complete benchmark mode."""

    rollout_url: str
    result_root: str
    task_timeout_seconds: float = 600.0
    poll_interval_seconds: float = 1.0
    max_poll_attempts: int = 900

    def __post_init__(self) -> None:
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
                "FormalExecutionConfig.rollout_url must be an uncredentialed "
                "loopback HTTP URL"
            )
        try:
            port = parsed_url.port
        except ValueError as exc:
            raise ValueError(
                "FormalExecutionConfig.rollout_url has an invalid port"
            ) from exc
        if port is None:
            raise ValueError("FormalExecutionConfig.rollout_url must include a port")

        result_path = PurePosixPath(self.result_root)
        allowed_prefixes = {
            ("results", "chembench"),
            ("results", "chembench_local_codex"),
        }
        legacy_result_root = (
            result_path.parts[:2] in allowed_prefixes
            and len(result_path.parts) == 3
            and result_path.parts[-1] in {"baseline", "evolution"}
        )
        frozen_formal_root = (
            result_path.parts[:2] == ("results", "formal")
            and len(result_path.parts) == 4
            and result_path.parts[2] in {"baseline", "evolution"}
            and _FORMAL_LAUNCH_ID.fullmatch(result_path.parts[3]) is not None
            and result_path.parts[3].startswith(f"{result_path.parts[2]}_")
        )
        if (
            result_path.is_absolute()
            or not (legacy_result_root or frozen_formal_root)
            or any(part in {"", ".", ".."} for part in result_path.parts)
            or str(result_path) != self.result_root
        ):
            raise ValueError(
                "FormalExecutionConfig.result_root must be "
                "results/chembench/{baseline,evolution} or "
                "results/chembench_local_codex/{baseline,evolution}, or a "
                "frozen results/formal/{mode}/{unique_launch_id} target"
            )
        for value, field_name in (
            (self.task_timeout_seconds, "task_timeout_seconds"),
            (self.poll_interval_seconds, "poll_interval_seconds"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or float(value) <= 0
            ):
                raise ValueError(f"FormalExecutionConfig.{field_name} must be positive")
        if (
            isinstance(self.max_poll_attempts, bool)
            or not isinstance(self.max_poll_attempts, int)
            or self.max_poll_attempts < 1
        ):
            raise ValueError(
                "FormalExecutionConfig.max_poll_attempts must be a positive integer"
            )


@dataclass(frozen=True, slots=True)
class LoadedFormalConfig:
    """A fully pinned experiment plus its closed execution controls."""

    experiment: ExperimentConfig
    execution: FormalExecutionConfig

    def __post_init__(self) -> None:
        if type(self.experiment) is not ExperimentConfig:
            raise TypeError("LoadedFormalConfig.experiment must be exact ExperimentConfig")
        if type(self.execution) is not FormalExecutionConfig:
            raise TypeError(
                "LoadedFormalConfig.execution must be exact FormalExecutionConfig"
            )
        if self.experiment.dataset.configurations != CHEMBENCH_CONFIGS:
            raise ValueError("formal config must select all nine ChemBench configs")
        expected_mode = (
            "baseline"
            if self.experiment.evolution.max_rounds == 0
            else "evolution"
        )
        result_parts = PurePosixPath(self.execution.result_root).parts
        actual_mode = (
            result_parts[2]
            if result_parts[:2] == ("results", "formal")
            else result_parts[-1]
        )
        if actual_mode != expected_mode:
            raise ValueError("formal result root must agree with max_rounds mode")
        managed_root = self.execution.result_root.startswith("results/chembench/")
        local_root = (
            self.execution.result_root.startswith("results/chembench_local_codex/")
            or self.execution.result_root.startswith("results/formal/")
        )
        if (
            self.experiment.execution_backend == "managed_codex"
            and not managed_root
        ) or (
            self.experiment.execution_backend == "local_codex_cli"
            and not local_root
        ):
            raise ValueError(
                "formal result root must agree with execution_backend"
            )
        if self.experiment.evolution.max_rounds not in {0, 1, 3}:
            raise ValueError("formal max_rounds must be 0, 1, or 3")


def load_formal_config(path: Path) -> LoadedFormalConfig:
    """Load one strict formal YAML; unknown and missing fields fail closed."""

    if not isinstance(path, Path):
        raise TypeError("load_formal_config.path must be pathlib.Path")
    try:
        with path.open("r", encoding="utf-8") as stream:
            loaded = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError("failed to load formal experiment config") from exc

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
            "formal",
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
    formal = _closed_mapping(
        root["formal"],
        expected={
            "rollout_url",
            "result_root",
            "task_timeout_seconds",
            "poll_interval_seconds",
            "max_poll_attempts",
        },
        field_name="formal",
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
    execution = FormalExecutionConfig(
        rollout_url=formal["rollout_url"],
        result_root=formal["result_root"],
        task_timeout_seconds=formal["task_timeout_seconds"],
        poll_interval_seconds=formal["poll_interval_seconds"],
        max_poll_attempts=formal["max_poll_attempts"],
    )
    return LoadedFormalConfig(experiment=experiment, execution=execution)


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
        detail: list[str] = []
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
    "FormalExecutionConfig",
    "LoadedFormalConfig",
    "load_formal_config",
]
