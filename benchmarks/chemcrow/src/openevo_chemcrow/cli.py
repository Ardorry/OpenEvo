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
from .audit import audit_completed_run
from .composite import (
    audit_paper_composite_repair,
    load_composite_pairs,
    validate_duplicate_authorization_receipt,
    validate_repair_execution_parity,
)
from .credentials import credential_report, probe_openrouter_key
from .evaluation import OpenEvoEvolutionEvaluator, OpenEvoFinalEvaluator
from .hashing import canonical_sha256, file_sha256
from .ledger import AmbiguousPhaseClaimError, PhaseLedger
from .models import ArtifactKind, FeedbackMode, PairResult, TaskItem
from .native_evolution import NativeEvolutionEngine
from .paper_core import run_paper_evaluation_plan
from .paper_evaluator import (
    FROZEN_PAPER_TASK_IDS,
    assert_sealed_run_ready,
    build_paper_evaluation_plan,
    extract_historical_answers,
    paper_cost_ceiling,
)
from .paper_human_review import (
    build_paper_human_review_bundle,
    load_or_create_human_review_secret,
    write_paper_human_review_bundle,
)
from .protocol import TaskLocalProtocolRunner
from .runtime import (
    CORE_MANAGED_CODEX_ROUTE,
    OpenEvoRolloutPort,
    assert_core_managed_codex_config,
    build_task_request,
    s0_config_hash,
)
from .tasks import extract_safety_demonstrations, extract_scored_tasks, write_jsonl
from .three_artifact_aggregate import aggregate_three_artifact_results
from .three_artifact_audit import audit_three_artifact_run
from .three_artifact_evolution import ThreeIsolatedEvolutionEngine
from .three_artifact_models import (
    ArtifactSeparationPolicy,
    ThreeArtifactPairResult,
)
from .three_artifact_protocol import ThreeArtifactTaskLocalProtocolRunner
from .three_artifact_runtime import (
    ThreeArtifactRolloutPort,
    candidate_pair_request_parity,
)
from .tool_service import PairToolService
from .tools import TOOL_INVENTORY, ChemCrowToolRegistry, environment_presence

_ENV_PATTERN = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")
_PAID_AUTHORIZATION = "I_UNDERSTAND_THIS_MAY_INCUR_COST"
_AUTHORITATIVE_MODEL = "gpt-5.5"


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


def _bounded_task_prefix(
    selected: list[TaskItem], *, stop_after_task_id: str | None
) -> list[TaskItem]:
    if stop_after_task_id is None:
        return selected
    matches = [index for index, item in enumerate(selected) if item.task_id == stop_after_task_id]
    if len(matches) != 1:
        raise ValueError("--stop-after-task-id must name exactly one configured task")
    return selected[: matches[0] + 1]


def _assert_authoritative_role_models(role_configs: dict[str, dict[str, Any]]) -> None:
    observed: dict[str, str] = {}
    for role, role_config in role_configs.items():
        agent = role_config.get("agent")
        if not isinstance(agent, dict):
            raise TypeError(f"{role} has no closed agent configuration")
        observed[role] = str(agent.get("model_name") or "")
    wrong = sorted(role for role, model in observed.items() if model != _AUTHORITATIVE_MODEL)
    if wrong:
        raise ValueError(
            "authoritative ChemCrow roles must all use frozen gpt-5.5: " + ", ".join(wrong)
        )


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
    print(
        json.dumps(
            {"tool_entries": len(TOOL_INVENTORY), "inventory_sha256": file_sha256(args.output)},
            sort_keys=True,
        )
    )
    return 0


