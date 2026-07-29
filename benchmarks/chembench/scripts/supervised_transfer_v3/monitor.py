#!/usr/bin/env python3
"""Five-minute compact monitor for one immutable v3 run."""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    parser.add_argument("--interval", type=int, default=300)
    args = parser.parse_args()
    state_path = (
        REPOSITORY_ROOT
        / "state/chembench_supervised_transfer_v3/runs"
        / args.run_id
        / "run_state.json"
    )
    report_root = REPOSITORY_ROOT / "reports/chembench_supervised_transfer_v3"
    report_root.mkdir(parents=True, exist_ok=True)
    log_path = report_root / f"formal_monitor_{args.run_id}.md"
    previous: dict[str, object] = {}
    while True:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        keys = (
            "status",
            "stage",
            "category",
            "task",
            "round_or_cycle",
            "task_sessions",
            "reflector_calls",
            "core_jobs",
            "text_memory_artifacts",
            "skill_artifacts",
            "agent_system_artifacts",
            "context_resolutions",
            "infrastructure_failures",
            "security_findings",
            "context_findings",
            "artifact_findings",
            "last_progress_utc",
            "failure_code",
        )
        current = {key: state.get(key) for key in keys}
        deltas = {
            key: current[key] - previous[key]
            for key in keys
            if type(current.get(key)) is int and type(previous.get(key)) is int
        }
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(f"\n## {now}\n\n```json\n{json.dumps({'state': current, 'delta': deltas}, sort_keys=True)}\n```\n")
        print(json.dumps({"timestamp": now, "run_id": args.run_id, **current, "delta": deltas}, sort_keys=True), flush=True)
        if state.get("status") in {"COMPLETED", "FAIL_CLOSED", "REPORTING_FAILED"}:
            return 0
        previous = current
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
