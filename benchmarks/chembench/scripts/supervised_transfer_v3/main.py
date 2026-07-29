#!/usr/bin/env python3
"""ChemBench three-answer evolved-only supervised transfer v3 entry point."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from openevo_chembench.supervised_transfer_v2.managed_codex import (
    require_paid_runtime_python_v2,
)
from openevo_chembench.supervised_transfer_v2.runtime_services import (
    runtime_services_status_v2,
    start_runtime_services_v2,
    stop_runtime_services_v2,
)
from openevo_chembench.supervised_transfer_v3.config import load_config_v3
from openevo_chembench.supervised_transfer_v3.experiment import (
    SupervisedTransferExperimentV3,
    build_complete_dry_run_v3,
    load_experiment_inputs_v3,
)
from openevo_chembench.supervised_transfer_v3.source_identity import (
    verify_source_manifest_v3,
    write_source_manifest_v3,
)
from openevo_chembench.supervised_transfer_v3.split_reference import (
    verify_split_reference_receipt_v3,
    write_split_reference_receipt_v3,
)

DEFAULT_CONFIG = PACKAGE_ROOT / "configs/supervised_transfer_v3/chembench_supervised_transfer_v3.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--run-id")
    parser.add_argument("--smoke-run-id")
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
            "smoke",
            "formal-online",
        ),
    )
    arguments = parser.parse_args()
    config = load_config_v3(arguments.config.resolve(strict=True))
    if arguments.command == "prepare":
        payload = write_split_reference_receipt_v3(REPOSITORY_ROOT)
    elif arguments.command == "source-manifest":
        payload = write_source_manifest_v3(REPOSITORY_ROOT)
    elif arguments.command == "verify":
        payload = {
            "status": "PASS",
            "split": verify_split_reference_receipt_v3(REPOSITORY_ROOT),
            "source": verify_source_manifest_v3(REPOSITORY_ROOT),
        }
    elif arguments.command == "dry-run":
        inputs = load_experiment_inputs_v3(REPOSITORY_ROOT, config, require_runtime=False)
        payload = build_complete_dry_run_v3(inputs)
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
            "shared_verified_runtime_abi": "chembench_supervised_transfer_v2",
        }
    elif arguments.command == "services-status":
        payload = runtime_services_status_v2(repository_root=REPOSITORY_ROOT)
    elif arguments.command == "services-stop":
        payload = stop_runtime_services_v2(repository_root=REPOSITORY_ROOT)
    else:
        if not arguments.allow_paid:
            parser.error("smoke/formal-online requires --allow-paid")
        if not arguments.run_id:
            parser.error("smoke/formal-online requires --run-id")
        if arguments.command == "formal-online" and not arguments.smoke_run_id:
            parser.error("formal-online requires --smoke-run-id")
        if arguments.command == "smoke" and arguments.smoke_run_id:
            parser.error("smoke cannot consume --smoke-run-id")
        require_paid_runtime_python_v2(repository_root=REPOSITORY_ROOT)
        inputs = load_experiment_inputs_v3(REPOSITORY_ROOT, config, require_runtime=True)
        experiment = SupervisedTransferExperimentV3(
            inputs=inputs,
            run_id=arguments.run_id,
            run_mode=("smoke" if arguments.command == "smoke" else "formal_online"),
            smoke_run_id=arguments.smoke_run_id,
        )
        payload = (
            experiment.run_smoke()
            if arguments.command == "smoke"
            else experiment.run_formal_online()
        )
    payload["config_sha256"] = config.digest
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