def command_tool_smoke(args: argparse.Namespace) -> int:
    registry = ChemCrowToolRegistry(
        controlled_chemicals_csv=args.controlled_chemicals_csv,
        network_enabled=False,
    )
    checks = {
        "SMILES2Weight": registry.execute(
            "SMILES2Weight", {"query": "CCO"}, call_id="smoke-weight"
        ),
        "MolSimilarity": registry.execute(
            "MolSimilarity", {"query": "CCO.CCOC"}, call_id="smoke-similarity"
        ),
        "FunctionalGroups": registry.execute(
            "FunctionalGroups", {"query": "CCO"}, call_id="smoke-groups"
        ),
        "ControlChemCheck": registry.execute(
            "ControlChemCheck", {"query": "CCO"}, call_id="smoke-control"
        ),
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


def command_tool_live_smoke(args: argparse.Namespace) -> int:
    """Exercise every enabled external reduced-profile capability without model calls."""

    registry = ChemCrowToolRegistry(
        controlled_chemicals_csv=args.controlled_chemicals_csv,
        network_enabled=True,
        timeout_seconds=args.timeout_seconds,
    )
    requests = (
        ("wikipedia", "Aspirin"),
        ("Name2SMILES", "aspirin"),
        ("Mol2CAS", "aspirin"),
        ("SMILES2Name", "CC(=O)OC1=CC=CC=C1C(=O)O"),
        ("ExplosiveCheck", "aspirin"),
        ("SafetySummary", "aspirin"),
        ("ReactionPredict", "CCO.CC(=O)O"),
        ("ReactionRetrosynthesis", "CC(=O)OC1=CC=CC=C1C(=O)O"),
    )
    checks: dict[str, dict[str, Any]] = {}
    for index, (tool_name, query) in enumerate(requests, start=1):
        observation = registry.execute(
            tool_name,
            {"query": query},
            call_id=f"live-readiness-{index:02d}-{tool_name.lower()}",
        )
        checks[tool_name] = {
            "call_id": observation.call_id,
            "canonical_arguments_sha256": observation.canonical_arguments_sha256,
            "source": observation.source,
            "error": observation.error,
            "result_present": observation.result is not None,
            "result_sha256": (
                canonical_sha256(observation.result)
                if observation.result is not None
                else None
            ),
            "elapsed_seconds": observation.elapsed_seconds,
            "result_body_included": False,
        }
    errors = sorted(name for name, value in checks.items() if value["error"] is not None)
    sources = sorted({str(value["source"]) for value in checks.values()})
    payload = {
        "schema_version": "chemcrow_external_tool_live_smoke_v1",
        "status": "PASS" if not errors else "FAIL_CLOSED",
        "logical_tool_calls": len(checks),
        "model_calls": 0,
        "mock_observations": 0,
        "fixture_observations": 0,
        "sources": sources,
        "failed_tools": errors,
        "web_search": {
            "configured": bool(os.environ.get("SERP_API_KEY")),
            "called": False,
            "reason": "not configured" if not os.environ.get("SERP_API_KEY") else "separate credentialed capability",
        },
        "excluded_capabilities": {
            "LiteratureSearch": "excluded because the legacy implementation adds hidden model calls",
            "GetMoleculePrice": "excluded commercial procurement capability",
            "python_repl": "excluded arbitrary-code capability",
        },
        "checks": checks,
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": payload["status"],
                "logical_tool_calls": len(checks),
                "failed_tools": errors,
                "model_calls": 0,
            },
            sort_keys=True,
        )
    )
    return 0 if not errors else 2


