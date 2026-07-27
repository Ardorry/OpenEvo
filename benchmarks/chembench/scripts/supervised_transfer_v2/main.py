#!/usr/bin/env python3
"""ChemBench supervised three-target transfer v2 entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from openevo_chembench.supervised_transfer_v2.config import load_config_v2
from openevo_chembench.supervised_transfer_v2.experiment import (
    SupervisedTransferExperimentV2,
    build_complete_dry_run_v2,
    load_experiment_inputs_v2,
)
from openevo_chembench.supervised_transfer_v2.prepare import (
    prepare_phase0_v2,
    verify_phase0_v2,
)
from openevo_chembench.supervised_transfer_v2.runtime_services import (
    runtime_services_status_v2,
    start_runtime_services_v2,
    stop_runtime_services_v2,
)
from openevo_chembench.supervised_transfer_v2.source_identity import (
    write_source_manifest_v2,
)

DEFAULT_CONFIG = (
    PACKAGE_ROOT / "configs/supervised_transfer_v2/chembench_supervised_transfer_v2.yaml"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--run-id")
    parser.add_argument("--preflight-run-id")
    parser.add_argument("--service-run-id")
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument(
        "command",
        choices=(
            "prepare",
            "verify",
            "source-manifest",
            "dry-run",
            "services-start",
            "services-status",
            "services-stop",
            "preflight",
            "formal",
        ),
    )
    arguments = parser.parse_args()
    config = load_config_v2(arguments.config.resolve(strict=True))
    if arguments.command == "prepare":
        payload = prepare_phase0_v2(REPOSITORY_ROOT)
    elif arguments.command == "verify":
        payload = verify_phase0_v2(REPOSITORY_ROOT)
    elif arguments.command == "source-manifest":
        payload = write_source_manifest_v2(REPOSITORY_ROOT)
    elif arguments.command == "dry-run":
        inputs = load_experiment_inputs_v2(
            REPOSITORY_ROOT,
            config,
            require_reflector_runtime=False,
        )
        payload = build_complete_dry_run_v2(inputs)
    elif arguments.command == "services-start":
        if not arguments.service_run_id:
            parser.error("services-start requires --service-run-id")
        identity = start_runtime_services_v2(
            repository_root=REPOSITORY_ROOT,
            service_run_id=arguments.service_run_id,
        )
        payload = {
            "status": "PASS",
            "service_run_id": identity.service_run_id,
            "source_commit": identity.source_commit,
            "runtime_services_identity_sha256": identity.digest,
        }
    elif arguments.command == "services-status":
        payload = runtime_services_status_v2(repository_root=REPOSITORY_ROOT)
    elif arguments.command == "services-stop":
        payload = stop_runtime_services_v2(repository_root=REPOSITORY_ROOT)
    else:
        if not arguments.allow_paid:
            parser.error("preflight/formal requires the explicit --allow-paid gate")
        if not arguments.run_id:
            parser.error("preflight/formal requires --run-id")
        inputs = load_experiment_inputs_v2(
            REPOSITORY_ROOT,
            config,
            require_reflector_runtime=True,
        )
        experiment = SupervisedTransferExperimentV2(
            inputs=inputs,
            run_id=arguments.run_id,
            run_mode=arguments.command,
            preflight_run_id=arguments.preflight_run_id,
        )
        payload = (
            experiment.run_preflight()
            if arguments.command == "preflight"
            else experiment.run_formal()
        )
    payload["config_sha256"] = config.digest
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
