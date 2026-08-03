"""Aggregate-only statistics, charts and dual local publication for Safe-Evolve V2."""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openevo_chembench.supervised_transfer_v1.common import (
    canonical_pretty_json_bytes,
    write_private_file,
)
from openevo_chembench.temperature_full_evolve_v1.statistics import paired_binary_metrics_v1
from openevo_chembench.temperature_safe_evolve_v2.campaign import CampaignOutcomeV2
from openevo_chembench.temperature_safe_evolve_v2.config import SafeEvolveConfigV2
from openevo_chembench.temperature_safe_evolve_v2.runtime_services import (
    TemperatureRuntimeServicesIdentityV1,
)


class SafeReportError(RuntimeError):
    def __init__(self, finding_code: str) -> None:
        self.finding_code = finding_code
        super().__init__(finding_code)


@dataclass(frozen=True, slots=True)
class SafeReportPublicationV2:
    aggregate_report_path: Path
    desktop_report_path: Path
    sha256sums_sha256: str
    candidate_calls: int
    reflector_calls: int
    core_jobs: int
    failures: int
    retries: int
    rejected: int
    recoveries: int


def generate_campaign_report_v2(
    *,
    repository_root: Path,
    config: SafeEvolveConfigV2,
    preflight_root: Path,
    outcome: CampaignOutcomeV2,
    runtime: TemperatureRuntimeServicesIdentityV1,
) -> SafeReportPublicationV2:
    repository = repository_root.resolve(strict=True)
    folds = tuple(_load_json(value.private_result_path) for value in outcome.folds)
    public_preflight = preflight_root / "public"
    preflight_report = _load_json(public_preflight / "preflight_report.json")
    runtime_receipt = _load_json(public_preflight / "runtime_identity_receipt.json")
    model_receipt = _load_json(public_preflight / "model_identity_receipt.json")
    source_receipt = _load_json(public_preflight / "source_code_receipt.json")
    fold_manifest = _load_json(public_preflight / "fold_manifest.json")
    near_duplicate = _load_json(public_preflight / "near_duplicate_group_manifest.json")
    calls = _aggregate_calls(outcome)
    pooled = _pooled_metrics(folds)
    timestamp = outcome.campaign_run_id.removeprefix("stv3-temperature-safe-evolve-v2-")
    staging = outcome.campaign_root / "private/report_staging"
    if staging.exists():
        _require_private_directory(staging)
    else:
        staging.mkdir(parents=True, mode=0o700)

    _copy_exact(public_preflight / "EXPERIMENT_PROTOCOL.md", staging / "EXPERIMENT_PROTOCOL.md")
    _copy_exact(public_preflight / "PRECHECK_REPORT.md", staging / "PRECHECK_REPORT.md")
    for source_name, destination_name in (
        ("frozen_c4_artifact_receipt.json", "frozen_c4_artifact_receipt.json"),
        ("fold_manifest.json", "fold_manifest.json"),
        ("near_duplicate_group_manifest.json", "near_duplicate_group_manifest.json"),
        ("model_identity_receipt.json", "model_identity_receipt.json"),
        ("runtime_identity_receipt.json", "managed_runtime_receipt.json"),
        ("runtime_identity_receipt.json", "runtime_identity_receipt.json"),
        ("preflight_test_receipt.json", "preflight_test_receipt.json"),
        ("source_code_receipt.json", "source_code_receipt.json"),
    ):
        _copy_exact(public_preflight / source_name, staging / destination_name)

    campaign_manifest = {
        "schema_version": "TemperatureSafeCampaignAggregateManifestV2",
        "status": outcome.status,
        "campaign_run_id": outcome.campaign_run_id,
        "phase_a_run_ids": [f"{outcome.campaign_run_id}-ablation-a{index}" for index in range(8)],
        "fold_run_ids": [value.run_id for value in outcome.folds],
        "invalidated_run_ids": list(outcome.invalidated_run_ids),
        "selected_target": outcome.selected_target,
        "classification": (
            "reused_official_temperature_pool_independent_state_mechanism_experiment"
        ),
        "fold_sha256": fold_manifest["fold_sha256"],
        "config_sha256": preflight_report["config_sha256"],
        "source_commit": outcome.source_commit,
        "model": "gpt-5.5",
        "reasoning_effort": "medium",
        "managed_codex_sha256": model_receipt["candidate_codex_executable_sha256"],
        "runtime_v8_compatibility_identity_sha256": runtime_receipt["prior_runtime_v8"][
            "runtime_v8_identity_sha256"
        ],
        "v2_formal_runtime_identity_sha256": runtime_receipt["v2_formal_runtime_identity_sha256"],
        "phase_a_runtime_services_run_id": runtime_receipt[
            "phase_a_runtime_services_run_id"
        ],
        "phase_a_runtime_services_identity_sha256": runtime_receipt[
            "runtime_services_identity_sha256"
        ],
        "fold_runtime_and_core_identities": [
            {
                "fold_id": value.fold_id,
                "runtime_services_run_id": value.runtime_services_run_id,
                "runtime_services_identity_sha256": value.runtime_services_identity_sha256,
                "core_store_id": value.core_store_id,
            }
            for value in outcome.folds
        ],
        "distinct_runtime_services_identity_per_fold": True,
        "distinct_core_store_identity_per_fold": True,
        "current_run_split_isolated": True,
        "fresh_state_database_artifact_lineage_per_run": True,
        "historically_never_exposed_claim": False,
        "external_independent_generalization_claim": False,
        "contains_item_identities": False,
        "contains_predictions": False,
    }
    _write_json(staging / "CAMPAIGN_MANIFEST.json", campaign_manifest)
    _write_phase_a(staging, outcome)
    _write_fold_tables(staging, outcome, folds)
    _write_json(staging / "pooled_paired_comparison.json", pooled)
    _write_reports(
        staging=staging,
        outcome=outcome,
        pooled=pooled,
        calls=calls,
        fold_manifest=fold_manifest,
        near_duplicate=near_duplicate,
        runtime_receipt=runtime_receipt,
    )
    _write_compatibility(
        staging=staging,
        outcome=outcome,
        runtime_receipt=runtime_receipt,
        model_receipt=model_receipt,
        source_receipt=source_receipt,
    )
    _write_charts(staging=staging, outcome=outcome, folds=folds, pooled=pooled)
    _validate_public_boundary(staging)
    sums = _write_checksums(staging)
    report_destination = repository / config.payload["roots"]["reports"] / outcome.campaign_run_id
    desktop_destination = (
        Path(str(config.payload["roots"]["desktop_parent"]))
        / f"Temperature_Safe_Evolve_V2_{timestamp}"
    )
    _publish_exact_tree(staging, report_destination)
    _publish_exact_tree(staging, desktop_destination)
    _verify_tree(report_destination, sums)
    _verify_tree(desktop_destination, sums)
    return SafeReportPublicationV2(
        aggregate_report_path=report_destination,
        desktop_report_path=desktop_destination,
        sha256sums_sha256=hashlib.sha256((staging / "SHA256SUMS.txt").read_bytes()).hexdigest(),
        candidate_calls=calls["candidate_calls"],
        reflector_calls=calls["reflector_calls"],
        core_jobs=calls["core_jobs"],
        failures=calls["failures"],
        retries=calls["retries"],
        rejected=calls["rejected"],
        recoveries=calls["recoveries"],
    )


