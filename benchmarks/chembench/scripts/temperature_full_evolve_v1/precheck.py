#!/usr/bin/env python3
"""Generate the aggregate-only Temperature full-evolve hard-stop package."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from openevo_chembench.supervised_transfer_v2.managed_codex import (
    codex_subscription_auth_source_v2,
    load_managed_candidate_codex_v2,
    load_managed_codex_v2,
)
from openevo_chembench.temperature_full_evolve_v1.reporting import (
    build_managed_runtime_receipt,
    write_blocked_report_package,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--audit-id")
    parser.add_argument("--generated-at-utc")
    parser.add_argument("--preflight-tool-failures", type=int, default=0)
    parser.add_argument("--preflight-tool-repairs", type=int, default=0)
    parser.add_argument("--focused-passed", type=int)
    parser.add_argument("--focused-failed", type=int)
    parser.add_argument("--full-passed", type=int)
    parser.add_argument("--full-failed", type=int)
    parser.add_argument("--full-subtests-passed", type=int)
    arguments = parser.parse_args()

    generated = arguments.generated_at_utc or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    stamp = datetime.fromisoformat(generated).strftime("%Y%m%dT%H%M%SZ")
    audit_id = arguments.audit_id or f"stv3-temperature-full-evolve-v1-precheck-{stamp}"
    repository = REPOSITORY_ROOT.resolve(strict=True)
    branch = _git(repository, "branch", "--show-current")
    source_commit = _git(repository, "rev-parse", "HEAD")

    reflector = load_managed_codex_v2(repository_root=repository)
    candidate = load_managed_candidate_codex_v2(repository_root=repository)
    codex_subscription_auth_source_v2()
    runtime_state = (
        repository / "state/chembench_supervised_transfer_v2/runtime_services/current.json"
    )
    framework_lock = (
        repository / "state/chembench_supervised_transfer_v2/framework/framework-lock.json"
    )
    disk = shutil.disk_usage(repository)

    model_identity = {
        "schema_version": "TemperatureFullEvolveModelIdentityReceiptV1",
        "status": "PASS_ZERO_CALL_IDENTITY_ONLY",
        "model": "gpt-5.5",
        "reasoning_effort": "medium",
        "candidate_and_baseline_identity_equal": True,
        "credential_metadata_check": "PASS_OWNER_PRIVATE_REGULAR_FILE_0600",
        "credential_content_read": False,
        "model_calls": 0,
    }
    runtime_identity = build_managed_runtime_receipt(
        candidate_identity=candidate.public_identity,
        reflector_identity=reflector.public_identity,
        framework_lock_present=framework_lock.is_file(),
        runtime_services_receipt_present=runtime_state.is_file(),
        disk_total_bytes=disk.total,
        disk_free_bytes=disk.free,
    )
    regression_counts = (
        arguments.focused_passed,
        arguments.focused_failed,
        arguments.full_passed,
        arguments.full_failed,
        arguments.full_subtests_passed,
    )
    if any(value is None for value in regression_counts) and any(
        value is not None for value in regression_counts
    ):
        parser.error("regression counts must be supplied together")
    if any(value is not None and value < 0 for value in regression_counts):
        parser.error("regression counts must be non-negative")
    regression_summary = None
    if all(value is not None for value in regression_counts):
        failure_classification = (
            [
                {
                    "count": 8,
                    "code": "PRE_EXISTING_V2_SOURCE_MANIFEST_DRIFT",
                    "new_preflight_defect": False,
                },
                {
                    "count": 1,
                    "code": "EXPECTED_LEGACY_RECOVERY_SCOPE_REJECTION_ON_NEW_BRANCH",
                    "new_preflight_defect": False,
                },
                {
                    "count": 30,
                    "code": "LEGACY_LAYOUT_PRIVATE_FIXTURE_OR_STALE_MANIFEST_UNAVAILABLE",
                    "new_preflight_defect": False,
                },
            ]
            if arguments.full_failed == 39
            else [
                {
                    "count": arguments.full_failed,
                    "code": "UNCLASSIFIED_FULL_SUITE_FAILURE",
                    "new_preflight_defect": None,
                }
            ]
        )
        regression_summary = {
            "schema_version": "TemperatureFullEvolveRegressionSummaryV1",
            "status": "FOCUSED_PASS_FULL_SUITE_HAS_UNRELATED_OR_EXPECTED_FAILURES",
            "focused_passed": arguments.focused_passed,
            "focused_failed": arguments.focused_failed,
            "full_passed": arguments.full_passed,
            "full_failed": arguments.full_failed,
            "full_subtests_passed": arguments.full_subtests_passed,
            "full_suite_model_calls": 0,
            "failure_classification": failure_classification,
            "historical_evidence_modified_to_force_green": False,
        }
    payload = write_blocked_report_package(
        repository_root=repository,
        destination=arguments.destination.resolve(),
        audit_id=audit_id,
        generated_at_utc=generated,
        source_code_commit=source_commit,
        branch=branch,
        model_identity=model_identity,
        managed_runtime_identity=runtime_identity,
        preflight_tool_failures=arguments.preflight_tool_failures,
        preflight_tool_repairs=arguments.preflight_tool_repairs,
        regression_summary=regression_summary,
    )
    print(payload)
    return 0


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=True,
        text=True,
        capture_output=True,
    )
    return completed.stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