def command_preflight(args: argparse.Namespace) -> int:
    config_path = args.config.resolve()
    config = _load_yaml(config_path)
    env_names = sorted(_env_names(config))
    required_environment_presence = environment_presence(env_names)
    runtime_config_resolved = all(required_environment_presence.values())
    if runtime_config_resolved:
        config = _resolve_env(config)
    duplicate_authorization_error: str | None = None
    if config.get("duplicate_authorization_receipt"):
        try:
            validate_duplicate_authorization_receipt(
                path=_path(config_path, str(config["duplicate_authorization_receipt"])),
                primary_run_root=_path(config_path, str(config["prior_run_root"])),
                primary_experiment_id=str(config["prior_experiment_id"]),
            )
        except (FileNotFoundError, TypeError, ValueError) as exc:
            duplicate_authorization_error = str(exc)
    execution_parity_error: str | None = None
    if config.get("execution_parity_report"):
        try:
            validate_repair_execution_parity(
                _path(config_path, str(config["execution_parity_report"]))
            )
        except (FileNotFoundError, TypeError, ValueError) as exc:
            execution_parity_error = str(exc)
    manifest = _path(config_path, str(config["task_manifest"]))
    items = _read_tasks(manifest)
    ids = [item.task_id for item in items]
    selected = ids if config.get("task_ids") == "all" else list(config.get("task_ids", []))
    unknown = sorted(set(selected) - set(ids))
    candidate = config["candidate"]
    three_artifact_protocol = config.get("artifact_protocol") == "three_isolated_v1"
    if three_artifact_protocol:
        reflectors = config.get("reflectors")
        if not isinstance(reflectors, dict) or set(reflectors) != {
            "memory",
            "skill_bundle",
            "agent_system",
        }:
            raise ValueError("three_isolated_v1 requires three explicit Reflector configs")
        role_configs = {
            "candidate": config["candidate"],
            "reflector_memory": reflectors["memory"],
            "reflector_skill_bundle": reflectors["skill_bundle"],
            "reflector_agent_system": reflectors["agent_system"],
            "evolution_evaluator": config["evolution_evaluator"],
            "final_evaluator": config["final_evaluator"],
        }
    else:
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
    model_identity_error = None
    try:
        _assert_authoritative_role_models(role_configs)
    except (TypeError, ValueError) as exc:
        model_identity_error = str(exc)
    s0_hash = s0_config_hash(candidate)
    baseline_request = build_task_request(
        task=items[0],
        run_id="preflight-baseline",
        role="baseline",
        candidate=candidate,
        artifact_ids=[],
        mcp_url="http://127.0.0.1:9/mcp",
    )
    if three_artifact_protocol:
        parity_receipt = candidate_pair_request_parity(
            task=items[0],
            candidate=candidate,
            mcp_url="http://127.0.0.1:9/mcp",
        )
        parity = bool(
            parity_receipt["all_invariant_fields_equal"]
            and parity_receipt["only_allowed_metadata_drift"]
        )
    else:
        evolved_request = build_task_request(
            task=items[0],
            run_id="preflight-evolved",
            role="evolved",
            candidate=candidate,
            artifact_ids=["art-preflight"],
            mcp_url="http://127.0.0.1:9/mcp",
        )
        parity = (
            baseline_request["agent"] == evolved_request["agent"]
            and baseline_request["runtime"] == evolved_request["runtime"]
            and baseline_request["builder"] == evolved_request["builder"]
            and baseline_request["instruction"] == evolved_request["instruction"]
        )
        parity_receipt = {"legacy_single_artifact_protocol": True}
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
            health = httpx.get(rollout_url.rstrip("/") + "/health", timeout=5.0, trust_env=False)
            nodes = httpx.get(rollout_url.rstrip("/") + "/nodes", timeout=5.0, trust_env=False)
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
    evolution_health: dict[str, Any] = {"configured": False, "reachable": False}
    evolution_store = config.get("evolution_store")
    if isinstance(evolution_store, dict) and evolution_store.get("backend_url"):
        evolution_health["configured"] = True
        try:
            response = httpx.get(
                str(evolution_store["backend_url"]).rstrip("/") + "/v1/health",
                timeout=5.0,
                trust_env=False,
            )
            response.raise_for_status()
            health_payload = response.json()
            evolution_health = {
                "configured": True,
                "reachable": health_payload.get("status") == "ok",
                "db": health_payload.get("db"),
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
            response = httpx.get(openapi_url, timeout=5.0, trust_env=False)
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
        "manifest_task_ids_match_authority": tuple(ids) == FROZEN_PAPER_TASK_IDS,
        "candidate_pair_parity": parity,
        "candidate_pair_parity_receipt": parity_receipt,
        "artifact_protocol": config.get("artifact_protocol", "legacy_single_artifact_v1"),
        "three_isolated_reflector_configs": three_artifact_protocol,
        "historical_answer_fields_absent": all(
            not {"answer", "trajectory", "evaluator_feedback", "reference_answer"}.intersection(
                item.model_fields_set
            )
            for item in items
        ),
        "python_version_supported": tuple(__import__("sys").version_info[:2]) == (3, 11),
        "rdkit_importable": importlib.util.find_spec("rdkit") is not None,
        "mcp_importable": importlib.util.find_spec("mcp") is not None,
        "docker_server_ready": docker_ok,
        "candidate_runtime_image_bound": managed_image_ok,
        "managed_runtime_image_id": managed_image_id,
        "all_codex_roles_core_managed": core_route_error is None,
        "all_model_roles_frozen_gpt_5_5": model_identity_error is None,
        "codex_execution_route": CORE_MANAGED_CODEX_ROUTE,
        "host_codex_exec_forbidden": True,
        "core_route_error": core_route_error,
        "model_identity_error": model_identity_error,
        "openevo_core_health": core_health,
        "openevo_evolution_health": evolution_health,
        "local_rxn_health": rxn_health,
        "model_authentication": codex_auth_metadata,
        "runtime_config_resolved": runtime_config_resolved,
        "duplicate_authorization_valid": duplicate_authorization_error is None,
        "duplicate_authorization_error": duplicate_authorization_error,
        "execution_parity_valid": execution_parity_error is None,
        "execution_parity_error": execution_parity_error,
        "required_environment_presence": required_environment_presence,
    }
    ready_for_model_calls = (
        not unknown
        and checks["manifest_frozen_count_is_14"]
        and checks["manifest_task_ids_match_authority"]
        and parity
        and checks["python_version_supported"]
        and checks["rdkit_importable"]
        and checks["mcp_importable"]
        and docker_ok
        and checks["candidate_runtime_image_bound"]
        and checks["all_codex_roles_core_managed"]
        and checks["all_model_roles_frozen_gpt_5_5"]
        and checks["runtime_config_resolved"]
        and checks["duplicate_authorization_valid"]
        and checks["execution_parity_valid"]
        and core_health["reachable"]
        and core_health["healthy_nodes"] > 0
        and evolution_health["reachable"]
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
    print(
        json.dumps(
            {"status": payload["status"], "model_calls": 0, "s0_config_sha256": s0_hash},
            sort_keys=True,
        )
    )
    return 0 if ready_for_model_calls else 2


def _authorize_paid(args: argparse.Namespace) -> None:
    if (
        not args.allow_paid
        or os.environ.get("CHEMCROW_FULL_RUN_AUTHORIZATION") != _PAID_AUTHORIZATION
    ):
        raise PermissionError(
            "model execution is gated: pass --allow-paid and set CHEMCROW_FULL_RUN_AUTHORIZATION to the documented acknowledgement"
        )


def command_run(args: argparse.Namespace, *, resume: bool) -> int:
    _authorize_paid(args)
    config_path = args.config.resolve()
    config = _resolve_env(_load_yaml(config_path))
    if "HUMAN_ACTION_REQUIRED" in json.dumps(config):
        raise ValueError("experiment config still contains HUMAN_ACTION_REQUIRED placeholders")
    if config.get("duplicate_authorization_receipt"):
        validate_duplicate_authorization_receipt(
            path=_path(config_path, str(config["duplicate_authorization_receipt"])),
            primary_run_root=_path(config_path, str(config["prior_run_root"])),
            primary_experiment_id=str(config["prior_experiment_id"]),
        )
    if config.get("execution_parity_report"):
        validate_repair_execution_parity(
            _path(config_path, str(config["execution_parity_report"]))
        )
    manifest_path = _path(config_path, str(config["task_manifest"]))
    items = _read_tasks(manifest_path)
    requested = (
        [item.task_id for item in items]
        if config.get("task_ids") == "all"
        else list(config["task_ids"])
    )
    selected = [item for item in items if item.task_id in requested]
    if [item.task_id for item in selected] != requested:
        raise ValueError("requested task order differs from sanitized manifest authority")
    full_selected_task_ids = [item.task_id for item in selected]
    stop_after_task_id = getattr(args, "stop_after_task_id", None)
    selected = _bounded_task_prefix(
        selected,
        stop_after_task_id=stop_after_task_id,
    )
    run_root = _path(config_path, str(config["run_root"]))
    cache_root = _path(config_path, str(config["cache_root"]))
    ledger_root = _path(config_path, str(config["ledger_root"]))
    controlled_csv = _path(config_path, str(config["controlled_chemicals_csv"]))
    three_artifact_protocol = config.get("artifact_protocol") == "three_isolated_v1"
    candidate = (
        ThreeArtifactRolloutPort(
            base_url=str(config["rollout_base_url"]), candidate=config["candidate"]
        )
        if three_artifact_protocol
        else OpenEvoRolloutPort(
            base_url=str(config["rollout_base_url"]), candidate=config["candidate"]
        )
    )
    evolution_eval_port = OpenEvoRolloutPort(
        base_url=str(config["rollout_base_url"]), candidate=config["evolution_evaluator"]
    )
    final_eval_port = OpenEvoRolloutPort(
        base_url=str(config["rollout_base_url"]), candidate=config["final_evaluator"]
    )
    if three_artifact_protocol:
        reflectors = config.get("reflectors")
        if not isinstance(reflectors, dict):
            raise ValueError("three_isolated_v1 Reflector configs are absent")
        _assert_authoritative_role_models(
            {
                "candidate": config["candidate"],
                "reflector_memory": reflectors["memory"],
                "reflector_skill_bundle": reflectors["skill_bundle"],
                "reflector_agent_system": reflectors["agent_system"],
                "evolution_evaluator": config["evolution_evaluator"],
                "final_evaluator": config["final_evaluator"],
            }
        )
        reflector_ports = {
            ArtifactKind.TEXT_MEMORY: OpenEvoRolloutPort(
                base_url=str(config["rollout_base_url"]), candidate=reflectors["memory"]
            ),
            ArtifactKind.SKILL_BUNDLE: OpenEvoRolloutPort(
                base_url=str(config["rollout_base_url"]), candidate=reflectors["skill_bundle"]
            ),
            ArtifactKind.AGENT_SYSTEM: OpenEvoRolloutPort(
                base_url=str(config["rollout_base_url"]), candidate=reflectors["agent_system"]
            ),
        }
        evolution = ThreeIsolatedEvolutionEngine(
            run_root=run_root,
            reflector_rollouts=reflector_ports,
            evolution_db_path=_path(config_path, str(config["evolution_store"]["db_path"])),
            evolution_artifact_root=_path(
                config_path, str(config["evolution_store"]["artifact_root"])
            ),
            separation_policy=ArtifactSeparationPolicy.model_validate(
                config.get("artifact_separation", {})
            ),
        )
        runner = ThreeArtifactTaskLocalProtocolRunner(
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
    else:
        reflector_port = OpenEvoRolloutPort(
            base_url=str(config["rollout_base_url"]), candidate=config["reflector"]
        )
        evolution = NativeEvolutionEngine(
            run_root=run_root,
            artifact_kind=ArtifactKind(config["artifact_type"]),
            reflector_rollout=reflector_port,
            evolution_db_path=_path(config_path, str(config["evolution_store"]["db_path"])),
            evolution_artifact_root=_path(
                config_path, str(config["evolution_store"]["artifact_root"])
            ),
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
    results: list[PairResult | ThreeArtifactPairResult] = []
    for item in selected:
        pair_id = f"{config['experiment_id']}--{item.task_id}"
        result_path = run_root / pair_id / "pair.result.json"
        if result_path.is_file():
            result = (
                ThreeArtifactPairResult.model_validate_json(
                    result_path.read_text(encoding="utf-8")
                )
                if three_artifact_protocol
                else PairResult.model_validate_json(result_path.read_text(encoding="utf-8"))
            )
            if result.reset_receipt_sha256 != file_sha256(
                run_root / pair_id / "reset.receipt.json"
            ):
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
    aggregate = (
        aggregate_three_artifact_results(results)
        if three_artifact_protocol
        else aggregate_results(results)
    )
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "aggregate.json").write_text(
        json.dumps(aggregate, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if stop_after_task_id is not None:
        prefix_receipt = {
            "schema_version": "chemcrow_planned_prefix_stop_v1",
            "status": "PLANNED_PREFIX_COMPLETE",
            "experiment_id": str(config["experiment_id"]),
            "configured_task_ids": full_selected_task_ids,
            "completed_prefix_task_ids": [item.task_id for item in selected],
            "stop_after_task_id": stop_after_task_id,
            "config_sha256": file_sha256(config_path),
            "resume_command_must_omit_stop_after_task_id": True,
        }
        prefix_path = run_root / f"PLANNED_PREFIX_STOP_AFTER_{stop_after_task_id}.json"
        serialized_prefix = json.dumps(prefix_receipt, indent=2, sort_keys=True) + "\n"
        if prefix_path.exists() and prefix_path.read_text(encoding="utf-8") != serialized_prefix:
            raise ValueError("planned prefix receipt differs from current authority")
        prefix_path.write_text(serialized_prefix, encoding="utf-8")
    print(
        json.dumps(
            {"status": aggregate["status"], "task_count": aggregate["task_count"]}, sort_keys=True
        )
    )
    return 0


def command_audit_run(args: argparse.Namespace) -> int:
    config_path = args.config.resolve()
    config = _resolve_env(_load_yaml(config_path))
    manifest_path = _path(config_path, str(config["task_manifest"]))
    items = _read_tasks(manifest_path)
    task_ids = (
        [item.task_id for item in items]
        if config.get("task_ids") == "all"
        else list(config["task_ids"])
    )
    task_ids = [
        item.task_id
        for item in _bounded_task_prefix(
            [item for item in items if item.task_id in task_ids],
            stop_after_task_id=args.stop_after_task_id,
        )
    ]
    if config.get("artifact_protocol") == "three_isolated_v1":
        reflectors = config.get("reflectors")
        if not isinstance(reflectors, dict):
            raise ValueError("three_isolated_v1 Reflector configs are absent")
        models = {str(value["agent"]["model_name"]) for value in reflectors.values()}
        if len(models) != 1:
            raise ValueError("three Reflector models differ")
        payload = audit_three_artifact_run(
            run_root=_path(config_path, str(config["run_root"])),
            core_completion_root=args.core_completions.resolve(),
            experiment_id=str(config["experiment_id"]),
            task_ids=task_ids,
            expected_s0_hash=s0_config_hash(config["candidate"]),
            expected_reflector_model=models.pop(),
            require_aggregate=True,
        )
    else:
        payload = audit_completed_run(
            run_root=_path(config_path, str(config["run_root"])),
            core_completion_root=args.core_completions.resolve(),
            experiment_id=str(config["experiment_id"]),
            task_ids=task_ids,
            expected_s0_hash=s0_config_hash(config["candidate"]),
        )
    args.output.resolve().parent.mkdir(parents=True, exist_ok=True)
    args.output.resolve().write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({"status": payload["status"], "task_count": payload["task_count"]}))
    return 0


def command_aggregate_run(args: argparse.Namespace) -> int:
    config_path = args.config.resolve()
    config = _resolve_env(_load_yaml(config_path))
    if config.get("artifact_protocol") != "three_isolated_v1":
        raise ValueError("authoritative aggregate requires three_isolated_v1")
    manifest_path = _path(config_path, str(config["task_manifest"]))
    items = _read_tasks(manifest_path)
    task_ids = (
        [item.task_id for item in items]
        if config.get("task_ids") == "all"
        else list(config["task_ids"])
    )
    task_ids = [
        item.task_id
        for item in _bounded_task_prefix(
            [item for item in items if item.task_id in task_ids],
            stop_after_task_id=args.stop_after_task_id,
        )
    ]
    run_root = _path(config_path, str(config["run_root"]))
    results: list[ThreeArtifactPairResult] = []
    for task_id in task_ids:
        pair_id = f"{config['experiment_id']}--{task_id}"
        path = run_root / pair_id / "pair.result.json"
        if not path.is_file():
            raise ValueError(f"sealed three-artifact pair is missing: {pair_id}")
        result = ThreeArtifactPairResult.model_validate_json(path.read_text(encoding="utf-8"))
        if result.task_id != task_id or result.pair_id != pair_id:
            raise ValueError(f"sealed pair authority differs: {pair_id}")
        if result.reset_receipt_sha256 != file_sha256(
            run_root / pair_id / "reset.receipt.json"
        ):
            raise ValueError(f"sealed reset receipt differs: {pair_id}")
        results.append(result)
    aggregate = aggregate_three_artifact_results(results)
    output = args.output.resolve() if args.output else run_root / "aggregate.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(aggregate, indent=2, sort_keys=True) + "\n"
    if output.exists() and output.read_text(encoding="utf-8") != serialized:
        raise ValueError("existing aggregate differs from sealed pair authority")
    if not output.exists():
        output.write_text(serialized, encoding="utf-8")
    print(
        json.dumps(
            {
                "status": aggregate["status"],
                "task_count": aggregate["task_count"],
                "model_calls": 0,
                "output_sha256": file_sha256(output),
            },
            sort_keys=True,
        )
    )
    return 0


def command_credential_check(args: argparse.Namespace) -> int:
    payload = credential_report()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": payload["status"],
                "model_calls": 0,
                "paid_operations": 0,
                "secret_values_included": False,
            },
            sort_keys=True,
        )
    )
    return 0 if payload["status"] == "READY_REDUCED_PROFILE" else 2