def _write_phase_a(staging: Path, outcome: CampaignOutcomeV2) -> None:
    rows = []
    flips = []
    for value in outcome.phase_a.arm_metrics:
        paired = value["paired_vs_a0"]
        rows.append(
            {
                "arm_id": value["arm_id"],
                "targets": "+".join(value["target_ids"]) or "generation_zero",
                "correct": value["correct"],
                "n": value["n"],
                "accuracy_percent": 100 * float(value["accuracy"]),
                "delta_pp": paired["delta_percentage_points"],
                "positive_flips": value["positive_flips"],
                "negative_flips": value["negative_flips"],
                "utility": value["utility"],
                "mcnemar_exact_p": paired["mcnemar_exact_p"],
                "bootstrap_low_pp": paired["paired_bootstrap_delta_95_percentage_points"][0],
                "bootstrap_high_pp": paired["paired_bootstrap_delta_95_percentage_points"][1],
                "parser_success": value["parser_success"],
                "retries": value["retries"],
                "failures": value["failures"],
                "injected_bytes": value["injected_bytes_per_question"],
                "h0_correct": value["h0_correct"],
                "h0_delta_pp": value["h0_delta_pp"],
                "h0_utility": value["h0_utility"],
                "h1_correct": value["h1_correct"],
                "h1_delta_pp": value["h1_delta_pp"],
                "h1_utility": value["h1_utility"],
            }
        )
        flips.append(
            {
                "arm_id": value["arm_id"],
                "both_correct": paired["both_correct"],
                "a0_only_correct": paired["reference_only_correct"],
                "arm_only_correct": paired["candidate_only_correct"],
                "both_wrong": paired["both_wrong"],
                "positive_flips": value["positive_flips"],
                "negative_flips": value["negative_flips"],
                "utility": value["utility"],
            }
        )
    _write_csv(staging / "phase_a_arm_metrics.csv", rows)
    _write_csv(staging / "phase_a_paired_flips.csv", flips)
    _write_json(staging / "phase_a_factorial_effects.json", outcome.phase_a.factorial_effects)
    _write_json(
        staging / "phase_a_target_selection_receipt.json",
        outcome.phase_a.target_selection_receipt,
    )
    selection = outcome.phase_a.target_selection_receipt
    text = [
        "# Phase A: Frozen C4 2^3 Ablation",
        "",
        (
            "Phase A reused the prior formal 100-item Test solely for mechanism diagnosis. It is not "
            "new generalization evidence."
        ),
        "",
        "| Arm | Correct | Accuracy | Positive | Negative | Utility |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    text.extend(
        f"| {row['arm_id']} | {row['correct']}/100 | {row['accuracy_percent']:.2f}% | "
        f"{row['positive_flips']} | {row['negative_flips']} | {row['utility']} |"
        for row in rows
    )
    text.extend(
        (
            "",
            f"Selection status: `{selection['status']}`",
            "",
            f"Selected target: `{selection['selected_target']}`",
            "",
        )
    )
    _write_text(staging / "PHASE_A_REPORT.md", "\n".join(text))


