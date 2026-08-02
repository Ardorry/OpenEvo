from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    write_private_file,
)
from openevo_chembench.temperature_full_evolve_v1 import monitoring
from openevo_chembench.temperature_full_evolve_v1.monitoring import (
    TemperatureMonitorError,
    collect_monitor_snapshot_v1,
    format_monitor_line_v1,
    load_controller_status_v1,
    poll_and_append_monitor_snapshot_v1,
)


def _controller(*, revision: int = 1, progress: int = 1) -> dict[str, object]:
    return {
        "schema_version": "TemperatureFullEvolveControllerStatusV1",
        "run_id": "stv3-temperature-full-evolve-v1-20260802T000000Z",
        "revision": revision,
        "phase": "TRAIN_PRE",
        "batch_index": 1,
        "item_ordinal": 3,
        "accepted_completions": progress,
        "candidate_calls": progress,
        "reflector_calls": 0,
        "core_jobs": 0,
        "active_lease": False,
        "staged_side_effects": 0,
        "failed_side_effects": 0,
        "last_progress_at_utc": "2026-08-02T00:00:00Z",
        "progress_counter": progress,
        "runner_pid": os.getpid(),
        "updated_at_utc": "2026-08-02T00:00:01Z",
    }


def _write_status(path: Path, payload: dict[str, object]) -> None:
    write_private_file(path, canonical_json_bytes(payload))


def _database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE calls (id INTEGER PRIMARY KEY)")
        connection.executemany("INSERT INTO calls DEFAULT VALUES", ((), (), ()))
        connection.commit()
    finally:
        connection.close()
    path.chmod(0o600)


def _formal_paths(root: Path) -> tuple[Path, Path, Path]:
    run = (
        root
        / "state/chembench_temperature_full_evolve_v1/runs"
        / "stv3-temperature-full-evolve-v1-20260802T000000Z"
    )
    return (
        run / "status/current.json",
        run / "core/evolution.sqlite3",
        run / "private/monitor/snapshots.jsonl",
    )


@pytest.fixture
def healthy_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        monitoring,
        "_probe_runtime",
        lambda _repository: {
            "status": "PASS",
            "service_run_id": "stv3-temperature-services-20260802T000000Z-1234abcd",
            "source_commit": "1" * 40,
            "runtime_services_identity_sha256": "2" * 64,
            "rollout_health_sha256": "3" * 64,
            "gateway_health_sha256": "4" * 64,
            "completion_root_identity_sha256": "5" * 64,
        },
    )
    monkeypatch.setattr(
        monitoring,
        "_probe_disk",
        lambda _run_root: {
            "status": "PASS",
            "total_bytes": 100,
            "used_bytes": 10,
            "free_bytes": 90,
            "minimum_free_bytes": 50,
            "finding_code": None,
        },
    )
    monkeypatch.setattr(
        monitoring,
        "_probe_tmux",
        lambda session: {
            "status": "PASS",
            "session": session,
            "alive": True,
            "windows": ["runner", "watch", "status"],
            "required_windows_alive": True,
            "finding_code": None,
        },
    )
    monkeypatch.setattr(
        monitoring,
        "_probe_auth_metadata",
        lambda: {
            "status": "PASS",
            "regular_file": True,
            "owner_matches": True,
            "link_count_one": True,
            "group_other_permissions_zero": True,
            "size_bytes": 100,
            "credential_content_read": False,
        },
    )


def test_monitor_appends_private_snapshots_and_detects_two_stagnant_polls(
    tmp_path: Path,
    healthy_probes: None,
) -> None:
    status_path, database_path, snapshot_log = _formal_paths(tmp_path)
    _write_status(status_path, _controller())
    _database(database_path)
    arguments = {
        "repository_root": tmp_path.resolve(),
        "controller_status_path": status_path.resolve(),
        "database_path": database_path.resolve(),
        "snapshot_log_path": snapshot_log.resolve(),
        "tmux_session": "temperature-full-evolve-v1",
    }

    first = poll_and_append_monitor_snapshot_v1(
        **arguments,
        now_utc="2026-08-02T00:00:00Z",
    )
    second = poll_and_append_monitor_snapshot_v1(
        **arguments,
        now_utc="2026-08-02T00:10:00Z",
    )
    third = poll_and_append_monitor_snapshot_v1(
        **arguments,
        now_utc="2026-08-02T00:20:00Z",
    )

    assert first["progress"]["consecutive_no_progress_polls"] == 0
    assert second["progress"]["consecutive_no_progress_polls"] == 1
    assert third["progress"]["consecutive_no_progress_polls"] == 2
    assert third["progress"]["stall_detected"] is True
    assert third["progress"]["diagnostics"] == ["CONTROLLER_NO_PROGRESS_TWO_POLLS"]
    assert third["database"]["integrity"] == "ok"
    assert third["database"]["table_row_counts"] == {"calls": 3}
    assert third["model_calls_by_monitor"] == 0
    assert first["run_binding"] == {
        "status": "PASS",
        "run_id": "stv3-temperature-full-evolve-v1-20260802T000000Z",
        "controller_status_relative": "status/current.json",
        "database_relative": "core/evolution.sqlite3",
    }
    assert first["runner_process"]["binding_status"] == "ESTABLISHED"
    assert second["runner_process"]["binding_status"] == "PASS"
    assert third["health"] == {"status": "PASS", "findings": []}
    assert stat.S_IMODE(snapshot_log.stat().st_mode) == 0o600
    assert len(snapshot_log.read_text(encoding="utf-8").splitlines()) == 3
    assert "phase=TRAIN_PRE" in format_monitor_line_v1(third)
    assert "stalled=true" in format_monitor_line_v1(third)

    _write_status(status_path, _controller(revision=2, progress=2))
    resumed = poll_and_append_monitor_snapshot_v1(
        **arguments,
        now_utc="2026-08-02T00:30:00Z",
    )
    assert resumed["progress"]["consecutive_no_progress_polls"] == 0
    assert resumed["progress"]["stall_detected"] is False


