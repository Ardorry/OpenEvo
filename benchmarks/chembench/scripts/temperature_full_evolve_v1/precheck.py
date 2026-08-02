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
    write_blocked_report_package,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--audit-id")
    parser.add_argument("--generated-at-utc")
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
    runtime_identity = {
        "schema_version": "TemperatureFullEvolveManagedRuntimeReceiptV1",
        "status": "PASS_STATIC_IDENTITY_NO_FORMAL_SERVICE_STARTED",
        "candidate_source": candidate.source,
        "candidate_image_id": candidate.image_id,
        "candidate_image_authority": candidate.image_authority,
        "candidate_codex_cli_version": candidate.codex_cli_version,
        "candidate_npm_package": candidate.npm_package,
        "candidate_executable_sha256": candidate.executable_sha256,
        "candidate_receipt_sha256": candidate.receipt_sha256,
        "reflector_source": reflector.source,
        "reflector_codex_cli_version": reflector.codex_cli_version,
        "reflector_npm_package": reflector.npm_package,
        "reflector_executable_sha256": reflector.executable_sha256,
        "reflector_receipt_sha256": reflector.receipt_sha256,
        "candidate_reflector_executable_equal": (
            candidate.executable_sha256 == reflector.executable_sha256
        ),
        "framework_lock_present": framework_lock.is_file(),
        "runtime_services_current_receipt_present": runtime_state.is_file(),
        "runtime_services_started_for_this_audit": False,
        "credential_content_read": False,
        "disk_total_bytes": disk.total,
        "disk_free_bytes": disk.free,
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
