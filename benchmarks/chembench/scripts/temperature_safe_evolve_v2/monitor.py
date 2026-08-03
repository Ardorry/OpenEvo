#!/usr/bin/env python3
"""Persist one aggregate Safe-Evolve status snapshot every 600 seconds."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]

from openevo_chembench.temperature_safe_evolve_v2.monitoring import (
    campaign_snapshot_v2,
    compact_status_line_v2,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--runner-pid-file", required=True, type=Path)
    parser.add_argument("--interval", type=int, default=600, choices=(600,))
    arguments = parser.parse_args()
    repository = REPOSITORY_ROOT.resolve(strict=True)
    stagnant = 0
    prior_progress: tuple[object, ...] | None = None
    while True:
        try:
            pid = int(arguments.runner_pid_file.read_text(encoding="ascii").strip())
        except (OSError, UnicodeError, ValueError):
            pid = None
        snapshot = campaign_snapshot_v2(
            repository_root=repository,
            campaign_run_id=arguments.run_id,
            runner_pid=pid,
        )
        print(compact_status_line_v2(snapshot), flush=True)
        progress = (
            snapshot["accepted_candidate_count"],
            snapshot["accepted_reflector_count"],
            snapshot["core_job_count"],
            snapshot["open_claim_count"],
            snapshot["campaign_phase"],
        )
        stagnant = stagnant + 1 if progress == prior_progress else 0
        prior_progress = progress
        if stagnant >= 2 and snapshot["runner_alive"]:
            print(
                json.dumps(
                    {
                        "status": "STALL_DIAGNOSTIC_TRIGGERED",
                        "open_claim_count": snapshot["open_claim_count"],
                        "active_leases": snapshot["active_leases"],
                        "runtime_health": snapshot["runtime_health"],
                        "database_integrity": snapshot["database_integrity"],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                flush=True,
            )
        if snapshot["terminal_outcome_present"] and not snapshot["runner_alive"]:
            return 0
        if not snapshot["runner_alive"] and snapshot["campaign_phase"] != "WAITING_FOR_RUN_ROOT":
            return 2
        time.sleep(arguments.interval)


if __name__ == "__main__":
    raise SystemExit(main())