def command_paper_evaluator_preflight(args: argparse.Namespace) -> int:
    config_path = args.config.resolve()
    config = _load_yaml(config_path)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    task_manifest = _path(config_path, str(config["task_manifest"]))
    tasks = _read_tasks(task_manifest)
    task_ids = [task.task_id for task in tasks]
    report: dict[str, Any] = {
        "schema_version": "chemcrow_paper_evaluator_preflight_v1",
        "status": "BLOCKED",
        "model_calls": 0,
        "paid_operations": 0,
        "protocol_task_ids": list(FROZEN_PAPER_TASK_IDS),
        "observed_task_ids": task_ids,
        "cost_ceiling": paper_cost_ceiling(),
        "openrouter_environment_presence": {
            name: bool(os.environ.get(name))
            for name in (
                "OPENROUTER_API_KEY",
                "OPENROUTER_BASE_URL",
                "CHEMCROW_PAPER_EVALUATOR_MODEL",
                "CHEMCROW_PAPER_EVALUATOR_MAX_USD",
                "CHEMCROW_PAPER_EVALUATOR_AUTHORIZATION",
            )
        },
        "secret_values_included": False,
        "historical_answers_extracted": False,
    }
    try:
        if config.get("composite_manifest"):
            pairs = load_composite_pairs(
                manifest_path=_path(config_path, str(config["composite_manifest"])),
                task_ids=task_ids,
            )
        else:
            pairs = assert_sealed_run_ready(
                run_root=_path(config_path, str(config["run_root"])),
                experiment_id=str(config["experiment_id"]),
                task_ids=task_ids,
                completed_run_audit=_path(config_path, str(config["completed_run_audit"])),
            )
        historical = extract_historical_answers(
            runs_root=_path(config_path, str(config["historical_runs_root"]))
        )
        plan = build_paper_evaluation_plan(
            tasks=tasks,
            pairs=pairs,
            historical=historical,
        )
        plan_path = _path(config_path, str(config["plan_path"]))
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        serialized_plan = json.dumps(plan, indent=2, sort_keys=True) + "\n"
        if plan_path.exists():
            if plan_path.read_text(encoding="utf-8") != serialized_plan:
                raise ValueError("existing paper evaluator plan differs from frozen inputs")
        else:
            descriptor = os.open(
                plan_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(serialized_plan)
        report.update(
            {
                "status": "READY_FOR_EXPLICIT_PAID_AUTHORIZATION",
                "historical_answers_extracted": True,
                "plan_path": str(plan_path),
                "plan_sha256": file_sha256(plan_path),
                "call_count": plan["call_count"],
            }
        )
    except (FileNotFoundError, TypeError, ValueError) as exc:
        report["blocking_reason"] = str(exc)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": report["status"],
                "model_calls": 0,
                "paid_operations": 0,
                "secret_values_included": False,
            },
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "READY_FOR_EXPLICIT_PAID_AUTHORIZATION" else 2


