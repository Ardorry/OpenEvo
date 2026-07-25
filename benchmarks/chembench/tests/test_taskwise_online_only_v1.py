from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from openevo_chembench import taskwise_online_only_v1 as online_only
from openevo_chembench.taskwise_attempt_v1 import (
    TaskwiseAttemptError,
    require_taskwise_completed_paired_attempt_v1,
    taskwise_attempts_root_v1,
)
from openevo_chembench.taskwise_config_v1 import load_taskwise_config_v1
from openevo_chembench.taskwise_sampling_v1 import PILOT500_STREAM_SCOPES


_SOURCE_COMMIT = "a" * 40
_GENERATION_ID = f"gen_{'b' * 64}"
_RECEIPT_SHA256 = "c" * 64
_PILOT_BINDING_SHA256 = "d" * 64
_RUN_ID = "online_only_fix_parser_v1"


def _authorization() -> SimpleNamespace:
    return SimpleNamespace(
        source_commit=_SOURCE_COMMIT,
        paired_canary_generation_id=_GENERATION_ID,
        receipt_sha256=_RECEIPT_SHA256,
        pilot_binding_sha256=_PILOT_BINDING_SHA256,
    )


def _allow_online_only_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        online_only,
        "require_taskwise_pilot_authorization_v1",
        _authorization,
    )
    monkeypatch.setattr(
        online_only,
        "verify_taskwise_source_gate_v1",
        lambda _config: _SOURCE_COMMIT,
    )
    monkeypatch.setattr(
        online_only,
        "_verify_static_inputs",
        lambda *_args: None,
    )


def test_online_only_runtime_reuses_frozen_online_config_and_security() -> None:
    template = load_taskwise_config_v1(
        online_only.CONFIG_ROOT / "online_pilot500_stream_00_taskwise_online_v1.yaml"
    )
    runtime = online_only.online_only_runtime_config_v1(template, run_id=_RUN_ID)

    template_payload = template.to_payload()
    runtime_payload = runtime.to_payload()
    assert runtime_payload.pop("run_name") != template_payload.pop("run_name")
    assert runtime_payload.pop("output_directory") != template_payload.pop("output_directory")
    assert runtime_payload == template_payload
    assert "/unpaired_online_only_pilot500_runs/" in runtime.output_directory
    assert runtime.executor == template.executor
    assert runtime.task_manifest == template.task_manifest
    assert runtime.private_task_manifest == template.private_task_manifest


def test_online_only_suite_runs_ten_streams_without_control_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_online_only_preflight(monkeypatch)
    calls: list[tuple[str, str]] = []

    def fake_stream_runner(
        config_path: Path,
        runtime_config: object,
        run_id: str,
        authorization: object,
    ) -> dict[str, object]:
        assert authorization is not None
        assert run_id == _RUN_ID
        calls.append((config_path.name, runtime_config.scope))
        return {
            "status": "COMPLETED",
            "resume_allowed": False,
            "finding_codes": [],
        }

    result = online_only.run_online_only_pilot500_v1(
        run_id=_RUN_ID,
        repository_root=tmp_path,
        stream_runner=fake_stream_runner,
    )

    assert result["status"] == "COMPLETED"
    assert result["completed_streams"] == 10
    assert result["classification"] == online_only.ONLINE_ONLY_CLASSIFICATION
    assert result["paired_inference_allowed"] is False
    assert result["paired_comparison_allowed"] is False
    assert result["pilot_go_receipt_allowed"] is False
    assert [scope for _path, scope in calls] == list(PILOT500_STREAM_SCOPES)
    assert all(path.startswith("online_") for path, _scope in calls)
    root = online_only.online_only_run_root_v1(tmp_path, _RUN_ID)
    assert (root.stat().st_mode & 0o777) == 0o700
    authority = root / "online_only_authority.json"
    assert (authority.stat().st_mode & 0o777) == 0o600
    assert (
        json.loads(authority.read_text(encoding="utf-8"))["classification"]
        == online_only.ONLINE_ONLY_CLASSIFICATION
    )
    assert not taskwise_attempts_root_v1(
        tmp_path,
        suite_kind="pilot500",
        generation_id=_GENERATION_ID,
    ).exists()


