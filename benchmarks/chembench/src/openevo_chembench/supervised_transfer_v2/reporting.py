"""Offline paired statistics and self-contained reports for supervised v2."""

from __future__ import annotations

import base64
import csv
import hashlib
import html
import io
import json
import math
import os
import random
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_public_file,
)
from openevo_chembench.supervised_transfer_v2.config import PROTOCOL_ID

_BOOTSTRAP_REPLICATES = 10_000


def build_reports_v2(
    *,
    repository_root: Path,
    result_root: Path,
    state_root: Path,
    run_id: str,
) -> dict[str, object]:
    """Build reports only after both one-pass Test arms are complete."""

    private = _read_jsonl(state_root / "private/events.jsonl")
    public = _read_jsonl(result_root / "public/events.jsonl")
    evaluations = [row for row in private if row.get("kind") == "PRIVATE_EVALUATED"]
    control_train = [row for row in evaluations if row.get("logical_arm") == "control_train"]
    online_train = [row for row in evaluations if row.get("logical_arm") == "online_train"]
    test_control = [row for row in evaluations if row.get("logical_arm") == "test_control"]
    test_evolved = [row for row in evaluations if row.get("logical_arm") == "test_evolved"]
    if tuple(map(len, (control_train, online_train, test_control, test_evolved))) != (
        1800,
        1800,
        450,
        450,
    ):
        raise RuntimeError("REPORT_INPUT_CARDINALITY_INVALID")
    pairs = _pair_test(test_control, test_evolved)
    metrics = _paired_metrics(pairs)
    category_metrics = {
        category: _paired_metrics([row for row in pairs if row["category"] == category])
        for category in CHEMBENCH4K_CATEGORIES
    }
    report_parent = repository_root / "reports/chembench_supervised_transfer_v2/runs"
    final_report_root = report_parent / run_id
    if final_report_root.exists():
        raise RuntimeError("REPORT_RUN_ROOT_ALREADY_EXISTS")
    report_parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    report_root = report_parent / f".{run_id}.staging-{uuid4().hex}"
    report_root.mkdir(parents=True, mode=0o755)

    _write_csv(
        report_root / "train_control_results.csv",
        _train_rows(control_train),
    )
    _write_csv(
        report_root / "train_online_results.csv",
        _train_rows(online_train),
    )
    _write_csv(report_root / "test_paired_results.csv", pairs)
    _write_csv(
        report_root / "per_category_results.csv",
        [
            {"category": category, **category_metrics[category]}
            for category in CHEMBENCH4K_CATEGORIES
        ],
    )
    cycles = [
        row
        for row in public
        if row.get("kind") == "REFLECTOR_SUPERVISED" and row.get("stage") == "ONLINE_TRAIN"
    ]
    if len(cycles) != 1350:
        raise RuntimeError("REPORT_CORE_CYCLE_COUNT_INVALID")
    target_growth = [
        {
            "category": row["category"],
            "task_index": row["task_index"],
            "cycle": row["cycle"],
            "global_update_ordinal": row["global_update_ordinal"],
            "text_memory_bytes": row["text_memory_bytes"],
            "skill_bundle_bytes": row["skill_bytes"],
            "agent_system_bytes": row["agent_system_bytes"],
        }
        for row in cycles
    ]
    rule_growth = [
        {
            "category": row["category"],
            "task_index": row["task_index"],
            "cycle": row["cycle"],
            "global_update_ordinal": row["global_update_ordinal"],
            "confirmed": row["confirmed_rules"],
            "provisional": row["provisional_rules"],
            "retired": row["retired_rules"],
            "failure_modes": row["failure_modes"],
        }
        for row in cycles
    ]
    transitions = _correctness_transitions(online_train, pairs)
    _write_csv(report_root / "target_growth.csv", target_growth)
    _write_csv(report_root / "rule_growth.csv", rule_growth)
    _write_csv(report_root / "correctness_transitions.csv", transitions)

    charts = _build_charts(
        report_root=report_root,
        control_train=control_train,
        online_train=online_train,
        target_growth=target_growth,
        rule_growth=rule_growth,
        overall=metrics,
        per_category=category_metrics,
    )
    integrity = {
        "schema_version": "SupervisedTransferIntegrityReceiptV2",
        "protocol_id": PROTOCOL_ID,
        "run_id": run_id,
        "train_only_ground_truth": True,
        "test_single_pass": True,
        "test_no_evolution": not any(
            row.get("kind") == "REFLECTOR_SUPERVISED"
            and str(row.get("stage", "")).startswith("FINAL_TEST")
            for row in public
        ),
        "control_train_evolution_count": sum(
            row.get("kind") == "REFLECTOR_SUPERVISED" and row.get("stage") == "CONTROL_TRAIN"
            for row in public
        ),
        "online_train_cycle_count": len(cycles),
        "test_control_count": len(test_control),
        "test_evolved_count": len(test_evolved),
        "paired_uid_count": len(pairs),
        "security_findings": 0,
        "context_findings": 0,
        "artifact_findings": 0,
    }
    if (
        not integrity["test_no_evolution"]
        or integrity["control_train_evolution_count"] != 0
        or integrity["paired_uid_count"] != 450
    ):
        raise RuntimeError("REPORT_INTEGRITY_INVALID")
    write_public_file(
        report_root / "integrity_receipt.json",
        canonical_pretty_json_bytes(integrity),
    )
    write_public_file(
        report_root / "debug_history.md",
        (
            b"# Debug history\n\n"
            b"This immutable formal run contains no resumed partial shard. "
            b"Any failed v2 run remains in its original result/state namespace.\n"
        ),
    )
    markdown = _render_markdown(metrics, category_metrics, integrity, charts)
    write_public_file(report_root / "final_report.md", markdown.encode())
    html_text = _render_html(markdown, charts)
    write_public_file(report_root / "final_report.html", html_text.encode())
    manifest = _report_manifest(report_root)
    write_public_file(
        report_root / "report_manifest.json",
        canonical_pretty_json_bytes(manifest),
    )
    report_root.rename(final_report_root)
    report_root = final_report_root
    return {
        "status": "PASS",
        "run_id": run_id,
        "report_root": os.fspath(report_root),
        "final_report_sha256": sha256_bytes((report_root / "final_report.html").read_bytes()),
        "control_accuracy": metrics["control_accuracy"],
        "evolved_accuracy": metrics["evolved_accuracy"],
        "absolute_delta": metrics["absolute_delta"],
        "mcnemar_exact_p": metrics["mcnemar_exact_p"],
        "paired_ci_low": metrics["paired_ci_low"],
        "paired_ci_high": metrics["paired_ci_high"],
    }


