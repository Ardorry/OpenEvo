#!/usr/bin/env python3
"""Lifecycle CLI for Temperature full-evolve owner-private runtime services."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from openevo_chembench.temperature_full_evolve_v1.runtime_services import (
    start_temperature_runtime_services_v1,
    stop_temperature_runtime_services_v1,
    temperature_runtime_services_status_v1,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "status", "stop"))
    parser.add_argument("--service-run-id")
    arguments = parser.parse_args()
    repository = REPOSITORY_ROOT.resolve(strict=True)
    if arguments.command == "start":
        if not arguments.service_run_id:
            parser.error("start requires --service-run-id")
        identity = start_temperature_runtime_services_v1(
            repository_root=repository,
            service_run_id=arguments.service_run_id,
        )
        payload = {
            "status": "PASS",
            "service_run_id": identity.service_run_id,
            "source_commit": identity.source_commit,
            "runtime_services_identity_sha256": identity.digest,
            "completion_root_identity_sha256": identity.require_current()[
                "completion_root_identity_sha256"
            ],
        }
    elif arguments.command == "status":
        if arguments.service_run_id is not None:
            parser.error("status does not accept --service-run-id")
        payload = temperature_runtime_services_status_v1(repository_root=repository)
    else:
        if arguments.service_run_id is not None:
            parser.error("stop does not accept --service-run-id")
        payload = stop_temperature_runtime_services_v1(repository_root=repository)
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
