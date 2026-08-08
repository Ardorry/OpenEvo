from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from openevo_researchclawbench import cli
from openevo_researchclawbench.config import ExperimentConfig

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
NATIVE_LIFE005_PROTOCOL = (
    REPOSITORY_ROOT / "configs/researchclawbench/native_life005_engineering.yaml"
)
PER_ITEM_COMMUNITY17_PROTOCOL = (
    REPOSITORY_ROOT / "configs/researchclawbench/per_item_community17.yaml"
)


class _MeetingConfig:
    formal_runs_v11 = None
    per_item_reset: ClassVar[dict[str, bool]] = {"only_per_item_reset": True}

    def __init__(self, root: Path) -> None:
        self.experiment_root = root
        self.raw = {}

    def require(self, key: str) -> str:
        return {
            "experiment_id": "sequential_task_reflector_evolution_v0",
            "protocol_name": "per_item_supervised_one_step_reset_v1",
            "judge.model": "openai/gpt-5.1",
            "tasks": list(cli.FROZEN_TASKS),
        }[key]


def _host_profile(*, ready: bool) -> dict[str, Any]:
    return {
        "ready": ready,
        "profile": "docker_user_container_v1",
        "mapping_identity_present": ready,
        "reason_code": None if ready else "DOCKER_USER_CONTAINER_MAPPING_UNAVAILABLE",
        "secret_recorded": False,
    }


def test_native_life005_protocol_pins_current_judge_identity_contract() -> None:
    config = ExperimentConfig.load(NATIVE_LIFE005_PROTOCOL)

    assert config.require("judge.scorer_commit") == (
        config.require("source_identity.researchclawbench_commit")
    )
    assert config.require("judge.api_key_env") == "JUDGE_API_KEY"
    assert config.require("judge.api_base_env") == "JUDGE_API_BASE"
    assert config.require("judge.model_env") == "JUDGE_MODEL_NAME"
    assert config.require("native_engineering_validation.execute_judge") is False


def test_per_item_community17_protocol_is_closed_and_has_no_official_flow() -> None:
    config = ExperimentConfig.load(PER_ITEM_COMMUNITY17_PROTOCOL)

    assert config.per_item_reset is not None
    assert config.require("attempts_per_task") == 2
    assert config.require("reflector_rounds_per_task") == 1
    assert config.require("candidate_runs") == 34
    assert config.require("artifact_evolution_requests") == 51
    assert config.require("official_frozen_test_runs") == 0
    assert "formal_runs" not in config.raw
    assert "official_freeze" not in config.raw
    assert config.require("cross_task.carry_project_head") is False
    assert config.require("cross_task.reset_active_state") is True
    assert config.require("judge.model") == "openai/gpt-5.1"
    assert config.require("judge.provider_only") == "azure"
    assert config.require("judge.allow_fallbacks") is False


def test_run_per_item_zero_call_demo_parses_and_prints_safe_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _MeetingConfig(tmp_path)
    monkeypatch.setattr(cli.ExperimentConfig, "load", lambda _path: config)
    monkeypatch.setattr(
        cli,
        "managed_core_profile_readiness",
        lambda _config: _host_profile(ready=False),
    )
    monkeypatch.setenv("RCB_JUDGE_API_KEY", "never-print-this-judge-secret")
    monkeypatch.setenv("CODEX_HOME", "/never/print/this/profile")

    result = cli.main(
        [
            "run-per-item",
            "--protocol",
            str(tmp_path / "protocol.yaml"),
            "--run-id",
            "rcb_oe_v0_meeting_demo_life_005",
            "--task",
            "Life_005",
            "--dry-run",
            "--no-model-calls",
        ]
    )

    output = capsys.readouterr().out
    payload = json.loads(output[output.index("{") :])
    assert result == 0
    assert "ResearchClawBench Per-Item Runner" in output
    assert "backend: OpenEvo Core / CodexHarness" in output
    assert "model: gpt-5.5" in output
    assert "Judge:\n  backend: OpenRouter" in output
    assert "Task: Life_005" in output
    assert "never-print-this" not in output
    assert payload["provider_calls"] == 0
    assert payload["task_discovery"] == ["Life_005"]
    assert payload["judge_feedback_in_evolution"] is False
    assert payload["gt_visible_to_candidate"] is False
    assert payload["managed_runtime_reason_code"] == ("DOCKER_USER_CONTAINER_MAPPING_UNAVAILABLE")


