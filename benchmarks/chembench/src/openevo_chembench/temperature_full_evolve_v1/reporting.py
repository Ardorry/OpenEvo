"""Aggregate-only hard-stop report package for Temperature full-evolve v1."""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping
from pathlib import Path

from openevo_chembench.supervised_transfer_v1.common import (
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_public_file,
)
from openevo_chembench.temperature_full_evolve_v1.capacity_preflight import (
    FINDING_CODE,
    PROTOCOL_ID,
    build_capacity_preflight,
)

REPORT_SCHEMA = "TemperatureFullEvolveBlockedReportPackageV1"


def write_blocked_report_package(
    *,
    repository_root: Path,
    destination: Path,
    audit_id: str,
    generated_at_utc: str,
    source_code_commit: str,
    branch: str,
    model_identity: Mapping[str, object],
    managed_runtime_identity: Mapping[str, object],
) -> dict[str, object]:
    """Write the closed aggregate package after the capacity gate blocks launch."""

    repository = repository_root.resolve(strict=True)
    receipt = build_capacity_preflight(repository)
    if receipt["status"] != "BLOCKED" or receipt["finding_code"] != FINDING_CODE:
        raise RuntimeError("BLOCKED_REPORT_REQUIRES_FAILED_CAPACITY_GATE")
    if receipt["side_effects"]["total_model_calls"] != 0:
        raise RuntimeError("BLOCKED_REPORT_REQUIRES_ZERO_MODEL_CALLS")
    if destination.exists():
        raise FileExistsError(destination)
    destination.mkdir(parents=True, mode=0o755)

    protocol_source = (
        repository
        / "benchmarks/chembench/configs/temperature_full_evolve_v1/EXPERIMENT_PROTOCOL.md"
    ).read_bytes()
    write_public_file(destination / "EXPERIMENT_PROTOCOL.md", protocol_source)

    historical = receipt["historical_exposure"]
    capacity = receipt["capacity_gate"]
    partition = receipt["v2_partition_crosscheck"]
    near_duplicate = receipt["near_duplicate_precheck"]

    historical_manifest = {
        "schema_version": "TemperatureHistoricalExclusionAggregateV1",
        "status": "CLOSED_FOR_CAPACITY_HARD_STOP",
        "category": "Temperature_Prediction",
        "taxonomy_actual_labels": historical["taxonomy_actual_labels"],
        "prior_snapshot_actual_count": historical["prior_snapshot_actual_count"],
        "current_actual_exposed_union_count": historical["actual_exposed_union_count"],
        "actual_exposed_uid_set_sha256": historical["actual_exposed_uid_set_sha256"],
        "maximum_remaining_count": capacity[
            "maximum_provable_never_exposed_count_before_near_duplicate_grouping"
        ],
        "maximum_remaining_uid_set_sha256": capacity[
            "maximum_provable_never_exposed_uid_set_sha256"
        ],
        "event_scan": historical["event_scan"],
        "v2_partition_crosscheck": partition,
        "contains_uid_values": False,
        "contains_private_item_content": False,
    }
    _write_json(destination / "historical_exclusion_manifest.json", historical_manifest)

    split_manifest = {
        "schema_version": "TemperatureFullEvolveSplitManifestV1",
        "status": "NOT_CREATED",
        "finding_code": FINDING_CODE,
        "train_count": 0,
        "test_count": 0,
        "train_uid_order_sha256": None,
        "test_uid_order_sha256": None,
        "split_sha256": None,
        "reason": "The capacity gate failed before deterministic split generation.",
    }
    _write_json(destination / "split_manifest.json", split_manifest)

    duplicate_manifest = {
        "schema_version": "TemperatureNearDuplicateGroupAggregateV1",
        "status": "NOT_USED_FOR_SPLIT",
        "finding_code": FINDING_CODE,
        **near_duplicate,
        "contains_group_members": False,
    }
    _write_json(destination / "near_duplicate_group_manifest.json", duplicate_manifest)

    config_manifest = {
        "schema_version": "TemperatureFullEvolveConfigIntentV1",
        "status": "NOT_FROZEN_FOR_FORMAL_RUN",
        "protocol_id": PROTOCOL_ID,
        "audit_id": audit_id,
        "source_code_commit": source_code_commit,
        "branch": branch,
        "model": "gpt-5.5",
        "reasoning_effort": "medium",
        "batch_size": 25,
        "targets": ["text_memory", "skill_bundle", "agent_system"],
        "target_context_budget_bytes": 6144,
        "hard_context_budget_bytes": 8192,
        "candidate_tool_policy": "zero_tool_transcript_audit",
        "feedback_on_test": False,
        "model_calls_before_gate": 0,
        "finding_code": FINDING_CODE,
    }
    _write_json(destination / "config_manifest.json", config_manifest)

    _write_json(destination / "model_identity_receipt.json", dict(model_identity))
    _write_json(destination / "managed_runtime_receipt.json", dict(managed_runtime_identity))
    _write_json(destination / "capacity_preflight_receipt.json", receipt)

    not_run = {
        "status": "NOT_RUN",
        "finding_code": FINDING_CODE,
        "N": 0,
        "accepted_completion_count": 0,
        "parser_success_count": 0,
        "model_call_count": 0,
        "reflector_call_count": 0,
        "core_job_count": 0,
    }
    _write_json(destination / "evolved_test_metrics.json", not_run)
    _write_json(destination / "baseline_test_metrics.json", not_run)
    _write_json(
        destination / "paired_test_comparison.json",
        {
            **not_run,
            "baseline_correct": None,
            "evolved_correct": None,
            "paired_delta_percentage_points": None,
            "both_correct": None,
            "baseline_only_correct": None,
            "evolved_only_correct": None,
            "both_wrong": None,
            "mcnemar_exact_p": None,
            "paired_95_percent_ci": None,
        },
    )
    _write_json(
        destination / "call_and_failure_summary.json",
        {
            "schema_version": "TemperatureFullEvolveCallSummaryV1",
            "status": "ZERO_CALL_HARD_STOP",
            "candidate_calls": 0,
            "reflector_calls": 0,
            "core_jobs": 0,
            "baseline_calls": 0,
            "failed_attempts": 0,
            "retries": 0,
            "recoveries": 0,
        },
    )

    _write_csv(
        destination / "train_batch_metrics.csv",
        (
            "status",
            "finding_code",
            "batch_index",
            "pre_correct",
            "post_correct",
            "delta_percentage_points",
            "mcnemar_exact_p",
        ),
        (("NOT_RUN", FINDING_CODE, "", "", "", "", ""),),
    )
    _write_csv(
        destination / "train_transition_metrics.csv",
        (
            "status",
            "finding_code",
            "batch_index",
            "both_correct",
            "pre_only_correct",
            "post_only_correct",
            "both_wrong",
        ),
        (("NOT_RUN", FINDING_CODE, "", "", "", "", ""),),
    )
    _write_csv(
        destination / "artifact_size_by_batch.csv",
        (
            "status",
            "finding_code",
            "batch_index",
            "text_memory_bytes",
            "skill_bundle_bytes",
            "agent_system_bytes",
            "combined_injected_bytes",
        ),
        (("NOT_RUN", FINDING_CODE, "", "", "", "", ""),),
    )
    _write_csv(
        destination / "rule_evidence_summary.csv",
        (
            "status",
            "finding_code",
            "batch_index",
            "active_rules",
            "supported_rules",
            "conflicted_rules",
            "retired_rules",
        ),
        (("NOT_RUN", FINDING_CODE, "", "", "", "", ""),),
    )

    implementation_audit = _implementation_audit_payload()
    _write_json(destination / "IMPLEMENTATION_AUDIT.json", implementation_audit)
    _write_json(
        destination / "chart_inventory.json",
        {
            "schema_version": "TemperatureFullEvolveChartInventoryV1",
            "generated": ["capacity_gate.svg"],
            "not_generated": [
                "train_batch_pre_post_accuracy",
                "train_cumulative_pre_post_curve",
                "train_correctness_transitions",
                "artifact_size_curve",
                "test_baseline_vs_evolved_accuracy",
                "test_paired_flips",
                "call_and_failure_chart",
            ],
            "reason": FINDING_CODE,
            "fabricated_zero-valued_result_charts": False,
        },
    )
    write_public_file(destination / "capacity_gate.svg", _capacity_svg(receipt).encode("utf-8"))

    precheck_markdown = _precheck_markdown(receipt, audit_id, generated_at_utc)
    final_markdown = _final_report_markdown(
        receipt=receipt,
        implementation_audit=implementation_audit,
        audit_id=audit_id,
        generated_at_utc=generated_at_utc,
        source_code_commit=source_code_commit,
        branch=branch,
        model_identity=model_identity,
        managed_runtime_identity=managed_runtime_identity,
    )
    handoff_markdown = _handoff_markdown(
        receipt=receipt,
        audit_id=audit_id,
        source_code_commit=source_code_commit,
        branch=branch,
    )
    write_public_file(destination / "PRECHECK_REPORT.md", precheck_markdown.encode("utf-8"))
    write_public_file(destination / "FINAL_REPORT.md", final_markdown.encode("utf-8"))
    write_public_file(destination / "HANDOFF.md", handoff_markdown.encode("utf-8"))

    expected_files = tuple(
        sorted(
            {
                "EXPERIMENT_PROTOCOL.md",
                "PRECHECK_REPORT.md",
                "split_manifest.json",
                "historical_exclusion_manifest.json",
                "near_duplicate_group_manifest.json",
                "config_manifest.json",
                "model_identity_receipt.json",
                "managed_runtime_receipt.json",
                "capacity_preflight_receipt.json",
                "train_batch_metrics.csv",
                "train_transition_metrics.csv",
                "artifact_size_by_batch.csv",
                "rule_evidence_summary.csv",
                "evolved_test_metrics.json",
                "baseline_test_metrics.json",
                "paired_test_comparison.json",
                "call_and_failure_summary.json",
                "IMPLEMENTATION_AUDIT.json",
                "chart_inventory.json",
                "capacity_gate.svg",
                "FINAL_REPORT.md",
                "HANDOFF.md",
                "package_manifest.json",
            }
        )
    )
    package_manifest = {
        "schema_version": REPORT_SCHEMA,
        "audit_id": audit_id,
        "generated_at_utc": generated_at_utc,
        "status": "HARD_BLOCKED",
        "finding_code": FINDING_CODE,
        "source_code_commit": source_code_commit,
        "branch": branch,
        "formal_run_ids": [],
        "expected_files_excluding_sha256sums": list(expected_files),
        "public_scope": "AGGREGATE_ONLY",
    }
    _write_json(destination / "package_manifest.json", package_manifest)

    actual_files = tuple(sorted(path.name for path in destination.iterdir() if path.is_file()))
    if actual_files != expected_files:
        raise RuntimeError("BLOCKED_REPORT_FILE_CLOSURE_INVALID")
    sums = "".join(
        f"{_file_sha256(destination / name)}  {name}\n" for name in expected_files
    ).encode("utf-8")
    write_public_file(destination / "SHA256SUMS.txt", sums)
    return {
        "schema_version": REPORT_SCHEMA,
        "status": "HARD_BLOCKED",
        "finding_code": FINDING_CODE,
        "audit_id": audit_id,
        "report_directory": destination.as_posix(),
        "source_code_commit": source_code_commit,
        "file_count": len(expected_files) + 1,
        "sha256sums_sha256": sha256_bytes(sums),
    }


