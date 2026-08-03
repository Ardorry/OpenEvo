from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

from openevo_chembench.supervised_transfer_v1.common import canonical_pretty_json_bytes
from openevo_chembench.temperature_full_evolve_v1.statistics import paired_binary_metrics_v1
from openevo_chembench.temperature_safe_evolve_v2.campaign import CampaignOutcomeV2
from openevo_chembench.temperature_safe_evolve_v2.config import (
    SafeEvolveConfigV2,
    load_safe_evolve_config_v2,
)
from openevo_chembench.temperature_safe_evolve_v2.phase_a import PhaseAOutcomeV2
from openevo_chembench.temperature_safe_evolve_v2.reporting import generate_campaign_report_v2


def test_no_go_ablation_report_is_complete_aggregate_only_and_readback_verified(
    tmp_path: Path, repository_root: Path
) -> None:
    original = load_safe_evolve_config_v2(
        repository_root
        / "benchmarks/chembench/configs/temperature_safe_evolve_v2/temperature_safe_evolve_v2.yaml"
    )
    payload = copy.deepcopy(original.payload)
    payload["roots"] = {
        **payload["roots"],
        "reports": "aggregate-reports",
        "desktop_parent": str(tmp_path / "desktop"),
    }
    config = SafeEvolveConfigV2(path=original.path, payload=payload, digest=original.digest)
    preflight = tmp_path / "preflight/public"
    preflight.mkdir(parents=True)
    identity = "1" * 64
    documents = {
        "preflight_report.json": {"config_sha256": original.digest},
            "runtime_identity_receipt.json": {
                "prior_runtime_v8": {"runtime_v8_identity_sha256": "2" * 64},
                "v2_formal_runtime_identity_sha256": "3" * 64,
                "phase_a_runtime_services_run_id": (
                    "stv3-temperature-safe-services-20990101T000000Z-12345678"
                ),
                "runtime_services_identity_sha256": identity,
            },
        "model_identity_receipt.json": {
            "candidate_codex_executable_sha256": "4" * 64,
            "model": "gpt-5.5",
            "reasoning_effort": "medium",
        },
        "source_code_receipt.json": {"source_commit": "5" * 40},
        "fold_manifest.json": {
            "fold_sha256": "6" * 64,
            "group_manifest_sha256": "7" * 64,
        },
        "near_duplicate_group_manifest.json": {"group_manifest_sha256": "7" * 64},
        "frozen_c4_artifact_receipt.json": {"status": "PASS"},
        "preflight_test_receipt.json": {"status": "PASS", "model_calls": 0},
    }
    for name, value in documents.items():
        (preflight / name).write_bytes(canonical_pretty_json_bytes(value))
    (preflight / "PRECHECK_REPORT.md").write_text("# Precheck\n", encoding="utf-8")
    (preflight / "EXPERIMENT_PROTOCOL.md").write_text("# Protocol\n", encoding="utf-8")

    same = (True,) * 100
    paired = paired_binary_metrics_v1(same, same, bootstrap_seed=20260803).to_dict()
    arm_rows = tuple(
        {
            "arm_id": f"A{index}",
            "target_ids": [],
            "correct": 100,
            "n": 100,
            "accuracy": 1.0,
            "paired_vs_a0": paired,
            "positive_flips": 0,
            "negative_flips": 0,
            "utility": 0,
            "parser_success": 100,
            "retries": 0,
            "failures": 0,
            "injected_bytes_per_question": 0,
            "h0_correct": 50,
            "h0_delta_pp": 0.0,
            "h0_utility": 0,
            "h1_correct": 50,
            "h1_delta_pp": 0.0,
            "h1_utility": 0,
        }
        for index in range(8)
    )
    phase_a = PhaseAOutcomeV2(
        selected_target=None,
        target_selection_receipt={
            "status": "NO_GO_ABLATION_NO_SAFE_SINGLE_TARGET",
            "selected_target": None,
        },
        arm_metrics=arm_rows,
        factorial_effects={
            "text_memory_main_effect_pp": 0.0,
            "skill_bundle_main_effect_pp": 0.0,
            "agent_system_main_effect_pp": 0.0,
            "memory_x_skill_pp": 0.0,
            "memory_x_agent_pp": 0.0,
            "skill_x_agent_pp": 0.0,
            "memory_x_skill_x_agent_pp": 0.0,
        },
        private_result_path=tmp_path / "unused-private-result.json",
    )
    campaign_root = tmp_path / "campaign"
    campaign_root.mkdir(mode=0o700)
    outcome = CampaignOutcomeV2(
        status="NO_GO_ABLATION_NO_SAFE_SINGLE_TARGET",
        campaign_run_id="stv3-temperature-safe-evolve-v2-20990101T000000Z",
        campaign_root=campaign_root,
        selected_target=None,
        phase_a=phase_a,
        folds=(),
        source_commit="5" * 40,
    )

    publication = generate_campaign_report_v2(
        repository_root=tmp_path,
        config=config,
        preflight_root=preflight.parent,
        outcome=outcome,
        runtime=SimpleNamespace(
            digest=identity,
            service_run_id="stv3-temperature-safe-services-20990101T000000Z-12345678",
        ),  # type: ignore[arg-type]
    )

    required = {
        "FINAL_REPORT.md",
        "POSTRUN_REVIEW.md",
        "PHASE_A_REPORT.md",
        "R0_REPORT.md",
        "R1_REPORT.md",
        "R2_REPORT.md",
        "R3_REPORT.md",
        "phase_a_arm_metrics.csv",
        "pooled_paired_comparison.json",
        "SHA256SUMS.txt",
    }
    assert required.issubset(
        {path.name for path in publication.aggregate_report_path.iterdir() if path.is_file()}
    )
    assert publication.desktop_report_path.is_dir()
    assert publication.sha256sums_sha256
    assert "NOT_RUN_BY_PREREGISTERED_STOP_RULE" in (
        publication.aggregate_report_path / "R3_REPORT.md"
    ).read_text(encoding="utf-8")
