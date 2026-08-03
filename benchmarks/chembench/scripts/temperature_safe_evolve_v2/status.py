#!/usr/bin/env python3
"""Print a compact read-only Safe-Evolve V2 status."""

from __future__ import annotations

import argparse
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
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--interval", type=int, default=600, choices=(600,))
    arguments = parser.parse_args()
    repository = REPOSITORY_ROOT.resolve(strict=True)
    while True:
        try:
            pid = int(arguments.runner_pid_file.read_text(encoding="ascii").strip())
        except (OSError, UnicodeError, ValueError):
            pid = None
        snapshot = campaign_snapshot_v2(
            repository_root=repository,
            campaign_run_id=arguments.run_id,
            runner_pid=pid,
            persist=False,
        )
        print(compact_status_line_v2(snapshot), flush=True)
        if not arguments.follow or (
            snapshot["terminal_outcome_present"] and not snapshot["runner_alive"]
        ):
            return 0
        time.sleep(arguments.interval)


if __name__ == "__main__":
    raise SystemExit(main())