def command_paper_credential_probe(args: argparse.Namespace) -> int:
    payload = probe_openrouter_key(
        api_key=os.environ.get("OPENROUTER_API_KEY", ""),
        base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
    )
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": payload["status"],
                "model_calls": 0,
                "paid_operations": 0,
                "secret_values_included": False,
            },
            sort_keys=True,
        )
    )
    return 0 if payload["status"] == "VALID" else 2


def command_paper_composite_audit(args: argparse.Namespace) -> int:
    config_path = args.config.resolve()
    config = _load_yaml(config_path)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = audit_paper_composite_repair(
            primary_run_root=_path(config_path, str(config["primary_run_root"])),
            primary_experiment_id=str(config["primary_experiment_id"]),
            repair_run_root=_path(config_path, str(config["repair_run_root"])),
            repair_experiment_id=str(config["repair_experiment_id"]),
            core_completion_root=_path(config_path, str(config["core_completion_root"])),
            expected_s0_hash=str(config["expected_s0_hash"]),
            duplicate_authorization_receipt=_path(
                config_path, str(config["duplicate_authorization_receipt"])
            ),
            composite_manifest_path=_path(config_path, str(config["composite_manifest_path"])),
        )
    except (FileNotFoundError, TypeError, ValueError) as exc:
        payload = {
            "schema_version": "chemcrow_paper_composite_audit_v1",
            "status": "BLOCKED",
            "blocking_reason": str(exc),
            "model_calls": 0,
            "paid_operations": 0,
            "answers_included": False,
        }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": payload["status"],
                "model_calls": 0,
                "paid_operations": 0,
            },
            sort_keys=True,
        )
    )
    return 0 if payload["status"] == "PASS" else 2


