from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from openevo_chembench.temperature_full_evolve_v1.config import (
    BATCH_SIZE,
    CANDIDATE_MAX_WORKERS,
    HISTORICAL_EXPOSURE_POLICY,
    POST_DURABLE_TERMINAL_COOLDOWN_SECONDS,
    TEST_COUNT,
    TRAIN_COUNT,
    TemperatureFullEvolveConfigError,
    load_temperature_full_evolve_config,
)

REPOSITORY = Path(__file__).resolve().parents[4]
CONFIG = (
    REPOSITORY
    / "benchmarks/chembench/configs/temperature_full_evolve_v1/"
    "temperature_full_evolve_v1.yaml"
)


def test_live_config_is_closed_and_generation_zero_provenance_is_explicit() -> None:
    config = load_temperature_full_evolve_config(CONFIG.resolve(strict=True))

    assert config.model == "gpt-5.5"
    assert config.reasoning_effort == "medium"
    assert config.payload["protocol_scope"]["historical_exposure_policy"] == (
        HISTORICAL_EXPOSURE_POLICY
    )
    assert config.payload["protocol_scope"]["prior_artifact_import"] == "forbidden"
    assert config.framework_lock_relative == (
        "state/chembench_temperature_full_evolve_v1/formal_runtime_v7/framework/"
        "framework-lock.json"
    )
    assert config.candidate_max_workers == CANDIDATE_MAX_WORKERS == 1
    assert (
        config.post_durable_terminal_cooldown_seconds
        == POST_DURABLE_TERMINAL_COOLDOWN_SECONDS
        == 15
    )
    assert TRAIN_COUNT == TEST_COUNT == 100
    assert TRAIN_COUNT % BATCH_SIZE == 0
    assert len(config.digest) == 64


def test_config_rejects_silent_never_exposed_claim(tmp_path: Path) -> None:
    payload = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    payload["protocol_scope"]["historical_exposure_policy"] = "never_exposed"
    candidate = tmp_path / "config.yaml"
    candidate.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(
        TemperatureFullEvolveConfigError,
        match="TEMPERATURE_PROTOCOL_SCOPE_INVALID",
    ):
        load_temperature_full_evolve_config(candidate.resolve())


def test_config_rejects_parallel_candidate_workers(tmp_path: Path) -> None:
    payload = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    payload["executor"]["candidate_max_workers"] = 2
    candidate = tmp_path / "config.yaml"
    candidate.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(
        TemperatureFullEvolveConfigError,
        match="TEMPERATURE_EXECUTOR_POLICY_INVALID",
    ):
        load_temperature_full_evolve_config(candidate.resolve())


def test_config_rejects_changed_post_durable_terminal_cooldown(
    tmp_path: Path,
) -> None:
    payload = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    payload["executor"]["post_durable_terminal_cooldown_seconds"] = 0
    candidate = tmp_path / "config.yaml"
    candidate.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(
        TemperatureFullEvolveConfigError,
        match="TEMPERATURE_EXECUTOR_POLICY_INVALID",
    ):
        load_temperature_full_evolve_config(candidate.resolve())


def test_config_rejects_non_integer_post_durable_terminal_cooldown(
    tmp_path: Path,
) -> None:
    payload = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    payload["executor"]["post_durable_terminal_cooldown_seconds"] = 15.0
    candidate = tmp_path / "config.yaml"
    candidate.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(
        TemperatureFullEvolveConfigError,
        match="TEMPERATURE_EXECUTOR_POLICY_INVALID",
    ):
        load_temperature_full_evolve_config(candidate.resolve())
