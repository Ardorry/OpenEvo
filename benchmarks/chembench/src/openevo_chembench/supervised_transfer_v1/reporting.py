"""Deterministic paired statistics, local charts, and offline report generation."""

from __future__ import annotations

import csv
import io
import json
import math
import random
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_public_file,
)


def build_all_reports_v1(
    *,
    repository_root: Path,
    run_result_root: Path,
    run_state_root: Path,
    run_id: str,
    preflight_authority: dict[str, object],
) -> dict[str, object]:
    private_rows = _read_jsonl(run_state_root / "private/events.jsonl")
    public_rows = _read_jsonl(run_result_root / "public/events.jsonl")
    run_state = json.loads(
        (run_result_root / "public/run_state.json").read_text(encoding="utf-8")
    )
    if not isinstance(run_state, dict):
        raise TypeError("formal run state is invalid")
    reports = run_result_root / "reports"
    charts = reports / "charts"
    reports.mkdir(parents=True, exist_ok=True)
    charts.mkdir(parents=True, exist_ok=True)

    train_rows = _training_round_rows(private_rows)
    checkpoint_zero = preflight_authority.get("checkpoint_zero_aggregate_rows")
    if not isinstance(checkpoint_zero, list):
        raise TypeError("preflight checkpoint-zero authority is unavailable")
    probe_rows = _probe_rows(private_rows, checkpoint_zero_rows=checkpoint_zero)
    test_rows, test_summary, per_category = _test_rows(private_rows)
    memory_rows, rule_rows, evidence_rows = _memory_rows(public_rows, run_state_root)
    auxiliary_rows = _auxiliary_growth_rows(public_rows)
    transition_rows = _transition_rows(probe_rows, test_rows)

    outputs = {
        "training_round_accuracy.csv": _csv_bytes(train_rows),
        "probe_learning_curve.csv": _csv_bytes(probe_rows),
        "test_paired_results.csv": _csv_bytes(test_rows),
        "per_category_results.csv": _csv_bytes(per_category),
        "memory_growth.csv": _csv_bytes(memory_rows),
        "auxiliary_context_growth.csv": _csv_bytes(auxiliary_rows),
        "rule_status_counts.csv": _csv_bytes(rule_rows),
        "correctness_transitions.csv": _csv_bytes(transition_rows),
        "memory_evidence_count_distribution.csv": _csv_bytes(evidence_rows),
    }
    for name, payload in outputs.items():
        write_public_file(reports / name, payload)
    _write_charts(
        charts,
        train_rows,
        probe_rows,
        test_summary,
        per_category,
        memory_rows,
        auxiliary_rows,
        rule_rows,
        transition_rows,
        evidence_rows,
    )

    markdown = _render_final_markdown(
        run_id=run_id,
        test_summary=test_summary,
        per_category=per_category,
        probe_rows=probe_rows,
        train_rows=train_rows,
        memory_rows=memory_rows,
        test_manifest=str(run_state.get("test_manifest")),
        infrastructure_failures=int(run_state.get("infrastructure_failures", 0)),
    )
    html = _markdown_to_offline_html(markdown)
    write_public_file(reports / "final_report.md", markdown.encode("utf-8"))
    write_public_file(reports / "final_report.html", html.encode("utf-8"))
    metrics = {
        "schema_version": "chembench_supervised_transfer_final_metrics_v1",
        "run_id": run_id,
        "test": test_summary,
        "per_category": per_category,
        "security_findings": sum(
            row.get("kind") == "SECURITY_VIOLATION" for row in public_rows
        ),
        "context_findings": sum(
            row.get("kind") == "CONTEXT_BINDING_VIOLATION" for row in public_rows
        ),
        "artifact_findings": sum(
            row.get("kind") == "ARTIFACT_VALIDATION_FAILURE" for row in public_rows
        ),
        "infrastructure_failures": int(run_state.get("infrastructure_failures", 0)),
        "test_manifest": run_state.get("test_manifest"),
        "test_ledger_claim_sha256": run_state.get("test_ledger_claim_sha256"),
        "test_ledger_completion_sha256": run_state.get(
            "test_ledger_completion_sha256"
        ),
    }
    write_public_file(
        reports / "final_metrics.json",
        canonical_pretty_json_bytes(metrics),
    )
    from openevo_chembench.supervised_transfer_v1.desktop_export import (
        export_desktop_audit_v1,
    )

    desktop = export_desktop_audit_v1(
        repository_root=repository_root,
        run_result_root=run_result_root,
        run_state_root=run_state_root,
        run_id=run_id,
        phase="final",
    )
    return {
        "status": "COMPLETED",
        "run_id": run_id,
        "final_report_sha256": sha256_bytes((reports / "final_report.md").read_bytes()),
        "final_metrics_sha256": sha256_bytes((reports / "final_metrics.json").read_bytes()),
        "test_summary": test_summary,
        "desktop_package": desktop,
    }


