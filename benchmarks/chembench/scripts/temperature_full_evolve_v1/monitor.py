#!/usr/bin/env python3
"""Run the read-only 600-second Temperature full-evolve monitor."""

from __future__ import annotations

import argparse
from pathlib import Path

from openevo_chembench.temperature_full_evolve_v1.monitoring import run_monitor_loop_v1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--controller-status", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--snapshot-log", type=Path, required=True)
    parser.add_argument("--tmux-session", required=True)
    parser.add_argument("--runner-start-time-ticks", type=int)
    arguments = parser.parse_args()
    return run_monitor_loop_v1(
        repository_root=arguments.repository_root.resolve(strict=True),
        controller_status_path=arguments.controller_status.absolute(),
        database_path=arguments.database.absolute(),
        snapshot_log_path=arguments.snapshot_log.absolute(),
        tmux_session=arguments.tmux_session,
        expected_runner_start_time_ticks=arguments.runner_start_time_ticks,
    )


if __name__ == "__main__":
    raise SystemExit(main())
