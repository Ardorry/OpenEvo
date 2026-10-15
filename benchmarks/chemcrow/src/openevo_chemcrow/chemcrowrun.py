"""Portable lifecycle for the frozen ChemCrow benchmark (not the Desktop product)."""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
import random
import re
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml

PACKAGE = Path(__file__).resolve().parents[2]
REPOSITORY = PACKAGE.parents[1]


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("w") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def initialize(
    *,
    run_id: str,
    output_root: Path,
    fixture_root: Path,
    port_base: int,
    model: str = "gpt-5.5",
    substitution_reason: str | None = None,
) -> Path:
    from .submission import ModelSubstitution

    substitution = None
    if model != "gpt-5.5":
        substitution = ModelSubstitution(
            actual_model=model, reason=substitution_reason or ""
        ).model_dump()
    elif substitution_reason:
        raise ValueError("a substitution reason requires a different model")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", run_id):
        raise ValueError("run ID must be 1-80 lowercase letters, digits, underscores or hyphens")
    if not 1024 <= port_base <= 65530:
        raise ValueError("port base must be 1024..65530")
    root = output_root.resolve() / run_id
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    for directory in ("logs", "state", "runs", "cache", "claims", "sessions"):
        (root / directory).mkdir(mode=0o700)
    fixture_root = fixture_root.resolve()
    environment = {
        "CHEMCROW_EXPERIMENT_ID": f"chemcrowrun-{run_id}",
        "CHEMCROW_RUN_ROOT": str(root / "runs"),
        "CHEMCROW_CACHE_ROOT": str(root / "cache"),
        "CHEMCROW_LEDGER_ROOT": str(root / "claims"),
        "CHEMCROW_EVOLUTION_DB": str(root / "state/evolution.sqlite3"),
        "CHEMCROW_EVOLUTION_ARTIFACT_ROOT": str(root / "state/artifacts"),
        "CHEMCROW_CONTROLLED_CSV": str(
            fixture_root / "chemcrow-public/chemcrow/data/chem_wep_smi.csv"
        ),
        "OPENEVO_ROLLOUT_BASE_URL": f"http://127.0.0.1:{port_base}",
        "OPENEVO_EVOLUTION_BASE_URL": f"http://127.0.0.1:{port_base + 2}",
        "CHEMCROW_RXN_PREDICT_URL": f"http://127.0.0.1:{port_base + 3}/product_prediction",
        "CHEMCROW_RXN_RETRO_URL": f"http://127.0.0.1:{port_base + 3}/retro_prediction",
        "CHEMCROW_RXN_TIMEOUT_SECONDS": "600",
        **{
            f"OPENEVO_{role}_MODEL": model
            for role in ("CANDIDATE", "REFLECTOR", "EVOLUTION_EVALUATOR", "FINAL_EVALUATOR")
        },
    }
    config = yaml.safe_load((PACKAGE / "configs/chemcrowrun.reproduction.yaml").read_text())
    config["task_manifest"] = str(PACKAGE / "tasks.jsonl")
    if substitution:
        config["model_substitution"] = substitution
        for role in (
            config["candidate"],
            config["evolution_evaluator"],
            config["final_evaluator"],
            *config["reflectors"].values(),
        ):
            role["agent"]["model_name"] = model
    (root / "chemcrowrun.reproduction.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    topology = {
        "rollout": {
            "host": "127.0.0.1",
            "port": port_base,
            "public_url": environment["OPENEVO_ROLLOUT_BASE_URL"],
            "save_dir": str(root / "state/completions"),
            "dispatch_poll_interval_seconds": 1,
            "callback_grace_seconds": 120,
        },
        "gateway": {
            "heartbeat_interval_seconds": 5,
            "rollout_server_url": environment["OPENEVO_ROLLOUT_BASE_URL"],
            "completion_persistence": {
                "enabled": True,
                "max_field_bytes": 1048576,
                "queue_size": 128,
            },
            "nodes": [
                {
                    "id": f"chemcrowrun-{run_id}",
                    "host": "127.0.0.1",
                    "port": port_base + 1,
                    "public_url": f"http://127.0.0.1:{port_base + 1}",
                    "max_init_workers": 1,
                    "max_run_workers": 1,
                    "max_postrun_workers": 1,
                    "model_served": "subscription-codex-via-managed-runtime",
                    "session_base_dir": str(root / "sessions"),
                    "inference": {"engine": "vllm", "base_url": "http://127.0.0.1:8999"},
                }
            ],
        },
        "evolution": {
            "enabled": True,
            "backend_url": environment["OPENEVO_EVOLUTION_BASE_URL"],
            "context": {
                "target_dir": "/openevo/session/evolution",
                "timeout_seconds": 10,
                "fail_open": False,
            },
            "event_export": {"enabled": False, "timeout_seconds": 10, "fail_open": False},
        },
    }
    (root / "topology.yaml").write_text(yaml.safe_dump(topology, sort_keys=False))
    write_json(
        root / "run.json",
        {
            "schema_version": "chemcrowrun_workspace_v1",
            "display_protocol": "ChemCrowRun v1.0",
            "run_id": run_id,
            "created_at": datetime.now(UTC).isoformat(),
            "fixture_root": str(fixture_root),
            "port_base": port_base,
            "environment": environment,
            "protocol_identity": "chemcrow_full_v8_reproduction_v1",
            "model_substitution": substitution,
            "historical_state_imported": False,
            "source_commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True
            ).strip(),
            "config_sha256": hashlib.sha256(
                (root / "chemcrowrun.reproduction.yaml").read_bytes()
            ).hexdigest(),
            "topology_sha256": hashlib.sha256((root / "topology.yaml").read_bytes()).hexdigest(),
            "environment_sha256": hashlib.sha256(
                json.dumps(environment, sort_keys=True).encode()
            ).hexdigest(),
        },
    )
    return root


