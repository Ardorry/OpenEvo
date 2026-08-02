from __future__ import annotations

import hashlib
from pathlib import Path

from openevo_chembench.temperature_full_evolve_v1.reporting import (
    build_managed_runtime_receipt,
    write_blocked_report_package,
)

REPOSITORY = Path(__file__).resolve().parents[4]


def test_runtime_receipt_uses_public_candidate_source_mapping() -> None:
    candidate = {
        "source": "openevo_core_managed_science_runtime_v1",
        "image_id": "sha256:" + "1" * 64,
        "image_authority": "sha256:" + "1" * 64,
        "codex_cli_version": "codex-cli 0.144.1",
        "npm_package": "@openai/codex@0.144.1",
        "executable_sha256": "2" * 64,
        "receipt_sha256": "3" * 64,
    }
    reflector = {
        "source": "openevo_core_managed_codex_v1",
        "codex_cli_version": "codex-cli 0.144.1",
        "npm_package": "@openai/codex@0.144.1",
        "executable_sha256": "2" * 64,
        "receipt_sha256": "4" * 64,
    }

    receipt = build_managed_runtime_receipt(
        candidate_identity=candidate,
        reflector_identity=reflector,
        framework_lock_present=True,
        runtime_services_receipt_present=False,
        disk_total_bytes=100,
        disk_free_bytes=50,
    )

    assert receipt["candidate_source"] == candidate["source"]
    assert receipt["reflector_source"] == reflector["source"]
    assert receipt["candidate_reflector_executable_equal"] is True


def test_blocked_report_package_is_closed_and_checksum_verified(tmp_path: Path) -> None:
    destination = tmp_path / "report"
    payload = write_blocked_report_package(
        repository_root=REPOSITORY.resolve(),
        destination=destination,
        audit_id="stv3-temperature-full-evolve-v1-precheck-test",
        generated_at_utc="2026-08-02T00:00:00Z",
        source_code_commit="1" * 40,
        branch="chembench-temperature-full-evolve-v1",
        model_identity={
            "model": "gpt-5.5",
            "reasoning_effort": "medium",
            "model_calls": 0,
        },
        managed_runtime_identity={
            "candidate_executable_sha256": "2" * 64,
            "candidate_image_id": "sha256:" + "3" * 64,
        },
        preflight_tool_failures=1,
        preflight_tool_repairs=1,
        regression_summary={
            "schema_version": "TemperatureFullEvolveRegressionSummaryV1",
            "status": "FOCUSED_PASS",
            "focused_passed": 17,
            "focused_failed": 0,
            "full_passed": 1023,
            "full_failed": 39,
            "full_subtests_passed": 58,
        },
    )

    assert payload["status"] == "HARD_BLOCKED"
    sums = (destination / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
    assert len(sums) == payload["file_count"] - 1
    for line in sums:
        digest, name = line.split("  ", maxsplit=1)
        assert hashlib.sha256((destination / name).read_bytes()).hexdigest() == digest
    assert "UID values" not in (destination / "historical_exclusion_manifest.json").read_text()
    assert not (destination / "train_private_manifest.jsonl").exists()
    call_summary = (destination / "call_and_failure_summary.json").read_text(encoding="utf-8")
    assert '"preflight_tool_failures": 1' in call_summary
    assert '"preflight_tool_repairs": 1' in call_summary
    assert '"focused_passed": 17' in (destination / "TEST_REPORT.json").read_text(encoding="utf-8")


def test_result_metrics_are_explicitly_not_run(tmp_path: Path) -> None:
    destination = tmp_path / "report"
    write_blocked_report_package(
        repository_root=REPOSITORY.resolve(),
        destination=destination,
        audit_id="stv3-temperature-full-evolve-v1-precheck-test",
        generated_at_utc="2026-08-02T00:00:00Z",
        source_code_commit="1" * 40,
        branch="chembench-temperature-full-evolve-v1",
        model_identity={"model": "gpt-5.5", "reasoning_effort": "medium"},
        managed_runtime_identity={
            "candidate_executable_sha256": "2" * 64,
            "candidate_image_id": "sha256:" + "3" * 64,
        },
    )

    for name in (
        "evolved_test_metrics.json",
        "baseline_test_metrics.json",
        "paired_test_comparison.json",
    ):
        assert '"status": "NOT_RUN"' in (destination / name).read_text(encoding="utf-8")
    chart_inventory = (destination / "chart_inventory.json").read_text(encoding="utf-8")
    assert '"fabricated_zero-valued_result_charts": false' in chart_inventory
