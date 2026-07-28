"""No-surprise CLI for native-path audits and gated canary submission."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from .candidate_runner import candidate_source_audit
from .config import CANARY_TASK, ExperimentConfig, ProtocolError
from .contamination_audit import run_offline_contamination_audit
from .evolution_loop import FrozenTrainingSchedule
from .hard_gt_teacher import native_teacher_attachment_capability
from .reflector_runner import reflector_runtime_audit
from .run_manifest import atomic_write_json


def _write(path: Path, payload: dict) -> None:
    atomic_write_json(path, payload)


def _package_root() -> Path:
    return Path(__file__).resolve().parent


def command_contamination_audit(config: ExperimentConfig) -> int:
    receipt = run_offline_contamination_audit(config.researchclawbench_root / "tasks")
    destination = config.experiment_root / "manifests/community_official_contamination_audit.json"
    _write(destination, receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "PASS" else 6


def command_static_audit(config: ExperimentConfig) -> int:
    schedule = FrozenTrainingSchedule.build()
    candidate = candidate_source_audit(_package_root())
    reflector = reflector_runtime_audit(config.project_root / "OpenEvo")
    teacher = native_teacher_attachment_capability()
    receipt = {
        "schema_version": "1.0.0",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "candidate": candidate,
        "reflector": reflector,
        "teacher": teacher,
        "formal_training_orchestrator": {
            "available": False,
            "reason": (
                "the adapter has no production community-training command or "
                "durable 17x3 supervisor"
            ),
        },
        "protocol_counts": {
            "community_tasks": len(config.require("tasks")),
            "candidate_runs": len(schedule.attempts),
            "reflector_cycles": len(
                {(item.task_id, item.reflector_round) for item in schedule.evolution_requests}
            ),
            "artifact_evolution_requests": len(schedule.evolution_requests),
            "official_frozen_test_runs": config.require("official_frozen_test_runs"),
        },
        "model_calls": 0,
        "scorer_calls": 0,
    }
    receipt["passed"] = bool(
        candidate["passed"]
        and reflector["model_execution_allowed"]
        and teacher["post_run_feedback_attachment_to_native_dataset"]
        and teacher["production_evolution_http_transport"]
        and teacher["production_science_successor_hook"]
        and receipt["formal_training_orchestrator"]["available"]
    )
    receipt["status"] = (
        "STATIC_AUDIT_PASS_RUNTIME_PROBES_REQUIRED"
        if receipt["passed"]
        else "BLOCKED_BY_OPENEVO_NATIVE_CAPABILITY_GAP"
    )
    _write(config.experiment_root / "manifests/native_path_static_audit.json", receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["passed"] else 5


def command_readiness(config: ExperimentConfig) -> int:
    receipt = config.environment_readiness()
    receipt.update(
        {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "secret_values_recorded": False,
            "host_codex_login_probed": False,
        }
    )
    _write(config.experiment_root / "manifests/configuration_readiness.json", receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["ready"] else 3


def command_native_canary(config: ExperimentConfig, task: str, attempt: str) -> int:
    if task != CANARY_TASK or attempt != f"{CANARY_TASK}_a0":
        raise ProtocolError("only the frozen Life_005_a0 canary identity is accepted")
    readiness = config.environment_readiness()
    if not readiness["ready"]:
        print(
            json.dumps(
                {
                    "status": "BLOCKED_BY_OPENEVO_NATIVE_CAPABILITY_GAP",
                    "task": task,
                    "attempt": attempt,
                    "model_started": False,
                    "blockers": readiness["blockers"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 5
    # The only executable branch must be the Core run-owner API.  This adapter
    # deliberately has no fallback to RolloutHttpClient without an opaque
    # workspace handoff and runtime-context binding.
    print(
        json.dumps(
            {
                "status": "NATIVE_CORE_RUN_OWNER_SUBMISSION_NOT_INVOKED",
                "reason": "this command requires an explicit future --execute authority",
                "model_started": False,
            },
            indent=2,
        )
    )
    return 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("readiness", "static-audit", "contamination-audit"):
        command = sub.add_parser(name)
        command.add_argument("--protocol", required=True, type=Path)
    canary = sub.add_parser("native-canary")
    canary.add_argument("--protocol", required=True, type=Path)
    canary.add_argument("--task", required=True)
    canary.add_argument("--attempt", required=True)
    args = parser.parse_args(argv)
    try:
        config = ExperimentConfig.load(args.protocol)
        if args.command == "readiness":
            return command_readiness(config)
        if args.command == "static-audit":
            return command_static_audit(config)
        if args.command == "contamination-audit":
            return command_contamination_audit(config)
        return command_native_canary(config, args.task, args.attempt)
    except (ProtocolError, OSError, ValueError, RuntimeError) as exc:
        print(
            json.dumps(
                {
                    "status": "FAILED_CLOSED",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                    "model_started": False,
                },
                indent=2,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