def _write_fold_tables(
    staging: Path,
    outcome: CampaignOutcomeV2,
    folds: tuple[dict[str, Any], ...],
) -> None:
    promotions: list[dict[str, object]] = []
    retirements: list[dict[str, object]] = []
    retrieval_rows: list[dict[str, object]] = []
    tests: list[dict[str, object]] = []
    for fold_outcome, private in zip(outcome.folds, folds, strict=True):
        for decision in private["promotion_history"]:
            forward = decision["forward_metrics"]
            promotions.append(
                {
                    "fold_id": fold_outcome.fold_id,
                    "block_id": decision["block_id"],
                    "promoted": decision["promoted"],
                    "reasons": ";".join(decision["reasons"]),
                    "incremental_positive": forward["incremental_positive"],
                    "incremental_negative": forward["incremental_negative"],
                    "incremental_utility": forward["incremental_utility"],
                    "baseline_positive": forward["baseline_positive"],
                    "baseline_negative": forward["baseline_negative"],
                    "baseline_utility": forward["baseline_utility"],
                }
            )
        for candidate in private["cumulative_evidence"]["candidate_history"]:
            statuses = [entry["status"] for entry in candidate["entries"]]
            retirements.append(
                {
                    "fold_id": fold_outcome.fold_id,
                    "batch_index": candidate["batch_index"],
                    "active_entries": statuses.count("active"),
                    "provisional_entries": statuses.count("provisional"),
                    "retired_entries": statuses.count("retired"),
                }
            )
        retrieval_rows.extend(_fold_retrieval_rows(fold_outcome.private_result_path.parent))
        raw = fold_outcome.test_metrics["raw_vs_g0"]
        deployed = fold_outcome.test_metrics["deployed_vs_g0"]
        tests.append(
            {
                "fold_id": fold_outcome.fold_id,
                "g0_correct": fold_outcome.test_metrics["g0_correct"],
                "raw_correct": fold_outcome.test_metrics["raw_correct"],
                "deployed_correct": fold_outcome.test_metrics["deployed_correct"],
                "n": 50,
                "raw_delta_pp": raw["delta_percentage_points"],
                "deployed_delta_pp": deployed["delta_percentage_points"],
                "positive_flips": fold_outcome.test_metrics["positive_flips"],
                "negative_flips": fold_outcome.test_metrics["negative_flips"],
                "utility": fold_outcome.test_metrics["utility"],
                "mcnemar_exact_p": raw["mcnemar_exact_p"],
                "bootstrap_low_pp": raw["paired_bootstrap_delta_95_percentage_points"][0],
                "bootstrap_high_pp": raw["paired_bootstrap_delta_95_percentage_points"][1],
                "promotion_count": fold_outcome.promotion_count,
                "retirement_count": fold_outcome.retirement_count,
                "v2_gate": "EVOLVED" if fold_outcome.deployment_real_evolved else "G0_FALLBACK",
            }
        )
    _write_csv(staging / "promotion_decisions.csv", promotions)
    _write_csv(staging / "retirement_decisions.csv", retirements)
    _write_csv(staging / "retrieval_metrics.csv", retrieval_rows)
    _write_csv(staging / "block_forward_metrics.csv", promotions)
    _write_csv(staging / "fold_test_metrics.csv", tests)


