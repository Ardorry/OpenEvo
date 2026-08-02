#!/usr/bin/env python3
"""Run or recover the fixed Temperature full-evolve v1 formal protocol."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]

from openevo_chembench.temperature_full_evolve_v1.formal_runtime import (
    require_temperature_formal_runtime_python_v1,
)
from openevo_chembench.temperature_full_evolve_v1.runner import (
    TemperatureFormalRunnerError,
    TemperatureFullEvolveFormalRunnerV1,
)

_RUN_ID = re.compile(
    r"stv3-temperature-full-evolve-v1-20[0-9]{6}T[0-9]{6}Z\Z",
    re.ASCII,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("fresh", "resume"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--preflight-root", required=True, type=Path)
    parser.add_argument("--private-split-root", required=True, type=Path)
    arguments = parser.parse_args()
    if _RUN_ID.fullmatch(arguments.run_id) is None:
        parser.error("--run-id must use stv3-temperature-full-evolve-v1-<UTC_TIMESTAMP>")
    runner: TemperatureFullEvolveFormalRunnerV1 | None = None
    try:
        if (
            os.environ.get("OPENEVO_ALLOW_PAID_CALLS") != "1"
            or os.environ.get("CHEMBENCH_ALLOW_PAID_CALLS") != "1"
        ):
            raise RuntimeError("FORMAL_PAID_CALL_AUTHORIZATION_MISSING")
        repository = REPOSITORY_ROOT.resolve(strict=True)
        if not sys.flags.isolated:
            raise RuntimeError("FORMAL_RUNTIME_ISOLATED_MODE_REQUIRED")
        require_temperature_formal_runtime_python_v1(repository_root=repository)
        runner = TemperatureFullEvolveFormalRunnerV1(
            repository_root=repository,
            controller_run_id=arguments.run_id,
            preflight_root=arguments.preflight_root.resolve(strict=True),
            private_split_root=arguments.private_split_root.resolve(strict=True),
            resume=arguments.mode == "resume",
        )
        aggregate = runner.run()
        payload = {
            "status": "READY_FOR_INDEPENDENT_AUDIT",
            "controller_run_id": runner.layout.controller_run_id,
            "evolved_run_id": runner.layout.evolved_run_id,
            "baseline_run_id": runner.layout.baseline_run_id,
            "core_run_id": runner.layout.core_run_id,
            "aggregate_report_input": os.fspath(aggregate),
        }
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0
    except TemperatureFormalRunnerError as exc:
        print(
            json.dumps(
                {"status": "FAIL_CLOSED", "finding_code": exc.finding_code},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
    except Exception:  # noqa: BLE001 - formal CLI must not leak boundary details
        print(
            json.dumps(
                {
                    "status": "FAIL_CLOSED",
                    "finding_code": "FORMAL_RUNNER_BOUNDARY_FAILURE",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 3
    finally:
        if runner is not None:
            runner.close()


if __name__ == "__main__":
    raise SystemExit(main())