def command_paper_human_review_prepare(args: argparse.Namespace) -> int:
    config_path = args.config.resolve()
    config = _load_yaml(config_path)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        tasks = _read_tasks(_path(config_path, str(config["task_manifest"])))
        task_ids = [task.task_id for task in tasks]
        if config.get("composite_manifest"):
            pairs = load_composite_pairs(
                manifest_path=_path(config_path, str(config["composite_manifest"])),
                task_ids=task_ids,
            )
        else:
            pairs = assert_sealed_run_ready(
                run_root=_path(config_path, str(config["run_root"])),
                experiment_id=str(config["experiment_id"]),
                task_ids=task_ids,
                completed_run_audit=_path(
                    config_path, str(config["completed_run_audit"])
                ),
            )
        historical = extract_historical_answers(
            runs_root=_path(config_path, str(config["historical_runs_root"]))
        )
        output_root = _path(config_path, str(config["output_root"]))
        randomization_secret = load_or_create_human_review_secret(output_root)
        bundle = build_paper_human_review_bundle(
            tasks=tasks,
            pairs=pairs,
            historical=historical,
            randomization_secret=randomization_secret,
        )
        manifest = write_paper_human_review_bundle(
            bundle=bundle,
            output_root=output_root,
        )
        payload = {
            "schema_version": "chemcrow_paper_human_review_prepare_v1",
            "status": manifest["status"],
            "task_count": manifest["task_count"],
            "comparison_count": manifest["comparison_count"],
            "required_independent_reviewer_count": manifest["required_independent_reviewer_count"],
            "required_completed_review_count": manifest["required_completed_review_count"],
            "score_minimum": manifest["score_minimum"],
            "score_maximum": manifest["score_maximum"],
            "dimensions": manifest["dimensions"],
            "model_calls": 0,
            "paid_operations": 0,
            "answers_in_report": False,
        }
    except (FileNotFoundError, TypeError, ValueError) as exc:
        payload = {
            "schema_version": "chemcrow_paper_human_review_prepare_v1",
            "status": "BLOCKED",
            "blocking_reason": str(exc),
            "model_calls": 0,
            "paid_operations": 0,
            "answers_in_report": False,
        }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": payload["status"],
                "model_calls": 0,
                "paid_operations": 0,
            },
            sort_keys=True,
        )
    )
    return 0 if payload["status"] == "READY_FOR_FOUR_EXPERT_REVIEWERS" else 2


