"""Read-only aggregate monitoring and 600-second snapshots for Safe-Evolve V2."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    write_private_file,
)
from openevo_chembench.supervised_transfer_v2.managed_codex import (
    codex_subscription_auth_source_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.ledger import read_ledger_progress_v2
from openevo_chembench.temperature_safe_evolve_v2.runtime_services import (
    load_temperature_safe_runtime_services_v2,
)

_CANDIDATE_POSITION = re.compile(
    r"safe-[0-9a-f]{14}-(?P<phase>phasea|train|validation1|validation2|test)-"
    r"(?P<block>a[0-7]|b[1-4]|v1|v2|test)-i(?P<ordinal>[0-9]{3})-c[0-9a-f]{64}\Z"
)


class SafeMonitoringError(RuntimeError):
    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


def campaign_snapshot_v2(
    *,
    repository_root: Path,
    campaign_run_id: str,
    runner_pid: int | None,
    persist: bool = True,
) -> dict[str, object]:
    root = repository_root / "state/chembench_temperature_safe_evolve_v2/runs" / campaign_run_id
    ledger_rows: list[dict[str, object]] = []
    accepted_candidate = 0
    accepted_reflector = 0
    core_jobs = 0
    open_claims = 0
    failed_calls = 0
    rejected = 0
    latest_progress_ns = 0
    context_bytes = 0
    latest_candidate_position: tuple[str, dict[str, object]] | None = None
    if root.exists():
        for path in sorted(root.glob("phase_a/*/private/events.jsonl")):
            arm = path.parents[1].name
            run_id = f"{campaign_run_id}-ablation-{arm}"
            row = read_ledger_progress_v2(path=path, run_id=run_id)
            ledger_rows.append({"scope": f"phase_a/{arm}", **row})
            counts = row["kind_counts"]
            accepted_candidate += int(counts.get("CALL_ACCEPTED", 0))
            open_claims += int(row["open_claim_count"])
            failed_calls += int(counts.get("CALL_NO_COMPLETION_FAILURE", 0))
            rejected += int(counts.get("CALL_REJECTED_COMPLETION", 0))
            latest_progress_ns = max(latest_progress_ns, path.stat().st_mtime_ns)
            latest_candidate_position = _newer_candidate_position(
                latest_candidate_position, _read_events(path)
            )
        for path in sorted(root.glob("folds/*/private/events.jsonl")):
            fold = path.parents[1].name
            run_id = f"{campaign_run_id}-{fold}"
            row = read_ledger_progress_v2(path=path, run_id=run_id)
            ledger_rows.append({"scope": f"folds/{fold}", **row})
            counts = row["kind_counts"]
            accepted_reflector += int(counts.get("REFLECTOR_ACCEPTED", 0))
            accepted_candidate += int(counts.get("CALL_ACCEPTED", 0)) - int(
                counts.get("REFLECTOR_ACCEPTED", 0)
            )
            core_jobs += int(counts.get("CORE_JOB_COMPLETED", 0))
            open_claims += int(row["open_claim_count"])
            failed_calls += int(counts.get("CALL_NO_COMPLETION_FAILURE", 0))
            rejected += int(counts.get("CALL_REJECTED_COMPLETION", 0))
            events = _read_events(path)
            latest_candidate_position = _newer_candidate_position(
                latest_candidate_position, events
            )
            context_bytes = max(
                context_bytes,
                max(
                    (
                        int(event["payload"].get("core_payload_utf8_bytes", 0))
                        for event in events
                        if event["kind"] == "PROVISIONAL_STATE_FROZEN"
                    ),
                    default=0,
                ),
            )
            latest_progress_ns = max(latest_progress_ns, path.stat().st_mtime_ns)
    status = _latest_status(root)
    db = _database_health(root)
    runtime = _runtime_health(repository_root, campaign_root=root)
    codex_subscription_auth_source_v2()
    usage = shutil.disk_usage(repository_root)
    terminal = root / "private/campaign_terminal_outcome.json"
    runner_alive = _pid_alive(runner_pid)
    phase = str(status.get("phase", "WAITING_FOR_RUN_ROOT" if not root.exists() else "UNKNOWN"))
    payload = {
        "schema_version": "TemperatureSafeMonitorSnapshotV2",
        "campaign_run_id": campaign_run_id,
        "recorded_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "campaign_phase": phase,
        "phase_a_arm": status.get("phase_a_arm"),
        "fold_id": status.get("fold_id"),
        "block": status.get("block"),
        "accepted_candidate_count": accepted_candidate,
        "accepted_reflector_count": accepted_reflector,
        "core_job_count": core_jobs,
        "open_claim_count": open_claims,
        "failed_call_count": failed_calls,
        "rejected_completion_count": rejected,
        "active_state_sha256": status.get("active_state_sha256"),
        "provisional_state_sha256": status.get("provisional_state_sha256"),
        "promotions": status.get("promotion_count", 0),
        "retired_entries": status.get("retirement_count", 0),
        "active_leases": db["active_leases"],
        "staged_side_effects": db["staged_side_effects"],
        "failed_side_effects": db["failed_side_effects"],
        "database_integrity": db["integrity"],
        "database_count": db["database_count"],
        "latest_progress_unix_ns": latest_progress_ns,
        "latest_progress_at_utc": (
            None
            if latest_progress_ns == 0
            else datetime.fromtimestamp(latest_progress_ns / 1_000_000_000, UTC)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        "current_question_number": (
            None if latest_candidate_position is None else latest_candidate_position[1]["number"]
        ),
        "current_call_phase": (
            None if latest_candidate_position is None else latest_candidate_position[1]["phase"]
        ),
        "current_call_block": (
            None if latest_candidate_position is None else latest_candidate_position[1]["block"]
        ),
        "runner_pid": runner_pid,
        "runner_alive": runner_alive,
        "tmux_windows": _tmux_windows(),
        "disk_free_bytes": usage.free,
        "credential_metadata": "PASS_CONTENT_NOT_READ",
        "runtime_health": runtime,
        "context_bytes": context_bytes,
        "candidate_budget": 2800 if (root / "folds/r1").exists() else 1300,
        "reflector_budget": 16 if (root / "folds/r1").exists() else 4,
        "core_budget": 16 if (root / "folds/r1").exists() else 4,
        "terminal_outcome_present": terminal.is_file(),
        "ledger_progress": ledger_rows,
    }
    if root.exists() and persist:
        monitor = root / "monitor"
        monitor.mkdir(mode=0o700, exist_ok=True)
        monitor.chmod(0o700)
        token = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        write_private_file(
            monitor / f"status-{token}.json", canonical_json_bytes(payload), replace=False
        )
        write_private_file(monitor / "current.json", canonical_json_bytes(payload), replace=True)
    return payload


def compact_status_line_v2(snapshot: dict[str, object]) -> str:
    return (
        f"phase={snapshot['campaign_phase']} arm={snapshot.get('phase_a_arm') or '-'} "
        f"fold={snapshot.get('fold_id') or '-'} block={snapshot.get('block') or '-'} "
        f"question={snapshot.get('current_question_number') or '-'} "
        f"candidate={snapshot['accepted_candidate_count']} reflector={snapshot['accepted_reflector_count']} "
        f"core={snapshot['core_job_count']} open={snapshot['open_claim_count']} "
        f"runner={'alive' if snapshot['runner_alive'] else 'stopped'} "
        f"terminal={snapshot['terminal_outcome_present']}"
    )


def _latest_status(root: Path) -> dict[str, Any]:
    paths = (
        [] if not root.exists() else [path for path in root.rglob("status.json") if path.is_file()]
    )
    if not paths:
        return {}
    path = max(paths, key=lambda value: value.stat().st_mtime_ns)
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SafeMonitoringError("SAFE_MONITOR_STATUS_INVALID") from exc
    return value if type(value) is dict else {}


def _database_health(root: Path) -> dict[str, object]:
    result = {
        "integrity": "not_started",
        "database_count": 0,
        "active_leases": 0,
        "staged_side_effects": 0,
        "failed_side_effects": 0,
    }
    if not root.exists():
        return result
    integrities = []
    for path in sorted(root.rglob("*.sqlite3")):
        result["database_count"] += 1
        uri = f"file:{path}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=2)
            connection.row_factory = sqlite3.Row
            integrities.extend(str(row[0]) for row in connection.execute("PRAGMA integrity_check"))
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "jobs" in tables:
                result["active_leases"] += int(
                    connection.execute(
                        "SELECT COUNT(*) FROM jobs WHERE state IN ('claimed','running')"
                    ).fetchone()[0]
                )
                result["failed_side_effects"] += int(
                    connection.execute(
                        "SELECT COUNT(*) FROM jobs WHERE state = 'failed'"
                    ).fetchone()[0]
                )
            if "artifacts" in tables:
                result["staged_side_effects"] += int(
                    connection.execute(
                        "SELECT COUNT(*) FROM artifacts WHERE state = 'staged'"
                    ).fetchone()[0]
                )
            connection.close()
        except sqlite3.Error:
            integrities.append("error")
    result["integrity"] = (
        "ok"
        if integrities and set(integrities) == {"ok"}
        else ("not_started" if not integrities else "failed")
    )
    return result


def _runtime_health(repository_root: Path, *, campaign_root: Path) -> dict[str, object]:
    try:
        identity = load_temperature_safe_runtime_services_v2(repository_root=repository_root)
        identity.require_current()
        return {
            "status": "PASS",
            "service_run_id": identity.service_run_id,
            "runtime_services_identity_sha256": identity.digest,
        }
    except Exception:  # noqa: BLE001 - observer emits only a closed aggregate state
        ledger = campaign_root / "private/campaign_events.jsonl"
        if ledger.is_file():
            try:
                events = _read_events(ledger)
            except (OSError, UnicodeError, json.JSONDecodeError):
                return {"status": "FAIL_CLOSED"}
            runtime_events = [
                event
                for event in events
                if event.get("kind")
                in {"RUNTIME_SERVICES_STARTED", "RUNTIME_SERVICES_STOPPED"}
            ]
            if runtime_events and runtime_events[-1].get("kind") == "RUNTIME_SERVICES_STOPPED":
                return {
                    "status": "PASS_STOPPED_BY_PROTOCOL",
                    "scope": runtime_events[-1].get("payload", {}).get("scope"),
                    "completion_evidence_preserved": True,
                }
        return {"status": "FAIL_CLOSED"}


def _tmux_windows() -> list[str]:
    completed = subprocess.run(
        ("tmux", "list-windows", "-t", "temperature-safe-evolve-v2", "-F", "#{window_name}"),
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return [] if completed.returncode else sorted(completed.stdout.splitlines())


def _pid_alive(pid: int | None) -> bool:
    if pid is None or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _read_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _newer_candidate_position(
    current: tuple[str, dict[str, object]] | None,
    events: list[dict[str, Any]],
) -> tuple[str, dict[str, object]] | None:
    for event in events:
        if (
            event.get("kind") != "CALL_CLAIMED"
            or event.get("payload", {}).get("logical_arm") != "safe_candidate"
        ):
            continue
        logical = str(event["payload"].get("logical_call_id", ""))
        match = _CANDIDATE_POSITION.search(logical)
        recorded = str(event.get("recorded_at_utc", ""))
        if match is None or (current is not None and recorded <= current[0]):
            continue
        current = (
            recorded,
            {
                "phase": match.group("phase"),
                "block": match.group("block"),
                "number": int(match.group("ordinal")) + 1,
            },
        )
    return current


__all__ = [
    "SafeMonitoringError",
    "campaign_snapshot_v2",
    "compact_status_line_v2",
]
