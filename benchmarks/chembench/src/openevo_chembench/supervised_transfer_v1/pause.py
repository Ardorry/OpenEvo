"""Immutable administrative pause evidence for externally stopped runs."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openevo_chembench.supervised_transfer_v1.common import (
    canonical_pretty_json_bytes,
    sha256_bytes,
)

PAUSE_RECEIPT_SCHEMA = "SupervisedTransferAdministrativePauseReceiptV1"
PAUSE_RECEIPT_FILENAME = "administrative_pause_receipt_v1.json"
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z", re.ASCII)
_TERMINAL = frozenset({"COMPLETED", "FAIL_CLOSED", "PREFLIGHT_COMPLETED"})


class AdministrativePauseError(RuntimeError):
    """Closed pause-evidence failure."""


def record_administrative_pause_v1(
    *,
    repository_root: Path,
    result_root_relative: str,
    state_root_relative: str,
    run_id: str,
) -> tuple[dict[str, object], str]:
    """Record zero-active-process evidence without rewriting run state."""

    if type(run_id) is not str or _RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError("run_id is invalid")
    repository = repository_root.resolve(strict=True)
    result_root = repository / result_root_relative / "runs" / run_id
    state_root = repository / state_root_relative / "runs" / run_id
    state_path = result_root / "public/run_state.json"
    public_events = result_root / "public/events.jsonl"
    private_events = state_root / "private/events.jsonl"
    receipt_path = result_root / "public" / PAUSE_RECEIPT_FILENAME
    if receipt_path.exists():
        raise AdministrativePauseError("PAUSE_RECEIPT_ALREADY_EXISTS")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        public_raw = public_events.read_bytes()
        private_raw = private_events.read_bytes()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AdministrativePauseError("PAUSE_RUN_EVIDENCE_UNAVAILABLE") from exc
    if not isinstance(state, dict) or state.get("run_id") != run_id:
        raise AdministrativePauseError("PAUSE_RUN_IDENTITY_INVALID")
    if state.get("status") in _TERMINAL:
        raise AdministrativePauseError("PAUSE_RUN_ALREADY_TERMINAL")
    process_snapshot = _relevant_process_snapshot(
        repository_root=repository,
        run_id=run_id,
    )
    if process_snapshot:
        raise AdministrativePauseError("PAUSE_RELEVANT_PROCESS_STILL_ACTIVE")
    receipt = {
        "schema_version": PAUSE_RECEIPT_SCHEMA,
        "protocol_id": "chembench_supervised_transfer_v1",
        "run_id": run_id,
        "administrative_status": "PAUSED_NONRESUMABLE",
        "reason": "USER_ORDERED_PROTOCOL_AUDIT",
        "last_persisted_status": state.get("status"),
        "last_persisted_stage": state.get("stage"),
        "source_commit": state.get("source_commit"),
        "split_receipt_sha256": state.get("split_receipt_sha256"),
        "task_sessions": state.get("task_sessions"),
        "reflector_completions": state.get("reflector_completions"),
        "core_jobs": state.get("core_jobs"),
        "core_artifacts": state.get("core_artifacts"),
        "run_state_sha256": sha256_bytes(state_path.read_bytes()),
        "public_events_sha256": sha256_bytes(public_raw),
        "private_events_sha256": sha256_bytes(private_raw),
        "public_event_count": len(public_raw.splitlines()),
        "private_event_count": len(private_raw.splitlines()),
        "active_relevant_process_count": 0,
        "active_model_calls": 0,
        "resume_allowed": False,
        "replacement_run_requires_new_run_id": True,
        "recorded_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    encoded = canonical_pretty_json_bytes(receipt)
    _write_exclusive_public(receipt_path, encoded)
    return receipt, sha256_bytes(encoded)


def _relevant_process_snapshot(
    *,
    repository_root: Path,
    run_id: str,
) -> list[dict[str, Any]]:
    """Return only processes owned by this repository/run process tree.

    Other independent ChemBench repositories may legitimately run Codex at the
    same time. A bare ``codex exec`` marker is therefore not sufficient to
    block an administrative receipt in this repository. Anchor the scan to an
    exact repository path or run ID, then include all descendants of those
    anchors so detached Codex/reflector process groups remain visible.
    """

    repository = repository_root.resolve(strict=True)
    completed = subprocess.run(
        ("ps", "-eo", "pid=,ppid=,pgid=,sid=,stat=,cmd="),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    if completed.returncode != 0:
        raise AdministrativePauseError("PAUSE_PROCESS_AUDIT_FAILED")
    rows: list[tuple[int, int, int, int, str, str]] = []
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=5)
        if len(fields) != 6:
            continue
        pid_text, ppid_text, pgid_text, sid_text, status, command = fields
        rows.append(
            (
                int(pid_text),
                int(ppid_text),
                int(pgid_text),
                int(sid_text),
                status,
                command,
            )
        )
    parent_by_pid = {pid: ppid for pid, ppid, *_rest in rows}
    excluded = {os.getpid()}
    cursor = os.getppid()
    while cursor > 0 and cursor not in excluded:
        excluded.add(cursor)
        cursor = parent_by_pid.get(cursor, 0)
    anchors = {
        pid
        for pid, _ppid, _pgid, _sid, _status, command in rows
        if pid not in excluded
        and _is_relevant_command(command)
        and (os.fspath(repository) in command or run_id in command)
    }
    relevant_pids = set(anchors)
    changed = True
    while changed:
        changed = False
        for pid, ppid, *_rest in rows:
            if pid not in excluded and ppid in relevant_pids and pid not in relevant_pids:
                relevant_pids.add(pid)
                changed = True

    relevant: list[dict[str, Any]] = []
    for pid, ppid, pgid, sid, status, command in rows:
        if pid not in relevant_pids:
            continue
        relevant.append(
            {
                "pid": pid,
                "ppid": ppid,
                "pgid": pgid,
                "sid": sid,
                "status": status,
                "command_sha256": sha256_bytes(command.encode("utf-8")),
            }
        )
    return relevant


def _is_relevant_command(command: str) -> bool:
    lowered = command.lower()
    return any(
        marker in lowered
        for marker in (
            "codex exec",
            "reflector_execution_boundary",
            "run-preflight",
            "run-formal",
            "supervised_transfer_monitor",
        )
    )


def _write_exclusive_public(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o644,
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise AdministrativePauseError("PAUSE_RECEIPT_FILE_UNSAFE")
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


__all__ = [
    "PAUSE_RECEIPT_FILENAME",
    "PAUSE_RECEIPT_SCHEMA",
    "AdministrativePauseError",
    "record_administrative_pause_v1",
]
