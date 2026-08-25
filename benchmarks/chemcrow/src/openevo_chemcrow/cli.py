from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import yaml

from .aggregate import aggregate_results
from .evaluation import OpenEvoEvolutionEvaluator, OpenEvoFinalEvaluator
from .hashing import file_sha256
from .ledger import AmbiguousPhaseClaimError, PhaseLedger
from .models import ArtifactKind, FeedbackMode, PairResult, TaskItem
from .native_evolution import NativeEvolutionEngine
from .protocol import TaskLocalProtocolRunner
from .runtime import (
    CORE_MANAGED_CODEX_ROUTE,
    OpenEvoRolloutPort,
    assert_core_managed_codex_config,
    build_task_request,
    s0_config_hash,
)
from .tasks import extract_safety_demonstrations, extract_scored_tasks, write_jsonl
from .tool_service import PairToolService
from .tools import TOOL_INVENTORY, ChemCrowToolRegistry, environment_presence

_ENV_PATTERN = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")
_PAID_AUTHORIZATION = "I_UNDERSTAND_THIS_MAY_INCUR_COST"


def _read_tasks(path: Path) -> list[TaskItem]:
    items = [
        TaskItem.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not items:
        raise ValueError("task manifest is empty")
    return items


def _load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("experiment config must be a YAML object")
    return value


def _env_names(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, str):
        match = _ENV_PATTERN.fullmatch(value)
        if match:
            found.add(match.group(1))
    elif isinstance(value, list):
        for item in value:
            found.update(_env_names(item))
    elif isinstance(value, dict):
        for item in value.values():
            found.update(_env_names(item))
    return found


def _resolve_env(value: Any) -> Any:
    if isinstance(value, str):
        match = _ENV_PATTERN.fullmatch(value)
        if not match:
            return value
        name = match.group(1)
        resolved = os.environ.get(name)
        if not resolved:
            raise ValueError(f"required environment variable is absent: {name}")
        return resolved
    if isinstance(value, list):
        return [_resolve_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_env(item) for key, item in value.items()}
    return value


def _path(config_path: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (config_path.parent / path).resolve()


def command_extract(args: argparse.Namespace) -> int:
    items, audit = extract_scored_tasks(args.runs_root.resolve())
    write_jsonl(items, args.output.resolve())
    safety_items = extract_safety_demonstrations(args.runs_root.resolve())
    write_jsonl(safety_items, args.safety_output.resolve())
    args.audit_output.resolve().parent.mkdir(parents=True, exist_ok=True)
    args.audit_output.resolve().write_text(
        json.dumps(audit, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "task_count": len(items),
                "safety_task_count": len(safety_items),
                "manifest_sha256": file_sha256(args.output.resolve()),
                "audit": "PASS",
            },
            sort_keys=True,
        )
    )
    return 0


def command_inventory(args: argparse.Namespace) -> int:
    payload = {
        "schema_version": "chemcrow_tool_inventory_v1",
        "chemcrow_public_commit": "e7ebd5193334ac1d8dea137b635721c7cb470d33",
        "tools": list(TOOL_INVENTORY),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"tool_entries": len(TOOL_INVENTORY), "inventory_sha256": file_sha256(args.output)}, sort_keys=True))
    return 0


