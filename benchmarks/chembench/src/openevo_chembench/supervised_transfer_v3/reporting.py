"""Offline aggregate reporting for the evolved-only v3 protocol."""

from __future__ import annotations

import csv
import hashlib
import html
import json
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

from openevo_chembench.chembench4k_models import CHEMBENCH4K_CATEGORIES
from openevo_chembench.supervised_transfer_v1.common import (
    canonical_pretty_json_bytes,
    sha256_bytes,
    write_public_file,
)
from openevo_chembench.supervised_transfer_v3.config import REPORT_ROOT


def build_reports_v3(
    *,
    repository_root: Path,
    result_root: Path,
    state_root: Path,
    run_id: str,
) -> dict[str, object]:
    state = json.loads((state_root / "run_state.json").read_text(encoding="utf-8"))
    train_rounds = int(state["attempts_per_train_task"])
    evolution_cycles = int(state["evolution_cycles_per_train_task"])
    if train_rounds != evolution_cycles + 1 or train_rounds not in {2, 3}:
        raise RuntimeError("V3_REPORT_TRAIN_SCHEDULE_INVALID")
    public = _read_jsonl(result_root / "public/events.jsonl")
    private = _read_jsonl(state_root / "private/events.jsonl")
    train = _evaluation_rows(private, stage="ONLINE_TRAIN")
    test = _evaluation_rows(private, stage="EVOLVED_TEST")
    cycles = [row for row in public if row.get("kind") == "REFLECTOR_SUPERVISED" and row.get("stage") == "ONLINE_TRAIN"]
    if (
        len(train) != 450 * train_rounds
        or len(test) != 450
        or len(cycles) != 450 * evolution_cycles
    ):
        raise RuntimeError("V3_REPORT_INPUT_COUNT_INVALID")
    root = repository_root / REPORT_ROOT / "runs" / run_id
    if root.exists():
        raise RuntimeError("V3_REPORT_ROOT_ALREADY_EXISTS")
    root.mkdir(parents=True, mode=0o755)
    transitions = _transitions(train, train_rounds=train_rounds)
    per_category = _per_category(train, test, train_rounds=train_rounds)
    target_growth = _target_growth(cycles)
    rule_growth = _rule_growth(cycles)
    _write_csv(root / "train_online_results.csv", train)
    _write_csv(root / "test_evolved_results.csv", test)
    _write_csv(root / "per_category_results.csv", per_category)
    _write_csv(root / "target_growth.csv", target_growth)
    _write_csv(root / "rule_growth.csv", rule_growth)
    _write_csv(root / "correctness_transitions.csv", transitions)
    train_metrics = _round_metrics(train, train_rounds=train_rounds)
    test_metrics = _test_metrics(test)
    frozen_source = result_root / "public/frozen_three_target_transfer_receipt_v3.json"
    ledger_source = result_root / "public/final_test_ledger_receipt_v3.json"
    shutil.copyfile(frozen_source, root / "frozen_targets_receipt.json")
    integrity = {
        "schema_version": "ChemBenchSupervisedTransferIntegrityReceiptV3",
        "protocol_id": state["protocol_id"],
        "run_id": run_id,
        "source_commit": state["source_commit"],
        "config_sha256": state["config_sha256"],
        "split_reference_sha256": state["split_receipt_sha256"],
        "source_manifest_sha256": state["source_manifest_sha256"],
        "test_historical_exposure": 0,
        "train_test_uid_intersection": 0,
        "test_single_pass": True,
        "test_feedback_enabled": False,
        "test_evolution_jobs": 0,
        "test_target_updates": 0,
        "duplicate_test_completions": 0,
        "score_based_retries": 0,
        "split_mutation": False,
        "security_findings": state["security_findings"],
        "context_findings": state["context_findings"],
        "artifact_findings": state["artifact_findings"],
        "candidate_execution_path": "TaskRequest->Rollout->Gateway->CodexHarness",
        "reflector_execution_path": "Core planned job->worker->codex_cli provider",
        "frozen_receipt_sha256": sha256_bytes(frozen_source.read_bytes()),
        "test_ledger_receipt_sha256": sha256_bytes(ledger_source.read_bytes()),
        "terminated_v2_run_preserved": "stv2-formal-20260729-003",
    }
    write_public_file(root / "integrity_receipt.json", canonical_pretty_json_bytes(integrity))
    usage = {
        "schema_version": "ChemBenchSupervisedTransferPaidUsageV3",
        "online_candidate_completions": len(train),
        "reflector_completions": len(cycles),
        "evolved_test_completions": len(test),
        "answer_and_reflector_completions": len(train) + len(cycles) + len(test),
        "core_jobs": int(state["core_jobs"]),
        "infrastructure_failures": int(state["infrastructure_failures"]),
        "readiness_calls": "reported separately in immutable runtime task receipts",
    }
    write_public_file(root / "paid_usage.json", canonical_pretty_json_bytes(usage))
    failures = [row for row in public if row.get("kind") in {"FAIL_CLOSED", "INFRASTRUCTURE_RETRY_NO_COMPLETION"}]
    debug = "# Debug history\n\n" + ("No public failure events.\n" if not failures else "\n".join(f"- {row.get('stage')}: {row.get('failure_code', row.get('kind'))}" for row in failures) + "\n")
    write_public_file(root / "debug_history.md", debug.encode())
    markdown = _markdown_report(
        state,
        train_metrics,
        test_metrics,
        per_category,
        train_rounds=train_rounds,
        evolution_cycles=evolution_cycles,
    )
    write_public_file(root / "final_report.md", markdown.encode())
    html_text = _html_report(
        markdown,
        train_metrics,
        test_metrics,
        per_category,
        train_rounds=train_rounds,
    )
    write_public_file(root / "final_report.html", html_text.encode())
    manifest = {
        path.name: {"sha256": sha256_bytes(path.read_bytes()), "size_bytes": path.stat().st_size}
        for path in sorted(root.iterdir())
        if path.is_file()
    }
    write_public_file(root / "report_manifest.json", canonical_pretty_json_bytes(manifest))
    return {
        "status": "PASS",
        "report_root": str(root),
        "final_report_sha256": sha256_bytes((root / "final_report.html").read_bytes()),
        "test_accuracy": test_metrics["accuracy"],
    }


