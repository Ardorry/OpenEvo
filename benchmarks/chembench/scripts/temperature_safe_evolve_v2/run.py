#!/usr/bin/env python3
"""Run or recover the preregistered Safe-Evolve V2 campaign through final reporting."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]

from openevo_chembench.supervised_transfer_v1.common import (
    canonical_json_bytes,
    write_private_file,
)
from openevo_chembench.temperature_safe_evolve_v2.campaign import (
    CampaignError,
    SafeEvolveCampaignV2,
)
from openevo_chembench.temperature_safe_evolve_v2.config import (
    load_safe_evolve_config_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.formal_runtime import (
    require_temperature_safe_formal_runtime_python_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.reporting import (
    SafeReportError,
    generate_campaign_report_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.runtime_services import (
    TemperatureRuntimeServicesError,
    load_temperature_safe_runtime_services_v2,
)

_RUN_ID = re.compile(r"stv3-temperature-safe-evolve-v2-20[0-9]{6}T[0-9]{6}Z\Z")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("fresh", "resume"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--preflight-root", required=True, type=Path)
    arguments = parser.parse_args()
    if _RUN_ID.fullmatch(arguments.run_id) is None:
        parser.error("--run-id is invalid")
    if (
        os.environ.get("OPENEVO_ALLOW_PAID_CALLS") != "1"
        or os.environ.get("CHEMBENCH_ALLOW_PAID_CALLS") != "1"
    ):
        raise CampaignError("SAFE_CAMPAIGN_PAID_CALL_AUTHORIZATION_MISSING")
    repository = REPOSITORY_ROOT.resolve(strict=True)
    if not sys.flags.isolated:
        raise CampaignError("SAFE_CAMPAIGN_ISOLATED_RUNTIME_REQUIRED")
    require_temperature_safe_formal_runtime_python_v2(repository_root=repository)
    runtime = load_temperature_safe_runtime_services_v2(repository_root=repository)
    preflight = arguments.preflight_root.resolve(strict=True)
    campaign = SafeEvolveCampaignV2(
        repository_root=repository,
        campaign_run_id=arguments.run_id,
        preflight_root=preflight,
        runtime=runtime,
        resume=arguments.mode == "resume",
    )
    outcome = campaign.run()
    config = load_safe_evolve_config_v2(
        repository / "benchmarks/chembench/configs/temperature_safe_evolve_v2/"
        "temperature_safe_evolve_v2.yaml"
    )
    publication = generate_campaign_report_v2(
        repository_root=repository,
        config=config,
        preflight_root=preflight,
        outcome=outcome,
        runtime=runtime,
    )
    payload = {
        "schema_version": "TemperatureSafeCampaignTerminalOutcomeV2",
        "status": outcome.status,
        "campaign_run_id": outcome.campaign_run_id,
        "selected_target": outcome.selected_target,
        "completed_folds": len(outcome.folds),
        "fold_run_ids": [value.run_id for value in outcome.folds],
        "invalidated_run_ids": list(outcome.invalidated_run_ids),
        "source_commit": outcome.source_commit,
        "aggregate_report_path": publication.aggregate_report_path.relative_to(
            repository
        ).as_posix(),
        "desktop_report_path": os.fspath(publication.desktop_report_path),
        "sha256sums_sha256": publication.sha256sums_sha256,
        "candidate_calls": publication.candidate_calls,
        "reflector_calls": publication.reflector_calls,
        "core_jobs": publication.core_jobs,
        "failures": publication.failures,
        "retries": publication.retries,
        "rejected": publication.rejected,
        "recoveries": publication.recoveries,
    }
    outcome_path = campaign.root / "private/campaign_terminal_outcome.json"
    if outcome_path.exists():
        if outcome_path.read_bytes() != canonical_json_bytes(payload):
            raise CampaignError("SAFE_CAMPAIGN_TERMINAL_OUTCOME_DRIFT")
    else:
        write_private_file(outcome_path, canonical_json_bytes(payload), replace=False)
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (CampaignError, TemperatureRuntimeServicesError, SafeReportError) as exc:
        print(
            json.dumps(
                {"status": "FAIL_CLOSED", "finding_code": exc.finding_code},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    except Exception:  # noqa: BLE001 - formal CLI must not expose private boundary details
        print(
            json.dumps(
                {
                    "status": "FAIL_CLOSED",
                    "finding_code": "SAFE_CAMPAIGN_BOUNDARY_FAILURE",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        raise SystemExit(3) from None