def _fold_retrieval_rows(private_root: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    fold_id = private_root.parent.name.upper()
    for path in sorted((private_root / "blocks").glob("*.json")):
        value = _load_json(path)
        for arm, arm_value in value["arms"].items():
            receipts = [item for item in arm_value["retrieval"] if item is not None]
            injected = [int(item["injected_utf8_bytes"]) for item in receipts]
            selected = [len(item["selected_entry_ids"]) for item in receipts]
            rows.append(
                {
                    "fold_id": fold_id,
                    "block": path.stem,
                    "arm": arm,
                    "question_count": len(arm_value["retrieval"]),
                    "retrieval_receipt_count": len(receipts),
                    "payload_nonempty_count": sum(value > 0 for value in injected),
                    "coverage": (
                        0.0
                        if not receipts
                        else sum(value > 0 for value in injected) / len(receipts)
                    ),
                    "mean_injected_bytes": (
                        0.0 if not injected else sum(injected) / len(injected)
                    ),
                    "max_injected_bytes": max(injected, default=0),
                    "mean_selected_entries": (
                        0.0 if not selected else sum(selected) / len(selected)
                    ),
                }
            )
    return rows


def _pooled_metrics(folds: tuple[dict[str, Any], ...]) -> dict[str, object]:
    if not folds:
        return {
            "schema_version": "TemperatureSafePooledPairedComparisonV2",
            "status": "NOT_RUN_BY_PREREGISTERED_STOP_RULE",
            "completed_folds": 0,
        }
    g0 = tuple(bool(item) for fold in folds for item in fold["test"]["g0_correctness"])
    raw = tuple(bool(item) for fold in folds for item in fold["test"]["raw_correctness"])
    deployed = tuple(bool(item) for fold in folds for item in fold["test"]["deployed_correctness"])
    raw_metrics = paired_binary_metrics_v1(g0, raw, bootstrap_seed=20260803)
    deployed_metrics = paired_binary_metrics_v1(g0, deployed, bootstrap_seed=20260803)
    return {
        "schema_version": "TemperatureSafePooledPairedComparisonV2",
        "status": "FOUR_FOLD_POOLED" if len(folds) == 4 else "R0_PILOT_ONLY",
        "completed_folds": len(folds),
        "n": len(g0),
        "g0_correct": sum(g0),
        "raw_evolved_correct": sum(raw),
        "deployment_aware_correct": sum(deployed),
        "raw_vs_g0": raw_metrics.to_dict(),
        "deployment_aware_vs_g0": deployed_metrics.to_dict(),
        "positive_flips": raw_metrics.candidate_only_correct,
        "negative_flips": raw_metrics.reference_only_correct,
        "utility": raw_metrics.candidate_only_correct - 2 * raw_metrics.reference_only_correct,
        "repeated_pool_out_of_fold_mechanism_evidence": len(folds) == 4,
        "external_independent_confirmation": False,
    }


def _aggregate_calls(outcome: CampaignOutcomeV2) -> dict[str, int]:
    ledgers = sorted(outcome.campaign_root.glob("phase_a/*/private/events.jsonl"))
    ledgers.extend(sorted(outcome.campaign_root.glob("folds/*/private/events.jsonl")))
    totals = {
        "candidate_calls": 0,
        "reflector_calls": 0,
        "core_jobs": 0,
        "failures": 0,
        "retries": 0,
        "rejected": 0,
        "recoveries": 0,
    }
    for path in ledgers:
        events = [json.loads(line) for line in path.read_text().splitlines() if line]
        claim_arm = {
            event["payload"]["logical_call_id"]: event["payload"]["logical_arm"]
            for event in events
            if event["kind"] == "CALL_CLAIMED"
        }
        attempts: dict[str, int] = {}
        for event in events:
            kind = event["kind"]
            logical = event["payload"].get("logical_call_id")
            if kind == "CALL_CLAIMED":
                attempts[str(logical)] = attempts.get(str(logical), 0) + 1
            elif kind == "CALL_ACCEPTED":
                if claim_arm.get(logical) == "safe_reflector":
                    totals["reflector_calls"] += 1
                else:
                    totals["candidate_calls"] += 1
            elif kind == "CORE_JOB_COMPLETED":
                totals["core_jobs"] += 1
            elif kind == "CALL_NO_COMPLETION_FAILURE":
                totals["failures"] += 1
            elif kind == "CALL_REJECTED_COMPLETION":
                totals["rejected"] += 1
            elif kind == "RECOVERY_COMPLETED":
                totals["recoveries"] += 1
        totals["retries"] += sum(max(0, count - 1) for count in attempts.values())
    return totals


def _write_reports(
    *,
    staging: Path,
    outcome: CampaignOutcomeV2,
    pooled: dict[str, object],
    calls: dict[str, int],
    fold_manifest: dict[str, object],
    near_duplicate: dict[str, object],
    runtime_receipt: dict[str, object],
) -> None:
    fold_by_id = {value.fold_id: value for value in outcome.folds}
    for fold_id in ("R0", "R1", "R2", "R3"):
        value = fold_by_id.get(fold_id)
        if value is None:
            body = f"# {fold_id} Report\n\nStatus: `NOT_RUN_BY_PREREGISTERED_STOP_RULE`\n"
        else:
            metrics = value.test_metrics
            body = "\n".join(
                (
                    f"# {fold_id} Report",
                    "",
                    f"Promotions: {value.promotion_count}",
                    f"Retirements: {value.retirement_count}",
                    f"V2 deployment: {'evolved' if value.deployment_real_evolved else 'G0 fallback'}",
                    f"G0: {metrics['g0_correct']}/50",
                    f"Raw evolved: {metrics['raw_correct']}/50",
                    f"Deployed: {metrics['deployed_correct']}/50",
                    f"Raw delta: {metrics['raw_vs_g0']['delta_percentage_points']:+.2f} pp",
                    f"Positive/negative flips: {metrics['positive_flips']}/{metrics['negative_flips']}",
                    f"McNemar exact p: {metrics['raw_vs_g0']['mcnemar_exact_p']}",
                    "",
                    (
                        "Validation and Test GT were sealed until all required arms closed. No "
                        "Reflector, Core job, or artifact update occurred after final freeze."
                    ),
                    "",
                )
            )
        _write_text(staging / f"{fold_id}_REPORT.md", body)

    if pooled.get("n"):
        raw = pooled["raw_vs_g0"]
        summary = (
            f"Pooled completed-fold G0/raw/deployed: {pooled['g0_correct']}/{pooled['raw_evolved_correct']}/"
            f"{pooled['deployment_aware_correct']} of {pooled['n']}. Raw delta "
            f"{raw['delta_percentage_points']:+.2f} pp; positive/negative flips "
            f"{pooled['positive_flips']}/{pooled['negative_flips']}."
        )
    else:
        summary = "Safe-Evolve fold execution was not entered because Phase A hit its preregistered stop rule."
    final = "\n".join(
        (
            "# ChemBench Temperature Safe-Evolve V2 Final Report",
            "",
            f"Final status: `{outcome.status}`",
            "",
            f"Selected evolvable target: `{outcome.selected_target}`",
            "",
            summary,
            "",
            (
                "This campaign reuses the official Temperature_Prediction pool. Each run has fresh "
                "state, database, artifact lineage and managed model calls, and Train/Validation/Test "
                "are isolated within that run. It does not establish historical never-exposure or "
                "external independent generalization."
            ),
            "",
            (
                "Post-evolution Train or forward-block behavior is diagnostic; the primary mechanism "
                "comparison is frozen raw evolved versus same-run G0 on each Test fold."
            ),
            "",
        )
    )
    _write_text(staging / "FINAL_REPORT.md", final)
    review = _postrun_review(outcome, pooled)
    _write_text(staging / "POSTRUN_REVIEW.md", review)
    handoff = "\n".join(
        (
            "# Handoff",
            "",
            f"- Status: `{outcome.status}`",
            f"- Campaign: `{outcome.campaign_run_id}`",
            f"- Selected target: `{outcome.selected_target}`",
            f"- Completed folds: {len(outcome.folds)}",
            f"- Fold SHA-256: `{fold_manifest['fold_sha256']}`",
            f"- Group manifest SHA-256: `{near_duplicate['group_manifest_sha256']}`",
            f"- Prior runtime-v8 compatibility identity: `{runtime_receipt['prior_runtime_v8']['runtime_v8_identity_sha256']}`",
            f"- Candidate/Reflector/Core calls: {calls['candidate_calls']}/{calls['reflector_calls']}/{calls['core_jobs']}",
            f"- Failures/retries/rejections/recoveries: {calls['failures']}/{calls['retries']}/{calls['rejected']}/{calls['recoveries']}",
            "",
            (
                "All item-level prompts, options, labels, predictions, completions, transcripts, UIDs "
                "and databases remain only in the owner-private run evidence tree."
            ),
            "",
        )
    )
    _write_text(staging / "HANDOFF.md", handoff)


def _postrun_review(outcome: CampaignOutcomeV2, pooled: dict[str, object]) -> str:
    lines = ["# Post-run Review", ""]
    if outcome.status == "NO_GO_ABLATION_NO_SAFE_SINGLE_TARGET":
        lines.extend(
            (
                (
                    "Neither frozen memory-only nor skill-only passed the preregistered loss-sensitive "
                    "ablation gate. Continuing to evolve either target would have contradicted the "
                    "campaign's safety premise, so R0-R3 were correctly not run."
                ),
                "",
                (
                    "The actionable conclusion is architectural: first reduce the harmful component or "
                    "its retrieval scope using Train-only development evidence, then preregister a new "
                    "ablation. Do not tune the current thresholds or reuse these Test outcomes to rewrite "
                    "rules and claim confirmation."
                ),
            )
        )
    elif outcome.status == "NO_GO_R0_SAFE_EVOLVE_PILOT_FAILED":
        lines.extend(
            (
                (
                    "R0 failed at least one preregistered continuation condition. The forward/deployment "
                    "gates therefore prevented the campaign from scaling a mechanism that did not show "
                    "safe Test behavior in the pilot."
                ),
                "",
                (
                    "Inspect aggregate promotion rejection reasons, retrieval coverage and entry-status "
                    "curves. Any semantic redesign requires a new generation-zero campaign and new run ID."
                ),
            )
        )
    elif outcome.status == "COMPLETE_GO_MECHANISM_SIGNAL":
        lines.extend(
            (
                (
                    "The repeated official pool shows a preregistered positive mechanism signal. This is "
                    "not external confirmation because each item appears as Test in one run and as Train "
                    "or Validation in another independent run."
                ),
                "",
                (
                    "Next, freeze the mechanism and evaluate once on a genuinely new Temperature set with "
                    "no prior exposure or threshold adjustment."
                ),
            )
        )
    elif outcome.status == "COMPLETE_SAFE_FALLBACK_ONLY":
        lines.extend(
            (
                (
                    "The deployment-aware system avoided aggregate loss, but raw evolution did not meet "
                    "the GO standard and most folds used G0 fallback. The gates provided protection; "
                    "positive evolution value remains unproven."
                ),
            )
        )
    else:
        lines.extend(
            (
                (
                    "Raw evolution failed the fixed Safe-Evolve V2 criteria. Preserve this result and use "
                    "only new Train-side evidence for any subsequent mechanism redesign."
                ),
            )
        )
    if pooled.get("n"):
        lines.extend(
            (
                "",
                f"Completed-fold raw delta: {pooled['raw_vs_g0']['delta_percentage_points']:+.2f} pp.",
                f"Positive/negative flips: {pooled['positive_flips']}/{pooled['negative_flips']}.",
            )
        )
    lines.append("")
    return "\n".join(lines)


def _write_compatibility(
    *,
    staging: Path,
    outcome: CampaignOutcomeV2,
    runtime_receipt: dict[str, object],
    model_receipt: dict[str, object],
    source_receipt: dict[str, object],
) -> None:
    _write_json(
        staging / "COMPATIBILITY_RECEIPT.json",
        {
            "schema_version": "TemperatureSafeCompatibilityReceiptV2",
            "status": "PASS",
            "campaign_run_id": outcome.campaign_run_id,
            "source_commit": source_receipt["source_commit"],
            "prior_runtime_v8_identity_sha256": runtime_receipt["prior_runtime_v8"][
                "runtime_v8_identity_sha256"
            ],
            "prior_runtime_v8_used_as_compatibility_reference": True,
            "prior_runtime_v8_reused_as_mutable_v2_runtime": False,
            "v2_source_bound_formal_runtime_identity_sha256": runtime_receipt[
                "v2_formal_runtime_identity_sha256"
            ],
            "managed_codex_executable_sha256": model_receipt["candidate_codex_executable_sha256"],
            "model": model_receipt["model"],
            "reasoning_effort": model_receipt["reasoning_effort"],
            "candidate_baseline_stack_equal": True,
            "parser_evaluator_equal": True,
            "timeout_retry_policy_equal": True,
            "semantic_repairs_between_test_arms": 0,
            "phase_a_runtime_services_run_id": runtime_receipt[
                "phase_a_runtime_services_run_id"
            ],
            "fold_runtime_and_core_identities": [
                {
                    "fold_id": value.fold_id,
                    "runtime_services_run_id": value.runtime_services_run_id,
                    "runtime_services_identity_sha256": (
                        value.runtime_services_identity_sha256
                    ),
                    "core_store_id": value.core_store_id,
                }
                for value in outcome.folds
            ],
            "distinct_runtime_services_identity_per_fold": True,
            "distinct_core_store_identity_per_fold": True,
        },
    )


def _write_charts(
    *,
    staging: Path,
    outcome: CampaignOutcomeV2,
    folds: tuple[dict[str, Any], ...],
    pooled: dict[str, object],
) -> None:
    phase_rows = list(outcome.phase_a.arm_metrics)
    _bar_svg(
        staging / "chart_phase_a_accuracy.svg",
        "Phase A accuracy (%)",
        [str(value["arm_id"]) for value in phase_rows],
        [100 * float(value["accuracy"]) for value in phase_rows],
    )
    _two_bar_svg(
        staging / "chart_phase_a_flips.svg",
        "Phase A paired flips",
        [str(value["arm_id"]) for value in phase_rows],
        [float(value["positive_flips"]) for value in phase_rows],
        [float(value["negative_flips"]) for value in phase_rows],
        "positive",
        "negative",
    )
    effects = {
        key: float(value)
        for key, value in outcome.phase_a.factorial_effects.items()
        if key.endswith("_pp")
    }
    _bar_svg(
        staging / "chart_phase_a_factorial_effects.svg",
        "Phase A factorial effects (pp)",
        list(effects),
        list(effects.values()),
    )
    fold_labels = [value.fold_id for value in outcome.folds] or ["not-run"]
    utilities = [
        float(
            sum(
                item["forward_metrics"]["incremental_utility"]
                for item in fold["promotion_history"]
            )
        )
        for fold in folds
    ] or [0.0]
    _bar_svg(
        staging / "chart_fold_forward_utility.svg",
        "Fold forward utility",
        fold_labels,
        utilities,
    )
    _two_bar_svg(
        staging / "chart_fold_promotions_rejections.svg",
        "Fold promotions and rejections",
        fold_labels,
        [float(value.promotion_count) for value in outcome.folds] or [0.0],
        [float(4 - value.promotion_count) for value in outcome.folds] or [0.0],
        "promoted",
        "rejected",
    )
    entry_labels: list[str] = []
    active_entries: list[float] = []
    provisional_entries: list[float] = []
    retired_entries: list[float] = []
    for fold_outcome, private in zip(outcome.folds, folds, strict=True):
        for candidate in private["cumulative_evidence"]["candidate_history"]:
            statuses = [entry["status"] for entry in candidate["entries"]]
            entry_labels.append(f"{fold_outcome.fold_id}-B{candidate['batch_index']}")
            active_entries.append(float(statuses.count("active")))
            provisional_entries.append(float(statuses.count("provisional")))
            retired_entries.append(float(statuses.count("retired")))
    _three_bar_svg(
        staging / "chart_entry_status.svg",
        "Active, provisional and retired entries by candidate",
        entry_labels or ["not-run"],
        active_entries or [0.0],
        provisional_entries or [0.0],
        retired_entries or [0.0],
        "active",
        "provisional",
        "retired",
    )
    retrieval = [
        value
        for value in _read_csv(staging / "retrieval_metrics.csv")
        if "mean_injected_bytes" in value and "coverage" in value
    ]
    _bar_svg(
        staging / "chart_injected_bytes.svg",
        "Mean injected bytes by recorded arm",
        [str(index + 1) for index in range(len(retrieval))] or ["not-run"],
        [float(value["mean_injected_bytes"]) for value in retrieval] or [0.0],
    )
    _bar_svg(
        staging / "chart_retrieval_coverage.svg",
        "Retrieval coverage (%)",
        [str(index + 1) for index in range(len(retrieval))] or ["not-run"],
        [100 * float(value["coverage"]) for value in retrieval] or [0.0],
    )
    _two_bar_svg(
        staging / "chart_fold_test_accuracy.svg",
        "Fold Test G0 vs raw evolved",
        fold_labels,
        [2 * float(value.test_metrics["g0_correct"]) for value in outcome.folds] or [0.0],
        [2 * float(value.test_metrics["raw_correct"]) for value in outcome.folds] or [0.0],
        "G0",
        "raw evolved",
    )
    _two_bar_svg(
        staging / "chart_pooled_paired_flips.svg",
        "Pooled paired flips",
        ["completed folds"],
        [float(pooled.get("positive_flips", 0))],
        [float(pooled.get("negative_flips", 0))],
        "positive",
        "negative",
    )
    _bar_svg(
        staging / "chart_raw_vs_deployment_aware.svg",
        "Raw and deployment-aware delta (pp)",
        ["raw", "deployed"],
        [
            float(pooled.get("raw_vs_g0", {}).get("delta_percentage_points", 0)),
            float(pooled.get("deployment_aware_vs_g0", {}).get("delta_percentage_points", 0)),
        ],
    )


def _bar_svg(path: Path, title: str, labels: list[str], values: list[float]) -> None:
    _two_bar_svg(path, title, labels, values, [], "value", "")


def _three_bar_svg(
    path: Path,
    title: str,
    labels: list[str],
    first: list[float],
    second: list[float],
    third: list[float],
    first_name: str,
    second_name: str,
    third_name: str,
) -> None:
    if not (len(labels) == len(first) == len(second) == len(third)):
        raise SafeReportError("SAFE_REPORT_CHART_SERIES_INVALID")
    width = 1000
    height = 430
    left = 70
    bottom = 350
    maximum = max([abs(value) for value in (*first, *second, *third)], default=1.0) or 1.0
    scale = 260 / maximum
    group = (width - left - 30) / max(1, len(labels))
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{width / 2}" y="28" text-anchor="middle" font-family="sans-serif" font-size="18">{html.escape(title)}</text>',
        f'<line x1="{left}" y1="{bottom}" x2="{width - 20}" y2="{bottom}" stroke="#333"/>',
    ]
    colors = ("#3569b7", "#d6922e", "#777777")
    for index, label in enumerate(labels):
        center = left + group * (index + 0.5)
        values = (first[index], second[index], third[index])
        bar_width = min(24.0, group / 4)
        for offset, (value, color) in enumerate(zip(values, colors, strict=True)):
            x = center + (offset - 1) * (bar_width + 2) - bar_width / 2
            bar_height = abs(value) * scale
            y = bottom - bar_height if value >= 0 else bottom
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" fill="{color}"/>'
            )
        parts.append(
            f'<text x="{center:.1f}" y="{bottom + 20}" text-anchor="middle" font-family="sans-serif" font-size="9">{html.escape(label[:20])}</text>'
        )
    for offset, (name, color) in enumerate(
        zip((first_name, second_name, third_name), colors, strict=True)
    ):
        parts.append(
            f'<text x="{left + 120 * offset}" y="{height - 18}" font-family="sans-serif" font-size="11" fill="{color}">{html.escape(name)}</text>'
        )
    parts.append("</svg>")
    _write_text(path, "\n".join(parts) + "\n")