def _training_round_rows(rows: list[dict[str, Any]]) -> list[dict[str, object]]:
    selected = [
        row
        for row in rows
        if row.get("kind") == "PRIVATE_EVALUATED"
        and row.get("logical_arm") in {"control_train", "online_train"}
    ]
    result: list[dict[str, object]] = []
    for arm in ("control_train", "online_train"):
        for round_index in range(3):
            subset = [
                row
                for row in selected
                if row["logical_arm"] == arm and row["round_index"] == round_index
            ]
            if len(subset) != 450:
                raise RuntimeError("formal Train round is incomplete")
            result.append(
                {
                    "arm": arm,
                    "round": round_index,
                    "n": len(subset),
                    "correct": sum(bool(row["correct"]) for row in subset),
                    "accuracy": _accuracy(subset),
                    "strict_parse_rate": _strict_rate(subset),
                }
            )
    return result


def _probe_rows(
    rows: list[dict[str, Any]],
    *,
    checkpoint_zero_rows: list[object],
) -> list[dict[str, object]]:
    selected = [
        row
        for row in rows
        if row.get("kind") == "PRIVATE_EVALUATED"
        and row.get("logical_arm") in {"control_probe", "online_probe"}
    ]
    if any(row.get("checkpoint") == 0 for row in selected):
        raise RuntimeError("formal run duplicated checkpoint-zero Probe")
    zero_by_category: dict[str, dict[str, object]] = {}
    for item in checkpoint_zero_rows:
        if not isinstance(item, dict):
            raise TypeError("checkpoint-zero authority row is invalid")
        category = item.get("category")
        expected_keys = {
            "checkpoint",
            "category",
            "n",
            "control_accuracy",
            "online_accuracy",
            "delta",
            "wrong_to_correct",
            "correct_to_wrong",
            "control_strict_parse_rate",
            "online_strict_parse_rate",
        }
        if (
            type(category) is not str
            or category in zero_by_category
            or set(item) != expected_keys
            or item.get("checkpoint") != 0
        ):
            raise RuntimeError("checkpoint-zero authority row is invalid")
        zero_by_category[category] = item
    expected_categories = {"overall", *CHEMBENCH4K_CATEGORIES}
    if set(zero_by_category) != expected_categories:
        raise RuntimeError("checkpoint-zero authority categories are incomplete")
    result: list[dict[str, object]] = []
    for checkpoint in (0, 10, 20, 30, 40, 50):
        for category in ("overall", *CHEMBENCH4K_CATEGORIES):
            if checkpoint == 0:
                authority = zero_by_category[category]
                expected = 90 if category == "overall" else 10
                if authority["n"] != expected:
                    raise RuntimeError("checkpoint-zero authority count is invalid")
                result.append(
                    {
                        "checkpoint": 0,
                        "category": category,
                        "n": authority["n"],
                        "control_accuracy": authority["control_accuracy"],
                        "online_accuracy": authority["online_accuracy"],
                        "delta": authority["delta"],
                        "wrong_to_correct": authority["wrong_to_correct"],
                        "correct_to_wrong": authority["correct_to_wrong"],
                    }
                )
                continue
            subset = [
                row
                for row in selected
                if row.get("checkpoint") == checkpoint
                and (category == "overall" or row.get("category") == category)
            ]
            control = [row for row in subset if row["logical_arm"] == "control_probe"]
            online = [row for row in subset if row["logical_arm"] == "online_probe"]
            pairs = _paired_by_uid(control, online)
            expected = 90 if category == "overall" else 10
            if len(pairs) != expected:
                raise RuntimeError("formal Probe checkpoint is incomplete")
            result.append(
                {
                    "checkpoint": checkpoint,
                    "category": category,
                    "n": len(pairs),
                    "control_accuracy": _accuracy(control),
                    "online_accuracy": _accuracy(online),
                    "delta": _accuracy(online) - _accuracy(control),
                    "wrong_to_correct": sum(
                        not left["correct"] and right["correct"]
                        for left, right in pairs
                    ),
                    "correct_to_wrong": sum(
                        left["correct"] and not right["correct"]
                        for left, right in pairs
                    ),
                }
            )
    return result


