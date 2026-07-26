from __future__ import annotations

import json
import os
import stat
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
from openevo_chembench.local_codex_executor import TaskwiseExecutorSuccessReceiptV1
from openevo_chembench.taskwise_sampling_v1 import PILOT500_STREAM_SCOPES


_SOURCE_COMMIT = "a" * 40
_GENERATION_ID = f"online_canary_{'b' * 64}"
_PAIRED_GENERATION_ID = f"gen_{'e' * 64}"
_RECEIPT_SHA256 = "c" * 64
_PILOT_BINDING_SHA256 = "d" * 64
_RUN_ID = "online_only_fix_parser_v1"


def _authorization() -> SimpleNamespace:
    return SimpleNamespace(
        source_commit=_SOURCE_COMMIT,
        online_canary_generation_id=_GENERATION_ID,
        receipt_sha256=_RECEIPT_SHA256,
        online_pilot_binding_sha256=_PILOT_BINDING_SHA256,
    )


def _allow_online_only_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        online_only,
        "require_online_canary_authorization_v1",
        lambda _package_root: _authorization(),
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
    monkeypatch.setattr(
        online_only,
        "_build_online_only_descriptive_report",
        lambda **_kwargs: {
            "schema_version": online_only.ONLINE_ONLY_REPORT_SCHEMA_V1,
            "labels": list(online_only.ONLINE_ONLY_REPORT_LABELS),
            "standard_chembench4k_score_claimed": False,
            "causal_comparison_claimed": False,
        },
    )


def _completed_stream_result() -> dict[str, object]:
    return {
        "schema_version": "taskwise_online_run_result_v1",
        "protocol_id": "taskwise_online_evolution_v1",
        "protocol_labels": [
            "ONLINE_TASKWISE_EVOLUTION",
            "TEST_TIME_ADAPTATION",
            "NON_STANDARD_CHEMBENCH4K_PROTOCOL",
            "NOT_A_STANDARD_LEADERBOARD_SCORE",
        ],
        "standard_chembench4k_score_claimed": False,
        "status": "COMPLETED",
        "arm": "online",
        "planned_tasks": 50,
        "completed_tasks": 50,
        "completion_count": 150,
        "update_count": 100,
        "core_job_count": 100,
        "core_artifact_count": 100,
        "context_resolution_count": 100,
        "context_binding_violation_count": 0,
        "session_attempt_count": 150,
        "resume_allowed": False,
        "memory_aggregate": {"approved_artifact_count": 100},
        "finding_codes": [],
    }


def _private_executor_fixture(
    tmp_path: Path,
) -> tuple[object, tuple[dict[str, object], ...], Path]:
    template = load_taskwise_config_v1(
        online_only.CONFIG_ROOT / "online_pilot500_stream_00_taskwise_online_v1.yaml"
    )
    config = online_only.online_only_runtime_config_v1(template, run_id=_RUN_ID)
    root = (
        tmp_path
        / "state"
        / "taskwise_online_v1"
        / "private_executor_events"
        / config.scope
        / config.run_name
        / config.arm
    )
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    completions: list[dict[str, object]] = []
    for index in range(150):
        task_index, round_index = divmod(index, 3)
        session_id = f"session_{index:03d}"
        task_uid = online_only.hashlib.sha256(f"task-{task_index}".encode()).hexdigest()
        event_sha256 = online_only.hashlib.sha256(f"event-{index}".encode()).hexdigest()
        receipt = TaskwiseExecutorSuccessReceiptV1(
            run_id=config.run_name,
            task_uid=task_uid,
            task_index=task_index,
            round_index=round_index,
            session_id=session_id,
            event_stream_sha256=event_sha256,
            event_count=1,
            tool_event_count=0,
            completion_observed=True,
            process_return_code=0,
            process_signal=None,
            cleanup_status="COMPLETE",
            residual_root_count=0,
            codex_cli_version=config.codex_cli_version,
            model=config.model,
            executor_policy_sha256=online_only._executor_policy_sha256(config),
            created_at_utc="2026-07-26T00:00:00+00:00",
        )
        receipt_path = root / f"success_{index:032x}.json"
        receipt_path.write_bytes(online_only._canonical_bytes(receipt.to_payload()))
        receipt_path.chmod(0o600)
        completions.append(
            {
                "session_id": session_id,
                "task_uid": task_uid,
                "task_ordinal": task_index,
                "round_index": round_index,
                "transcript_reference": f"local-codex-jsonl:sha256:{event_sha256}",
                "runtime_metadata": {
                    "model": config.model,
                    "codex_cli_version": config.codex_cli_version,
                    "harness": "codex_cli",
                    "execution_backend": "local_codex_cli",
                },
            }
        )
    return config, tuple(completions), root


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