def _implementation_audit_payload() -> dict[str, object]:
    return {
        "schema_version": "TemperatureFullEvolveImplementationAuditV1",
        "status": "NO_GO_IN_ADDITION_TO_DATA_GATE",
        "src_openevo_modification_required": False,
        "reusable": {
            "candidate_harness": "supervised_transfer_v2/executor.py",
            "core_planned_jobs_and_typed_artifacts": "supervised_transfer_v2/core.py",
            "reflector_schema_and_sandbox": "supervised_transfer_v2/reflector_boundary.py",
            "parser_and_evaluator": "chembench4k_evaluation.py",
            "paired_statistics": "paired_statistics_v2.py",
            "baseline_empty_context": "supervised_transfer_v3/composed_control_test_baseline.py",
        },
        "blocking_gaps_if_new_data_is_acquired": [
            "Current STV3 evolves per item instead of once per 25-item batch.",
            "Current formal V3 marks resume_allowed=false and lacks an exactly-once batch ledger.",
            "Reflector uses an attested managed CLI inside a Core job but not the formal TaskRequest-Rollout-Gateway-CodexHarness chain required here.",
            "Current artifact limits are materially above the requested combined 8 KiB hard limit and do not enforce cross-target responsibility separation.",
            "Current reporting can emit per-item CSV into the report root instead of a separate aggregate-only public package.",
            "No current runtime_services/current.json binds live services to this branch and commit.",
        ],
        "required_future_modules": [
            "closed Batch25 supervised packet",
            "canonical cumulative rule evidence index",
            "deterministic three-target projection and responsibility validator",
            "atomic batch state and exactly-once recovery ledger",
            "harness-backed Reflector provider",
            "combined serialized context budget validator",
            "600-second monitor and recovery supervisor",
            "aggregate-only paired reporting",
        ],
    }