def _two_bar_svg(
    path: Path,
    title: str,
    labels: list[str],
    first: list[float],
    second: list[float],
    first_name: str,
    second_name: str,
) -> None:
    width = 1000
    height = 430
    left = 70
    bottom = 350
    maximum = max([abs(value) for value in (*first, *second)], default=1.0) or 1.0
    scale = 260 / maximum
    group = (width - left - 30) / max(1, len(labels))
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{width / 2}" y="28" text-anchor="middle" font-family="sans-serif" font-size="18">{html.escape(title)}</text>',
        f'<line x1="{left}" y1="{bottom}" x2="{width - 20}" y2="{bottom}" stroke="#333"/>',
    ]
    for index, label in enumerate(labels):
        center = left + group * (index + 0.5)
        values = ((first[index], "#3569b7"),)
        if second:
            values = (*values, (second[index], "#c94b4b"))
        bar_width = min(34.0, group / (len(values) + 1))
        for offset, (value, color) in enumerate(values):
            x = center + (offset - (len(values) - 1) / 2) * (bar_width + 3) - bar_width / 2
            bar_height = abs(value) * scale
            y = bottom - bar_height if value >= 0 else bottom
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" fill="{color}"/>'
            )
            parts.append(
                f'<text x="{x + bar_width / 2:.1f}" y="{y - 5 if value >= 0 else y + bar_height + 14:.1f}" text-anchor="middle" font-family="sans-serif" font-size="10">{value:.2f}</text>'
            )
        parts.append(
            f'<text x="{center:.1f}" y="{bottom + 20}" text-anchor="middle" font-family="sans-serif" font-size="10">{html.escape(label[:20])}</text>'
        )
    parts.append(
        f'<text x="{left}" y="{height - 18}" font-family="sans-serif" font-size="11" fill="#3569b7">{html.escape(first_name)}</text>'
    )
    if second:
        parts.append(
            f'<text x="{left + 120}" y="{height - 18}" font-family="sans-serif" font-size="11" fill="#c94b4b">{html.escape(second_name)}</text>'
        )
    parts.append("</svg>")
    _write_text(path, "\n".join(parts) + "\n")