def test_online_only_report_binds_cleanup_to_private_success_receipts(
    tmp_path: Path,
) -> None:
    config, completions, _root = _private_executor_fixture(tmp_path)

    count, digest = online_only._verify_stream_executor_success_evidence(
        package_root=tmp_path,
        config=config,
        completions=completions,
    )

    assert count == 150
    assert len(digest) == 64
    assert all("cleanup_status" not in row["runtime_metadata"] for row in completions)


@pytest.mark.parametrize("corruption", ["missing", "unsafe_mode", "identity", "cleanup"])
def test_online_only_report_rejects_invalid_private_success_evidence(
    tmp_path: Path,
    corruption: str,
) -> None:
    config, completions, root = _private_executor_fixture(tmp_path)
    receipt = sorted(root.glob("success_*.json"))[0]
    if corruption == "missing":
        receipt.unlink()
    elif corruption == "unsafe_mode":
        receipt.chmod(0o644)
        assert stat.S_IMODE(receipt.lstat().st_mode) == 0o644
    else:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
        if corruption == "identity":
            payload["task_uid"] = "f" * 64
        else:
            payload["cleanup_status"] = "FAILED"
            payload["residual_root_count"] = 1
        receipt.write_bytes(online_only._canonical_bytes(payload))
        receipt.chmod(0o600)

    with pytest.raises(
        online_only.TaskwiseOnlineOnlyError,
        match="ONLINE_ONLY_EXECUTOR_EVIDENCE_INVALID",
    ):
        online_only._verify_stream_executor_success_evidence(
            package_root=tmp_path,
            config=config,
            completions=completions,
        )


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
        return _completed_stream_result()

    result = online_only.run_online_only_pilot500_v1(
        run_id=_RUN_ID,
        repository_root=tmp_path,
        stream_runner=fake_stream_runner,
    )

    assert result["status"] == "COMPLETED"
    assert result["completed_streams"] == 10
    assert result["classification"] == list(online_only.ONLINE_ONLY_REPORT_LABELS)
    assert result["primary_classification"] == online_only.ONLINE_ONLY_CLASSIFICATION
    assert result["paired_inference_allowed"] is False
    assert result["paired_comparison_allowed"] is False
    assert result["pilot_go_receipt_allowed"] is False
    assert [scope for _path, scope in calls] == list(PILOT500_STREAM_SCOPES)
    assert all(path.startswith("online_") for path, _scope in calls)
    root = online_only.online_only_run_root_v1(tmp_path, _RUN_ID)
    assert (root.stat().st_mode & 0o777) == 0o700
    authority = root / "online_only_authority.json"
    assert (authority.stat().st_mode & 0o777) == 0o600
    assert json.loads(authority.read_text(encoding="utf-8"))["classification"] == list(
        online_only.ONLINE_ONLY_REPORT_LABELS
    )
    assert not taskwise_attempts_root_v1(
        tmp_path,
        suite_kind="pilot500",
        generation_id=_PAIRED_GENERATION_ID,
    ).exists()
    assert all(
        entry["completion_count"] == 150
        and entry["update_count"] == 100
        and entry["core_job_count"] == 100
        and entry["core_artifact_count"] == 100
        and entry["context_resolution_count"] == 100
        for entry in result["streams"].values()
    )
    assert result["completed_tasks"] == 500
    assert result["completion_count"] == 1500
    assert result["session_attempt_count"] == 1500
    assert result["update_count"] == 1000
    assert result["core_job_count"] == 1000
    assert result["core_artifact_count"] == 1000
    assert result["context_resolution_count"] == 1000
    report = json.loads(
        (
            online_only.online_only_run_root_v1(tmp_path, _RUN_ID)
            / "online_only_descriptive_report.json"
        ).read_text(encoding="utf-8")
    )
    assert report["labels"] == list(online_only.ONLINE_ONLY_REPORT_LABELS)
    assert "decision" not in report
    assert "go_gate" not in report
    assert (
        result["descriptive_report_sha256"]
        == online_only.hashlib.sha256(online_only._canonical_bytes(report)).hexdigest()
    )


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("planned_tasks", 49),
        ("completed_tasks", 49),
        ("completion_count", 149),
        ("session_attempt_count", 149),
        ("update_count", 99),
        ("core_job_count", 99),
        ("core_artifact_count", 99),
        ("context_resolution_count", 99),
        ("context_binding_violation_count", 1),
        ("resume_allowed", True),
        ("finding_codes", ["EXECUTOR_SECURITY_TOOL_USE_VIOLATION"]),
    ],
)
def test_completed_stream_result_requires_exact_online_evidence(
    field: str,
    invalid: object,
) -> None:
    result = _completed_stream_result()
    result[field] = invalid

    with pytest.raises(online_only.TaskwiseCLIError, match="EXECUTOR_OUTPUT_INVALID"):
        online_only._online_only_stream_result_evidence(result)


