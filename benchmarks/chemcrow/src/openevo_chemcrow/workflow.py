"""Audited exports and the separate, sealed-output GPT-4 evaluation lifecycle."""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml

from .chemcrowrun import (
    PACKAGE,
    REPOSITORY,
    environment,
    paired_statistics,
    verify_workspace_binding,
    write_json,
)
from .hashing import file_sha256
from .paper_evaluator import (
    FROZEN_PAPER_TASK_IDS,
    PAPER_EVALUATOR_AUTHORIZATION,
    paper_cost_ceiling,
)
from .three_artifact_models import ThreeArtifactPairResult


def invoke(arguments: list[str]) -> None:
    from .cli import build_parser

    args = build_parser().parse_args(arguments)
    if args.function(args) != 0:
        raise RuntimeError(f"{arguments[0]} did not pass; inspect its report")


def export_report(root: Path, *, smoke: bool = False) -> dict:
    root = root.resolve()
    os.environ.update(environment(root))
    record = json.loads((root / "run.json").read_text())
    config = root / "chemcrowrun.reproduction.yaml"
    ids = FROZEN_PAPER_TASK_IDS[:1] if smoke else FROZEN_PAPER_TASK_IDS
    results = []
    missing = []
    for task_id in ids:
        path = (
            root
            / "runs"
            / f"{record['environment']['CHEMCROW_EXPERIMENT_ID']}--{task_id}"
            / "pair.result.json"
        )
        if not path.is_file():
            missing.append(task_id)
        else:
            results.append(ThreeArtifactPairResult.model_validate_json(path.read_text()))
    report = {
        "schema_version": "chemcrowrun_report_v1",
        "display_protocol": "ChemCrowRun v1.0",
        "protocol_identity": record["protocol_identity"],
        "model_substitution": record.get("model_substitution"),
        "run_id": record["run_id"],
        "scope": "independent_single_task_smoke" if smoke else "full_14_task_reproduction",
        "status": "BLOCKED",
        "completed_tasks": len(results),
        "missing_tasks": missing,
        "source_commit": record.get("source_commit"),
        "config_sha256": file_sha256(config),
        "audit": "PENDING",
        "submission": "NOT_ELIGIBLE_SMOKE" if smoke else "PENDING",
        "paper_judge": "PENDING",
        "historical_results_reused": False,
        "subscription_cost_usd": None,
        "cost_limitation": "Subscription cost is not attributable from API credits. Judge receipts are separate.",
        "server_model_snapshot": "Not guaranteed pinned by the subscription service",
        "requested_model": record["environment"]["OPENEVO_CANDIDATE_MODEL"],
        "generated_at": datetime.now(UTC).isoformat(),
        "execution_attempts": [
            json.loads(path.read_text())
            for path in sorted((root / "logs").glob("execution-*.json"))
        ],
    }
    completions = [
        json.loads(path.read_text())
        for path in sorted((root / "state/completions").glob("task_*/ses_*.json"))
    ]
    report["core_call_accounting"] = {
        "terminal_records": len(completions),
        "completed": sum(c.get("status") == "COMPLETED" for c in completions),
        "failed": sum(c.get("status") != "COMPLETED" for c in completions),
        "total_core_run_seconds": sum(
            c.get("timing", {}).get("run_ms", 0) or 0 for c in completions
        )
        / 1000,
        "token_count": None,
        "limitation": "Transcript capture does not expose provider token totals; readiness canaries are not separate terminal records",
    }
    if results:
        g1 = [sum(r.final_evaluation.baseline_scores.model_dump().values()) for r in results]
        g2 = [sum(r.final_evaluation.evolved_scores.model_dump().values()) for r in results]
        report["internal_blind_statistics"] = paired_statistics(g1, g2)
        report["per_task"] = [
            {"task_id": r.task_id, "g1": a, "g2": b}
            for r, a, b in zip(results, g1, g2, strict=True)
        ]
    write_json(root / "chemcrowrun-report.json", report)
    if missing:
        return report
    suffix = ["--stop-after-task-id", ids[-1]] if smoke else []
    try:
        verify_workspace_binding(root)
        invoke(["aggregate-run", "--config", str(config), "--no-model-calls", *suffix])
        audit = root / "runs/completed-run.audit.json"
        invoke(
            [
                "audit-run",
                "--config",
                str(config),
                "--core-completions",
                str(root / "state/completions"),
                "--output",
                str(audit),
                *suffix,
            ]
        )
        audit_payload = json.loads(audit.read_text())
        report["audit"] = audit_payload["status"]
        if report["audit"] != "PASS":
            raise ValueError("completed-run audit did not pass")
        report["status"] = "SMOKE_PASS" if smoke else "INTERNAL_EXPERIMENT_PASS"
        if not smoke:
            invoke(
                [
                    "export-submission",
                    "--config",
                    str(config),
                    "--audit",
                    str(audit),
                    "--aggregate",
                    str(root / "runs/aggregate.json"),
                    "--repository-root",
                    str(REPOSITORY),
                    "--source-commit",
                    report["source_commit"],
                    "--provider",
                    "OpenAI Codex subscription",
                    "--started-at",
                    record["created_at"],
                    "--completed-at",
                    report["generated_at"],
                    "--output",
                    str(root / "chemcrowrun-submission.json"),
                    "--no-model-calls",
                ]
            )
            invoke(
                [
                    "validate-submission",
                    "--submission",
                    str(root / "chemcrowrun-submission.json"),
                    "--no-model-calls",
                ]
            )
            report["submission"] = "PASS"
            report["historical_reference"] = {
                "legacy_alias": "full-v8",
                "internal_blind_g1": 9.928571428571429,
                "internal_blind_g2": 10.285714285714286,
                "paper_g1": 9.357142857142858,
                "paper_g2": 9.214285714285714,
                "no_significant_g2_improvement_established": True,
                "provenance_manifest": "reproduction/legacy-aliases.json",
            }
        aggregate = root / "judge/results/aggregate.json"
        if aggregate.is_file():
            report["paper_judge"] = json.loads(aggregate.read_text())
    except (ValueError, RuntimeError, OSError) as exc:
        report["status"] = "BLOCKED"
        report["blocking_reason"] = str(exc)
    write_json(root / "chemcrowrun-report.json", report)
    return report