def test_run_per_item_live_fails_before_control_when_runtime_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _MeetingConfig(tmp_path)
    monkeypatch.setattr(cli.ExperimentConfig, "load", lambda _path: config)
    monkeypatch.setattr(
        cli,
        "managed_core_profile_readiness",
        lambda _config: _host_profile(ready=False),
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
            "rcb_oe_v0_meeting_demo_life_005",
            "--task",
            "Life_005",
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
        cli,
        "managed_core_profile_readiness",
        lambda _config: _host_profile(ready=True),
    )
    monkeypatch.setattr(
        cli,
        "judge_credential_readiness",
        lambda _config: {"ready": True},
    )
    observed: dict[str, Any] = {}

    class Authority:
        def close(self) -> None:
            observed["authority_closed"] = True

    class Control:
        def __init__(self, **kwargs: Any) -> None:
            observed.update(kwargs)

        def initialize(self) -> dict[str, Any]:
            return {
                "stage": "ITEM_RESET",
                "active_artifact_ids": [],
                "core_project_id": None,
            }

        def verify(self) -> dict[str, Any]:
            return {"status": "PASS"}

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
            "rcb_oe_v0_meeting_demo_life_005",
            "--task",
            "Life_005",
        ]
    )

    capsys.readouterr()
    assert result == 0
    assert observed["task_ids"] == ("Life_005",)
    assert observed["production"] is True
    assert observed["current_task_gt_supervision"] is True
    assert observed["per_item_reset"] is True
    assert observed["require_existing"] is False
    assert observed["authority_closed"] is True


def test_per_item_community17_zero_call_plan_is_complete_and_non_mutating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _MeetingConfig(tmp_path)
    monkeypatch.setattr(cli.ExperimentConfig, "load", lambda _path: config)
    monkeypatch.setattr(
        cli,
        "acquire_managed_core_control",
        lambda _config: (_ for _ in ()).throw(
            AssertionError("Community dry-run acquired Core authority")
        ),
    )

    result = cli.main(
        [
            "run-per-item-community",
            "--protocol",
            str(tmp_path / "protocol.yaml"),
            "--run-id",
            "rcb_oe_v0_per_item_community17_meeting",
            "--dry-run",
            "--no-model-calls",
        ]
    )

    output = capsys.readouterr().out
    payload = json.loads(output[output.index("{") :])
    assert result == 0
    assert payload["provider_calls"] == 0
    assert payload["judge_calls"] == 0
    assert payload["reflector_calls"] == 0
    assert payload["task_discovery"] == list(cli.FROZEN_TASKS)
    assert len(payload["plan"]) == 17
    assert all(item["generation_zero"] is True for item in payload["plan"])
    assert all(
        item["cross_task_inheritance"] is False for item in payload["plan"]
    )
    assert "Official" not in output
    assert "FINAL_FROZEN" not in output
    assert not (tmp_path / "per_item_community").exists()