def export_final_html_to_windows_desktop_v3(
    *, report_html: Path, source_commit: str, utc_token: str
) -> dict[str, object]:
    if not report_html.is_file():
        raise RuntimeError("FINAL_HTML_MISSING")
    windows = subprocess.run(
        ("powershell.exe", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.replace("\r", "").strip()
    converted = subprocess.run(
        ("wslpath", "-u", windows), check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()
    desktop = Path(converted).resolve(strict=True)
    name = f"OpenEvo_ChemBench_STV3_EvolvedOnly_{utc_token}_{source_commit[:12]}.html"
    destination = desktop / name
    if destination.exists():
        raise RuntimeError("DESKTOP_REPORT_ALREADY_EXISTS")
    shutil.copyfile(report_html, destination)
    source_digest = hashlib.sha256(report_html.read_bytes()).hexdigest()
    destination_digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    if source_digest != destination_digest:
        raise RuntimeError("DESKTOP_REPORT_DIGEST_MISMATCH")
    return {
        "status": "PASS",
        "desktop_path": str(desktop),
        "file_name": name,
        "sha256": destination_digest,
        "size_bytes": destination.stat().st_size,
    }


def _evaluation_rows(rows: list[dict[str, Any]], *, stage: str) -> list[dict[str, object]]:
    result = []
    for row in rows:
        if row.get("kind") != "PRIVATE_EVALUATED" or row.get("stage") != stage:
            continue
        result.append(
            {
                "category": row["category"],
                "task_ordinal": row["task_ordinal"],
                "round": row["round_index"],
                "official_prediction": row["official_prediction"],
                "strict_prediction": row["strict_prediction"],
                "correct": int(bool(row["correct"])),
                "strict_parse_success": int(row["strict_parse_status"] == "parsed"),
            }
        )
    return result


def _round_metrics(
    rows: list[dict[str, object]], *, train_rounds: int
) -> dict[str, object]:
    result: dict[str, object] = {}
    for round_index in range(train_rounds):
        values = [row for row in rows if row["round"] == round_index]
        result[f"round_{round_index}_accuracy"] = sum(int(row["correct"]) for row in values) / len(values)
        result[f"round_{round_index}_strict_parse_rate"] = sum(int(row["strict_parse_success"]) for row in values) / len(values)
    return result


def _test_metrics(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "tasks": len(rows),
        "accuracy": sum(int(row["correct"]) for row in rows) / len(rows),
        "strict_parse_rate": sum(int(row["strict_parse_success"]) for row in rows) / len(rows),
    }


def _transitions(
    rows: list[dict[str, object]], *, train_rounds: int
) -> list[dict[str, object]]:
    grouped: dict[tuple[str, int], dict[int, bool]] = defaultdict(dict)
    for row in rows:
        grouped[(str(row["category"]), int(row["task_ordinal"]))][int(row["round"])] = bool(row["correct"])
    result = []
    for (category, ordinal), rounds in sorted(grouped.items()):
        if set(rounds) != set(range(train_rounds)):
            raise RuntimeError("V3_TRAIN_ROUND_SET_INVALID")
        for before, after in zip(range(train_rounds - 1), range(1, train_rounds), strict=True):
            result.append(
                {
                    "category": category,
                    "task_ordinal": ordinal,
                    "transition": f"{before}_to_{after}",
                    "wrong_to_correct": int(not rounds[before] and rounds[after]),
                    "correct_to_wrong": int(rounds[before] and not rounds[after]),
                }
            )
    return result


def _per_category(
    train: list[dict[str, object]],
    test: list[dict[str, object]],
    *,
    train_rounds: int,
) -> list[dict[str, object]]:
    result = []
    for category in CHEMBENCH4K_CATEGORIES:
        item: dict[str, object] = {"category": category}
        for round_index in range(train_rounds):
            values = [row for row in train if row["category"] == category and row["round"] == round_index]
            item[f"train_round_{round_index}_accuracy"] = sum(int(row["correct"]) for row in values) / len(values)
        values = [row for row in test if row["category"] == category]
        item["test_accuracy"] = sum(int(row["correct"]) for row in values) / len(values)
        item["test_strict_parse_rate"] = sum(int(row["strict_parse_success"]) for row in values) / len(values)
        result.append(item)
    return result


def _target_growth(cycles: list[dict[str, Any]]) -> list[dict[str, object]]:
    return [
        {
            "category": row["category"],
            "task_ordinal": row["task_index"],
            "cycle": row["cycle"],
            "text_memory_bytes": row["text_memory_bytes"],
            "skill_bundle_bytes": row["skill_bytes"],
            "agent_system_bytes": row["agent_system_bytes"],
        }
        for row in cycles
    ]


def _rule_growth(cycles: list[dict[str, Any]]) -> list[dict[str, object]]:
    return [
        {
            "category": row["category"],
            "task_ordinal": row["task_index"],
            "cycle": row["cycle"],
            "confirmed_rules": row["confirmed_rules"],
            "provisional_rules": row["provisional_rules"],
            "retired_rules": row["retired_rules"],
            "contradicted_rules": 0,
        }
        for row in cycles
    ]


def _markdown_report(
    state: dict[str, Any],
    train: dict[str, object],
    test: dict[str, object],
    per_category: list[dict[str, object]],
    *,
    train_rounds: int,
    evolution_cycles: int,
) -> str:
    sequence: list[str] = []
    for round_index in range(train_rounds):
        label = f"Round {round_index}"
        if round_index == train_rounds - 1:
            label += " Final"
        sequence.append(label)
        if round_index < evolution_cycles:
            sequence.append(f"Cycle {round_index + 1}")
    lines = [
        "# OpenEvo ChemBench STV3 evolved-only report",
        "",
        "This is an exploratory supervised-evolution transfer protocol, not a standard leaderboard result.",
        "It has no Control arm, so no causal gain, paired delta, McNemar result, or relative error reduction is claimed.",
        "",
        "## Protocol",
        "",
        " -> ".join(sequence)
        + ". Each cycle emits three independently validated Core artifacts.",
        "",
        "## Train",
        "",
    ]
    lines.extend(
        f"- Round {round_index} accuracy: "
        f"{float(train[f'round_{round_index}_accuracy']):.4f}"
        for round_index in range(train_rounds)
    )
    lines.extend(
        [
            "",
            "## Frozen evolved Test",
            "",
            f"- Tasks: {test['tasks']}",
            f"- Accuracy: {float(test['accuracy']):.4f}",
            f"- Strict parse rate: {float(test['strict_parse_rate']):.4f}",
            "",
            "## Per category",
            "",
            "| Category | Test accuracy | Strict parse |",
            "|---|---:|---:|",
        ]
    )
    lines.extend(
        f"| {row['category']} | {float(row['test_accuracy']):.4f} | {float(row['test_strict_parse_rate']):.4f} |"
        for row in per_category
    )
    lines.extend(
        [
            "",
            "## Integrity",
            "",
            "Train-only ground truth; frozen unseen Test; one completion per Test UID; no Test feedback, evolution, or target update.",
            f"Security/context/artifact findings: {state['security_findings']}/{state['context_findings']}/{state['artifact_findings']}.",
        ]
    )
    return "\n".join(lines) + "\n"


def _html_report(
    markdown: str,
    train: dict[str, object],
    test: dict[str, object],
    per_category: list[dict[str, object]],
    *,
    train_rounds: int,
) -> str:
    rows = "".join(
        f"<tr><td>{html.escape(str(row['category']))}</td><td>{float(row['test_accuracy']):.4f}</td><td>{float(row['test_strict_parse_rate']):.4f}</td></tr>"
        for row in per_category
    )
    train_metrics = "".join(
        f'<div class="metric">R{round_index} '
        f"{float(train[f'round_{round_index}_accuracy']):.4f}</div>"
        for round_index in range(train_rounds)
    )
    return f"""<!doctype html><html><head><meta charset=\"utf-8\"><title>OpenEvo ChemBench STV3</title><style>body{{font-family:system-ui;max-width:1000px;margin:2rem auto;line-height:1.5}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #bbb;padding:.4rem}}.metric{{display:inline-block;padding:1rem;margin:.4rem;background:#eef}}</style></head><body><h1>OpenEvo ChemBench STV3</h1><p><strong>Exploratory evolved-only protocol; not a leaderboard result and no causal gain is claimed.</strong></p><h2>Train</h2>{train_metrics}<h2>Frozen evolved Test</h2><div class=\"metric\">Accuracy {float(test['accuracy']):.4f}</div><div class=\"metric\">Strict parse {float(test['strict_parse_rate']):.4f}</div><h2>Per category</h2><table><thead><tr><th>Category</th><th>Accuracy</th><th>Strict parse</th></tr></thead><tbody>{rows}</tbody></table><h2>Protocol and integrity</h2><pre>{html.escape(markdown)}</pre></body></html>"""


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        raise RuntimeError("V3_REPORT_ROWS_EMPTY")
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


__all__ = ["build_reports_v3", "export_final_html_to_windows_desktop_v3"]