def test_controller_status_rejects_extra_private_fields_and_broad_permissions(
    tmp_path: Path,
) -> None:
    status_path, _database_path, _snapshot_log = _formal_paths(tmp_path)
    payload = _controller()
    payload["uid"] = "private-item"
    _write_status(status_path, payload)
    with pytest.raises(TemperatureMonitorError, match="CONTROLLER_STATUS_SCHEMA_INVALID"):
        load_controller_status_v1(status_path.resolve())

    _write_status(status_path, _controller())
    status_path.chmod(0o644)
    with pytest.raises(TemperatureMonitorError, match="PRIVATE_INPUT_IDENTITY"):
        load_controller_status_v1(status_path.resolve())


def test_terminal_status_cannot_keep_active_lease(tmp_path: Path) -> None:
    status_path, _database_path, _snapshot_log = _formal_paths(tmp_path)
    payload = _controller()
    payload["phase"] = "COMPLETE"
    payload["active_lease"] = True
    _write_status(status_path, payload)
    with pytest.raises(TemperatureMonitorError, match="CONTROLLER_STATUS_SCHEMA_INVALID"):
        load_controller_status_v1(status_path.resolve())


def test_snapshot_log_corruption_is_fail_closed(
    tmp_path: Path,
    healthy_probes: None,
) -> None:
    status_path, database_path, snapshot_log = _formal_paths(tmp_path)
    _write_status(status_path, _controller())
    _database(database_path)
    write_private_file(snapshot_log, b'{"schema_version":"broken"}')
    with pytest.raises(TemperatureMonitorError, match="TRUNCATED"):
        poll_and_append_monitor_snapshot_v1(
            repository_root=tmp_path.resolve(),
            controller_status_path=status_path.resolve(),
            database_path=database_path.resolve(),
            snapshot_log_path=snapshot_log.resolve(),
            tmux_session="temperature-full-evolve-v1",
            now_utc="2026-08-02T00:00:00Z",
        )


def test_snapshot_json_contains_only_aggregate_controller_fields(
    tmp_path: Path,
    healthy_probes: None,
) -> None:
    status_path, database_path, snapshot_log = _formal_paths(tmp_path)
    _write_status(status_path, _controller())
    _database(database_path)
    poll_and_append_monitor_snapshot_v1(
        repository_root=tmp_path.resolve(),
        controller_status_path=status_path.resolve(),
        database_path=database_path.resolve(),
        snapshot_log_path=snapshot_log.resolve(),
        tmux_session="temperature-full-evolve-v1",
        now_utc="2026-08-02T00:00:00Z",
    )
    payload = json.loads(snapshot_log.read_text(encoding="utf-8"))
    assert set(payload["controller"]) == {
        "schema_version",
        "run_id",
        "revision",
        "phase",
        "batch_index",
        "item_ordinal",
        "accepted_completions",
        "candidate_calls",
        "reflector_calls",
        "core_jobs",
        "active_lease",
        "staged_side_effects",
        "failed_side_effects",
        "last_progress_at_utc",
        "progress_counter",
        "runner_pid",
        "updated_at_utc",
    }
    assert payload["credential_metadata"]["credential_content_read"] is False


