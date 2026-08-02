"""Read-only 600-second monitoring for Temperature full-evolve v1.

The monitor never submits work, changes experiment state, reads credentials,
or repairs the database.  Its only write is an owner-private append-only status
snapshot log.  A two-poll stagnation finding is diagnostic evidence for the
separate controller/supervisor recovery path.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openevo_chembench.supervised_transfer_v1.common import canonical_json_bytes
from openevo_chembench.supervised_transfer_v2.managed_codex import (
    codex_subscription_auth_source_v2,
)
from openevo_chembench.temperature_full_evolve_v1.candidate import (
    CANDIDATE_TIMEOUT_SECONDS,
)
from openevo_chembench.temperature_full_evolve_v1.reflector import (
    REFLECTOR_TIMEOUT_SECONDS,
)
from openevo_chembench.temperature_full_evolve_v1.runtime_services import (
    TemperatureRuntimeServicesError,
    temperature_runtime_services_status_v1,
)

CONTROLLER_STATUS_SCHEMA = "TemperatureFullEvolveControllerStatusV1"
MONITOR_SNAPSHOT_SCHEMA = "TemperatureFullEvolveMonitorSnapshotV1"
MONITOR_INTERVAL_SECONDS = 600
STAGNANT_POLL_LIMIT = 2
MAX_CONTROLLER_STATUS_BYTES = 128 * 1024
MAX_SNAPSHOT_BYTES = 256 * 1024
MAX_SNAPSHOT_LOG_BYTES = 64 * 1024 * 1024
MAX_DATABASE_TABLES = 64
MAX_MEMINFO_BYTES = 64 * 1024
MIN_MEMORY_ERROR_BYTES = 1 * 1024**3
MIN_MEMORY_WARNING_BYTES = 2 * 1024**3
FORMAL_RUNS_RELATIVE = Path("state/chembench_temperature_full_evolve_v1/runs")
PROC_MEMINFO_PATH = Path("/proc/meminfo")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)
_SAFE_TABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z", re.ASCII)
_UTC = re.compile(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_PHASES = {
    "INITIALIZATION",
    "PREFLIGHT",
    "TRAIN_PRE",
    "TRAIN_REFLECTOR",
    "EVIDENCE",
    "CORE_EVOLUTION",
    "CORE_COMMIT",
    "TRAIN_POST",
    "FREEZE",
    "EVOLVED_TEST",
    "BASELINE_TEST",
    "AUDIT",
    "RECOVERY",
    "COMPLETE",
    "HARD_BLOCKED",
    "FAILED",
}
_TERMINAL_PHASES = {"COMPLETE", "HARD_BLOCKED", "FAILED"}


class TemperatureMonitorError(RuntimeError):
    """Closed monitoring input, evidence, or snapshot-log failure."""


class ControllerStatusV1(BaseModel):
    """Exact aggregate controller status contract shared with the runner."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["TemperatureFullEvolveControllerStatusV1"]
    run_id: str
    revision: int = Field(ge=0)
    phase: str
    batch_index: int | None = Field(default=None, ge=1, le=4)
    item_ordinal: int | None = Field(default=None, ge=1, le=100)
    accepted_completions: int = Field(ge=0, le=404)
    candidate_calls: int = Field(ge=0, le=400)
    reflector_calls: int = Field(ge=0, le=4)
    core_jobs: int = Field(ge=0, le=12)
    active_lease: bool
    staged_side_effects: int = Field(ge=0)
    failed_side_effects: int = Field(ge=0)
    last_progress_at_utc: str
    progress_counter: int = Field(ge=0)
    runner_pid: int = Field(ge=1)
    updated_at_utc: str

    @model_validator(mode="after")
    def _closed(self) -> ControllerStatusV1:
        if (
            _SAFE_ID.fullmatch(self.run_id) is None
            or self.phase not in _PHASES
            or _UTC.fullmatch(self.last_progress_at_utc) is None
            or _UTC.fullmatch(self.updated_at_utc) is None
            or self.accepted_completions > self.candidate_calls + self.reflector_calls
        ):
            raise ValueError("controller aggregate status is invalid")
        if self.phase in _TERMINAL_PHASES and self.active_lease:
            raise ValueError("terminal controller status cannot retain an active lease")
        return self


def load_controller_status_v1(path: Path) -> ControllerStatusV1:
    """Read one owner-private atomically published controller status."""

    encoded = _read_owner_private_file(path, maximum=MAX_CONTROLLER_STATUS_BYTES)
    payload = _decode_unique_json(encoded, finding="CONTROLLER_STATUS_JSON_INVALID")
    try:
        return ControllerStatusV1.model_validate(payload)
    except Exception as exc:
        raise TemperatureMonitorError("CONTROLLER_STATUS_SCHEMA_INVALID") from exc