def _test_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, object]], dict[str, object], list[dict[str, object]]]:
    control = [
        row
        for row in rows
        if row.get("kind") == "PRIVATE_EVALUATED"
        and row.get("logical_arm") == "control_test"
    ]
    online = [
        row
        for row in rows
        if row.get("kind") == "PRIVATE_EVALUATED"
        and row.get("logical_arm") == "online_test"
    ]
    pairs = _paired_by_uid(control, online)
    if len(pairs) != 450:
        raise RuntimeError("final Test is not a complete 450-item pair")
    paired_rows: list[dict[str, object]] = []
    for left, right in pairs:
        paired_rows.append(
            {
                "uid": left["task_uid"],
                "category": left["category"],
                "control_prediction": left["official_prediction"],
                "online_prediction": right["official_prediction"],
                "control_correct": left["correct"],
                "online_correct": right["correct"],
                "transition": _transition(bool(left["correct"]), bool(right["correct"])),
            }
        )
    summary = _paired_summary(pairs)
    per_category = []
    for category in CHEMBENCH4K_CATEGORIES:
        category_pairs = [pair for pair in pairs if pair[0]["category"] == category]
        per_category.append({"category": category, **_paired_summary(category_pairs)})
    return paired_rows, summary, per_category


def _paired_summary(pairs: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, object]:
    n = len(pairs)
    control_correct = sum(bool(left["correct"]) for left, _right in pairs)
    online_correct = sum(bool(right["correct"]) for _left, right in pairs)
    wrong_to_correct = sum(
        not left["correct"] and right["correct"] for left, right in pairs
    )
    correct_to_wrong = sum(
        left["correct"] and not right["correct"] for left, right in pairs
    )
    control_accuracy = control_correct / n if n else 0.0
    online_accuracy = online_correct / n if n else 0.0
    delta = online_accuracy - control_accuracy
    return {
        "n": n,
        "control_correct": control_correct,
        "online_correct": online_correct,
        "control_accuracy": control_accuracy,
        "online_accuracy": online_accuracy,
        "absolute_delta": delta,
        "relative_error_reduction": (
            delta / (1.0 - control_accuracy) if control_accuracy < 1.0 else 0.0
        ),
        "wrong_to_correct": wrong_to_correct,
        "correct_to_wrong": correct_to_wrong,
        "both_correct": sum(left["correct"] and right["correct"] for left, right in pairs),
        "both_wrong": sum(not left["correct"] and not right["correct"] for left, right in pairs),
        "mcnemar_exact_p": _mcnemar_exact(wrong_to_correct, correct_to_wrong),
        "paired_bootstrap_ci95": list(_paired_bootstrap_ci(pairs)),
        "control_strict_parse_rate": _strict_rate([left for left, _right in pairs]),
        "online_strict_parse_rate": _strict_rate([right for _left, right in pairs]),
        "control_official_parse_rate": _official_rate(
            [left for left, _right in pairs]
        ),
        "online_official_parse_rate": _official_rate(
            [right for _left, right in pairs]
        ),
    }