def command_tool_smoke(args: argparse.Namespace) -> int:
    registry = ChemCrowToolRegistry(
        controlled_chemicals_csv=args.controlled_chemicals_csv,
        network_enabled=False,
    )
    checks = {
        "SMILES2Weight": registry.execute("SMILES2Weight", {"query": "CCO"}, call_id="smoke-weight"),
        "MolSimilarity": registry.execute("MolSimilarity", {"query": "CCO.CCOC"}, call_id="smoke-similarity"),
        "FunctionalGroups": registry.execute("FunctionalGroups", {"query": "CCO"}, call_id="smoke-groups"),
        "ControlChemCheck": registry.execute("ControlChemCheck", {"query": "CCO"}, call_id="smoke-control"),
        "SimilarityToControlChem": registry.execute(
            "SimilarityToControlChem", {"query": "CCO"}, call_id="smoke-control-similarity"
        ),
        "PatentCheck": registry.execute("PatentCheck", {"query": "CCO"}, call_id="smoke-patent"),
    }
    payload = {
        "schema_version": "chemcrow_local_tool_smoke_v1",
        "status": "PASS",
        "network_calls": 0,
        "model_calls": 0,
        "checks": {name: value.model_dump(mode="json") for name, value in checks.items()},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": "PASS", "checks": sorted(checks)}, sort_keys=True))
    return 0


def command_preflight(args: argparse.Namespace) -> int:
    config_path = args.config.resolve()
    config = _load_yaml(config_path)
    manifest = _path(config_path, str(config["task_manifest"]))
    items = _read_tasks(manifest)
    ids = [item.task_id for item in items]
    selected = ids if config.get("task_ids") == "all" else list(config.get("task_ids", []))
    unknown = sorted(set(selected) - set(ids))
    env_names = sorted(_env_names(config))
    candidate = config["candidate"]
    role_configs = {
        role: config[role]
        for role in ("candidate", "reflector", "evolution_evaluator", "final_evaluator")
    }
    core_route_error = None
    try:
        for role_config in role_configs.values():
            assert_core_managed_codex_config(role_config)
    except ValueError as exc:
        core_route_error = str(exc)
    s0_hash = s0_config_hash(candidate)
    baseline_request = build_task_request(
        task=items[0], run_id="preflight-baseline", role="baseline", candidate=candidate, artifact_ids=[], mcp_url="http://127.0.0.1:9/mcp"
    )
    evolved_request = build_task_request(
        task=items[0], run_id="preflight-evolved", role="evolved", candidate=candidate, artifact_ids=["art-preflight"], mcp_url="http://127.0.0.1:9/mcp"
    )
    parity = (
        baseline_request["agent"] == evolved_request["agent"]
        and baseline_request["runtime"] == evolved_request["runtime"]
        and baseline_request["builder"] == evolved_request["builder"]
        and baseline_request["instruction"] == evolved_request["instruction"]
    )
    docker_ok = False
    managed_image_ok = False
    managed_image_id = None
    if shutil.which("docker"):
        check = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        docker_ok = check.returncode == 0
        if docker_ok:
            image_check = subprocess.run(
                [
                    "docker",
                    "image",
                    "inspect",
                    str(candidate["runtime"]["image"]),
                    "--format",
                    '{{.Id}}|{{index .Config.Labels "io.openevo.managed-runtime"}}',
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            image_parts = image_check.stdout.strip().split("|", 1)
            managed_image_ok = (
                image_check.returncode == 0
                and len(image_parts) == 2
                and image_parts[0] == candidate["runtime"]["image"]
                and image_parts[1] == "true"
            )
            managed_image_id = image_parts[0] if image_parts and image_parts[0] else None
    core_health: dict[str, Any] = {"reachable": False, "healthy_nodes": 0}
    rollout_url = os.environ.get("OPENEVO_ROLLOUT_BASE_URL")
    if rollout_url:
        try:
            health = httpx.get(rollout_url.rstrip("/") + "/health", timeout=5.0)
            nodes = httpx.get(rollout_url.rstrip("/") + "/nodes", timeout=5.0)
            health.raise_for_status()
            nodes.raise_for_status()
            node_payload = nodes.json()
            core_health = {
                "reachable": health.json().get("status") == "ok",
                "registered_nodes": len(node_payload) if isinstance(node_payload, list) else 0,
                "healthy_nodes": (
                    sum(bool(item.get("healthy")) for item in node_payload)
                    if isinstance(node_payload, list)
                    else 0
                ),
            }
        except (httpx.HTTPError, TypeError, ValueError):
            pass
    rxn_health: dict[str, Any] = {"configured": False, "reachable": False, "paths": []}
    rxn_url = os.environ.get("CHEMCROW_RXN_PREDICT_URL")
    if rxn_url:
        rxn_health["configured"] = True
        parsed = urlsplit(rxn_url)
        openapi_url = urlunsplit((parsed.scheme, parsed.netloc, "/openapi.json", "", ""))
        try:
            response = httpx.get(openapi_url, timeout=5.0)
            response.raise_for_status()
            paths = response.json().get("paths", {})
            rxn_health = {
                "configured": True,
                "reachable": isinstance(paths, dict),
                "paths": sorted(paths) if isinstance(paths, dict) else [],
            }
        except (httpx.HTTPError, TypeError, ValueError):
            pass
    codex_auth = Path.home() / ".codex" / "auth.json"
    codex_auth_metadata = {
        "credential_name": "Codex subscription auth.json",
        "present": codex_auth.is_file(),
        "mode": oct(codex_auth.stat().st_mode & 0o777) if codex_auth.is_file() else None,
        "owner_uid": codex_auth.stat().st_uid if codex_auth.is_file() else None,
    }
    checks = {
        "no_model_calls": True,
        "unknown_selected_task_ids": unknown,
        "manifest_count": len(items),
        "manifest_frozen_count_is_14": len(items) == 14,
        "candidate_pair_parity": parity,
        "historical_answer_fields_absent": all(
            not {"answer", "trajectory", "evaluator_feedback", "reference_answer"}.intersection(item.model_fields_set)
            for item in items
        ),
        "python_version_supported": tuple(__import__("sys").version_info[:2]) == (3, 11),
        "rdkit_importable": importlib.util.find_spec("rdkit") is not None,
        "mcp_importable": importlib.util.find_spec("mcp") is not None,
        "docker_server_ready": docker_ok,
        "candidate_runtime_image_bound": managed_image_ok,
        "managed_runtime_image_id": managed_image_id,
        "all_codex_roles_core_managed": core_route_error is None,
        "codex_execution_route": CORE_MANAGED_CODEX_ROUTE,
        "host_codex_exec_forbidden": True,
        "core_route_error": core_route_error,
        "openevo_core_health": core_health,
        "local_rxn_health": rxn_health,
        "model_authentication": codex_auth_metadata,
        "required_environment_presence": environment_presence(env_names),
    }
    ready_for_model_calls = (
        not unknown
        and checks["manifest_frozen_count_is_14"]
        and parity
        and checks["python_version_supported"]
        and checks["rdkit_importable"]
        and checks["mcp_importable"]
        and docker_ok
        and checks["candidate_runtime_image_bound"]
        and checks["all_codex_roles_core_managed"]
        and core_health["reachable"]
        and core_health["healthy_nodes"] > 0
        and codex_auth_metadata["present"]
        and all(checks["required_environment_presence"].values())
    )
    payload = {
        "schema_version": "chemcrow_preflight_v1",
        "status": "READY" if ready_for_model_calls else "BLOCKED_HUMAN_ACTION_REQUIRED",
        "model_calls": 0,
        "paid_operations": 0,
        "config_sha256": file_sha256(config_path),
        "task_manifest_sha256": file_sha256(manifest),
        "selected_task_ids": selected,
        "s0_config_sha256": s0_hash,
        "checks": checks,
    }
    output = args.output or _path(config_path, str(config.get("run_root"))) / "preflight.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "model_calls": 0, "s0_config_sha256": s0_hash}, sort_keys=True))
    return 0 if ready_for_model_calls else 2


def _authorize_paid(args: argparse.Namespace) -> None:
    if not args.allow_paid or os.environ.get("CHEMCROW_FULL_RUN_AUTHORIZATION") != _PAID_AUTHORIZATION:
        raise PermissionError(
            "model execution is gated: pass --allow-paid and set CHEMCROW_FULL_RUN_AUTHORIZATION to the documented acknowledgement"
        )


def command_run(args: argparse.Namespace, *, resume: bool) -> int:
    _authorize_paid(args)
    config_path = args.config.resolve()
    config = _resolve_env(_load_yaml(config_path))
    if "HUMAN_ACTION_REQUIRED" in json.dumps(config):
        raise ValueError("experiment config still contains HUMAN_ACTION_REQUIRED placeholders")
    manifest_path = _path(config_path, str(config["task_manifest"]))
    items = _read_tasks(manifest_path)
    requested = [item.task_id for item in items] if config.get("task_ids") == "all" else list(config["task_ids"])
    selected = [item for item in items if item.task_id in requested]
    if [item.task_id for item in selected] != requested:
        raise ValueError("requested task order differs from sanitized manifest authority")
    run_root = _path(config_path, str(config["run_root"]))
    cache_root = _path(config_path, str(config["cache_root"]))
    ledger_root = _path(config_path, str(config["ledger_root"]))
    controlled_csv = _path(config_path, str(config["controlled_chemicals_csv"]))
    candidate = OpenEvoRolloutPort(base_url=str(config["rollout_base_url"]), candidate=config["candidate"])
    evolution_eval_port = OpenEvoRolloutPort(base_url=str(config["rollout_base_url"]), candidate=config["evolution_evaluator"])
    final_eval_port = OpenEvoRolloutPort(base_url=str(config["rollout_base_url"]), candidate=config["final_evaluator"])
    reflector_port = OpenEvoRolloutPort(base_url=str(config["rollout_base_url"]), candidate=config["reflector"])
    evolution = NativeEvolutionEngine(
        run_root=run_root,
        artifact_kind=ArtifactKind(config["artifact_type"]),
        reflector_rollout=reflector_port,
    )
    runner = TaskLocalProtocolRunner(
        run_root=run_root,
        candidate=candidate,
        evolution=evolution,
        evolution_evaluator=OpenEvoEvolutionEvaluator(evolution_eval_port),
        final_evaluator=OpenEvoFinalEvaluator(final_eval_port),
        feedback_mode=FeedbackMode(config["feedback_mode"]),
        s0_hash=s0_config_hash(config["candidate"]),
        real_mode=True,
        ledger_root=ledger_root,
    )
    results: list[PairResult] = []
    for item in selected:
        pair_id = f"{config['experiment_id']}--{item.task_id}"
        result_path = run_root / pair_id / "pair.result.json"
        if result_path.is_file():
            result = PairResult.model_validate_json(result_path.read_text(encoding="utf-8"))
            if result.reset_receipt_sha256 != file_sha256(run_root / pair_id / "reset.receipt.json"):
                raise ValueError(f"sealed reset receipt drift: {pair_id}")
            results.append(result)
            continue
        claim_dir = ledger_root / pair_id
        if resume and claim_dir.is_dir() and any(claim_dir.glob("*.json")):
            PhaseLedger(ledger_root, pair_id=pair_id).audit_resume()
            raise AmbiguousPhaseClaimError(
                f"all claims for incomplete pair {pair_id} are terminal but reconstruction is not automatic; audit before replacement"
            )
        with PairToolService(
            pair_id=pair_id,
            cache_root=cache_root,
            controlled_chemicals_csv=controlled_csv,
            log_path=run_root / pair_id / "tool_service.log",
            network_enabled=bool(config.get("network_enabled", True)),
        ) as mcp_url:
            results.append(runner.run_item(item, pair_id=pair_id, mcp_url=mcp_url))
    aggregate = aggregate_results(results)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "aggregate.json").write_text(json.dumps(aggregate, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": aggregate["status"], "task_count": aggregate["task_count"]}, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openevo-chemcrow")
    commands = parser.add_subparsers(dest="command", required=True)
    extract = commands.add_parser("extract-tasks")
    extract.add_argument("--runs-root", type=Path, required=True)
    extract.add_argument("--output", type=Path, required=True)
    extract.add_argument("--safety-output", type=Path, required=True)
    extract.add_argument("--audit-output", type=Path, required=True)
    extract.set_defaults(function=command_extract)
    inventory = commands.add_parser("tool-inventory")
    inventory.add_argument("--output", type=Path, required=True)
    inventory.set_defaults(function=command_inventory)
    smoke = commands.add_parser("tool-smoke")
    smoke.add_argument("--controlled-chemicals-csv", type=Path, required=True)
    smoke.add_argument("--output", type=Path, required=True)
    smoke.set_defaults(function=command_tool_smoke)
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--config", type=Path, required=True)
    preflight.add_argument("--output", type=Path)
    preflight.add_argument("--no-model-calls", action="store_true", required=True)
    preflight.set_defaults(function=command_preflight)
    for name, resume in (("run", False), ("resume", True)):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--allow-paid", action="store_true")
        command.set_defaults(function=lambda args, resume=resume: command_run(args, resume=resume))
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    raise SystemExit(args.function(args))


if __name__ == "__main__":
    main()
