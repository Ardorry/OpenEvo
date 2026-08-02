from __future__ import annotations

import hashlib
from pathlib import Path

from openevo_chembench.temperature_full_evolve_v1.reporting import (
    write_blocked_report_package,
)

REPOSITORY = Path(__file__).resolve().parents[4]


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
    )

    assert payload["status"] == "HARD_BLOCKED"
    sums = (destination / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
    assert len(sums) == payload["file_count"] - 1
    for line in sums:
        digest, name = line.split("  ", maxsplit=1)
        assert hashlib.sha256((destination / name).read_bytes()).hexdigest() == digest
    assert "UID values" not in (destination / "historical_exclusion_manifest.json").read_text()
    assert not (destination / "train_private_manifest.jsonl").exists()


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