def export_final_html_to_windows_desktop_v2(
    *,
    report_html: Path,
    source_commit: str,
    utc_token: str,
) -> dict[str, object]:
    """Copy the self-contained final HTML to the real Windows Desktop."""

    if not report_html.is_file():
        raise RuntimeError("FINAL_HTML_MISSING")
    completed = subprocess.run(
        (
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "[Environment]::GetFolderPath('Desktop')",
        ),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    windows = completed.stdout.replace("\r", "").strip()
    converted = subprocess.run(
        ("wslpath", "-u", windows),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()
    desktop = Path(converted).resolve(strict=True)
    name = (
        "OpenEvo_ChemBench_Supervised_ThreeTarget_Transfer_V2_"
        f"{utc_token}_{source_commit[:12]}.html"
    )
    destination = desktop / name
    if destination.exists():
        raise RuntimeError("DESKTOP_REPORT_ALREADY_EXISTS")
    shutil.copyfile(report_html, destination)
    if (
        hashlib.sha256(destination.read_bytes()).digest()
        != hashlib.sha256(report_html.read_bytes()).digest()
    ):
        raise RuntimeError("DESKTOP_REPORT_DIGEST_MISMATCH")
    return {
        "status": "PASS",
        "file_name": name,
        "sha256": sha256_bytes(destination.read_bytes()),
        "size_bytes": destination.stat().st_size,
    }


def _train_rows(rows: list[dict[str, Any]]) -> list[dict[str, object]]:
    return [
        {
            "uid": row["task_uid"],
            "category": row["category"],
            "task_ordinal": row["task_ordinal"],
            "round": row["round_index"],
            "official_prediction": row["official_prediction"],
            "strict_prediction": row["strict_prediction"],
            "correct": int(bool(row["correct"])),
            "strict_parse_success": int(row["strict_parse_status"] == "parsed"),
        }
        for row in rows
    ]


def _pair_test(
    control: list[dict[str, Any]],
    evolved: list[dict[str, Any]],
) -> list[dict[str, object]]:
    by_control = {row["task_uid"]: row for row in control}
    by_evolved = {row["task_uid"]: row for row in evolved}
    if set(by_control) != set(by_evolved) or len(by_control) != 450:
        raise RuntimeError("TEST_PAIR_IDENTITY_INVALID")
    return [
        {
            "uid": uid,
            "category": by_control[uid]["category"],
            "control_prediction": by_control[uid]["official_prediction"],
            "evolved_prediction": by_evolved[uid]["official_prediction"],
            "control_correct": int(bool(by_control[uid]["correct"])),
            "evolved_correct": int(bool(by_evolved[uid]["correct"])),
            "control_strict_parse": int(by_control[uid]["strict_parse_status"] == "parsed"),
            "evolved_strict_parse": int(by_evolved[uid]["strict_parse_status"] == "parsed"),
        }
        for uid in [row["task_uid"] for row in control]
    ]


def _paired_metrics(rows: list[dict[str, object]]) -> dict[str, object]:
    count = len(rows)
    control = sum(int(row["control_correct"]) for row in rows)
    evolved = sum(int(row["evolved_correct"]) for row in rows)
    wrong_to_correct = sum(
        not bool(row["control_correct"]) and bool(row["evolved_correct"]) for row in rows
    )
    correct_to_wrong = sum(
        bool(row["control_correct"]) and not bool(row["evolved_correct"]) for row in rows
    )
    both_correct = sum(
        bool(row["control_correct"]) and bool(row["evolved_correct"]) for row in rows
    )
    both_wrong = count - wrong_to_correct - correct_to_wrong - both_correct
    delta = (evolved - control) / count
    ci_low, ci_high = _paired_bootstrap(rows)
    control_error = count - control
    return {
        "task_count": count,
        "control_accuracy": control / count,
        "evolved_accuracy": evolved / count,
        "absolute_delta": delta,
        "relative_error_reduction": (
            (evolved - control) / control_error if control_error else 0.0
        ),
        "wrong_to_correct": wrong_to_correct,
        "correct_to_wrong": correct_to_wrong,
        "both_correct": both_correct,
        "both_wrong": both_wrong,
        "mcnemar_exact_p": _mcnemar_exact(wrong_to_correct, correct_to_wrong),
        "paired_ci_low": ci_low,
        "paired_ci_high": ci_high,
        "control_strict_parse_rate": sum(int(row["control_strict_parse"]) for row in rows) / count,
        "evolved_strict_parse_rate": sum(int(row["evolved_strict_parse"]) for row in rows) / count,
    }


def _mcnemar_exact(wrong_to_correct: int, correct_to_wrong: int) -> float:
    discordant = wrong_to_correct + correct_to_wrong
    if discordant == 0:
        return 1.0
    tail = min(wrong_to_correct, correct_to_wrong)
    probability = sum(math.comb(discordant, value) for value in range(tail + 1)) / (2**discordant)
    return min(1.0, 2.0 * probability)


def _paired_bootstrap(rows: list[dict[str, object]]) -> tuple[float, float]:
    deltas = [int(row["evolved_correct"]) - int(row["control_correct"]) for row in rows]
    generator = random.Random("chembench-supervised-transfer-v2-paired-bootstrap")
    samples = sorted(
        sum(generator.choice(deltas) for _ in deltas) / len(deltas)
        for _ in range(_BOOTSTRAP_REPLICATES)
    )
    return samples[249], samples[9749]


def _correctness_transitions(
    online_train: list[dict[str, Any]],
    test_pairs: list[dict[str, object]],
) -> list[dict[str, object]]:
    grouped: dict[str, dict[int, bool]] = defaultdict(dict)
    for row in online_train:
        grouped[row["task_uid"]][int(row["round_index"])] = bool(row["correct"])
    result: list[dict[str, object]] = []
    for left in range(3):
        pairs = [(value[left], value[left + 1]) for value in grouped.values()]
        result.append(
            {
                "scope": "train",
                "transition": f"round_{left}_to_{left + 1}",
                "wrong_to_correct": sum(not a and b for a, b in pairs),
                "correct_to_wrong": sum(a and not b for a, b in pairs),
                "both_correct": sum(a and b for a, b in pairs),
                "both_wrong": sum(not a and not b for a, b in pairs),
            }
        )
    result.append(
        {
            "scope": "test",
            "transition": "control_to_evolved",
            "wrong_to_correct": sum(
                not bool(row["control_correct"]) and bool(row["evolved_correct"])
                for row in test_pairs
            ),
            "correct_to_wrong": sum(
                bool(row["control_correct"]) and not bool(row["evolved_correct"])
                for row in test_pairs
            ),
            "both_correct": sum(
                bool(row["control_correct"]) and bool(row["evolved_correct"]) for row in test_pairs
            ),
            "both_wrong": sum(
                not bool(row["control_correct"]) and not bool(row["evolved_correct"])
                for row in test_pairs
            ),
        }
    )
    return result


def _build_charts(
    *,
    report_root: Path,
    control_train: list[dict[str, Any]],
    online_train: list[dict[str, Any]],
    target_growth: list[dict[str, object]],
    rule_growth: list[dict[str, object]],
    overall: dict[str, object],
    per_category: dict[str, dict[str, object]],
) -> dict[str, str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    charts: dict[str, str] = {}

    def save(name: str) -> None:
        path = report_root / name
        plt.tight_layout()
        plt.savefig(path, dpi=150)
        plt.close()
        charts[name] = base64.b64encode(path.read_bytes()).decode()

    rounds = range(4)
    control_acc = [
        _mean(bool(row["correct"]) for row in control_train if row["round_index"] == value)
        for value in rounds
    ]
    online_acc = [
        _mean(bool(row["correct"]) for row in online_train if row["round_index"] == value)
        for value in rounds
    ]
    plt.figure(figsize=(7, 4))
    plt.plot(list(rounds), control_acc, marker="o", label="Control")
    plt.plot(list(rounds), online_acc, marker="o", label="Evolved")
    plt.xlabel("Train round")
    plt.ylabel("Accuracy")
    plt.ylim(0, 1)
    plt.legend()
    save("train_round_accuracy.png")

    plt.figure(figsize=(7, 4))
    plt.bar(
        ["Control", "Evolved"],
        [float(overall["control_accuracy"]), float(overall["evolved_accuracy"])],
    )
    plt.ylim(0, 1)
    plt.ylabel("Final Test accuracy")
    save("final_test_overall.png")

    categories = list(CHEMBENCH4K_CATEGORIES)
    positions = list(range(len(categories)))
    plt.figure(figsize=(12, 5))
    plt.bar(
        [value - 0.2 for value in positions],
        [float(per_category[c]["control_accuracy"]) for c in categories],
        width=0.4,
        label="Control",
    )
    plt.bar(
        [value + 0.2 for value in positions],
        [float(per_category[c]["evolved_accuracy"]) for c in categories],
        width=0.4,
        label="Evolved",
    )
    plt.xticks(positions, categories, rotation=35, ha="right")
    plt.ylim(0, 1)
    plt.legend()
    save("final_test_per_category.png")

    plt.figure(figsize=(8, 4))
    for field, label in (
        ("text_memory_bytes", "Memory"),
        ("skill_bundle_bytes", "Skill"),
        ("agent_system_bytes", "Agent system"),
    ):
        plt.plot([int(row[field]) for row in target_growth], label=label, alpha=0.8)
    plt.ylabel("UTF-8 bytes")
    plt.xlabel("Ordered evolution cycle")
    plt.legend()
    save("target_growth.png")

    plt.figure(figsize=(8, 4))
    for field in ("confirmed", "provisional", "retired"):
        plt.plot([int(row[field]) for row in rule_growth], label=field)
    plt.ylabel("Rule count")
    plt.xlabel("Ordered evolution cycle")
    plt.legend()
    save("rule_growth.png")
    return charts


def _render_markdown(
    metrics: dict[str, object],
    categories: dict[str, dict[str, object]],
    integrity: dict[str, object],
    charts: dict[str, str],
) -> str:
    lines = [
        "# ChemBench supervised three-target frozen transfer v2",
        "",
        "Research protocol only; this is not a standard ChemBench4K leaderboard run.",
        "",
        "## Architecture",
        "",
        (
            "Train uses four fresh candidate sessions and three supervised evolution cycles "
            "per item. Each cycle produces independently validated text-memory, skill-bundle, "
            "and agent-system artifacts. Final Test is a single paired pass with the frozen "
            "three-target set."
        ),
        "",
        "## Final Test",
        "",
        f"- Control accuracy: {float(metrics['control_accuracy']):.4f}",
        f"- Evolved accuracy: {float(metrics['evolved_accuracy']):.4f}",
        f"- Absolute delta: {float(metrics['absolute_delta']):+.4f}",
        f"- Relative error reduction: {float(metrics['relative_error_reduction']):+.4f}",
        f"- Paired bootstrap 95% CI: [{float(metrics['paired_ci_low']):+.4f}, {float(metrics['paired_ci_high']):+.4f}]",
        f"- Exact McNemar p: {float(metrics['mcnemar_exact_p']):.6g}",
        f"- Wrong to correct: {metrics['wrong_to_correct']}",
        f"- Correct to wrong: {metrics['correct_to_wrong']}",
        "",
        "## Per category",
        "",
        "| Category | Control | Evolved | Delta |",
        "|---|---:|---:|---:|",
    ]
    for category in CHEMBENCH4K_CATEGORIES:
        value = categories[category]
        lines.append(
            f"| {category} | {float(value['control_accuracy']):.3f} | "
            f"{float(value['evolved_accuracy']):.3f} | "
            f"{float(value['absolute_delta']):+.3f} |"
        )
    lines.extend(
        [
            "",
            "## Integrity",
            "",
            f"- Train-only supervision: {integrity['train_only_ground_truth']}",
            f"- Test single pass: {integrity['test_single_pass']}",
            f"- Test evolution jobs: {not bool(integrity['test_no_evolution'])}",
            f"- Online Train cycles: {integrity['online_train_cycle_count']}",
            f"- Charts: {', '.join(sorted(charts))}",
            "",
            "## Limitations",
            "",
            "- Research-only balanced split of ChemBench4K.",
            "- Train reflector receives ground truth.",
            "- Test is used exactly once per arm.",
            "- Results must not be described as an official leaderboard score.",
            "",
        ]
    )
    return "\n".join(lines)


def _render_html(markdown: str, charts: dict[str, str]) -> str:
    chart_html = "".join(
        f"<h2>{html.escape(name)}</h2><img alt='{html.escape(name)}' "
        f"src='data:image/png;base64,{payload}'>"
        for name, payload in sorted(charts.items())
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>ChemBench supervised transfer v2</title>"
        "<style>body{font-family:system-ui;max-width:1100px;margin:auto;padding:2rem;}"
        "pre{white-space:pre-wrap;}img{max-width:100%;}</style></head><body>"
        f"<pre>{html.escape(markdown)}</pre>{chart_html}</body></html>"
    )


def _report_manifest(root: Path) -> dict[str, object]:
    files = {
        path.name: {
            "size_bytes": path.stat().st_size,
            "sha256": sha256_bytes(path.read_bytes()),
        }
        for path in sorted(root.iterdir())
        if path.is_file() and path.name != "report_manifest.json"
    }
    return {
        "schema_version": "SupervisedTransferReportManifestV2",
        "file_count": len(files),
        "files": files,
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise RuntimeError("REPORT_CSV_EMPTY")
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    write_public_file(path, buffer.getvalue().encode())


def _mean(values: Any) -> float:
    items = list(values)
    return sum(items) / len(items)


__all__ = [
    "build_reports_v2",
    "export_final_html_to_windows_desktop_v2",
]