def command_paper_evaluator_run(args: argparse.Namespace) -> int:
    config_path = args.config.resolve()
    config = _resolve_env(_load_yaml(config_path))
    aggregate = run_paper_evaluation_plan(
        plan_path=_path(config_path, str(config["plan_path"])),
        rollout_base_url=str(config["rollout_base_url"]),
        runtime=dict(config["runtime"]),
        result_root=_path(config_path, str(config["result_root"])),
        shim_receipt_root=_path(config_path, str(config["shim_receipt_root"])),
        historical_runs_root=_path(config_path, str(config["historical_runs_root"])),
        allow_paid=args.allow_paid,
    )
    print(json.dumps({"status": aggregate["status"], "result_count": aggregate["result_count"]}))
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
    live_smoke = commands.add_parser("tool-live-smoke")
    live_smoke.add_argument("--controlled-chemicals-csv", type=Path, required=True)
    live_smoke.add_argument("--output", type=Path, required=True)
    live_smoke.add_argument("--timeout-seconds", type=float, default=30.0)
    live_smoke.set_defaults(function=command_tool_live_smoke)
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--config", type=Path, required=True)
    preflight.add_argument("--output", type=Path)
    preflight.add_argument("--no-model-calls", action="store_true", required=True)
    preflight.set_defaults(function=command_preflight)
    audit = commands.add_parser("audit-run")
    audit.add_argument("--config", type=Path, required=True)
    audit.add_argument("--core-completions", type=Path, required=True)
    audit.add_argument("--output", type=Path, required=True)
    audit.add_argument("--stop-after-task-id")
    audit.set_defaults(function=command_audit_run)
    aggregate = commands.add_parser("aggregate-run")
    aggregate.add_argument("--config", type=Path, required=True)
    aggregate.add_argument("--output", type=Path)
    aggregate.add_argument("--no-model-calls", action="store_true", required=True)
    aggregate.add_argument("--stop-after-task-id")
    aggregate.set_defaults(function=command_aggregate_run)
    credentials = commands.add_parser("credential-check")
    credentials.add_argument("--output", type=Path, required=True)
    credentials.set_defaults(function=command_credential_check)
    paper_preflight = commands.add_parser("paper-evaluator-preflight")
    paper_preflight.add_argument("--config", type=Path, required=True)
    paper_preflight.add_argument("--output", type=Path, required=True)
    paper_preflight.add_argument("--no-model-calls", action="store_true", required=True)
    paper_preflight.set_defaults(function=command_paper_evaluator_preflight)
    paper_probe = commands.add_parser("paper-credential-probe")
    paper_probe.add_argument("--output", type=Path, required=True)
    paper_probe.add_argument("--no-model-calls", action="store_true", required=True)
    paper_probe.set_defaults(function=command_paper_credential_probe)
    paper_composite = commands.add_parser("paper-composite-audit")
    paper_composite.add_argument("--config", type=Path, required=True)
    paper_composite.add_argument("--output", type=Path, required=True)
    paper_composite.add_argument("--no-model-calls", action="store_true", required=True)
    paper_composite.set_defaults(function=command_paper_composite_audit)
    paper_human = commands.add_parser("paper-human-review-prepare")
    paper_human.add_argument("--config", type=Path, required=True)
    paper_human.add_argument("--output", type=Path, required=True)
    paper_human.add_argument("--no-model-calls", action="store_true", required=True)
    paper_human.set_defaults(function=command_paper_human_review_prepare)
    paper_run = commands.add_parser("paper-evaluator-run")
    paper_run.add_argument("--config", type=Path, required=True)
    paper_run.add_argument("--allow-paid", action="store_true")
    paper_run.set_defaults(function=command_paper_evaluator_run)
    for name, resume in (("run", False), ("resume", True)):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--allow-paid", action="store_true")
        command.add_argument("--stop-after-task-id")
        command.set_defaults(function=lambda args, resume=resume: command_run(args, resume=resume))
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    raise SystemExit(args.function(args))


if __name__ == "__main__":
    main()