def _validate_public_boundary(root: Path) -> None:
    forbidden = (
        '"uids"',
        '"uid"',
        '"question"',
        '"options"',
        '"ground_truth"',
        '"correctness"',
        '"predictions"',
        '"completion"',
        '"transcripts"',
        '"raw_transcript"',
    )
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix == ".svg":
            continue
        text = path.read_text(encoding="utf-8")
        if any(token in text for token in forbidden):
            raise SafeReportError("SAFE_REPORT_PRIVATE_FIELD_LEAKAGE")


def _write_checksums(root: Path) -> dict[str, str]:
    sums = {
        path.relative_to(root).as_posix(): _file_sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "SHA256SUMS.txt"
    }
    payload = "".join(f"{digest}  {name}\n" for name, digest in sums.items()).encode()
    _write_exact(root / "SHA256SUMS.txt", payload)
    return sums


def _publish_exact_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        _verify_same_tree(source, destination)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.tmp-{os.getpid()}"
    if temporary.exists():
        raise SafeReportError("SAFE_REPORT_TEMPORARY_COLLISION")
    shutil.copytree(source, temporary)
    os.replace(temporary, destination)


def _verify_same_tree(left: Path, right: Path) -> None:
    left_files = {path.relative_to(left) for path in left.rglob("*") if path.is_file()}
    right_files = {path.relative_to(right) for path in right.rglob("*") if path.is_file()}
    if left_files != right_files or any(
        (left / relative).read_bytes() != (right / relative).read_bytes()
        for relative in left_files
    ):
        raise SafeReportError("SAFE_REPORT_PUBLICATION_DRIFT")