def test_database_and_status_paths_must_bind_to_the_formal_run_root(
    tmp_path: Path,
    healthy_probes: None,
) -> None:
    status_path, database_path, _snapshot_log = _formal_paths(tmp_path)
    _write_status(status_path, _controller())
    _database(database_path)
    arbitrary = tmp_path / "other/evolution.sqlite3"
    _database(arbitrary)

    with pytest.raises(TemperatureMonitorError, match="DATABASE_PATH_NOT_RUN_BOUND"):
        collect_monitor_snapshot_v1(
            repository_root=tmp_path.resolve(),
            controller_status_path=status_path.resolve(),
            database_path=arbitrary.resolve(),
            tmux_session="temperature-full-evolve-v1",
            now_utc="2026-08-02T00:00:00Z",
        )

    copied_status = tmp_path / "other/current.json"
    _write_status(copied_status, _controller())
    with pytest.raises(TemperatureMonitorError, match="STATUS_PATH_NOT_RUN_BOUND"):
        collect_monitor_snapshot_v1(
            repository_root=tmp_path.resolve(),
            controller_status_path=copied_status.resolve(),
            database_path=database_path.resolve(),
            tmux_session="temperature-full-evolve-v1",
            now_utc="2026-08-02T00:00:00Z",
        )


def test_runner_pid_binding_detects_same_pid_reuse_immediately(
    tmp_path: Path,
    healthy_probes: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_path, database_path, _snapshot_log = _formal_paths(tmp_path)
    _write_status(status_path, _controller())
    _database(database_path)
    first = collect_monitor_snapshot_v1(
        repository_root=tmp_path.resolve(),
        controller_status_path=status_path.resolve(),
        database_path=database_path.resolve(),
        tmux_session="temperature-full-evolve-v1",
        now_utc="2026-08-02T00:00:00Z",
    )
    bound_ticks = first["runner_process"]["bound_start_time_ticks"]
    assert isinstance(bound_ticks, int)
    monkeypatch.setattr(
        monitoring,
        "_read_process_identity",
        lambda _pid: ("S", bound_ticks + 1),
    )

    second = collect_monitor_snapshot_v1(
        repository_root=tmp_path.resolve(),
        controller_status_path=status_path.resolve(),
        database_path=database_path.resolve(),
        tmux_session="temperature-full-evolve-v1",
        previous_snapshot=first,
        now_utc="2026-08-02T00:10:00Z",
    )

    assert second["runner_process"]["status"] == "ERROR"
    assert second["runner_process"]["finding_code"] == "RUNNER_PID_REUSED"
    assert second["runner_process"]["bound_start_time_ticks"] == bound_ticks
    assert second["health"]["status"] == "ERROR"
    assert second["health"]["findings"] == [
        {
            "component": "runner_process",
            "severity": "ERROR",
            "finding_code": "RUNNER_PID_REUSED",
        }
    ]
    assert second["progress"]["recovery_disposition"] == "DIAGNOSE_HEALTH_FAILURE"


def test_active_reflector_lease_waits_through_legal_timeout_window(
    tmp_path: Path,
    healthy_probes: None,
) -> None:
    status_path, database_path, snapshot_log = _formal_paths(tmp_path)
    payload = _controller()
    payload["phase"] = "TRAIN_REFLECTOR"
    payload["active_lease"] = True
    payload["staged_side_effects"] = 1
    _write_status(status_path, payload)
    _database(database_path)
    arguments = {
        "repository_root": tmp_path.resolve(),
        "controller_status_path": status_path.resolve(),
        "database_path": database_path.resolve(),
        "snapshot_log_path": snapshot_log.resolve(),
        "tmux_session": "temperature-full-evolve-v1",
    }
    first = poll_and_append_monitor_snapshot_v1(
        **arguments,
        now_utc="2026-08-02T00:00:00Z",
    )
    poll_and_append_monitor_snapshot_v1(
        **arguments,
        now_utc="2026-08-02T00:10:00Z",
    )
    third = poll_and_append_monitor_snapshot_v1(
        **arguments,
        now_utc="2026-08-02T00:20:00Z",
    )
    fourth = poll_and_append_monitor_snapshot_v1(
        **arguments,
        now_utc="2026-08-02T00:30:00Z",
    )

    assert first["progress"]["active_lease"]["timeout_seconds"] == 1800
    assert third["progress"]["consecutive_no_progress_polls"] == 2
    assert third["progress"]["stall_detected"] is False
    assert third["progress"]["diagnostics"] == ["WAIT_ACTIVE_LEASE"]
    assert third["progress"]["automatic_recovery_suppressed"] is True
    assert third["progress"]["recovery_disposition"] == "WAIT_ACTIVE_LEASE"
    assert third["health"]["status"] == "WAIT_ACTIVE_LEASE"
    assert fourth["progress"]["active_lease"]["age_seconds"] == 1800
    assert fourth["progress"]["stall_detected"] is False

    expired = poll_and_append_monitor_snapshot_v1(
        **arguments,
        now_utc="2026-08-02T00:30:01Z",
    )
    assert expired["progress"]["stall_detected"] is True
    assert expired["progress"]["automatic_recovery_suppressed"] is False
    assert "ACTIVE_LEASE_TIMEOUT_EXCEEDED" in expired["progress"]["diagnostics"]


def test_probe_failures_are_immediate_structured_health_findings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_path, database_path, _snapshot_log = _formal_paths(tmp_path)
    _write_status(status_path, _controller())
    _database(database_path)
    monkeypatch.setattr(
        monitoring,
        "_probe_runtime",
        lambda _repository: {"status": "ERROR", "finding_code": "RUNTIME_DOWN"},
    )
    monkeypatch.setattr(
        monitoring,
        "_probe_database",
        lambda _path: {
            "status": "ERROR",
            "integrity": "NOT_PROVEN",
            "finding_code": "DATABASE_DOWN",
            "table_row_counts": {},
        },
    )
    monkeypatch.setattr(
        monitoring,
        "_probe_process",
        lambda _pid, *, bound_process: {
            "status": "ERROR",
            "alive": False,
            "finding_code": "PROCESS_DOWN",
            "bound_pid": _pid,
            "bound_start_time_ticks": None,
        },
    )
    monkeypatch.setattr(
        monitoring,
        "_probe_tmux",
        lambda _session: {
            "status": "ERROR",
            "required_windows_alive": False,
            "finding_code": "TMUX_DOWN",
        },
    )
    monkeypatch.setattr(
        monitoring,
        "_probe_disk",
        lambda _run_root: {"status": "ERROR", "finding_code": "DISK_DOWN"},
    )
    monkeypatch.setattr(
        monitoring,
        "_probe_auth_metadata",
        lambda: {"status": "ERROR", "finding_code": "AUTH_DOWN"},
    )

    snapshot = collect_monitor_snapshot_v1(
        repository_root=tmp_path.resolve(),
        controller_status_path=status_path.resolve(),
        database_path=database_path.resolve(),
        tmux_session="temperature-full-evolve-v1",
        now_utc="2026-08-02T00:00:00Z",
    )

    assert snapshot["progress"]["stall_detected"] is False
    assert snapshot["progress"]["recovery_disposition"] == "DIAGNOSE_HEALTH_FAILURE"
    assert snapshot["health"]["status"] == "ERROR"
    assert {
        (finding["component"], finding["finding_code"])
        for finding in snapshot["health"]["findings"]
    } == {
        ("runtime", "RUNTIME_DOWN"),
        ("database", "DATABASE_DOWN"),
        ("runner_process", "PROCESS_DOWN"),
        ("tmux", "TMUX_DOWN"),
        ("disk", "DISK_DOWN"),
        ("credential_metadata", "AUTH_DOWN"),
    }


def test_stale_active_lease_does_not_mask_dead_runner_health(
    tmp_path: Path,
    healthy_probes: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status_path, database_path, _snapshot_log = _formal_paths(tmp_path)
    payload = _controller()
    payload["phase"] = "TRAIN_REFLECTOR"
    payload["active_lease"] = True
    payload["staged_side_effects"] = 1
    _write_status(status_path, payload)
    _database(database_path)
    monkeypatch.setattr(
        monitoring,
        "_probe_process",
        lambda pid, *, bound_process: {
            "status": "ERROR",
            "pid": pid,
            "alive": False,
            "binding_status": "ERROR",
            "bound_pid": pid,
            "bound_start_time_ticks": None,
            "finding_code": "RUNNER_PROCESS_NOT_ALIVE",
        },
    )
    arguments = {
        "repository_root": tmp_path.resolve(),
        "controller_status_path": status_path.resolve(),
        "database_path": database_path.resolve(),
        "tmux_session": "temperature-full-evolve-v1",
    }
    first = collect_monitor_snapshot_v1(
        **arguments,
        now_utc="2026-08-02T00:00:00Z",
    )
    second = collect_monitor_snapshot_v1(
        **arguments,
        previous_snapshot=first,
        now_utc="2026-08-02T00:10:00Z",
    )
    third = collect_monitor_snapshot_v1(
        **arguments,
        previous_snapshot=second,
        now_utc="2026-08-02T00:20:00Z",
    )

    assert first["health"]["status"] == "ERROR"
    assert first["progress"]["recovery_disposition"] == "DIAGNOSE_HEALTH_FAILURE"
    assert first["progress"]["automatic_recovery_suppressed"] is False
    assert third["progress"]["consecutive_no_progress_polls"] == 2
    assert third["progress"]["stall_detected"] is True
    assert third["progress"]["recovery_disposition"] == "DIAGNOSE_HEALTH_FAILURE"
    assert third["progress"]["automatic_recovery_suppressed"] is False
    assert third["progress"]["diagnostics"] == [
        "RUNNER_PROCESS_NOT_ALIVE",
        "ACTIVE_LEASE_HEALTH_NOT_PASS",
        "STAGED_SIDE_EFFECT_WITHOUT_PROGRESS",
    ]