def test_minimal_completed_result_is_rejected_and_closes_suite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_online_only_preflight(monkeypatch)

    with pytest.raises(online_only.TaskwiseCLIError, match="EXECUTOR_OUTPUT_INVALID"):
        online_only.run_online_only_pilot500_v1(
            run_id=_RUN_ID,
            repository_root=tmp_path,
            stream_runner=lambda *_args: {
                "status": "COMPLETED",
                "resume_allowed": False,
                "finding_codes": [],
            },
        )

    state_path = (
        online_only.online_only_run_root_v1(tmp_path, _RUN_ID) / "online_only_suite_state.json"
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["status"] == "FAILED"
    assert state["active_scope"] is None
    assert state["streams"]["pilot500_stream_00"]["failure_code"] == ("EXECUTOR_OUTPUT_INVALID")
    assert state["streams"]["pilot500_stream_01"]["action"] == "NOT_STARTED"


def test_runtime_config_failure_closes_active_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_online_only_preflight(monkeypatch)
    monkeypatch.setattr(
        online_only,
        "online_only_runtime_config_v1",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("synthetic runtime config failure")
        ),
    )

    with pytest.raises(ValueError, match="synthetic runtime config failure"):
        online_only.run_online_only_pilot500_v1(
            run_id=_RUN_ID,
            repository_root=tmp_path,
            stream_runner=lambda *_args: pytest.fail("stream runner must not start"),
        )

    state = json.loads(
        (
            online_only.online_only_run_root_v1(tmp_path, _RUN_ID) / "online_only_suite_state.json"
        ).read_text(encoding="utf-8")
    )
    assert state["status"] == "FAILED"
    assert state["active_scope"] is None
    assert state["streams"]["pilot500_stream_01"]["action"] == "NOT_STARTED"