def _memory_rows(
    public_rows: list[dict[str, Any]],
    run_state_root: Path,
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    updates = [
        row
        for row in public_rows
        if row.get("kind") == "REFLECTOR_SUPERVISED"
        and row.get("stage") == "ONLINE_TRAIN"
        and row.get("update_index") == 2
    ]
    memory_rows: list[dict[str, object]] = []
    rule_rows: list[dict[str, object]] = []
    evidence = Counter[int]()
    for row in updates:
        metrics = row["memory_metrics"]
        item = {
            "category": row["category"],
            "training_items": int(row["task_index"]) + 1,
            "utf8_bytes": metrics["utf8_byte_count"],
            "estimated_tokens": metrics["estimated_token_count"],
        }
        memory_rows.append(item)
        rule_rows.append(
            {
                "category": row["category"],
                "training_items": int(row["task_index"]) + 1,
                "confirmed": metrics["confirmed_rule_count"],
                "provisional": metrics["provisional_rule_count"],
                "failure_modes": metrics["failure_mode_count"],
                "retired": metrics["retired_rule_count"],
            }
        )
    for path in (run_state_root / "private/checkpoint_memory").glob("*/checkpoint_*.md"):
        for match in re.finditer(r"Evidence\s+Count\s*[:=]\s*([0-9]+)", path.read_text()):
            evidence[int(match.group(1))] += 1
    evidence_rows = [
        {"evidence_count": count, "rule_count": evidence[count]} for count in sorted(evidence)
    ]
    return memory_rows, rule_rows, evidence_rows


def _auxiliary_growth_rows(
    public_rows: list[dict[str, Any]],
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for row in public_rows:
        if (
            row.get("kind") != "REFLECTOR_SUPERVISED"
            or row.get("stage") != "ONLINE_TRAIN"
            or row.get("update_index") != 2
        ):
            continue
        auxiliary = row.get("auxiliary_targets")
        if not isinstance(auxiliary, list) or len(auxiliary) != 2:
            raise RuntimeError("formal auxiliary target evidence is incomplete")
        for item in auxiliary:
            if not isinstance(item, dict) or not isinstance(item.get("inspection"), dict):
                raise TypeError("formal auxiliary inspection is invalid")
            inspection = item["inspection"]
            result.append(
                {
                    "category": row["category"],
                    "training_items": int(row["task_index"]) + 1,
                    "target_id": item["target_id"],
                    "utf8_bytes": inspection["utf8_byte_count"],
                    "estimated_tokens": inspection["estimated_token_count"],
                    "source_evidence_count": inspection["source_evidence_count"],
                }
            )
    return result


def _transition_rows(
    probe_rows: list[dict[str, object]],
    test_rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    result = [
        {
            "stage": "probe",
            "checkpoint": row["checkpoint"],
            "category": row["category"],
            "wrong_to_correct": row["wrong_to_correct"],
            "correct_to_wrong": row["correct_to_wrong"],
        }
        for row in probe_rows
    ]
    counts = Counter(str(row["transition"]) for row in test_rows)
    result.append(
        {
            "stage": "final_test",
            "checkpoint": 50,
            "category": "overall",
            "wrong_to_correct": counts["wrong_to_correct"],
            "correct_to_wrong": counts["correct_to_wrong"],
        }
    )
    return result


def _write_charts(
    root: Path,
    train: list[dict[str, object]],
    probe: list[dict[str, object]],
    test_summary: dict[str, object],
    categories: list[dict[str, object]],
    memory: list[dict[str, object]],
    auxiliary: list[dict[str, object]],
    rules: list[dict[str, object]],
    transitions: list[dict[str, object]],
    evidence: list[dict[str, object]],
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    def save(name: str) -> None:
        plt.tight_layout()
        plt.savefig(root / name, dpi=150)
        plt.close()

    for arm in ("control_train", "online_train"):
        selected = [row for row in train if row["arm"] == arm]
        plt.plot([row["round"] for row in selected], [row["accuracy"] for row in selected], label=arm)
    plt.xlabel("Round")
    plt.ylabel("Accuracy")
    plt.legend()
    save("train_round_accuracy.png")

    overall = [row for row in probe if row["category"] == "overall"]
    plt.plot([row["checkpoint"] for row in overall], [row["control_accuracy"] for row in overall], label="Control")
    plt.plot([row["checkpoint"] for row in overall], [row["online_accuracy"] for row in overall], label="Online")
    plt.xlabel("Training items per category")
    plt.ylabel("Probe accuracy")
    plt.legend()
    save("probe_accuracy.png")

    plt.plot([row["checkpoint"] for row in overall], [row["delta"] for row in overall])
    plt.axhline(0.0, color="black", linewidth=0.8)
    plt.xlabel("Training items per category")
    plt.ylabel("Online - Control")
    save("probe_delta.png")

    plt.bar(
        ["Control", "Frozen online-trained"],
        [test_summary["control_accuracy"], test_summary["online_accuracy"]],
    )
    plt.ylim(0.0, 1.0)
    plt.ylabel("Final Test accuracy")
    save("test_overall.png")

    names = [str(row["category"]) for row in categories]
    positions = list(range(len(names)))
    width = 0.4
    plt.bar([value - width / 2 for value in positions], [row["control_accuracy"] for row in categories], width, label="Control")
    plt.bar([value + width / 2 for value in positions], [row["online_accuracy"] for row in categories], width, label="Online")
    plt.xticks(positions, names, rotation=45, ha="right")
    plt.ylabel("Final Test accuracy")
    plt.legend()
    save("test_per_category.png")

    for category in CHEMBENCH4K_CATEGORIES:
        selected = [row for row in memory if row["category"] == category]
        plt.plot([row["training_items"] for row in selected], [row["utf8_bytes"] for row in selected], label=category)
    plt.xlabel("Training items")
    plt.ylabel("Memory UTF-8 bytes")
    save("memory_size.png")

    for target in ("skill_bundle", "agent_system"):
        by_checkpoint: dict[int, list[int]] = defaultdict(list)
        for row in auxiliary:
            if row["target_id"] == target:
                by_checkpoint[int(row["training_items"])].append(
                    int(row["utf8_bytes"])
                )
        checkpoints = sorted(by_checkpoint)
        plt.plot(
            checkpoints,
            [sum(by_checkpoint[key]) / len(by_checkpoint[key]) for key in checkpoints],
            label=target,
        )
    plt.xlabel("Training items per category")
    plt.ylabel("Mean auxiliary UTF-8 bytes")
    plt.legend()
    save("auxiliary_context_size.png")

    totals = defaultdict(lambda: [0, 0])
    for row in rules:
        totals[int(row["training_items"])][0] += int(row["confirmed"])
        totals[int(row["training_items"])][1] += int(row["provisional"])
    plt.plot(sorted(totals), [totals[key][0] for key in sorted(totals)], label="Confirmed")
    plt.plot(sorted(totals), [totals[key][1] for key in sorted(totals)], label="Provisional")
    plt.xlabel("Training items per category")
    plt.ylabel("Rule count across categories")
    plt.legend()
    save("rule_counts.png")

    final_transition = next(
        row for row in transitions if row["stage"] == "final_test"
    )
    plt.bar(
        ["Wrong to correct", "Correct to wrong"],
        [
            final_transition["wrong_to_correct"],
            final_transition["correct_to_wrong"],
        ],
    )
    plt.ylabel("Paired Test items")
    save("correctness_transitions.png")

    plt.bar(
        [str(row["evidence_count"]) for row in evidence],
        [row["rule_count"] for row in evidence],
    )
    plt.xlabel("Evidence count")
    plt.ylabel("Rules")
    save("memory_evidence_distribution.png")


def _render_final_markdown(
    *,
    run_id: str,
    test_summary: dict[str, object],
    per_category: list[dict[str, object]],
    probe_rows: list[dict[str, object]],
    train_rows: list[dict[str, object]],
    memory_rows: list[dict[str, object]],
    test_manifest: str,
    infrastructure_failures: int,
) -> str:
    lines = [
        "# ChemBench Supervised Transfer V1",
        "",
        (
            "RESEARCH_ONLY_SUPERVISED_EVOLUTION; TRAIN_ANSWER_SUPERVISION; "
            "FROZEN_UNSEEN_TEST_TRANSFER; NOT_A_STANDARD_LEADERBOARD_SCORE."
        ),
        "",
        f"Run ID: `{run_id}`",
        "",
        "## Exposure definition",
        "",
        (
            "Holdouts exclude actual task-model attempts, completions, private evaluations, "
            "supervised reflector inputs, and item-level human review. Manifest listing, "
            "deterministic prompt rendering, and aggregate-only review do not by themselves "
            "exclude an item."
        ),
        "",
        "## Final paired Test",
        "",
        f"- Control accuracy: {float(test_summary['control_accuracy']):.6f}",
        f"- Online accuracy: {float(test_summary['online_accuracy']):.6f}",
        f"- Absolute delta: {float(test_summary['absolute_delta']):+.6f}",
        f"- Relative error reduction: {float(test_summary['relative_error_reduction']):+.6f}",
        f"- McNemar exact p: {float(test_summary['mcnemar_exact_p']):.8g}",
        f"- Paired bootstrap 95% CI: {test_summary['paired_bootstrap_ci95']}",
        f"- Wrong to correct: {test_summary['wrong_to_correct']}",
        f"- Correct to wrong: {test_summary['correct_to_wrong']}",
        "",
        "## Per-category results",
        "",
        "| Category | Control | Online | Delta |",
        "|---|---:|---:|---:|",
    ]
    lines.extend(
        f"| {row['category']} | {float(row['control_accuracy']):.4f} | "
        f"{float(row['online_accuracy']):.4f} | {float(row['absolute_delta']):+.4f} |"
        for row in per_category
    )
    lines.extend(
        [
            "",
            "## Integrity",
            "",
            "- Probe and Test created zero evolution jobs.",
            f"- Final Test used the preregistered `{test_manifest}` manifest once.",
            "- Reflector packets were Train-only and answer-supervised.",
            (
                "- Each frozen category context contained exactly one text-memory, "
                "skill-bundle, and agent-system artifact fixed before Test."
            ),
            f"- Infrastructure failures recorded: {infrastructure_failures}.",
            "",
            (
                f"Training rows: {len(train_rows)}; Probe curve rows: {len(probe_rows)}; "
                f"memory measurements: {len(memory_rows)}; auxiliary context "
                "growth is reported separately."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _markdown_to_offline_html(markdown: str) -> str:
    import html

    body = "\n".join(
        f"<p>{html.escape(line)}</p>" if line else ""
        for line in markdown.splitlines()
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Supervised Transfer"
        "</title><style>body{font-family:system-ui;max-width:1100px;margin:2rem auto;"
        "line-height:1.45}p{white-space:pre-wrap;margin:.25rem 0}</style></head><body>"
        f"{body}</body></html>"
    )


def _paired_by_uid(
    control: list[dict[str, Any]],
    online: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    right = {row["task_uid"]: row for row in online}
    if len(right) != len(online) or {row["task_uid"] for row in control} != set(right):
        raise RuntimeError("paired result UID sets differ")
    return [(row, right[row["task_uid"]]) for row in control]


def _accuracy(rows: list[dict[str, Any]]) -> float:
    return sum(bool(row["correct"]) for row in rows) / len(rows) if rows else 0.0


def _strict_rate(rows: list[dict[str, Any]]) -> float:
    return (
        sum(row.get("strict_parse_status") == "parsed" for row in rows) / len(rows)
        if rows
        else 0.0
    )


def _official_rate(rows: list[dict[str, Any]]) -> float:
    return (
        sum(row.get("official_parse_status") == "parsed" for row in rows) / len(rows)
        if rows
        else 0.0
    )


def _transition(control: bool, online: bool) -> str:
    if control and online:
        return "both_correct"
    if control:
        return "correct_to_wrong"
    if online:
        return "wrong_to_correct"
    return "both_wrong"


def _mcnemar_exact(wrong_to_correct: int, correct_to_wrong: int) -> float:
    discordant = wrong_to_correct + correct_to_wrong
    if discordant == 0:
        return 1.0
    smaller = min(wrong_to_correct, correct_to_wrong)
    tail = sum(math.comb(discordant, value) for value in range(smaller + 1)) / (2**discordant)
    return min(1.0, 2.0 * tail)


def _paired_bootstrap_ci(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    samples: int = 10_000,
) -> tuple[float, float]:
    if not pairs:
        return (0.0, 0.0)
    deltas = [int(right["correct"]) - int(left["correct"]) for left, right in pairs]
    generator = random.Random("openevo-chembench-supervised-transfer-v1-bootstrap")
    means = sorted(
        sum(deltas[generator.randrange(len(deltas))] for _ in deltas) / len(deltas)
        for _sample in range(samples)
    )
    return means[int(0.025 * samples)], means[min(samples - 1, int(0.975 * samples))]


def _csv_bytes(rows: list[dict[str, object]]) -> bytes:
    if not rows:
        return b"\n"
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        if not isinstance(value, dict):
            raise TypeError("event stream contains a non-object")
        result.append(value)
    return result


__all__ = ["build_all_reports_v1"]