def verify_workspace_binding(root: Path) -> None:
    """Prevent changed code/configuration from being attributed to a completed run."""
    record = json.loads((root / "run.json").read_text())
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPOSITORY, text=True).strip()
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain=v1"], cwd=REPOSITORY, text=True
    ).strip()
    if dirty or record.get("source_commit") != head:
        raise ValueError(
            "workspace requires its original clean source commit; use a new workspace"
        )
    for filename, field in (
        ("chemcrowrun.reproduction.yaml", "config_sha256"),
        ("topology.yaml", "topology_sha256"),
    ):
        if hashlib.sha256((root / filename).read_bytes()).hexdigest() != record.get(field):
            raise ValueError(f"workspace {filename} differs from its initialized identity")
    digest = hashlib.sha256(json.dumps(record["environment"], sort_keys=True).encode()).hexdigest()
    if digest != record.get("environment_sha256"):
        raise ValueError("workspace environment differs from its initialized identity")


def environment(root: Path) -> dict[str, str]:
    record = json.loads((root / "run.json").read_text())
    return {
        **os.environ,
        **record["environment"],
        "PYTHONUNBUFFERED": "1",
        "NO_PROXY": "localhost,127.0.0.1,::1",
        "no_proxy": "localhost,127.0.0.1,::1",
    }