def collect_monitor_snapshot_v1(
    *,
    repository_root: Path,
    controller_status_path: Path,
    database_path: Path,
    tmux_session: str,
    previous_snapshot: dict[str, Any] | None = None,
    expected_runner_start_time_ticks: int | None = None,
    now_utc: str | None = None,
) -> dict[str, object]:
    """Collect one content-free, read-only system and experiment status snapshot."""

    repository = _absolute_directory(repository_root)
    if (
        not isinstance(controller_status_path, Path)
        or not controller_status_path.is_absolute()
        or not isinstance(database_path, Path)
        or not database_path.is_absolute()
        or type(tmux_session) is not str
        or _SAFE_ID.fullmatch(tmux_session) is None
    ):
        raise TypeError("monitor paths must be absolute and tmux session must be safe")
    controller = load_controller_status_v1(controller_status_path)
    run_root = _bind_formal_run_paths(
        repository=repository,
        controller=controller,
        controller_status_path=controller_status_path,
        database_path=database_path,
    )
    timestamp = now_utc or _utc_now()
    if _UTC.fullmatch(timestamp) is None:
        raise ValueError("monitor timestamp is invalid")
    if (
        expected_runner_start_time_ticks is not None
        and (
            type(expected_runner_start_time_ticks) is not int
            or expected_runner_start_time_ticks < 1
        )
    ):
        raise TypeError("expected runner start time ticks must be a positive integer")
    prior_process = _previous_process_binding(
        previous_snapshot,
        run_id=controller.run_id,
    )
    if (
        prior_process is not None
        and expected_runner_start_time_ticks is not None
        and prior_process[1] != expected_runner_start_time_ticks
    ):
        raise TemperatureMonitorError("MONITOR_RUNNER_START_TIME_BINDING_DRIFT")
    bound_process = prior_process
    if bound_process is None and expected_runner_start_time_ticks is not None:
        bound_process = (controller.runner_pid, expected_runner_start_time_ticks)

    runtime = _probe_runtime(repository)
    database = _probe_database(database_path)
    process = _probe_process(controller.runner_pid, bound_process=bound_process)
    tmux = _probe_tmux(tmux_session)
    disk = _probe_disk(run_root)
    memory = _probe_memory()
    auth = _probe_auth_metadata()
    progress_fingerprint = _progress_fingerprint(controller, database)
    previous_progress = _previous_progress(previous_snapshot)
    unchanged_count = 0
    if previous_progress is not None and previous_progress[0] == progress_fingerprint:
        unchanged_count = previous_progress[1] + 1
    terminal = controller.phase in _TERMINAL_PHASES
    lease = _active_lease_disposition(controller=controller, snapshot_at_utc=timestamp)
    stall_candidate = not terminal and unchanged_count >= STAGNANT_POLL_LIMIT
    health = _health_summary(
        controller=controller,
        runtime=runtime,
        database=database,
        process=process,
        tmux=tmux,
        disk=disk,
        memory=memory,
        auth=auth,
        lease=lease,
    )
    healthy_active_lease = (
        lease["disposition"] == "WAIT_ACTIVE_LEASE"
        and health["status"] != "ERROR"
    )
    stall = stall_candidate and not healthy_active_lease
    diagnostics = _stall_diagnostics(
        stall=stall,
        controller=controller,
        runtime=runtime,
        database=database,
        process=process,
        tmux=tmux,
        memory=memory,
        auth=auth,
        lease=lease,
        healthy_active_lease=healthy_active_lease,
    )
    if terminal:
        recovery_disposition = "TERMINAL_NO_RECOVERY"
    elif health["status"] == "ERROR":
        recovery_disposition = "DIAGNOSE_HEALTH_FAILURE"
    elif healthy_active_lease:
        recovery_disposition = "WAIT_ACTIVE_LEASE"
    elif stall:
        recovery_disposition = "DIAGNOSE_STALL"
    else:
        recovery_disposition = "NO_ACTION"
    snapshot: dict[str, object] = {
        "schema_version": MONITOR_SNAPSHOT_SCHEMA,
        "snapshot_at_utc": timestamp,
        "monitor_interval_seconds": MONITOR_INTERVAL_SECONDS,
        "model_calls_by_monitor": 0,
        "controller": controller.model_dump(mode="json"),
        "controller_status_sha256": hashlib.sha256(
            canonical_json_bytes(controller.model_dump(mode="json"))
        ).hexdigest(),
        "run_binding": {
            "status": "PASS",
            "run_id": controller.run_id,
            "controller_status_relative": "status/current.json",
            "database_relative": "core/evolution.sqlite3",
        },
        "runtime": runtime,
        "database": database,
        "runner_process": process,
        "tmux": tmux,
        "disk": disk,
        "host_memory": memory,
        "credential_metadata": auth,
        "health": health,
        "progress": {
            "fingerprint_sha256": progress_fingerprint,
            "consecutive_no_progress_polls": unchanged_count,
            "stall_detected": stall,
            "diagnostics": diagnostics,
            "active_lease": lease,
            "automatic_recovery_suppressed": healthy_active_lease,
            "recovery_disposition": recovery_disposition,
        },
        "terminal": terminal,
    }
    encoded = canonical_json_bytes(snapshot)
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise TemperatureMonitorError("MONITOR_SNAPSHOT_SIZE_EXCEEDED")
    return snapshot