def _precheck_markdown(receipt: Mapping[str, object], audit_id: str, generated_at_utc: str) -> str:
    dataset = receipt["dataset"]
    historical = receipt["historical_exposure"]
    capacity = receipt["capacity_gate"]
    return f"""# Temperature Full-Evolve v1 Precheck

- Audit ID: `{audit_id}`
- Generated: `{generated_at_utc}`
- Status: **{receipt["finding_code"]}**
- Model calls: **0**

The frozen Temperature test pool contains {dataset["temperature_test_count"]} items. The
current authoritative exposure closure contains {historical["actual_exposed_union_count"]}
items, leaving at most
{capacity["maximum_provable_never_exposed_count_before_near_duplicate_grouping"]} candidates
before semantic near-duplicate grouping. The protocol requires at least
{capacity["minimum_required_total"]} candidates for 100 Train + 100 Test, so the shortfall
is {capacity["capacity_shortfall_before_near_duplicate_grouping"]}.

The five dev items are not candidates because the official renderer injects them with
answers as category-local demonstrations. No split, run root, database, artifact lineage,
Core generation, tmux session, credential staging, or model call was created.

Exact normalized question/option checks found no literal duplicates. The dataset exposes no
reaction, paper, template, or source-group identifier; semantic grouping would only reduce
the eligible upper bound and cannot repair this capacity failure.
"""