def test_canary_receipt_is_reverified_before_each_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_online_only_preflight(monkeypatch)
    drifted = SimpleNamespace(
        **{
            **vars(_authorization()),
            "receipt_sha256": "e" * 64,
        }
    )
    authorizations = iter((_authorization(), drifted))
    monkeypatch.setattr(
        online_only,
        "require_online_canary_authorization_v1",
        lambda _package_root: next(authorizations),
    )
    calls = 0

    def runner(*_args: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return _completed_stream_result()

    with pytest.raises(
        online_only.TaskwiseOnlineOnlyError,
        match="CANARY_AUTHORITY_MISMATCH",
    ):
        online_only.run_online_only_pilot500_v1(
            run_id=_RUN_ID,
            repository_root=tmp_path,
            stream_runner=runner,
        )

    assert calls == 0
    state = json.loads(
        (
            online_only.online_only_run_root_v1(tmp_path, _RUN_ID) / "online_only_suite_state.json"
        ).read_text(encoding="utf-8")
    )
    assert state["status"] == "FAILED"
    assert state["active_scope"] is None


def test_resource_cleanup_attempts_both_and_preserves_primary_failure() -> None:
    closed: list[str] = []

    class Resource:
        def __init__(self, name: str, *, fail: bool) -> None:
            self.name = name
            self.fail = fail

        def close(self) -> None:
            closed.append(self.name)
            if self.fail:
                raise RuntimeError(f"{self.name} cleanup failed")

    cleanup = online_only._close_online_only_resources(
        Resource("core", fail=True),
        Resource("executor", fail=True),
    )
    assert closed == ["core", "executor"]
    assert len(cleanup) == 2

    primary = RuntimeError("primary security failure")
    with pytest.raises(RuntimeError, match="primary security failure") as caught:
        online_only._raise_primary_or_cleanup(primary, cleanup)
    assert caught.value is primary
    with pytest.raises(
        online_only.TaskwiseCLIError,
        match="EXECUTOR_CLEANUP_FAILED",
    ):
        online_only._raise_primary_or_cleanup(None, cleanup)


def test_stream_cleanup_runs_for_base_exception_and_preserves_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = load_taskwise_config_v1(
        online_only.CONFIG_ROOT / "online_pilot500_stream_00_taskwise_online_v1.yaml"
    )
    closed: list[str] = []
    primary = KeyboardInterrupt("synthetic operator interrupt")

    class Resource:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            closed.append(self.name)
            raise SystemExit(f"{self.name} cleanup interrupt")

    class Runner:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def run(self) -> object:
            raise primary

    executor = Resource("executor")
    core = Resource("core")
    loader = SimpleNamespace(manifest=SimpleNamespace(combined_sha256="a" * 64))
    manifest = SimpleNamespace(item_count=0, public_sha256="b" * 64)
    monkeypatch.setattr(online_only, "load_taskwise_config_v1", lambda _path: template)
    monkeypatch.setattr(
        online_only,
        "verify_taskwise_source_gate_v1",
        lambda _config: _SOURCE_COMMIT,
    )
    monkeypatch.setattr(
        online_only,
        "_verify_static_inputs",
        lambda *_args: (loader, manifest, {}),
    )
    monkeypatch.setattr(online_only, "_build_episodes", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        online_only,
        "_resolve_workspace_path",
        lambda *_args, **_kwargs: tmp_path / "output",
    )
    monkeypatch.setattr(online_only, "verify_taskwise_codex_policy_v1", lambda _config: None)
    monkeypatch.setattr(
        online_only, "build_default_taskwise_executor_v1", lambda _config: executor
    )
    monkeypatch.setattr(online_only, "_ClosedExecutorFailureAdapterV1", lambda value: value)
    monkeypatch.setattr(online_only, "build_default_taskwise_core_port_v1", lambda _config: core)
    monkeypatch.setattr(online_only, "_build_run_config", lambda **_kwargs: object())
    monkeypatch.setattr(online_only, "TaskwiseOnlineRunnerV1", Runner)

    with pytest.raises(KeyboardInterrupt, match="synthetic operator interrupt") as caught:
        online_only._run_online_only_stream(
            Path("synthetic.yaml"),
            template,
            _RUN_ID,
            _authorization(),
        )

    assert caught.value is primary
    assert closed == ["core", "executor"]


def test_completed_suite_requires_exact_aggregate_counts() -> None:
    state = {
        "total_task_count": 500,
        "streams": {
            scope: {
                "completed": True,
                **online_only._completed_stream_counts(_completed_stream_result()),
            }
            for scope in PILOT500_STREAM_SCOPES
        },
    }
    online_only._update_online_only_suite_totals(state)
    online_only._verify_completed_suite_totals(state)

    state["streams"]["pilot500_stream_09"]["core_artifact_count"] = 99
    online_only._update_online_only_suite_totals(state)
    with pytest.raises(
        online_only.TaskwiseOnlineOnlyError,
        match="ONLINE_ONLY_SUITE_TOTAL_MISMATCH",
    ):
        online_only._verify_completed_suite_totals(state)


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (
            "SECURITY_TOOL_USE_VIOLATION",
            "EXECUTOR_SECURITY_TOOL_USE_VIOLATION",
            "EXECUTOR_SECURITY_TOOL_USE_VIOLATION",
        ),
        (
            "TASKWISE_CONTEXT_BINDING_VIOLATION",
            "TASKWISE_CONTEXT_BINDING_VIOLATION",
            "TASKWISE_CONTEXT_BINDING_VIOLATION",
        ),
        (
            "TASKWISE_EVOLUTION_UPDATE_FAILED",
            "TASKWISE_EVOLUTION_UPDATE_FAILED",
            "TASKWISE_EVOLUTION_UPDATE_FAILED",
        ),
        ("EXECUTION_FAILED", "EXECUTOR_INTERNAL_ERROR", "EXECUTOR_INTERNAL_ERROR"),
    ],
)
def test_existing_run_state_failure_precedes_generic_exception(
    tmp_path: Path,
    status: str,
    code: str,
    expected: str,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    state = {
        "schema_version": "taskwise_online_run_state_v1",
        "arm": "online",
        "status": status,
        "standard_chembench4k_score_claimed": False,
        "resume_allowed": False,
        "failure": {"code": code, "completion_observed": True},
        "completed_tasks": 10,
        "completion_count": 30,
        "session_attempt_count": 30,
        "update_count": 20,
        "core_job_count": 20,
        "core_artifact_count": 20,
        "context_resolution_count": 20,
    }
    (output / "run_state.json").write_bytes(online_only._canonical_bytes(state))

    evidence = online_only._failure_evidence(
        output,
        RuntimeError("generic wrapper exception"),
    )

    assert evidence["run_status"] == status
    assert evidence["failure_code"] == expected
    assert evidence["completion_count"] == 30
    assert evidence["update_count"] == 20


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
        result = _completed_stream_result()
        result.update(
            {
                "status": "EXECUTION_FAILED",
                "completed_tasks": 0,
                "completion_count": 0,
                "session_attempt_count": 1,
                "update_count": 0,
                "core_job_count": 0,
                "core_artifact_count": 0,
                "context_resolution_count": 0,
                "memory_aggregate": {"approved_artifact_count": 0},
                "resume_allowed": False,
                "finding_codes": ["EXECUTOR_NONZERO_EXIT"],
            }
        )
        return result

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


def test_failed_result_preserves_trusted_partial_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_online_only_preflight(monkeypatch)

    def failed_stream(*_args: object) -> dict[str, object]:
        result = _completed_stream_result()
        result.update(
            {
                "status": "TASKWISE_EVOLUTION_UPDATE_FAILED",
                "completed_tasks": 34,
                "completion_count": 103,
                "session_attempt_count": 103,
                "update_count": 68,
                "core_job_count": 68,
                "core_artifact_count": 68,
                "context_resolution_count": 68,
                "memory_aggregate": {"approved_artifact_count": 68},
                "finding_codes": ["TASKWISE_ARTIFACT_VALIDATION_FAILED"],
            }
        )
        return result

    with pytest.raises(
        online_only.TaskwiseOnlineOnlyError,
        match="ONLINE_ONLY_SUITE_TERMINAL_FAILURE",
    ):
        online_only.run_online_only_pilot500_v1(
            run_id=_RUN_ID,
            repository_root=tmp_path,
            stream_runner=failed_stream,
        )

    state = json.loads(
        (
            online_only.online_only_run_root_v1(tmp_path, _RUN_ID) / "online_only_suite_state.json"
        ).read_text(encoding="utf-8")
    )
    assert state["completed_tasks"] == 34
    assert state["completion_count"] == state["session_attempt_count"] == 103
    assert state["update_count"] == state["core_job_count"] == 68
    assert state["core_artifact_count"] == state["context_resolution_count"] == 68
    assert state["artifact_validation_failures"] == 1
    assert state["evolution_update_failures"] == 0
    assert state["streams"]["pilot500_stream_00"]["failure_code"] == (
        "TASKWISE_ARTIFACT_VALIDATION_FAILED"
    )


def test_online_only_namespace_is_not_a_paired_comparison_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_online_only_preflight(monkeypatch)
    online_only.run_online_only_pilot500_v1(
        run_id=_RUN_ID,
        repository_root=tmp_path,
        stream_runner=lambda *_args: _completed_stream_result(),
    )

    with pytest.raises(TaskwiseAttemptError, match="PAIRED_ATTEMPT_MISSING"):
        require_taskwise_completed_paired_attempt_v1(
            tmp_path,
            suite_kind="pilot500",
            generation_id=_PAIRED_GENERATION_ID,
        )


def test_report_recovery_keeps_failed_run_immutable_and_writes_disjoint_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = online_only.online_only_run_root_v1(tmp_path, _RUN_ID)
    run_root.mkdir(parents=True)
    state = {
        "schema_version": online_only.ONLINE_ONLY_SCHEMA_V1,
        "run_id": _RUN_ID,
        "status": "FAILED",
        "active_scope": None,
        "source_commit": _SOURCE_COMMIT,
        "stream_count": 10,
        "total_task_count": 500,
        "completed_streams": 10,
        "completed_tasks": 500,
        "completion_count": 1500,
        "session_attempt_count": 1500,
        "update_count": 1000,
        "core_job_count": 1000,
        "core_artifact_count": 1000,
        "context_resolution_count": 1000,
        "security_violations": 0,
        "context_binding_violations": 0,
        "artifact_validation_failures": 0,
        "streams": {
            scope: {
                "completed": True,
                "run_status": "COMPLETED",
                "failure_code": None,
            }
            for scope in PILOT500_STREAM_SCOPES
        },
    }
    state_path = run_root / "online_only_suite_state.json"
    state_path.write_bytes(online_only._canonical_bytes(state))
    before = state_path.read_bytes()
    reporter_commit = "f" * 40
    monkeypatch.setattr(
        online_only,
        "verify_taskwise_source_gate_v1",
        lambda _config: reporter_commit,
    )
    monkeypatch.setattr(online_only, "_verify_static_inputs", lambda *_args: None)
    monkeypatch.setattr(
        online_only,
        "_build_online_only_descriptive_report",
        lambda **_kwargs: {
            "schema_version": online_only.ONLINE_ONLY_REPORT_SCHEMA_V1,
            "labels": list(online_only.ONLINE_ONLY_REPORT_LABELS),
        },
    )

    receipt = online_only.recover_completed_online_only_report_v1(
        run_id=_RUN_ID,
        repository_root=tmp_path,
    )

    assert state_path.read_bytes() == before
    assert receipt["status"] == "PASS"
    assert receipt["completed_tasks"] == 500
    assert receipt["core_artifact_count"] == 1000
    assert receipt["original_run_mutated"] is False
    recovered_root = online_only.online_only_recovered_report_root_v1(
        tmp_path,
        run_id=_RUN_ID,
        reporter_source_commit=reporter_commit,
    )
    report = json.loads(
        (recovered_root / "online_only_descriptive_report.json").read_text(encoding="utf-8")
    )
    assert report["report_recovery"]["execution_evidence_status"] == "COMPLETED"
    assert report["report_recovery"]["original_run_mutated"] is False
    serialized = json.dumps(report, sort_keys=True).casefold()
    assert "target" not in serialized
    assert "private feedback" not in serialized
    assert "memory_payload" not in serialized


def test_report_recovery_rejects_incomplete_failed_suite(
    tmp_path: Path,
) -> None:
    run_root = online_only.online_only_run_root_v1(tmp_path, _RUN_ID)
    run_root.mkdir(parents=True)
    state = {
        "schema_version": online_only.ONLINE_ONLY_SCHEMA_V1,
        "run_id": _RUN_ID,
        "status": "FAILED",
        "active_scope": None,
        "source_commit": _SOURCE_COMMIT,
        "total_task_count": 500,
        "completed_streams": 9,
        "completed_tasks": 450,
        "completion_count": 1350,
        "session_attempt_count": 1350,
        "update_count": 900,
        "core_job_count": 900,
        "core_artifact_count": 900,
        "context_resolution_count": 900,
        "streams": {
            scope: {
                "completed": index < 9,
                "run_status": "COMPLETED" if index < 9 else "MISSING",
                "failure_code": None,
            }
            for index, scope in enumerate(PILOT500_STREAM_SCOPES)
        },
    }
    (run_root / "online_only_suite_state.json").write_bytes(online_only._canonical_bytes(state))

    with pytest.raises(
        online_only.TaskwiseOnlineOnlyError,
        match="ONLINE_ONLY_RECOVERY_STATE_INVALID",
    ):
        online_only.recover_completed_online_only_report_v1(
            run_id=_RUN_ID,
            repository_root=tmp_path,
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
    assert marker["classification"] == list(online_only.ONLINE_ONLY_REPORT_LABELS)
    assert marker["primary_classification"] == online_only.ONLINE_ONLY_CLASSIFICATION
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


def test_cli_failure_reports_observed_counts_without_false_zero_model_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(online_only, "REPOSITORY_ROOT", tmp_path)
    root = online_only.online_only_run_root_v1(tmp_path, _RUN_ID)
    root.mkdir(parents=True)
    state = {
        "schema_version": online_only.ONLINE_ONLY_SCHEMA_V1,
        "run_id": _RUN_ID,
        "completion_count": 180,
        "session_attempt_count": 180,
        "update_count": 120,
        "core_job_count": 120,
        "core_artifact_count": 120,
        "context_resolution_count": 120,
        "completed_tasks": 60,
    }
    (root / "online_only_suite_state.json").write_bytes(online_only._canonical_bytes(state))
    monkeypatch.setattr(
        online_only,
        "run_online_only_pilot500_v1",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic failure")),
    )

    assert online_only.main(["run-pilot500", "--run-id", _RUN_ID]) == 2

    failure = json.loads(capsys.readouterr().err)
    assert "model_calls" not in failure
    assert failure["observed_completion_count"] == 180
    assert failure["observed_session_attempt_count"] == 180
    assert failure["observed_update_count"] == 120
    assert failure["observed_core_job_count"] == 120
    assert failure["observed_core_artifact_count"] == 120
    assert failure["observed_context_resolution_count"] == 120