def poll_and_append_monitor_snapshot_v1(
    *,
    repository_root: Path,
    controller_status_path: Path,
    database_path: Path,
    snapshot_log_path: Path,
    tmux_session: str,
    expected_runner_start_time_ticks: int | None = None,
    now_utc: str | None = None,
) -> dict[str, object]:
    """Serialize monitors, collect one poll, fsync one owner-private JSONL record."""

    if not isinstance(snapshot_log_path, Path) or not snapshot_log_path.is_absolute():
        raise TypeError("snapshot log path must be absolute")
    snapshot_log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    snapshot_log_path.parent.chmod(0o700)
    lock_path = snapshot_log_path.with_name(f".{snapshot_log_path.name}.lock")
    lock_fd = os.open(
        lock_path,
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        _require_owner_private_descriptor(lock_fd, maximum=None)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        previous = _load_last_snapshot(snapshot_log_path)
        snapshot = collect_monitor_snapshot_v1(
            repository_root=repository_root,
            controller_status_path=controller_status_path,
            database_path=database_path,
            tmux_session=tmux_session,
            previous_snapshot=previous,
            expected_runner_start_time_ticks=expected_runner_start_time_ticks,
            now_utc=now_utc,
        )
        encoded = canonical_json_bytes(snapshot)
        _append_owner_private(snapshot_log_path, encoded)
        return snapshot
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def run_monitor_loop_v1(
    *,
    repository_root: Path,
    controller_status_path: Path,
    database_path: Path,
    snapshot_log_path: Path,
    tmux_session: str,
    expected_runner_start_time_ticks: int | None = None,
    emit: Callable[[str], None] = print,
) -> int:
    """Poll every frozen 600 seconds until the controller reaches a terminal phase."""

    while True:
        snapshot = poll_and_append_monitor_snapshot_v1(
            repository_root=repository_root,
            controller_status_path=controller_status_path,
            database_path=database_path,
            snapshot_log_path=snapshot_log_path,
            tmux_session=tmux_session,
            expected_runner_start_time_ticks=expected_runner_start_time_ticks,
        )
        emit(format_monitor_line_v1(snapshot))
        if snapshot["terminal"] is True:
            return 0
        time.sleep(MONITOR_INTERVAL_SECONDS)


def format_monitor_line_v1(snapshot: dict[str, object]) -> str:
    """Render one compact line without benchmark or credential content."""

    try:
        controller = snapshot["controller"]
        progress = snapshot["progress"]
        runtime = snapshot["runtime"]
        database = snapshot["database"]
        memory = snapshot["host_memory"]
        health = snapshot["health"]
        if not all(
            isinstance(value, dict)
            for value in (controller, progress, runtime, database, memory, health)
        ):
            raise TypeError
        return (
            f"phase={controller['phase']} batch={controller['batch_index']} "
            f"item={controller['item_ordinal']} accepted={controller['accepted_completions']} "
            f"candidate={controller['candidate_calls']} reflector={controller['reflector_calls']} "
            f"core={controller['core_jobs']} runtime={runtime['status']} "
            f"db={database['status']} memory={memory['status']} health={health['status']} "
            f"action={progress['recovery_disposition']} "
            f"stalled={str(progress['stall_detected']).lower()}"
        )
    except (KeyError, TypeError) as exc:
        raise TemperatureMonitorError("MONITOR_SNAPSHOT_SCHEMA_INVALID") from exc


def _probe_runtime(repository: Path) -> dict[str, object]:
    try:
        status_payload = temperature_runtime_services_status_v1(repository_root=repository)
    except TemperatureRuntimeServicesError as exc:
        return {"status": "ERROR", "finding_code": exc.finding_code}
    except (OSError, RuntimeError, TypeError, ValueError):
        return {"status": "ERROR", "finding_code": "RUNTIME_STATUS_PROBE_FAILED"}
    allowed = (
        "service_run_id",
        "source_commit",
        "runtime_services_identity_sha256",
        "rollout_health_sha256",
        "gateway_health_sha256",
        "completion_root_identity_sha256",
    )
    if status_payload.get("status") != "PASS" or any(key not in status_payload for key in allowed):
        return {"status": "ERROR", "finding_code": "RUNTIME_STATUS_PAYLOAD_INVALID"}
    return {"status": "PASS", **{key: status_payload[key] for key in allowed}}


def _probe_database(path: Path) -> dict[str, object]:
    if not path.exists():
        return {
            "status": "MISSING",
            "integrity": "NOT_CHECKED",
            "size_bytes": 0,
            "table_row_counts": {},
        }
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise TemperatureMonitorError("MONITOR_DATABASE_NOT_REGULAR")
        uri = f"file:{quote(os.fspath(path), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        try:
            connection.execute("PRAGMA query_only=ON")
            integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
            integrity = "ok" if integrity_rows == [("ok",)] else "failed"
            rows = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            if len(rows) > MAX_DATABASE_TABLES or any(
                type(row[0]) is not str or _SAFE_TABLE.fullmatch(row[0]) is None for row in rows
            ):
                raise TemperatureMonitorError("MONITOR_DATABASE_SCHEMA_INVALID")
            counts: dict[str, int] = {}
            for (name,) in rows:
                quoted_name = name.replace('"', '""')
                value = connection.execute(f'SELECT COUNT(*) FROM "{quoted_name}"').fetchone()
                if value is None or type(value[0]) is not int or value[0] < 0:
                    raise TemperatureMonitorError("MONITOR_DATABASE_COUNT_INVALID")
                counts[name] = value[0]
        finally:
            connection.close()
        return {
            "status": "PASS" if integrity == "ok" else "ERROR",
            "integrity": integrity,
            "size_bytes": metadata.st_size,
            "table_row_counts": counts,
            "table_count": len(counts),
        }
    except TemperatureMonitorError as exc:
        return {
            "status": "ERROR",
            "integrity": "NOT_PROVEN",
            "finding_code": str(exc),
            "size_bytes": 0,
            "table_row_counts": {},
        }
    except (OSError, sqlite3.Error):
        return {
            "status": "ERROR",
            "integrity": "NOT_PROVEN",
            "finding_code": "MONITOR_DATABASE_PROBE_FAILED",
            "size_bytes": 0,
            "table_row_counts": {},
        }


def _probe_process(
    pid: int,
    *,
    bound_process: tuple[int, int] | None,
) -> dict[str, object]:
    try:
        process_state, start_time_ticks = _read_process_identity(pid)
        if process_state in {"X", "x", "Z"}:
            return {
                "status": "ERROR",
                "pid": pid,
                "alive": False,
                "process_state": process_state,
                "start_time_ticks": start_time_ticks,
                "binding_status": "ERROR",
                "bound_pid": pid,
                "bound_start_time_ticks": start_time_ticks,
                "finding_code": "RUNNER_PROCESS_ZOMBIE",
            }
        if bound_process is None:
            return {
                "status": "PASS",
                "pid": pid,
                "alive": True,
                "process_state": process_state,
                "start_time_ticks": start_time_ticks,
                "binding_status": "ESTABLISHED",
                "bound_pid": pid,
                "bound_start_time_ticks": start_time_ticks,
            }
        bound_pid, bound_start_time_ticks = bound_process
        if bound_pid != pid:
            return {
                "status": "ERROR",
                "pid": pid,
                "alive": True,
                "process_state": process_state,
                "start_time_ticks": start_time_ticks,
                "binding_status": "ERROR",
                "bound_pid": bound_pid,
                "bound_start_time_ticks": bound_start_time_ticks,
                "finding_code": "RUNNER_PID_CHANGED",
            }
        if bound_start_time_ticks != start_time_ticks:
            return {
                "status": "ERROR",
                "pid": pid,
                "alive": True,
                "process_state": process_state,
                "start_time_ticks": start_time_ticks,
                "binding_status": "ERROR",
                "bound_pid": bound_pid,
                "bound_start_time_ticks": bound_start_time_ticks,
                "finding_code": "RUNNER_PID_REUSED",
            }
        return {
            "status": "PASS",
            "pid": pid,
            "alive": True,
            "process_state": process_state,
            "start_time_ticks": start_time_ticks,
            "binding_status": "PASS",
            "bound_pid": bound_pid,
            "bound_start_time_ticks": bound_start_time_ticks,
        }
    except (OSError, UnicodeError, ValueError):
        bound_pid = pid if bound_process is None else bound_process[0]
        bound_start_time_ticks = None if bound_process is None else bound_process[1]
        return {
            "status": "ERROR",
            "pid": pid,
            "alive": False,
            "binding_status": "ERROR",
            "bound_pid": bound_pid,
            "bound_start_time_ticks": bound_start_time_ticks,
            "finding_code": "RUNNER_PROCESS_NOT_ALIVE",
        }


def _read_process_identity(pid: int) -> tuple[str, int]:
    encoded = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
    command_end = encoded.rfind(")")
    if command_end < 2:
        raise ValueError("process stat command is malformed")
    suffix = encoded[command_end + 1 :].split()
    if len(suffix) < 20 or len(suffix[0]) != 1:
        raise ValueError("process stat fields are incomplete")
    start_time_ticks = int(suffix[19])
    if start_time_ticks < 1:
        raise ValueError("process start time is invalid")
    return suffix[0], start_time_ticks


def _probe_tmux(session: str) -> dict[str, object]:
    try:
        process = subprocess.run(
            ("tmux", "list-windows", "-t", session, "-F", "#{window_name}"),
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return {
            "status": "ERROR",
            "session": session,
            "alive": False,
            "windows": [],
            "required_windows_alive": False,
            "finding_code": "TMUX_STATUS_PROBE_FAILED",
        }
    windows = tuple(line.strip() for line in process.stdout.splitlines() if line.strip())
    if process.returncode or any(_SAFE_ID.fullmatch(value) is None for value in windows):
        return {
            "status": "ERROR",
            "session": session,
            "alive": False,
            "windows": [],
            "required_windows_alive": False,
            "finding_code": "TMUX_SESSION_NOT_ALIVE",
        }
    required = {"runner", "watch", "status"}
    required_alive = required <= set(windows)
    return {
        "status": "PASS" if required_alive else "ERROR",
        "session": session,
        "alive": True,
        "windows": list(windows),
        "required_windows_alive": required_alive,
        "finding_code": None if required_alive else "TMUX_REQUIRED_WINDOW_MISSING",
    }


def _probe_disk(repository: Path) -> dict[str, object]:
    try:
        usage = shutil.disk_usage(repository)
        return {
            "status": "PASS" if usage.free >= 5 * 1024**3 else "LOW_SPACE",
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "minimum_free_bytes": 5 * 1024**3,
            "finding_code": None if usage.free >= 5 * 1024**3 else "DISK_SPACE_LOW",
        }
    except OSError:
        return {
            "status": "ERROR",
            "total_bytes": 0,
            "used_bytes": 0,
            "free_bytes": 0,
            "minimum_free_bytes": 5 * 1024**3,
            "finding_code": "DISK_STATUS_PROBE_FAILED",
        }


def _probe_memory(path: Path = PROC_MEMINFO_PATH) -> dict[str, object]:
    """Read bounded aggregate host memory counters from Linux procfs."""

    empty = {
        "mem_total_bytes": 0,
        "mem_available_bytes": 0,
        "swap_total_bytes": 0,
        "swap_free_bytes": 0,
        "warning_below_bytes": MIN_MEMORY_WARNING_BYTES,
        "error_below_bytes": MIN_MEMORY_ERROR_BYTES,
    }
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            encoded = os.read(descriptor, MAX_MEMINFO_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(encoded) > MAX_MEMINFO_BYTES:
            raise ValueError("meminfo exceeds monitor budget")
        text = encoded.decode("ascii")
        expected = {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}
        values: dict[str, int] = {}
        for line in text.splitlines():
            key, separator, raw_value = line.partition(":")
            if not separator or key not in expected:
                continue
            if key in values:
                raise ValueError("duplicate meminfo field")
            match = re.fullmatch(r"([0-9]+) kB", raw_value.strip(), re.ASCII)
            if match is None:
                raise ValueError("invalid meminfo field")
            value_kib = int(match.group(1))
            if value_kib > (2**63 - 1) // 1024:
                raise ValueError("meminfo field exceeds monitor range")
            values[key] = value_kib * 1024
        if set(values) != expected:
            raise ValueError("required meminfo fields are missing")
        if (
            values["MemTotal"] < 1
            or values["MemAvailable"] > values["MemTotal"]
            or values["SwapFree"] > values["SwapTotal"]
        ):
            raise ValueError("meminfo counters are inconsistent")
        available = values["MemAvailable"]
        if available < MIN_MEMORY_ERROR_BYTES:
            status = "ERROR"
            finding_code = "HOST_MEMORY_CRITICAL"
        elif available < MIN_MEMORY_WARNING_BYTES:
            status = "WARN"
            finding_code = "HOST_MEMORY_LOW"
        else:
            status = "PASS"
            finding_code = None
        return {
            "status": status,
            "mem_total_bytes": values["MemTotal"],
            "mem_available_bytes": available,
            "swap_total_bytes": values["SwapTotal"],
            "swap_free_bytes": values["SwapFree"],
            "warning_below_bytes": MIN_MEMORY_WARNING_BYTES,
            "error_below_bytes": MIN_MEMORY_ERROR_BYTES,
            "finding_code": finding_code,
        }
    except (OSError, UnicodeError, ValueError):
        return {
            "status": "ERROR",
            **empty,
            "finding_code": "HOST_MEMORY_PROBE_FAILED",
        }


def _probe_auth_metadata() -> dict[str, object]:
    try:
        auth_path = codex_subscription_auth_source_v2()
        metadata = auth_path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        passed = (
            stat.S_ISREG(metadata.st_mode)
            and not stat.S_ISLNK(metadata.st_mode)
            and metadata.st_uid == os.getuid()
            and metadata.st_nlink == 1
            and mode & 0o077 == 0
        )
        return {
            "status": "PASS" if passed else "ERROR",
            "regular_file": stat.S_ISREG(metadata.st_mode),
            "owner_matches": metadata.st_uid == os.getuid(),
            "link_count_one": metadata.st_nlink == 1,
            "group_other_permissions_zero": mode & 0o077 == 0,
            "size_bytes": metadata.st_size,
            "credential_content_read": False,
        }
    except (OSError, RuntimeError, TypeError, ValueError):
        return {
            "status": "ERROR",
            "regular_file": False,
            "owner_matches": False,
            "link_count_one": False,
            "group_other_permissions_zero": False,
            "size_bytes": 0,
            "credential_content_read": False,
            "finding_code": "CREDENTIAL_METADATA_PROBE_FAILED",
        }


def _bind_formal_run_paths(
    *,
    repository: Path,
    controller: ControllerStatusV1,
    controller_status_path: Path,
    database_path: Path,
) -> Path:
    run_root = repository / FORMAL_RUNS_RELATIVE / controller.run_id
    expected_status = run_root / "status/current.json"
    expected_database = run_root / "core/evolution.sqlite3"
    actual_status = Path(os.path.abspath(controller_status_path))
    actual_database = Path(os.path.abspath(database_path))
    if actual_status != expected_status:
        raise TemperatureMonitorError("MONITOR_CONTROLLER_STATUS_PATH_NOT_RUN_BOUND")
    if actual_database != expected_database:
        raise TemperatureMonitorError("MONITOR_DATABASE_PATH_NOT_RUN_BOUND")
    try:
        run_metadata = run_root.lstat()
        if (
            not stat.S_ISDIR(run_metadata.st_mode)
            or stat.S_ISLNK(run_metadata.st_mode)
            or run_metadata.st_uid != os.getuid()
        ):
            raise TemperatureMonitorError("MONITOR_RUN_ROOT_IDENTITY_INVALID")
    except FileNotFoundError as exc:
        raise TemperatureMonitorError("MONITOR_RUN_ROOT_MISSING") from exc
    return run_root


def _previous_process_binding(
    snapshot: dict[str, Any] | None,
    *,
    run_id: str,
) -> tuple[int, int] | None:
    if snapshot is None:
        return None
    if type(snapshot) is not dict or snapshot.get("schema_version") != MONITOR_SNAPSHOT_SCHEMA:
        raise TemperatureMonitorError("MONITOR_SNAPSHOT_LOG_SCHEMA_INVALID")
    controller = snapshot.get("controller")
    process = snapshot.get("runner_process")
    if type(controller) is not dict or controller.get("run_id") != run_id:
        raise TemperatureMonitorError("MONITOR_SNAPSHOT_RUN_ID_MISMATCH")
    if type(process) is not dict:
        raise TemperatureMonitorError("MONITOR_SNAPSHOT_PROCESS_BINDING_INVALID")
    pid = process.get("bound_pid", process.get("pid"))
    start_time_ticks = process.get(
        "bound_start_time_ticks",
        process.get("start_time_ticks"),
    )
    if start_time_ticks is None and process.get("status") == "ERROR":
        return None
    if (
        type(pid) is not int
        or pid < 1
        or type(start_time_ticks) is not int
        or start_time_ticks < 1
    ):
        raise TemperatureMonitorError("MONITOR_SNAPSHOT_PROCESS_BINDING_INVALID")
    return pid, start_time_ticks


def _active_lease_disposition(
    *,
    controller: ControllerStatusV1,
    snapshot_at_utc: str,
) -> dict[str, object]:
    if not controller.active_lease:
        return {
            "disposition": "NO_ACTIVE_LEASE",
            "age_seconds": None,
            "timeout_seconds": None,
            "within_timeout_window": False,
        }
    timeout_seconds = (
        REFLECTOR_TIMEOUT_SECONDS
        if controller.phase in {"TRAIN_REFLECTOR", "RECOVERY"}
        else CANDIDATE_TIMEOUT_SECONDS
    )
    start = datetime.strptime(controller.last_progress_at_utc, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )
    current = datetime.strptime(snapshot_at_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    age_seconds = int((current - start).total_seconds())
    if age_seconds < 0:
        return {
            "disposition": "WAIT_ACTIVE_LEASE",
            "age_seconds": 0,
            "timeout_seconds": timeout_seconds,
            "within_timeout_window": True,
            "clock_finding_code": "CONTROLLER_PROGRESS_TIME_IN_FUTURE",
        }
    within = age_seconds <= timeout_seconds
    return {
        "disposition": (
            "WAIT_ACTIVE_LEASE" if within else "ACTIVE_LEASE_TIMEOUT_EXCEEDED"
        ),
        "age_seconds": age_seconds,
        "timeout_seconds": timeout_seconds,
        "within_timeout_window": within,
    }


def _health_summary(
    *,
    controller: ControllerStatusV1,
    runtime: dict[str, object],
    database: dict[str, object],
    process: dict[str, object],
    tmux: dict[str, object],
    disk: dict[str, object],
    memory: dict[str, object],
    auth: dict[str, object],
    lease: dict[str, object],
) -> dict[str, object]:
    findings: list[dict[str, str]] = []

    def add(component: str, code: str, *, severity: str = "ERROR") -> None:
        findings.append(
            {
                "component": component,
                "severity": severity,
                "finding_code": code,
            }
        )

    if runtime.get("status") != "PASS":
        add("runtime", str(runtime.get("finding_code") or "RUNTIME_HEALTH_NOT_PASS"))
    if database.get("status") != "PASS" or database.get("integrity") != "ok":
        add(
            "database",
            str(database.get("finding_code") or "DATABASE_INTEGRITY_NOT_PASS"),
        )
    if process.get("status") != "PASS" or process.get("alive") is not True:
        add(
            "runner_process",
            str(process.get("finding_code") or "RUNNER_PROCESS_NOT_ALIVE"),
        )
    if tmux.get("status") != "PASS" or tmux.get("required_windows_alive") is not True:
        add("tmux", str(tmux.get("finding_code") or "TMUX_REQUIRED_WINDOW_MISSING"))
    if disk.get("status") != "PASS":
        add("disk", str(disk.get("finding_code") or "DISK_SPACE_LOW"))
    if memory.get("status") == "WARN":
        add(
            "host_memory",
            str(memory.get("finding_code") or "HOST_MEMORY_LOW"),
            severity="WARN",
        )
    elif memory.get("status") != "PASS":
        add(
            "host_memory",
            str(memory.get("finding_code") or "HOST_MEMORY_PROBE_FAILED"),
        )
    if auth.get("status") != "PASS":
        add(
            "credential_metadata",
            str(auth.get("finding_code") or "CREDENTIAL_METADATA_NOT_PASS"),
        )
    if controller.failed_side_effects:
        add("controller", "FAILED_SIDE_EFFECT_PRESENT")
    clock_finding = lease.get("clock_finding_code")
    if type(clock_finding) is str:
        add("controller", clock_finding)
    if lease.get("disposition") == "WAIT_ACTIVE_LEASE":
        add("controller", "WAIT_ACTIVE_LEASE", severity="INFO")
    elif lease.get("disposition") == "ACTIVE_LEASE_TIMEOUT_EXCEEDED":
        add("controller", "ACTIVE_LEASE_TIMEOUT_EXCEEDED")
    if any(value["severity"] == "ERROR" for value in findings):
        status = "ERROR"
    elif any(value["severity"] == "WARN" for value in findings):
        status = "WARN"
    else:
        status = "WAIT_ACTIVE_LEASE" if findings else "PASS"
    return {"status": status, "findings": findings}


def _progress_fingerprint(
    controller: ControllerStatusV1,
    database: dict[str, object],
) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "run_id": controller.run_id,
                "revision": controller.revision,
                "phase": controller.phase,
                "batch_index": controller.batch_index,
                "item_ordinal": controller.item_ordinal,
                "accepted_completions": controller.accepted_completions,
                "candidate_calls": controller.candidate_calls,
                "reflector_calls": controller.reflector_calls,
                "core_jobs": controller.core_jobs,
                "staged_side_effects": controller.staged_side_effects,
                "failed_side_effects": controller.failed_side_effects,
                "last_progress_at_utc": controller.last_progress_at_utc,
                "database_table_row_counts": database.get("table_row_counts", {}),
            }
        )
    ).hexdigest()


def _previous_progress(snapshot: dict[str, Any] | None) -> tuple[str, int] | None:
    if snapshot is None:
        return None
    if type(snapshot) is not dict or snapshot.get("schema_version") != MONITOR_SNAPSHOT_SCHEMA:
        raise TemperatureMonitorError("MONITOR_SNAPSHOT_LOG_SCHEMA_INVALID")
    progress = snapshot.get("progress")
    legacy_keys = {
        "fingerprint_sha256",
        "consecutive_no_progress_polls",
        "stall_detected",
        "diagnostics",
    }
    current_keys = legacy_keys | {
        "active_lease",
        "automatic_recovery_suppressed",
        "recovery_disposition",
    }
    if type(progress) is not dict or set(progress) not in (legacy_keys, current_keys):
        raise TemperatureMonitorError("MONITOR_SNAPSHOT_LOG_SCHEMA_INVALID")
    fingerprint = progress["fingerprint_sha256"]
    count = progress["consecutive_no_progress_polls"]
    if (
        type(fingerprint) is not str
        or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
        or type(count) is not int
        or count < 0
    ):
        raise TemperatureMonitorError("MONITOR_SNAPSHOT_LOG_SCHEMA_INVALID")
    return fingerprint, count


def _stall_diagnostics(
    *,
    stall: bool,
    controller: ControllerStatusV1,
    runtime: dict[str, object],
    database: dict[str, object],
    process: dict[str, object],
    tmux: dict[str, object],
    memory: dict[str, object],
    auth: dict[str, object],
    lease: dict[str, object],
    healthy_active_lease: bool,
) -> list[str]:
    if healthy_active_lease:
        return ["WAIT_ACTIVE_LEASE"]
    if not stall:
        return []
    findings: list[str] = []
    if process.get("alive") is not True:
        findings.append("RUNNER_PROCESS_NOT_ALIVE")
    if tmux.get("required_windows_alive") is not True:
        findings.append("TMUX_REQUIRED_WINDOW_MISSING")
    if runtime.get("status") != "PASS":
        findings.append("RUNTIME_HEALTH_NOT_PASS")
    if database.get("status") != "PASS" or database.get("integrity") != "ok":
        findings.append("DATABASE_INTEGRITY_NOT_PASS")
    if auth.get("status") != "PASS":
        findings.append("CREDENTIAL_METADATA_NOT_PASS")
    if memory.get("status") != "PASS":
        findings.append(str(memory.get("finding_code") or "HOST_MEMORY_NOT_PASS"))
    if lease.get("disposition") == "ACTIVE_LEASE_TIMEOUT_EXCEEDED":
        findings.append("ACTIVE_LEASE_TIMEOUT_EXCEEDED")
    elif lease.get("disposition") == "WAIT_ACTIVE_LEASE":
        findings.append("ACTIVE_LEASE_HEALTH_NOT_PASS")
    if controller.staged_side_effects:
        findings.append("STAGED_SIDE_EFFECT_WITHOUT_PROGRESS")
    if controller.failed_side_effects:
        findings.append("FAILED_SIDE_EFFECT_PRESENT")
    if not findings:
        findings.append("CONTROLLER_NO_PROGRESS_TWO_POLLS")
    return findings


def _read_owner_private_file(path: Path, *, maximum: int) -> bytes:
    if not isinstance(path, Path) or not path.is_absolute():
        raise TypeError("private monitor input must be an absolute Path")
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size < 2
            or metadata.st_size > maximum
        ):
            raise TemperatureMonitorError("MONITOR_PRIVATE_INPUT_IDENTITY_INVALID")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW)
        try:
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise TemperatureMonitorError("MONITOR_PRIVATE_INPUT_CHANGED")
            chunks: list[bytes] = []
            remaining = maximum + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 1024 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            encoded = b"".join(chunks)
            if len(encoded) != metadata.st_size:
                raise TemperatureMonitorError("MONITOR_PRIVATE_INPUT_CHANGED")
            return encoded
        finally:
            os.close(descriptor)
    except FileNotFoundError as exc:
        raise TemperatureMonitorError("MONITOR_PRIVATE_INPUT_MISSING") from exc


