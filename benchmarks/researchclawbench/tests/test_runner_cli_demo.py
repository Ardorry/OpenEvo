from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from openevo_researchclawbench import cli


class _MeetingConfig:
    formal_runs_v11 = None

    def __init__(self, root: Path) -> None:
        self.experiment_root = root

    def require(self, key: str) -> str:
        return {
            "experiment_id": "sequential_task_reflector_evolution_v0",
            "judge.model": "openai/gpt-5.1",
        }[key]


def _host_profile(*, ready: bool) -> dict[str, Any]:
    return {
        "ready": ready,
        "profile": "docker_user_container_v1",
        "mapping_identity_present": ready,
        "reason_code": None if ready else "DOCKER_USER_CONTAINER_MAPPING_UNAVAILABLE",
        "secret_recorded": False,
    }


def test_run_per_item_zero_call_demo_parses_and_prints_safe_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _MeetingConfig(tmp_path)
    monkeypatch.setattr(cli.ExperimentConfig, "load", lambda _path: config)
    monkeypatch.setattr(
        cli, "managed_core_host_profile_readiness", lambda: _host_profile(ready=False)
    )
    monkeypatch.setenv("RCB_JUDGE_API_KEY", "never-print-this-judge-secret")
    monkeypatch.setenv("CODEX_HOME", "/never/print/this/profile")

    result = cli.main(
        [
            "run-per-item",
            "--protocol",
            str(tmp_path / "protocol.yaml"),
            "--run-id",
            "rcb_oe_v0_meeting_demo_earth_004",
            "--task",
            "Earth_004",
            "--dry-run",
            "--no-model-calls",
        ]
    )

    output = capsys.readouterr().out
    payload = json.loads(output[output.index("{") :])
    assert result == 0
    assert "ResearchClawBench Runner" in output
    assert "backend: OpenEvo harness" in output
    assert "model: gpt-5.5" in output
    assert "model: openai/gpt-5.1" in output
    assert "Task: Earth_004" in output
    assert "never-print-this" not in output
    assert payload["provider_calls"] == 0
    assert payload["task_discovery"] == ["Earth_004"]
    assert payload["managed_runtime_reason_code"] == ("DOCKER_USER_CONTAINER_MAPPING_UNAVAILABLE")


def test_run_per_item_live_fails_before_control_when_runtime_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _MeetingConfig(tmp_path)
    monkeypatch.setattr(cli.ExperimentConfig, "load", lambda _path: config)
    monkeypatch.setattr(
        cli, "managed_core_host_profile_readiness", lambda: _host_profile(ready=False)
    )
    monkeypatch.setattr(
        cli,
        "DurableTrainingControl",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("control created before managed-runtime readiness")
        ),
    )

    result = cli.main(
        [
            "run-per-item",
            "--protocol",
            str(tmp_path / "protocol.yaml"),
            "--run-id",
            "rcb_oe_v0_meeting_demo_earth_004",
            "--task",
            "Earth_004",
        ]
    )

    output = capsys.readouterr().out
    payload = json.loads(output[output.index("{") :])
    assert result == 5
    assert payload["status"] == "BLOCKED_OPEN_EVO_HARNESS_RUNTIME"
    assert payload["model_started"] is False
    assert payload["provider_calls"] == 0


def test_run_per_item_live_selects_existing_production_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _MeetingConfig(tmp_path)
    monkeypatch.setattr(cli.ExperimentConfig, "load", lambda _path: config)
    monkeypatch.setattr(
        cli, "managed_core_host_profile_readiness", lambda: _host_profile(ready=True)
    )
    observed: dict[str, Any] = {}

    class Authority:
        def close(self) -> None:
            observed["authority_closed"] = True

    class Control:
        def __init__(self, **kwargs: Any) -> None:
            observed.update(kwargs)

        def initialize(self) -> dict[str, Any]:
            return {"stage": "FINAL_FROZEN"}

        def run_next(self) -> dict[str, Any]:  # pragma: no cover - terminal fixture
            raise AssertionError("terminal fixture must not advance")

    monkeypatch.setattr(cli, "acquire_managed_core_control", lambda _config: Authority())
    monkeypatch.setattr(cli, "DurableTrainingControl", Control)

    result = cli.main(
        [
            "run-per-item",
            "--protocol",
            str(tmp_path / "protocol.yaml"),
            "--run-id",
            "rcb_oe_v0_meeting_demo_earth_004",
            "--task",
            "Earth_004",
        ]
    )

    capsys.readouterr()
    assert result == 0
    assert observed["task_ids"] == ("Earth_004",)
    assert observed["production"] is True
    assert observed["require_existing"] is False
    assert observed["authority_closed"] is True


def test_native_codex_delivery_cli_rejects_direct_subprocess_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Config:
        output_root = tmp_path
        dry_run = False

        def require(self, key: str) -> str:
            return {
                "candidate.provider": "native_codex",
                "candidate.model": "gpt-5.5",
            }[key]

    monkeypatch.setattr(cli.DeliveryCanaryConfig, "load", lambda _path: Config())
    monkeypatch.setattr(
        cli, "managed_core_host_profile_readiness", lambda: _host_profile(ready=True)
    )

    result = cli.main(
        [
            "delivery-canary-v3",
            "--config",
            str(tmp_path / "delivery.yaml"),
            "--batch-id",
            "delivery-audit",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 5
    assert payload["status"] == "BLOCKED_OPEN_EVO_HARNESS_WIRING"
    assert payload["reason_code"] == "DIRECT_CODEX_SUBPROCESS_ROUTE_REJECTED"
    assert payload["model_started"] is False
    assert payload["provider_calls"] == 0