def load_private_environment(path: Path) -> None:
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError("credential file must be a regular, private 0600 file")
    allowed = {
        "OPENROUTER_API_KEY",
        "OPENROUTER_BASE_URL",
        "CHEMCROW_PAPER_EVALUATOR_MODEL",
        "CHEMCROW_PAPER_EVALUATOR_MAX_USD",
        "https_proxy",
        "HTTPS_PROXY",
    }
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        key = key.strip()
        if key not in allowed:
            raise ValueError(f"unsupported credential variable: {key}")
        values = shlex.split(value, comments=True)
        if len(values) > 1:
            raise ValueError("credential values must be one literal string")
        os.environ[key] = values[0] if values else ""


def run_judge(root: Path, *, credential_file: Path, budget_usd: float, allow_paid: bool) -> dict:
    if not allow_paid:
        raise PermissionError("Judge requires --allow-paid")
    root = root.resolve()
    verify_workspace_binding(root)
    load_private_environment(credential_file)
    ceiling = paper_cost_ceiling()["list_price_ceiling_usd_total"]
    if not ceiling <= budget_usd <= 15:
        raise ValueError(f"budget must cover the frozen 42-call ceiling {ceiling} and be <=15 USD")
    key = os.environ.get("OPENROUTER_API_KEY")
    proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    if not key or not proxy:
        raise ValueError("OPENROUTER_API_KEY and an explicit HTTPS proxy are required")
    with httpx.Client(proxy=proxy, trust_env=False, timeout=20) as client:
        response = client.get(
            "https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {key}"}
        )
        response.raise_for_status()
        credit = response.json()["data"]
        remaining = credit["total_credits"] - credit["total_usage"]
    if remaining < budget_usd:
        raise ValueError("available credit is below the requested Judge budget")
    os.environ.update(
        {
            "CHEMCROW_PAPER_EVALUATOR_MODEL": "openai/gpt-4",
            "CHEMCROW_PAPER_EVALUATOR_AUTHORIZATION": PAPER_EVALUATOR_AUTHORIZATION,
            "CHEMCROW_PAPER_EVALUATOR_MAX_USD": str(budget_usd),
        }
    )
    record = json.loads((root / "run.json").read_text())
    judge = root / "judge"
    judge.mkdir(exist_ok=True, mode=0o700)
    config = yaml.safe_load(
        (
            PACKAGE
            / "configs/paper_evaluator.full-v8-core-full-worker-g2-continuation-20260831T052113Z-r2.yaml"
        ).read_text()
    )
    config.update(
        experiment_id=record["environment"]["CHEMCROW_EXPERIMENT_ID"],
        call_id_prefix=f"paper-chemcrowrun-{record['run_id'].replace('_', '-')}",
        task_manifest=str(PACKAGE / "tasks.jsonl"),
        historical_runs_root=str(Path(record["fixture_root"]) / "chemcrow-runs"),
        run_root=str(root / "runs"),
        completed_run_audit=str(root / "runs/completed-run.audit.json"),
        plan_path=str(judge / "private/plan.json"),
        result_root=str(judge / "results"),
        shim_receipt_root=str(judge / "openrouter-receipts"),
    )
    config_path = judge / "judge.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    invoke(
        [
            "paper-evaluator-preflight",
            "--config",
            str(config_path),
            "--output",
            str(judge / "preflight.json"),
            "--no-model-calls",
        ]
    )
    # Fixed port 8110 is part of the frozen PaperEvaluatorHarness contract.
    for port in (8110, 8180, 8400):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", port))
    topology = yaml.safe_load(
        (PACKAGE / "configs/openevo_paper_evaluator_topology.full-v8-20260831.yaml").read_text()
    )
    topology["rollout"]["save_dir"] = str(judge / "completions")
    (judge / "sessions").mkdir(exist_ok=True, mode=0o700)
    topology["gateway"]["nodes"][0]["session_base_dir"] = str(judge / "sessions")
    topology_path = judge / "topology.yaml"
    topology_path.write_text(yaml.safe_dump(topology, sort_keys=False))
    commands = [
        [
            "openevo_chemcrow.openrouter_shim",
            "--plan",
            str(judge / "private/plan.json"),
            "--receipt-root",
            str(judge / "openrouter-receipts"),
            "--port",
            "8400",
            "--use-environment-proxy",
        ],
        ["openevo.rollout.server", "--config", str(topology_path)],
        [
            "openevo.gateway.server",
            "--config",
            str(topology_path),
            "--node-id",
            "chemcrow-paper-gateway-01",
        ],
    ]
    processes = []
    write_json(
        judge / "budget.json",
        {
            "max_usd": budget_usd,
            "frozen_ceiling_usd": ceiling,
            "credit_before_usd": remaining,
            "max_logical_calls": 42,
            "automatic_retries": 0,
        },
    )
    try:
        for index, command in enumerate(commands):
            with (judge / f"service-{index}.log").open("a") as log:
                processes.append(
                    subprocess.Popen(
                        [sys.executable, "-m", *command],
                        stdout=log,
                        stderr=log,
                        env=environment(root),
                        cwd=REPOSITORY,
                    )
                )
        with httpx.Client(trust_env=False, timeout=2) as client:
            ready = False
            for _ in range(60):
                if any(p.poll() is not None for p in processes):
                    raise RuntimeError("Judge service exited; inspect private service logs")
                try:
                    shim = client.get("http://127.0.0.1:8400/health")
                    nodes = client.get("http://127.0.0.1:8180/nodes")
                    ready = shim.status_code == 200 and any(n.get("healthy") for n in nodes.json())
                except (httpx.HTTPError, ValueError, TypeError):
                    pass
                if ready:
                    break
                time.sleep(1)
        if not ready:
            raise RuntimeError("Judge services failed health checks")
        invoke(["paper-evaluator-run", "--config", str(config_path), "--allow-paid"])
    finally:
        for process in reversed(processes):
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
    return {"status": "COMPLETE", "result_root": str(judge / "results")}