def _decode_unique_json(encoded: bytes, *, finding: str) -> object:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        return json.loads(
            encoded,
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise TemperatureMonitorError(finding) from exc


def _append_owner_private(path: Path, encoded: bytes) -> None:
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise TemperatureMonitorError("MONITOR_SNAPSHOT_SIZE_EXCEEDED")
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_APPEND
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        metadata = _require_owner_private_descriptor(descriptor, maximum=MAX_SNAPSHOT_LOG_BYTES)
        if metadata.st_size + len(encoded) > MAX_SNAPSHOT_LOG_BYTES:
            raise TemperatureMonitorError("MONITOR_SNAPSHOT_LOG_SIZE_EXCEEDED")
        written = os.write(descriptor, encoded)
        if written != len(encoded):
            raise OSError("short status snapshot append")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _load_last_snapshot(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    encoded = _read_owner_private_file(path, maximum=MAX_SNAPSHOT_LOG_BYTES)
    lines = encoded.splitlines()
    if not lines or sum(len(line) + 1 for line in lines) != len(encoded):
        raise TemperatureMonitorError("MONITOR_SNAPSHOT_LOG_TRUNCATED")
    last: dict[str, Any] | None = None
    for line in lines:
        payload = _decode_unique_json(line, finding="MONITOR_SNAPSHOT_LOG_JSON_INVALID")
        if type(payload) is not dict or payload.get("schema_version") != MONITOR_SNAPSHOT_SCHEMA:
            raise TemperatureMonitorError("MONITOR_SNAPSHOT_LOG_SCHEMA_INVALID")
        last = payload
    return last


def _require_owner_private_descriptor(
    descriptor: int,
    *,
    maximum: int | None,
) -> os.stat_result:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or (maximum is not None and metadata.st_size > maximum)
    ):
        raise TemperatureMonitorError("MONITOR_PRIVATE_OUTPUT_IDENTITY_INVALID")
    return metadata


def _absolute_directory(path: Path) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise TypeError("repository root must be an absolute Path")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise TemperatureMonitorError("MONITOR_REPOSITORY_UNAVAILABLE") from exc
    if not resolved.is_dir():
        raise TemperatureMonitorError("MONITOR_REPOSITORY_UNAVAILABLE")
    return resolved


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "CONTROLLER_STATUS_SCHEMA",
    "MAX_CONTROLLER_STATUS_BYTES",
    "MAX_SNAPSHOT_BYTES",
    "MAX_SNAPSHOT_LOG_BYTES",
    "MONITOR_INTERVAL_SECONDS",
    "MONITOR_SNAPSHOT_SCHEMA",
    "STAGNANT_POLL_LIMIT",
    "ControllerStatusV1",
    "TemperatureMonitorError",
    "collect_monitor_snapshot_v1",
    "format_monitor_line_v1",
    "load_controller_status_v1",
    "poll_and_append_monitor_snapshot_v1",
    "run_monitor_loop_v1",
]