def process_identity(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def services_status(root: Path) -> dict:
    record = json.loads((root / "run.json").read_text())
    base = record["port_base"]
    checks = {}
    with httpx.Client(trust_env=False, timeout=3) as client:
        for service, port, route in [
            ("rollout", base, "/health"),
            ("evolution", base + 2, "/v1/health"),
            ("rxn", base + 3, "/openapi.json"),
        ]:
            try:
                response = client.get(f"http://127.0.0.1:{port}{route}")
                checks[service] = response.status_code == 200
            except httpx.HTTPError:
                checks[service] = False
        try:
            response = client.get(f"http://127.0.0.1:{base}/nodes")
            response.raise_for_status()
            nodes = response.json()
            checks["gateway"] = any(
                n.get("healthy")
                and n.get("node_id", n.get("id")) == f"chemcrowrun-{record['run_id']}"
                for n in nodes
            )
        except (httpx.HTTPError, TypeError, ValueError):
            checks["gateway"] = False
    return {
        "status": "READY" if all(checks.values()) else "BLOCKED",
        "services": checks,
        "model_calls": 0,
    }


def compose_command(root: Path) -> list[str]:
    record = json.loads((root / "run.json").read_text())
    return [
        "docker",
        "compose",
        "--project-name",
        f"chemcrowrun-{record['run_id']}",
        "--file",
        str(root / "rxn.compose.yaml"),
    ]


def services_up(root: Path, *, rxn: bool = True) -> dict:
    root = root.resolve()
    record = json.loads((root / "run.json").read_text())
    env = environment(root)
    registry_path = root / "services.json"
    registry = json.loads(registry_path.read_text()) if registry_path.exists() else {}
    if rxn:
        compose = yaml.safe_load((PACKAGE / "deployment/rxn.compose.yaml").read_text())
        # Only data/code fixtures from the public source checkout are mounted.
        fixture = Path(record["fixture_root"]) / "rxn-sandbox"
        for service in ("worker", "mcp"):
            compose["services"][service]["build"]["context"] = str(fixture / service)
            compose["services"][service]["build"]["dockerfile"] = str(
                PACKAGE / f"deployment/rxn-{service}.Dockerfile"
            )
        compose["services"]["worker"]["volumes"] = [
            f"{fixture / d}:/app/{d}:ro" for d in ("models", "vocab", "scripts")
        ]
        compose["services"]["mcp"]["ports"] = [f"127.0.0.1:{record['port_base'] + 3}:8000"]
        (root / "rxn.compose.yaml").write_text(yaml.safe_dump(compose, sort_keys=False))
        with (root / "logs/rxn-compose.log").open("a") as log:
            subprocess.run(
                compose_command(root) + ["up", "-d", "--build", "--wait", "--wait-timeout", "300"],
                env=env,
                stdout=log,
                stderr=log,
                check=True,
                timeout=1800,
            )
    commands = {
        "evolution": [
            sys.executable,
            "-m",
            "openevo_chemcrow.chemcrowrun",
            "_evolution",
            "--workspace",
            str(root),
        ],
        "rollout": [
            sys.executable,
            "-m",
            "openevo.rollout.server",
            "--config",
            str(root / "topology.yaml"),
        ],
        "gateway": [
            sys.executable,
            "-m",
            "openevo.gateway.server",
            "--config",
            str(root / "topology.yaml"),
            "--node-id",
            f"chemcrowrun-{record['run_id']}",
        ],
    }
    for name, command in commands.items():
        previous = registry.get(name)
        if previous and process_identity(previous["pid"]) == previous["start_ticks"]:
            continue
        with (root / f"logs/{name}.log").open("a") as log:
            process = subprocess.Popen(
                command, env=env, cwd=REPOSITORY, stdout=log, stderr=log, start_new_session=True
            )
        registry[name] = {"pid": process.pid, "start_ticks": process_identity(process.pid)}
        write_json(registry_path, registry)
        time.sleep(0.5)
    for _ in range(30):
        report = services_status(root)
        if all(report["services"][k] for k in ("rollout", "evolution", "gateway")):
            break
        time.sleep(1)
    write_json(root / "service-health.json", report)
    return report


def services_down(root: Path) -> None:
    path = root / "services.json"
    registry = json.loads(path.read_text()) if path.exists() else {}
    for entry in reversed(list(registry.values())):
        if (
            entry["start_ticks"] is not None
            and process_identity(entry["pid"]) == entry["start_ticks"]
        ):
            os.killpg(entry["pid"], signal.SIGTERM)
    if (root / "rxn.compose.yaml").exists():
        subprocess.run(compose_command(root) + ["down"], check=True, timeout=120)
    write_json(path, {})


def paired_statistics(g1: list[float], g2: list[float]) -> dict:
    if (
        not g1
        or len(g1) != len(g2)
        or len(g1) > 20
        or not all(math.isfinite(x) for x in [*g1, *g2])
    ):
        raise ValueError("expected 1-20 paired finite scores")
    delta = [b - a for a, b in zip(g1, g2, strict=True)]
    n = len(delta)
    observed = abs(sum(delta))
    exceed = sum(
        abs(sum(x * s for x, s in zip(delta, signs, strict=True))) >= observed - 1e-12
        for signs in itertools.product((-1, 1), repeat=n)
    )
    rng = random.Random(20260825)
    bootstrap = sorted(sum(rng.choices(delta, k=n)) / n for _ in range(10000))
    return {
        "n": n,
        "mean_g1": sum(g1) / n,
        "mean_g2": sum(g2) / n,
        "mean_delta": sum(delta) / n,
        "bootstrap_95_ci": [bootstrap[249], bootstrap[9749]],
        "bootstrap_resamples": 10000,
        "seed": 20260825,
        "exact_sign_flip_p_two_sided": exceed / (2**n),
        "wins": sum(x > 0 for x in delta),
        "ties": delta.count(0),
        "losses": sum(x < 0 for x in delta),
        "test_assumption": "paired differences exchangeable under sign reversal under the null",
    }


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Existing advanced commands retain their exact protocol and CLI contracts.
    custom = {
        "init",
        "up",
        "down",
        "status",
        "runtime",
        "experiment",
        "report",
        "judge",
        "_evolution",
    }
    if argv and argv[0] not in custom and argv[0] not in {"-h", "--help"}:
        from .cli import main as legacy_main

        legacy_main(argv)
        return
    parser = argparse.ArgumentParser(
        prog="chemcrowrun", description="ChemCrowRun v1.0 reproduction"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create an isolated experiment workspace")
    init.add_argument("--run-id", required=True)
    init.add_argument("--output-root", type=Path, default=Path("outputs/chemcrowrun"))
    init.add_argument("--fixture-root", type=Path, required=True)
    init.add_argument("--port-base", type=int, default=18080)
    init.add_argument("--model", default="gpt-5.5")
    init.add_argument("--substitution-reason")
    for command in ("up", "down", "status", "experiment", "report", "judge", "_evolution"):
        p = commands.add_parser(command)
        p.add_argument("--workspace", type=Path, required=True)
        if command == "up":
            p.add_argument("--without-rxn", action="store_true")
        if command == "experiment":
            p.add_argument("--allow-paid", action="store_true", required=True)
            p.add_argument("--smoke", action="store_true")
            p.add_argument("--resume", action="store_true")
        if command == "report":
            p.add_argument("--smoke", action="store_true")
        if command == "judge":
            p.add_argument("--env-file", type=Path, required=True)
            p.add_argument("--budget-usd", type=float, required=True)
            p.add_argument("--allow-paid", action="store_true", required=True)
    runtime = commands.add_parser(
        "runtime", help="Verify/download and load the original OCI archive"
    )
    source = runtime.add_mutually_exclusive_group(required=True)
    source.add_argument("--archive", type=Path)
    source.add_argument("--url")
    runtime.add_argument("--cache", type=Path, default=Path(".reproduction-cache/runtime"))
    runtime.add_argument("--verify-only", action="store_true")
    runtime.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.command == "init":
        print(
            initialize(
                run_id=args.run_id,
                output_root=args.output_root,
                fixture_root=args.fixture_root,
                port_base=args.port_base,
                model=args.model,
                substitution_reason=args.substitution_reason,
            )
        )
    elif args.command == "runtime":
        from .runtime_assets import install_runtime

        result = install_runtime(
            archive=args.archive, url=args.url, cache=args.cache, load=not args.verify_only
        )
        if args.output:
            write_json(args.output, result)
        print(json.dumps(result, indent=2))
    elif args.command == "up":
        result = services_up(args.workspace, rxn=not args.without_rxn)
        print(json.dumps(result))
        raise SystemExit(0 if result["status"] == "READY" else 2)
    elif args.command == "down":
        services_down(args.workspace)
    elif args.command == "status":
        result = services_status(args.workspace)
        print(json.dumps(result))
        raise SystemExit(0 if result["status"] == "READY" else 2)
    elif args.command == "_evolution":
        # This benchmark uses the retained v1 API, including its inline native
        # worker claims. It is not the framework-locked Desktop/Daemon release.
        import uvicorn
        from openevo.evolution.server import create_app

        env = environment(args.workspace)
        record = json.loads((args.workspace / "run.json").read_text())
        app = create_app(
            db_path=env["CHEMCROW_EVOLUTION_DB"],
            artifact_root=env["CHEMCROW_EVOLUTION_ARTIFACT_ROOT"],
        )
        uvicorn.run(app, host="127.0.0.1", port=record["port_base"] + 2)
    elif args.command == "experiment":
        from .cli import build_parser

        verify_workspace_binding(args.workspace)
        os.environ.update(environment(args.workspace))
        if services_status(args.workspace)["status"] != "READY":
            raise SystemExit("required services are not ready; run chemcrowrun status")
        config = str(args.workspace.resolve() / "chemcrowrun.reproduction.yaml")
        preflight = build_parser().parse_args(
            ["preflight", "--config", config, "--no-model-calls"]
        )
        if preflight.function(preflight) != 0:
            raise SystemExit(2)
        arguments = ["resume" if args.resume else "run", "--config", config, "--allow-paid"]
        if args.smoke:
            arguments += ["--stop-after-task-id", "chemcrow-01"]
        run = build_parser().parse_args(arguments)
        started = datetime.now(UTC)
        receipt = {
            "started_at": started.isoformat(),
            "scope": "smoke" if args.smoke else "full",
            "resume": args.resume,
            "automatic_retries": 0,
            "planned_logical_calls": 8 if args.smoke else 112,
            "additional_readiness_calls": "Managed isolation canaries are additional subscription turns",
            "status": "ERROR",
        }
        receipt_path = (
            args.workspace / "logs" / f"execution-{started.strftime('%Y%m%dT%H%M%S%fZ')}.json"
        )
        write_json(receipt_path, receipt)
        try:
            code = run.function(run)
            receipt["status"] = "COMPLETED" if code == 0 else "ERROR"
            receipt["exit_code"] = code
        finally:
            completed = datetime.now(UTC)
            receipt["completed_at"] = completed.isoformat()
            receipt["elapsed_seconds"] = (completed - started).total_seconds()
            write_json(receipt_path, receipt)
        raise SystemExit(code)
    elif args.command == "report":
        from .workflow import export_report

        report = export_report(args.workspace, smoke=args.smoke)
        print(
            json.dumps(
                {k: report[k] for k in ("status", "completed_tasks", "audit", "submission")}
            )
        )
        raise SystemExit(0 if report["status"] != "BLOCKED" else 2)
    elif args.command == "judge":
        from .workflow import run_judge

        print(
            json.dumps(
                run_judge(
                    args.workspace,
                    credential_file=args.env_file,
                    budget_usd=args.budget_usd,
                    allow_paid=args.allow_paid,
                )
            )
        )


if __name__ == "__main__":
    main()