def test_per_item_community_failure_is_recorded_and_never_silently_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _MeetingConfig(tmp_path)
    monkeypatch.setattr(cli.ExperimentConfig, "load", lambda _path: config)
    monkeypatch.setattr(
        cli,
        "managed_core_profile_readiness",
        lambda _config: _host_profile(ready=True),
    )
    monkeypatch.setattr(
        cli,
        "judge_credential_readiness",
        lambda _config: {"ready": True},
    )

    class Authority:
        def close(self) -> None:
            pass

    created: list[tuple[str, tuple[str, ...]]] = []

    class Control:
        def __init__(self, **kwargs: Any) -> None:
            self.run_id = kwargs["run_id"]
            self.task_ids = kwargs["task_ids"]
            created.append((self.run_id, self.task_ids))

        def initialize(self) -> dict[str, Any]:
            if self.task_ids == ("Astronomy_004",):
                return {
                    "stage": "ITEM_RESET",
                    "paired_result": {
                        "baseline_score": 1.0,
                        "evolved_score": 2.0,
                        "delta": 1.0,
                    },
                    "item_reset_receipt": {"active_artifact_ids": []},
                    "_state_sha256": "1" * 64,
                }
            return {"stage": "BLOCKED", "_state_sha256": "2" * 64}

        def verify(self) -> dict[str, Any]:
            return {"status": "PASS"}

        def run_next(self) -> dict[str, Any]:  # pragma: no cover
            raise AssertionError("terminal fixture must not advance")

    monkeypatch.setattr(cli, "acquire_managed_core_control", lambda _config: Authority())
    monkeypatch.setattr(cli, "DurableTrainingControl", Control)

    result = cli.main(
        [
            "run-per-item-community",
            "--protocol",
            str(tmp_path / "protocol.yaml"),
            "--run-id",
            "rcb_oe_v0_per_item_failure_contract",
        ]
    )

    capsys.readouterr()
    progress = json.loads(
        (
            tmp_path
            / "per_item_community/rcb_oe_v0_per_item_failure_contract/progress.json"
        ).read_text(encoding="utf-8")
    )
    second_isolation = json.loads(
        (
            tmp_path
            / "per_item_community/rcb_oe_v0_per_item_failure_contract/items"
            / "02_Chemistry_004/pre_dispatch_isolation.json"
        ).read_text(encoding="utf-8")
    )
    assert result == 5
    assert len(created) == 2
    assert created[0][1] == ("Astronomy_004",)
    assert created[1][1] == ("Chemistry_004",)
    assert progress["status"] == "PER_ITEM_COMMUNITY17_STOPPED_ON_ITEM_FAILURE"
    assert progress["failure"]["task_id"] == "Chemistry_004"
    assert progress["statistics"]["attempted"] == 2
    assert progress["statistics"]["paired_completed"] == 1
    assert progress["statistics"]["failed"] == 1
    assert progress["silent_skip"] is False
    assert progress["cross_task_continuation_after_failure"] is False
    assert second_isolation["active_artifact_ids"] == []
    assert second_isolation["cross_project_fork_from_previous_task"] is False


def test_native_evolution_recovery_zero_call_command_parses_without_core(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _MeetingConfig(tmp_path)
    monkeypatch.setattr(cli.ExperimentConfig, "load", lambda _path: config)
    monkeypatch.setattr(
        cli,
        "native_evolution_recovery_dry_run",
        lambda _config, *, source_run_id, recovery_run_id: {
            "status": "NATIVE_EVOLUTION_RECOVERY_DRY_RUN_NO_MODEL_CALLS",
            "source_run_id": source_run_id,
            "recovery_run_id": recovery_run_id,
            "provider_calls": 0,
            "model_started": False,
        },
    )
    monkeypatch.setattr(
        cli,
        "acquire_managed_core_control",
        lambda _config: (_ for _ in ()).throw(
            AssertionError("dry-run acquired Core authority")
        ),
    )

    result = cli.main(
        [
            "recover-native-evolution",
            "--protocol",
            str(tmp_path / "protocol.yaml"),
            "--source-run-id",
            "rcb_oe_v0_native_life005_source",
            "--run-id",
            "rcb_oe_v0_native_life005_recovery",
            "--dry-run",
            "--no-model-calls",
        ]
    )

    output = capsys.readouterr().out
    payload = json.loads(output[output.index("{") :])
    assert result == 0
    assert "Native Evolution Recovery" in output
    assert payload["provider_calls"] == 0
    assert payload["model_started"] is False


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