def _final_report_markdown(
    *,
    receipt: Mapping[str, object],
    implementation_audit: Mapping[str, object],
    audit_id: str,
    generated_at_utc: str,
    source_code_commit: str,
    branch: str,
    model_identity: Mapping[str, object],
    managed_runtime_identity: Mapping[str, object],
) -> str:
    dataset = receipt["dataset"]
    historical = receipt["historical_exposure"]
    capacity = receipt["capacity_gate"]
    partition = receipt["v2_partition_crosscheck"]
    event_scan = historical["event_scan"]
    return f"""# ChemBench Temperature Full-Evolve v1 Final Report

## Decision

**HARD BLOCKED — `{FINDING_CODE}`**

This is the terminal, compliant result of the zero-model preflight. It is not an
incomplete formal experiment: the user-defined protocol explicitly forbids launch below
100 never-exposed Train + 100 never-exposed Test items.

## Capacity evidence

| Evidence | Count |
|---|---:|
| Frozen Temperature test items | {dataset["temperature_test_count"]} |
| Dev demonstrations, excluded | {dataset["temperature_dev_count"]} |
| Old authoritative snapshot actual exposure | {historical["prior_snapshot_actual_count"]} |
| Current actual-exposure union | {historical["actual_exposed_union_count"]} |
| Maximum provable never-exposed candidates | {capacity["maximum_provable_never_exposed_count_before_near_duplicate_grouping"]} |
| Minimum required total | {capacity["minimum_required_total"]} |
| Shortfall before near-duplicate grouping | {capacity["capacity_shortfall_before_near_duplicate_grouping"]} |

The 72 private event ledgers total {event_scan["byte_count"]} bytes and have inventory
SHA-256 `{event_scan["inventory_sha256"]}`. All records parsed, no Temperature UID fell
outside the frozen dataset, and the current union maps onto the old v2 partitions as
{partition["exposed_union_by_partition"]}. The remaining upper bound maps as
{partition["remaining_by_partition"]}.

Set digests use `SHA256(LF-joined sorted unique lowercase UID values plus final LF)` and do
not disclose UID values:

- Full Temperature set: `{dataset["temperature_uid_set_sha256"]}`
- Actual exposure union: `{historical["actual_exposed_uid_set_sha256"]}`
- Maximum remaining set: `{capacity["maximum_provable_never_exposed_uid_set_sha256"]}`

The tracked v2/v3 statement that historical Test exposure was zero is a time-bounded old
snapshot, not a valid current isolation proof. Current v1 ledgers alone intersect the later
v2 Test by 9 items and its Reserve by 21 items; current v3 ledgers cover all 50 Train and all
50 Test Temperature members.

## Zero-side-effect and identity audit

- Formal run IDs: none.
- Candidate, Reflector, baseline and total model calls: 0.
- Core jobs, retries and recoveries: 0.
- Split SHA-256: unavailable because no split was created.
- Source code commit used for the audit: `{source_code_commit}` on `{branch}`.
- Model configuration: `{model_identity.get("model")}`, reasoning
  `{model_identity.get("reasoning_effort")}`.
- Managed native executable SHA-256:
  `{managed_runtime_identity.get("candidate_executable_sha256")}`.
- Managed candidate image:
  `{managed_runtime_identity.get("candidate_image_id")}`.
- Credential check inspected only owner/type/mode metadata; credential contents were not
  read or copied.

## Code review

Candidate and baseline inference can reuse the formal
`TaskRequest -> Rollout -> Gateway -> CodexHarness` executor, identical parser/evaluator,
managed runtime identity, empty-context baseline, Core planned jobs, typed artifacts and
paired statistics. No `src/openevo/**` change is required.

The existing STV3 runner is not the requested experiment: it updates per item, records
`resume_allowed=false`, has no atomic batch-25 exactly-once ledger, allows much larger
artifact payloads, and its report path can contain per-item CSV. Its Reflector is managed
and attested but calls the Codex CLI inside a Core planned job rather than traversing the
formal Candidate harness chain. These gaps must be closed in a distinct benchmark package
after a qualifying dataset exists. The implementation audit lists
{len(implementation_audit["blocking_gaps_if_new_data_is_acquired"])} concrete gaps.

## Why the previous baseline beat evolved

The prior Train same-item jump from 38/50 to 48/50 is supervised replay evidence, not
independent generalization. A high generation-zero baseline (47/50) also leaves little
upside: a few negative flips dominate the category delta. The previous per-item updater
could encode recency and item-specific correlations, while three overlapping artifacts and
about 17.1 KiB of context diluted the original task signal. Selecting the final accumulated
state without forward, unseen validation makes these errors persist. Parser success only
proves that an answer was syntactically scoreable; it does not imply chemical correctness.

The most plausible interpretation is therefore overfitting plus context interference, not
evidence that evolution is intrinsically harmful.

## Recommended redesign

1. Acquire and pin a new authoritative Temperature source with at least 119 additional
   never-exposed items beyond the current 81; acquire a margin for source-group and semantic
   near-duplicate losses. Do not fabricate or rewrite items.
2. Reconstruct historical exposure against the new revision before coding or model calls,
   then freeze a group-aware split and source closure.
3. Implement one batch-level synthesis per 25 items, a canonical evidence index, support and
   contradiction counts, conditional rules, and deterministic retirement. Never treat the
   same-item post score as generalization.
4. Make the projections disjoint: facts in text memory, procedure in skill, stable behavior
   in agent system. Enforce 6 KiB target and 8 KiB serialized hard limit including framing.
5. Add forward-only Train diagnostics: rules learned through batch i should be evaluated on
   batch i+1 before that batch's feedback. Use this only to calibrate or retire rules from
   Train evidence; never use Test for selection.
6. Add a pre-registered Train validation slice or obtain enough extra data for one. Guard
   against negative flips on that independent Train-only slice while always freezing Ck
   before Test.
7. Route Reflector synthesis through the formal managed harness, retain Core ownership of
   the planned jobs, and add crash-injection tests at every accepted-completion and artifact
   commit boundary.
8. Repeat across pre-registered splits or a larger external holdout before claiming that
   full-evolve is beneficial. Report effect size, paired flips and confidence intervals, not
   only one p value.

## Missing experiment metrics

Train curves, artifacts, evolved Test, baseline Test, paired delta, flips, McNemar p and
confidence intervals are intentionally `NOT_RUN`. Creating zero-valued charts would be
misleading; `capacity_gate.svg` is the only generated chart.

Audit ID: `{audit_id}`. Generated: `{generated_at_utc}`.
"""