def _verify_tree(root: Path, sums: dict[str, str]) -> None:
    for relative, expected in sums.items():
        if _file_sha256(root / relative) != expected:
            raise SafeReportError("SAFE_REPORT_READBACK_FAILED")


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if rows:
        fields = list(rows[0])
    else:
        fields = ["status"]
        rows = [{"status": "NOT_RUN_BY_PREREGISTERED_STOP_RULE"}]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    _write_text(path, stream.getvalue())


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _write_json(path: Path, value: object) -> None:
    _write_exact(path, canonical_pretty_json_bytes(value))


def _write_text(path: Path, value: str) -> None:
    _write_exact(path, value.encode("utf-8"))


def _write_exact(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.read_bytes() != payload:
            raise SafeReportError("SAFE_REPORT_STAGING_DRIFT")
        return
    write_private_file(path, payload, replace=False)


def _copy_exact(source: Path, destination: Path) -> None:
    _write_exact(destination, source.read_bytes())


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SafeReportError("SAFE_REPORT_INPUT_INVALID") from exc
    if type(value) is not dict:
        raise SafeReportError("SAFE_REPORT_INPUT_INVALID")
    return value


def _require_private_directory(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
    ):
        raise SafeReportError("SAFE_REPORT_STAGING_UNSAFE")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "SafeReportError",
    "SafeReportPublicationV2",
    "generate_campaign_report_v2",
]