def test_online_only_failure_stops_without_resume_or_stitch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_online_only_preflight(monkeypatch)
    calls = 0

    def failing_stream_runner(
        _config_path: Path,
        _runtime_config: object,
        _run_id: str,
        _authorization: object,
    ) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {
            "status": "EXECUTION_FAILED",
            "resume_allowed": False,
            "finding_codes": ["EXECUTOR_NONZERO_EXIT"],
        }

    with pytest.raises(
        online_only.TaskwiseOnlineOnlyError,
        match="ONLINE_ONLY_SUITE_TERMINAL_FAILURE",
    ):
        online_only.run_online_only_pilot500_v1(
            run_id=_RUN_ID,
            repository_root=tmp_path,
            stream_runner=failing_stream_runner,
        )
    assert calls == 1
    state_path = (
        online_only.online_only_run_root_v1(tmp_path, _RUN_ID) / "online_only_suite_state.json"
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "FAILED"
    assert state["active_scope"] is None
    assert state["streams"]["pilot500_stream_00"]["action"] == "STARTED"
    assert state["streams"]["pilot500_stream_01"]["action"] == "NOT_STARTED"
    assert state["streams"]["pilot500_stream_00"]["resume_allowed"] is False
    with pytest.raises(
        online_only.TaskwiseOnlineOnlyError,
        match="ONLINE_ONLY_RUN_EXISTS",
    ):
        online_only.run_online_only_pilot500_v1(
            run_id=_RUN_ID,
            repository_root=tmp_path,
            stream_runner=failing_stream_runner,
        )
    assert calls == 1


def test_online_only_namespace_is_not_a_paired_comparison_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_online_only_preflight(monkeypatch)
    online_only.run_online_only_pilot500_v1(
        run_id=_RUN_ID,
        repository_root=tmp_path,
        stream_runner=lambda *_args: {
            "status": "COMPLETED",
            "resume_allowed": False,
            "finding_codes": [],
        },
    )

    with pytest.raises(TaskwiseAttemptError, match="PAIRED_ATTEMPT_MISSING"):
        require_taskwise_completed_paired_attempt_v1(
            tmp_path,
            suite_kind="pilot500",
            generation_id=_GENERATION_ID,
        )


def test_stream_marker_is_public_content_free_and_exclusive(tmp_path: Path) -> None:
    template = load_taskwise_config_v1(
        online_only.CONFIG_ROOT / "online_pilot500_stream_00_taskwise_online_v1.yaml"
    )
    output = tmp_path / "stream"
    output.mkdir()
    online_only._write_stream_marker(
        output,
        run_id=_RUN_ID,
        source_commit=_SOURCE_COMMIT,
        authorization=_authorization(),
        config=template,
    )
    marker_path = output / "UNPAIRED_ONLINE_ONLY.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker["classification"] == online_only.ONLINE_ONLY_CLASSIFICATION
    assert marker["paired_inference_allowed"] is False
    assert marker["paired_comparison_allowed"] is False
    assert marker["pilot_go_receipt_allowed"] is False
    assert (marker_path.stat().st_mode & 0o777) == 0o644
    assert "target" not in json.dumps(marker).casefold()
    with pytest.raises(FileExistsError):
        online_only._write_stream_marker(
            output,
            run_id=_RUN_ID,
            source_commit=_SOURCE_COMMIT,
            authorization=_authorization(),
            config=template,
        )


def test_online_only_cli_has_no_resume_compare_or_go_commands() -> None:
    with pytest.raises(SystemExit):
        online_only.main(["compare"])
    with pytest.raises(SystemExit):
        online_only.main(["run-pilot500", "--run-id", _RUN_ID, "--resume"])
    script = (
        online_only.CONFIG_ROOT.parent
        / "scripts"
        / "run_online_only_pilot500_streams_taskwise_online_v1.sh"
    )
    text = script.read_text(encoding="utf-8")
    assert online_only.ONLINE_ONLY_CLASSIFICATION in text
    assert "run-pilot500" in text
    assert "run-pilot-stream-suite" not in text
    assert os.access(script, os.X_OK)