def _handoff_markdown(
    *,
    receipt: Mapping[str, object],
    audit_id: str,
    source_code_commit: str,
    branch: str,
) -> str:
    capacity = receipt["capacity_gate"]
    return f"""# Handoff

- Status: `{FINDING_CODE}`
- Audit ID: `{audit_id}`
- Formal run IDs: none
- Train/Test selected: 0/0
- Maximum never-exposed pool: {capacity["maximum_provable_never_exposed_count_before_near_duplicate_grouping"]}
- Required minimum pool: {capacity["minimum_required_total"]}
- Split SHA-256: not created
- Source code commit: `{source_code_commit}`
- Branch: `{branch}`
- Model calls / Core jobs / retries / recoveries: 0 / 0 / 0 / 0
- tmux attach/status/stop: not applicable; no session was created

To unblock, provide a pinned authoritative source revision with at least 119 additional
never-exposed Temperature items plus margin for group isolation. Rerun the zero-model gate;
do not relax the 100+100 minimum or import old artifacts.
"""


def _capacity_svg(receipt: Mapping[str, object]) -> str:
    dataset = receipt["dataset"]
    capacity = receipt["capacity_gate"]
    historical = receipt["historical_exposure"]
    total = int(dataset["temperature_test_count"])
    exposed = int(historical["actual_exposed_union_count"])
    remaining = int(
        capacity["maximum_provable_never_exposed_count_before_near_duplicate_grouping"]
    )
    scale = 650 / total
    exposed_width = round(exposed * scale, 2)
    remaining_width = round(remaining * scale, 2)
    required_x = round(150 + int(capacity["minimum_required_total"]) * scale, 2)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="900" height="420" viewBox="0 0 900 420">
  <rect width="900" height="420" fill="#ffffff"/>
  <text x="40" y="46" font-family="sans-serif" font-size="24" font-weight="700">Temperature capacity gate</text>
  <text x="40" y="78" font-family="sans-serif" font-size="15" fill="#374151">Aggregate-only preflight; no model calls</text>
  <text x="40" y="145" font-family="sans-serif" font-size="16">Frozen pool</text>
  <rect x="150" y="120" width="650" height="32" rx="4" fill="#d1d5db"/>
  <text x="810" y="143" font-family="sans-serif" font-size="15">{total}</text>
  <text x="40" y="215" font-family="sans-serif" font-size="16">Exposed</text>
  <rect x="150" y="190" width="{exposed_width}" height="32" rx="4" fill="#dc2626"/>
  <text x="{160 + exposed_width}" y="213" font-family="sans-serif" font-size="15">{exposed}</text>
  <text x="40" y="285" font-family="sans-serif" font-size="16">Eligible upper bound</text>
  <rect x="150" y="260" width="{remaining_width}" height="32" rx="4" fill="#2563eb"/>
  <text x="{160 + remaining_width}" y="283" font-family="sans-serif" font-size="15">{remaining}</text>
  <line x1="{required_x}" y1="245" x2="{required_x}" y2="315" stroke="#111827" stroke-width="3"/>
  <text x="{required_x - 35}" y="338" font-family="sans-serif" font-size="14">required 200</text>
  <text x="40" y="390" font-family="sans-serif" font-size="16" font-weight="700" fill="#991b1b">BLOCKED: shortfall {capacity["capacity_shortfall_before_near_duplicate_grouping"]} before near-duplicate grouping</text>
</svg>
"""


def _write_json(path: Path, payload: object) -> None:
    write_public_file(path, canonical_pretty_json_bytes(payload))


def _write_csv(path: Path, header: tuple[str, ...], rows: tuple[tuple[str, ...], ...]) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    write_public_file(path, buffer.getvalue().encode("utf-8"))


def _file_sha256(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


__all__ = ["REPORT_SCHEMA", "write_blocked_report_package"]
